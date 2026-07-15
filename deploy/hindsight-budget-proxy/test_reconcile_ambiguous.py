import json
import sys

import pytest

import budget_proxy
import reconcile_ambiguous
from budget_proxy import GuardViolation, HINDSIGHT_MODEL, reservation_manifest
from reconcile_ambiguous import reconcile

REQUEST_1 = "00000000-0000-4000-8000-000000000001"
REQUEST_PRESTARTUP = "00000000-0000-4000-8000-000000000002"
REQUEST_CRASH = "00000000-0000-4000-8000-000000000003"
REQUEST_REPAIR = "00000000-0000-4000-8000-000000000004"
REQUEST_MIXED_CHARGE = "00000000-0000-4000-8000-000000000005"


def test_if_needed_refuses_absent_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reconcile_ambiguous.py",
            "--ledger",
            str(tmp_path / "absent-ledger.json"),
            "--state",
            str(tmp_path / "absent-state.json"),
            "--expected-manifest-sha256",
            "a" * 64,
            "--provider-max-in-flight",
            "6",
            "--acknowledge-max-spend",
            "--if-needed",
        ],
    )
    with pytest.raises(ValueError, match="state is absent"):
        reconcile_ambiguous.main()


GUARD = {
    "provider_total_limit_usd": 50.0,
    "provider_safety_margin_usd": 0.5,
    "hindsight_backfill_limit_usd": 30.0,
    "hindsight_monthly_limit_usd": 10.0,
    "qwen_monthly_limit_usd": 5.0,
    "prices": {
        HINDSIGHT_MODEL: {
            "input_usd_per_m": 0.15,
            "output_usd_per_m": 0.60,
        },
        "qwen/qwen3-embedding-8b": {
            "input_usd_per_m": 0.01,
            "output_usd_per_m": 0.0,
        },
    },
}
PRICES = GUARD["prices"]


def write(path, value):
    path.write_text(json.dumps(value))


def exact_ledger(provider_spent, calls=None, *, initial=None):
    calls = list(calls or [])
    if initial is None:
        initial = provider_spent - sum(float(call["cost_usd"]) for call in calls)
    return {
        "version": 1,
        "guard": GUARD,
        "initial_provider_spend_usd": initial,
        "provider_spent_usd": provider_spent,
        "calls": calls,
    }


def manifest_sha(path):
    value = json.loads(path.read_text())
    return reservation_manifest(value.get("in_flight") or {}, PRICES)["sha256"]


def exact_reservation(**overrides):
    item = {
        "model": HINDSIGHT_MODEL,
        "endpoint": "chat",
        "scope": "hindsight_backfill",
        "period": "all",
        "started_at": "2026-01-01T00:00:00+00:00",
        "reservation": {
            "input_tokens": 1,
            "output_tokens": 2,
            "total_tokens": 3,
            "cost_usd": 0.00000135,
        },
    }
    item.update(overrides)
    return item


def exact_normal_call(request_id, item, *, input_tokens=1, output_tokens=2):
    cost = (input_tokens * 0.15 + output_tokens * 0.60) / 1_000_000
    return {
        "at": "2026-01-01T00:00:01+00:00",
        "started_at": item["started_at"],
        "request_id": request_id,
        "endpoint": item["endpoint"],
        "model": item["model"],
        "scope": item["scope"],
        "period": item["period"],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
        "provider_reported_cost_usd": None,
        "estimated_from_reservation": False,
        "provider_call_recorded": True,
        "reservation_manifest_sha256": reservation_manifest(
            {request_id: item}, PRICES
        )["sha256"],
    }


def proxy_settings(*, initial_provider_spend_usd=0.0):
    return budget_proxy.Settings(
        provider_base_url="https://provider.invalid/v1",
        provider_api_key="provider-secret",
        proxy_api_key="proxy-secret",
        admin_api_key="admin-secret",
        provider_egress_enabled=False,
        provider_total_limit_usd=50,
        provider_safety_margin_usd=0.5,
        hindsight_phase="backfill",
        hindsight_backfill_limit_usd=30,
        hindsight_monthly_limit_usd=10,
        qwen_monthly_limit_usd=5,
        hindsight_llm_max_concurrent=3,
        provider_max_in_flight=6,
        rpm_limit=30,
        tpm_limit=1_000_000,
        prices={
            HINDSIGHT_MODEL: (0.15, 0.60),
            "qwen/qwen3-embedding-8b": (0.01, 0.0),
        },
        initial_provider_spend_usd=initial_provider_spend_usd,
    )


@pytest.mark.parametrize("bad_cost", [0, -1, float("nan"), float("inf")])
def test_manifest_rejects_nonpositive_or_nonfinite_maximum_cost(bad_cost):
    item = exact_reservation()
    item["reservation"]["cost_usd"] = bad_cost
    with pytest.raises(GuardViolation, match="finite and positive"):
        reservation_manifest({REQUEST_1: item}, PRICES)


def test_manifest_rejects_malformed_lineage_and_token_totals():
    with pytest.raises(GuardViolation, match="exact UUID"):
        reservation_manifest({"not-a-uuid": exact_reservation()}, PRICES)
    wrong_total = exact_reservation()
    wrong_total["reservation"]["total_tokens"] = 99
    with pytest.raises(GuardViolation, match="total is inconsistent"):
        reservation_manifest({REQUEST_1: wrong_total}, PRICES)
    with pytest.raises(GuardViolation, match="model/endpoint/scope/period"):
        reservation_manifest(
            {REQUEST_1: exact_reservation(model="unapproved-model")}, PRICES
        )


def test_manifest_rejects_cost_that_does_not_match_exact_tokens_and_prices():
    item = exact_reservation()
    item["reservation"]["cost_usd"] *= 2
    with pytest.raises(GuardViolation, match="exact tokens/prices"):
        reservation_manifest({REQUEST_1: item}, PRICES)


def test_reconcile_charges_maximum_and_clears_circuit(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(ledger, exact_ledger(1.0))
    write(
        state,
        {
            "circuit_open": True,
            "reason": "ambiguous upstream failure",
            "in_flight": {
                REQUEST_1: {
                    "model": HINDSIGHT_MODEL,
                    "endpoint": "chat",
                    "scope": "hindsight_backfill",
                    "period": "all",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "reservation": {
                        "input_tokens": 100,
                        "output_tokens": 200,
                        "total_tokens": 300,
                        "cost_usd": 0.000135,
                    },
                }
            },
        },
    )

    expected_sha = manifest_sha(state)
    result = reconcile(
        ledger,
        state,
        acknowledgement=True,
        expected_manifest_sha256=expected_sha,
    )

    assert result["requests"] == 1
    assert result["reservation_ids"] == [REQUEST_1]
    assert result["manifest_sha256"] == expected_sha
    assert result["charged_maximum_usd"] == 0.000135
    assert result["newly_charged_usd"] == 0.000135
    assert result["provider_spent_usd"] == 1.000135
    assert result["replayed"] is False
    saved_ledger = json.loads(ledger.read_text())
    assert saved_ledger["calls"][0]["ambiguous_upstream_reconciled"] is True
    assert saved_ledger["calls"][0]["estimated_from_reservation"] is True
    saved_state = json.loads(state.read_text())
    assert saved_state["circuit_open"] is False
    assert saved_state["in_flight"] == {}
    assert saved_state["last_ambiguous_reconciliation"]["requests"] == 1
    assert saved_state["last_ambiguous_reconciliation"]["reservation_ids"] == [
        REQUEST_1
    ]
    assert saved_state["last_ambiguous_reconciliation"]["manifest_sha256"] == (
        expected_sha
    )


def test_reconcile_requires_acknowledgement_and_ambiguity(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(ledger, exact_ledger(0))
    write(state, {"circuit_open": False, "in_flight": {}})
    with pytest.raises(ValueError, match="acknowledgement"):
        reconcile(
            ledger,
            state,
            acknowledgement=False,
            expected_manifest_sha256="0" * 64,
        )
    with pytest.raises(ValueError, match="no matching ambiguous reservation manifest"):
        reconcile(
            ledger,
            state,
            acknowledgement=True,
            expected_manifest_sha256="0" * 64,
        )


def test_reconcile_validates_then_materializes_exact_prestartup_reservation(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(ledger, exact_ledger(2.0))
    reservation = {
        "model": HINDSIGHT_MODEL,
        "endpoint": "chat",
        "scope": "hindsight_backfill",
        "period": "all",
        "started_at": "2026-01-01T00:00:00+00:00",
        "request_provenance_sha256": "c" * 64,
        "reservation": {
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "cost_usd": 0.0000135,
        },
    }
    write(
        state,
        {
            "circuit_open": False,
            "in_flight": {REQUEST_PRESTARTUP: reservation},
        },
    )

    result = reconcile(
        ledger,
        state,
        acknowledgement=True,
        expected_manifest_sha256=manifest_sha(state),
    )

    assert result["requests"] == 1
    assert result["charged_maximum_usd"] == 0.0000135
    saved_state = json.loads(state.read_text())
    assert saved_state["circuit_open"] is False
    assert saved_state["in_flight"] == {}
    assert json.loads(ledger.read_text())["calls"][0][
        "request_provenance_sha256"
    ] == "c" * 64


def test_reconcile_malformed_unrelated_state_makes_no_changes(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(ledger, exact_ledger(2.0))
    write(
        state,
        {
            "circuit_open": False,
            "in_flight": {REQUEST_PRESTARTUP: exact_reservation()},
            "provider_failure_history": "corrupt-unrelated-proof",
        },
    )
    ledger_before = ledger.read_bytes()
    state_before = state.read_bytes()
    with pytest.raises(GuardViolation, match="failure history"):
        reconcile(
            ledger,
            state,
            acknowledgement=True,
            expected_manifest_sha256=manifest_sha(state),
        )
    assert ledger.read_bytes() == ledger_before
    assert state.read_bytes() == state_before


def test_reconcile_rejects_changed_manifest(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(ledger, exact_ledger(0))
    write(
        state,
        {
            "circuit_open": True,
            "reason": "ambiguous upstream failure: ReadTimeout",
            "in_flight": {
                REQUEST_1: {
                    "model": HINDSIGHT_MODEL,
                    "endpoint": "chat",
                    "scope": "hindsight_backfill",
                    "period": "all",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "reservation": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                        "cost_usd": 0.00000075,
                    },
                }
            },
        },
    )
    with pytest.raises(ValueError, match="manifest changed"):
        reconcile(
            ledger,
            state,
            acknowledgement=True,
            expected_manifest_sha256="f" * 64,
        )


def test_reconcile_crash_after_ledger_write_is_exactly_once(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(ledger, exact_ledger(1.0))
    write(
        state,
        {
            "circuit_open": True,
            "reason": "ambiguous upstream failure",
            "in_flight": {
                REQUEST_CRASH: {
                    "model": HINDSIGHT_MODEL,
                    "endpoint": "chat",
                    "scope": "hindsight_backfill",
                    "period": "all",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "reservation": {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "total_tokens": 3,
                        "cost_usd": 0.00000135,
                    },
                }
            },
        },
    )
    expected_sha = manifest_sha(state)
    real_atomic_json = reconcile_ambiguous.atomic_json

    def crash_before_state_write(path, value):
        if path == state:
            raise RuntimeError("simulated crash after ledger commit")
        real_atomic_json(path, value)

    monkeypatch.setattr(reconcile_ambiguous, "atomic_json", crash_before_state_write)
    with pytest.raises(RuntimeError, match="simulated crash"):
        reconcile(
            ledger,
            state,
            acknowledgement=True,
            expected_manifest_sha256=expected_sha,
        )
    assert json.loads(ledger.read_text())["provider_spent_usd"] == 1.00000135
    assert json.loads(state.read_text())["in_flight"]

    monkeypatch.setattr(reconcile_ambiguous, "atomic_json", real_atomic_json)
    result = reconcile(
        ledger,
        state,
        acknowledgement=True,
        expected_manifest_sha256=expected_sha,
    )
    assert result["replayed"] is True
    assert result["newly_charged_usd"] == 0
    assert result["provider_spent_usd"] == 1.00000135
    assert len(json.loads(ledger.read_text())["calls"]) == 1

    replay = reconcile(
        ledger,
        state,
        acknowledgement=True,
        expected_manifest_sha256=expected_sha,
    )
    assert replay["replayed"] is True
    assert replay["newly_charged_usd"] == 0
    assert replay["reservation_ids"] == [REQUEST_CRASH]


def test_proxy_startup_finishes_reconciler_ledger_first_crash(
    tmp_path, monkeypatch
):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    item = exact_reservation()
    write(ledger, exact_ledger(1.0))
    write(
        state,
        {
            "circuit_open": True,
            "reason": "ambiguous upstream failure: ReadTimeout",
            "in_flight": {REQUEST_CRASH: item},
        },
    )
    expected_sha = manifest_sha(state)
    real_atomic_json = reconcile_ambiguous.atomic_json

    def crash_before_state_write(path, value):
        if path == state:
            raise RuntimeError("simulated VM loss after reconciliation ledger")
        real_atomic_json(path, value)

    monkeypatch.setattr(reconcile_ambiguous, "atomic_json", crash_before_state_write)
    with pytest.raises(RuntimeError, match="simulated VM loss"):
        reconcile(
            ledger,
            state,
            acknowledgement=True,
            expected_manifest_sha256=expected_sha,
        )
    spend_after_ledger = json.loads(ledger.read_text())["provider_spent_usd"]
    assert json.loads(state.read_text())["in_flight"]

    monkeypatch.setattr(reconcile_ambiguous, "atomic_json", real_atomic_json)
    restarted = budget_proxy.BudgetState(
        ledger,
        state,
        proxy_settings(initial_provider_spend_usd=1.0),
    )
    assert restarted.health()["circuit_open"] is False
    assert restarted.health()["in_flight"] == 0
    assert restarted.ledger["provider_spent_usd"] == spend_after_ledger
    proof = restarted.value["last_ambiguous_reconciliation"]
    assert proof["manifest_sha256"] == expected_sha
    assert proof["maximum_charged_ids"] == [REQUEST_CRASH]
    assert proof["result"] == "maximum_charged_and_drained"
    assert proof["cleared_exact_ambiguity"] is True

    second_restart = budget_proxy.BudgetState(
        ledger,
        state,
        proxy_settings(initial_provider_spend_usd=1.0),
    )
    assert second_restart.ledger["provider_spent_usd"] == spend_after_ledger
    assert len(second_restart.ledger["calls"]) == 1


def test_reconcile_rejects_existing_non_reconciliation_call(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(
        ledger,
        exact_ledger(
            0.25,
            [
                    {
                        "at": "2026-01-01T00:00:01+00:00",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "request_id": REQUEST_1,
                    "cost_usd": 0.25,
                    "model": HINDSIGHT_MODEL,
                    "endpoint": "chat",
                    "scope": "hindsight_backfill",
                    "period": "all",
                    "input_tokens": 1,
                    "output_tokens": 1,
                        "provider_reported_cost_usd": 0.25,
                        "estimated_from_reservation": False,
                        "provider_call_recorded": True,
                        "reservation_manifest_sha256": "a" * 64,
                }
            ],
            initial=0,
        ),
    )
    write(
        state,
        {
            "circuit_open": True,
            "reason": "ambiguous upstream failure: RuntimeError",
            "in_flight": {
                REQUEST_1: {
                    "model": HINDSIGHT_MODEL,
                    "endpoint": "chat",
                    "scope": "hindsight_backfill",
                    "period": "all",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "reservation": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                        "cost_usd": 0.00000075,
                    },
                }
            },
        },
    )
    with pytest.raises(ValueError, match="non-reconciliation ledger row"):
        reconcile(
            ledger,
            state,
            acknowledgement=True,
            expected_manifest_sha256=manifest_sha(state),
        )


def test_reconcile_repairs_exact_normal_record_without_second_charge(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    item = exact_reservation()
    call = exact_normal_call(REQUEST_REPAIR, item)
    write(
        ledger,
        exact_ledger(1.0 + call["cost_usd"], [call], initial=1.0),
    )
    write(
        state,
        {
            "circuit_open": True,
            "reason": "ambiguous upstream failure: RuntimeError",
            "in_flight": {REQUEST_REPAIR: item},
        },
    )
    expected_sha = manifest_sha(state)
    before_spend = json.loads(ledger.read_text())["provider_spent_usd"]

    result = reconcile(
        ledger,
        state,
        acknowledgement=True,
        expected_manifest_sha256=expected_sha,
    )

    assert result["result"] == "record_commits_repaired_and_drained"
    assert result["record_commit_repaired_ids"] == [REQUEST_REPAIR]
    assert result["maximum_charged_ids"] == []
    assert result["charged_maximum_usd"] == 0
    assert result["newly_charged_usd"] == 0
    assert json.loads(ledger.read_text())["provider_spent_usd"] == before_spend
    saved_state = json.loads(state.read_text())
    assert saved_state["in_flight"] == {}
    assert saved_state["circuit_open"] is False
    assert saved_state["last_record_commit_repair"]["manifest_sha256"] == (
        expected_sha
    )

    replay = reconcile(
        ledger,
        state,
        acknowledgement=True,
        expected_manifest_sha256=expected_sha,
    )
    assert replay["result"] == "record_commits_repaired_and_drained"
    assert replay["record_commit_repaired_ids"] == [REQUEST_REPAIR]
    assert replay["newly_charged_usd"] == 0


def test_mixed_record_repair_and_maximum_charge_is_crash_idempotent(
    tmp_path, monkeypatch
):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    repaired_item = exact_reservation()
    charged_item = exact_reservation(
        reservation={
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "cost_usd": 0.0000135,
        }
    )
    normal = exact_normal_call(REQUEST_REPAIR, repaired_item)
    write(
        ledger,
        exact_ledger(1.0 + normal["cost_usd"], [normal], initial=1.0),
    )
    write(
        state,
        {
            "circuit_open": True,
            "reason": "ambiguous upstream failure: ReadTimeout",
            "in_flight": {
                REQUEST_REPAIR: repaired_item,
                REQUEST_MIXED_CHARGE: charged_item,
            },
        },
    )
    expected_sha = manifest_sha(state)
    real_atomic_json = reconcile_ambiguous.atomic_json

    def crash_before_state_write(path, value):
        if path == state:
            raise RuntimeError("simulated mixed reconciliation crash")
        real_atomic_json(path, value)

    monkeypatch.setattr(reconcile_ambiguous, "atomic_json", crash_before_state_write)
    with pytest.raises(RuntimeError, match="mixed reconciliation crash"):
        reconcile(
            ledger,
            state,
            acknowledgement=True,
            expected_manifest_sha256=expected_sha,
        )
    after_crash = json.loads(ledger.read_text())
    assert len(after_crash["calls"]) == 2
    assert after_crash["provider_spent_usd"] == pytest.approx(
        1.0 + normal["cost_usd"] + 0.0000135
    )

    monkeypatch.setattr(reconcile_ambiguous, "atomic_json", real_atomic_json)
    resumed = reconcile(
        ledger,
        state,
        acknowledgement=True,
        expected_manifest_sha256=expected_sha,
    )
    assert resumed["result"] == "record_commits_repaired_and_maximum_charged"
    assert resumed["record_commit_repaired_ids"] == [REQUEST_REPAIR]
    assert resumed["maximum_charged_ids"] == [REQUEST_MIXED_CHARGE]
    assert resumed["newly_charged_usd"] == 0
    assert len(json.loads(ledger.read_text())["calls"]) == 2

    replay = reconcile(
        ledger,
        state,
        acknowledgement=True,
        expected_manifest_sha256=expected_sha,
    )
    assert replay["result"] == "record_commits_repaired_and_maximum_charged"
    assert replay["newly_charged_usd"] == 0


def test_reconcile_rejects_ledger_contradiction_before_any_write(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(ledger, exact_ledger(1.0, initial=0.0))
    write(
        state,
        {
            "circuit_open": True,
            "reason": "ambiguous upstream failure: ConnectError",
            "in_flight": {REQUEST_1: exact_reservation()},
        },
    )
    ledger_before = ledger.read_bytes()
    state_before = state.read_bytes()
    with pytest.raises(GuardViolation, match="does not balance"):
        reconcile(
            ledger,
            state,
            acknowledgement=True,
            expected_manifest_sha256=manifest_sha(state),
        )
    assert ledger.read_bytes() == ledger_before
    assert state.read_bytes() == state_before


def test_reconcile_rejects_resulting_scope_overage_before_any_write(tmp_path):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    prior_id = "00000000-0000-4000-8000-000000000006"
    prior = {
        "at": "2026-01-01T00:00:01+00:00",
        "started_at": "2026-01-01T00:00:00+00:00",
        "request_id": prior_id,
        "model": HINDSIGHT_MODEL,
        "endpoint": "chat",
        "scope": "hindsight_backfill",
        "period": "all",
        "input_tokens": 1,
        "output_tokens": 1,
        "provider_reported_cost_usd": 29.8,
        "cost_usd": 29.8,
        "estimated_from_reservation": False,
        "provider_call_recorded": True,
        "reservation_manifest_sha256": "b" * 64,
    }
    write(ledger, exact_ledger(29.8, [prior], initial=0.0))
    item = exact_reservation(
        reservation={
            "input_tokens": 1,
            "output_tokens": 500000,
            "total_tokens": 500001,
            "cost_usd": 0.30000015,
        }
    )
    write(
        state,
        {
            "circuit_open": True,
            "reason": "ambiguous upstream failure: ReadTimeout",
            "in_flight": {REQUEST_1: item},
        },
    )
    ledger_before = ledger.read_bytes()
    state_before = state.read_bytes()
    with pytest.raises(GuardViolation, match="backfill.*exceeds"):
        reconcile(
            ledger,
            state,
            acknowledgement=True,
            expected_manifest_sha256=manifest_sha(state),
        )
    assert ledger.read_bytes() == ledger_before
    assert state.read_bytes() == state_before


@pytest.mark.parametrize(
    "reason,final_hold",
    [
        ("provider returned 401", None),
        (
            "hindsight backfill complete: maintenance hold",
            {
                "reason": "hindsight backfill complete: maintenance hold",
                "entered_at": "2026-01-01T00:00:00+00:00",
            },
        ),
    ],
)
def test_reconcile_never_clears_unrelated_or_final_circuit(
    tmp_path, reason, final_hold
):
    ledger = tmp_path / "ledger.json"
    state = tmp_path / "state.json"
    write(ledger, exact_ledger(0))
    state_value = {
        "circuit_open": True,
        "reason": reason,
        "in_flight": {REQUEST_1: exact_reservation()},
    }
    if final_hold is not None:
        state_value["final_maintenance_hold"] = final_hold
    write(state, state_value)
    ledger_before = ledger.read_bytes()
    state_before = state.read_bytes()
    with pytest.raises(
        (ValueError, GuardViolation),
        match="hold forbids|exact open ambiguity|exactly coupled",
    ):
        reconcile(
            ledger,
            state,
            acknowledgement=True,
            expected_manifest_sha256=manifest_sha(state),
        )
    assert ledger.read_bytes() == ledger_before
    assert state.read_bytes() == state_before
