from __future__ import annotations

import json
import math
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import budget_proxy


def test_ordered_float_total_is_stable_across_python_sum_algorithms() -> None:
    costs = [0.005] * 6
    legacy_wire_total = budget_proxy.ordered_float_total(costs)

    assert legacy_wire_total == 0.030000000000000002
    assert math.fsum(costs) == 0.03
    assert legacy_wire_total != math.fsum(costs)

    round_tripped = json.loads(json.dumps(costs))
    assert budget_proxy.ordered_float_total(round_tripped) == legacy_wire_total

    incident_costs = [0.01245255, 0.007404, 0.00889845]
    assert budget_proxy.ordered_float_total(incident_costs).hex() == (
        "0x1.d71f36262cba8p-6"
    )
    assert math.fsum(incident_costs).hex() == "0x1.d71f36262cba7p-6"


def settings(**overrides) -> budget_proxy.Settings:
    base = budget_proxy.Settings(
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
            budget_proxy.HINDSIGHT_MODEL: (0.15, 0.60),
            budget_proxy.QWEN_MODEL: (0.01, 0.0),
        },
        initial_provider_spend_usd=0.00000006,
    )
    return replace(base, **overrides)


def state(tmp_path: Path, configured: budget_proxy.Settings | None = None):
    return budget_proxy.BudgetState(
        tmp_path / "ledger.json",
        tmp_path / "state.json",
        configured or settings(),
    )


@pytest.mark.parametrize(
    "name",
    [
        "PROVIDER_TOTAL_LIMIT_USD",
        "PROVIDER_SAFETY_MARGIN_USD",
        "HINDSIGHT_BACKFILL_LIMIT_USD",
        "HINDSIGHT_MONTHLY_LIMIT_USD",
        "QWEN_MONTHLY_LIMIT_USD",
        "GPT_OSS_120B_INPUT_USD_PER_M",
        "GPT_OSS_120B_OUTPUT_USD_PER_M",
        "QWEN_INPUT_USD_PER_M",
        "QWEN_OUTPUT_USD_PER_M",
        "INITIAL_PROVIDER_SPEND_USD",
    ],
)
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_every_nonfinite_float_setting_fails_closed(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("PROVIDER_API_KEY", "provider-secret")
    monkeypatch.setenv("PROXY_API_KEY", "proxy-secret")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    monkeypatch.delenv("HINDSIGHT_INPUT_USD_PER_M", raising=False)
    monkeypatch.delenv("HINDSIGHT_OUTPUT_USD_PER_M", raising=False)
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match="finite"):
        budget_proxy.Settings.from_env()


def test_payload_guards_pin_models_and_reasoning() -> None:
    payload, prompt_bytes, maximum = budget_proxy.prepare_chat_payload(
        {
            "model": budget_proxy.HINDSIGHT_MODEL,
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "low",
            "include_reasoning": False,
            "max_tokens": 1024,
        }
    )
    assert payload["max_completion_tokens"] == 1024
    assert "max_tokens" not in payload
    assert prompt_bytes > 0
    assert maximum == 1024
    with pytest.raises(budget_proxy.GuardViolation):
        budget_proxy.prepare_chat_payload(
            {"model": "other", "messages": [], "max_completion_tokens": 1}
        )


@pytest.mark.parametrize("bad_n", [2, 0, True, 1.0, "1"])
def test_chat_payload_pins_exactly_one_completion(bad_n) -> None:
    with pytest.raises(budget_proxy.GuardViolation, match="exact integer 1"):
        budget_proxy.prepare_chat_payload(
            {
                "model": budget_proxy.HINDSIGHT_MODEL,
                "messages": [{"role": "user", "content": "hello"}],
                "n": bad_n,
            }
        )


def test_chat_reservation_counts_full_normalized_payload() -> None:
    base = {
        "model": budget_proxy.HINDSIGHT_MODEL,
        "messages": [{"role": "user", "content": "hello"}],
        "max_completion_tokens": 32,
    }
    normalized, base_bytes, _ = budget_proxy.prepare_chat_payload(base)
    with_tool = dict(base)
    with_tool["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "large_schema",
                "description": "x" * 4000,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    _, tool_bytes, _ = budget_proxy.prepare_chat_payload(with_tool)
    assert normalized["n"] == 1
    assert base_bytes == len(
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert tool_bytes > base_bytes + 3000
    with pytest.raises(budget_proxy.GuardViolation):
        budget_proxy.prepare_chat_payload(
            {
                "model": budget_proxy.HINDSIGHT_MODEL,
                "messages": [],
                "reasoning_effort": "high",
            }
        )


def test_embedding_guards_limit_model_batch_and_dimensions() -> None:
    payload, prompt_bytes, output = budget_proxy.prepare_embedding_payload(
        {"model": budget_proxy.QWEN_MODEL, "input": ["a", "b"], "dimensions": 1024}
    )
    assert payload["dimensions"] == 1024
    assert prompt_bytes > 0
    assert output == 0
    with pytest.raises(budget_proxy.GuardViolation):
        budget_proxy.prepare_embedding_payload(
            {"model": budget_proxy.QWEN_MODEL, "input": ["x"] * 65}
        )


@pytest.mark.parametrize("bad_dimensions", [True, 1024.0, "1024"])
def test_embedding_dimensions_require_an_exact_integer(bad_dimensions) -> None:
    with pytest.raises(budget_proxy.GuardViolation, match="exact integer"):
        budget_proxy.prepare_embedding_payload(
            {
                "model": budget_proxy.QWEN_MODEL,
                "input": "hello",
                "dimensions": bad_dimensions,
            }
        )


def test_backfill_and_qwen_use_separate_scopes(tmp_path: Path) -> None:
    current = state(tmp_path)
    chat_id, chat = current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 1000, 1000)
    assert chat["scope"] == "hindsight_backfill"
    assert chat["period"] == "all"
    current.record(chat_id, {"prompt_tokens": 1000, "completion_tokens": 1000})
    qwen_id, qwen = current.reserve("embeddings", budget_proxy.QWEN_MODEL, 1000, 0)
    assert qwen["scope"] == "qwen_monthly"
    assert qwen["period"] == budget_proxy.utc_month()
    current.record(qwen_id, {"prompt_tokens": 1000, "completion_tokens": 0})
    assert current.health()["hindsight_backfill_spent_usd"] > 0
    assert current.health()["qwen_monthly_spent_usd"] > 0


def test_monthly_hindsight_scope_uses_utc_calendar_month(tmp_path: Path) -> None:
    current = state(tmp_path, settings(hindsight_phase="monthly"))
    request_id, reservation = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 100
    )
    assert reservation["scope"] == "hindsight_monthly"
    assert reservation["period"] == datetime.now(timezone.utc).strftime("%Y-%m")
    current.discard(request_id)


def test_concurrent_reservations_count_against_scope_limit(tmp_path: Path) -> None:
    configured = settings(
        hindsight_backfill_limit_usd=0.001,
        provider_total_limit_usd=1,
        provider_safety_margin_usd=0.1,
    )
    current = state(tmp_path, configured)
    current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 1000, 1000)
    with pytest.raises(budget_proxy.BudgetViolation, match="backfill"):
        current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 1000, 1000)


def test_provider_concurrent_ceiling_is_atomic(tmp_path: Path) -> None:
    current = state(tmp_path, settings(provider_max_in_flight=2))
    current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 100)
    current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 100)
    with pytest.raises(budget_proxy.BudgetViolation, match="concurrent"):
        current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 100)
    assert current.health()["in_flight"] == 2
    assert current.health()["provider_max_in_flight"] == 2


def test_provider_total_includes_initial_and_pending_spend(tmp_path: Path) -> None:
    configured = settings(
        provider_total_limit_usd=0.002,
        provider_safety_margin_usd=0.0001,
        hindsight_backfill_limit_usd=0.0018,
        initial_provider_spend_usd=0.001,
    )
    current = state(tmp_path, configured)
    current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 1000)
    with pytest.raises(budget_proxy.BudgetViolation, match="provider-total"):
        current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 1000)


def test_provider_reported_cost_cannot_be_undercounted(tmp_path: Path) -> None:
    current = state(tmp_path)
    request_id, _ = current.reserve("embeddings", budget_proxy.QWEN_MODEL, 100, 0)
    call = current.record(
        request_id,
        {"prompt_tokens": 10, "completion_tokens": 0, "cost": 0.25},
    )
    assert call["cost_usd"] == 0.25
    assert current.ledger["provider_spent_usd"] == pytest.approx(0.25000006)


@pytest.mark.parametrize("status_code", [401, 402])
def test_provider_auth_failure_atomically_records_lineage_and_opens_circuit(
    tmp_path: Path, status_code: int
) -> None:
    current = state(tmp_path)
    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )
    failure = current.record_provider_failure(
        request_id,
        status_code,
        retry_after=None,
        circuit_reason=f"provider returned {status_code}",
    )
    assert current.health()["circuit_open"] is True
    assert current.health()["in_flight"] == 0
    assert failure["fugu_session_id"] == request_id
    assert failure["fugu_agent_id"] == budget_proxy.FUGU_HINDSIGHT_AGENT_ID
    assert current.value["provider_failure_history"] == [failure]
    assert current.value["provider_auth_circuit_incident_request_id"] == request_id
    assert current.value["provider_auth_circuit_incident_status"] == status_code


@pytest.mark.parametrize(
    ("first_status", "second_status"), [(401, 402), (402, 401)]
)
def test_concurrent_mixed_auth_failures_preserve_first_exact_root(
    tmp_path: Path, first_status: int, second_status: int
) -> None:
    configured = settings(provider_max_in_flight=6)
    current = state(tmp_path, configured)
    first_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )
    second_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )

    current.record_provider_failure(
        first_id,
        first_status,
        retry_after=None,
        circuit_reason=f"provider returned {first_status}",
    )
    current.record_provider_failure(
        second_id,
        second_status,
        retry_after=None,
        circuit_reason=f"provider returned {second_status}",
    )

    assert [
        item["status_code"]
        for item in current.value["provider_failure_history"][-2:]
    ] == [first_status, second_status]
    assert current.value["provider_auth_circuit_incident_request_id"] == first_id
    assert current.value["provider_auth_circuit_incident_status"] == first_status
    assert current.health()["circuit_reason"] == f"provider returned {first_status}"
    restarted = state(tmp_path, configured)
    manifest = restarted.exact_reservation_manifest()
    assert manifest["provider_auth_circuit_incident_request_id"] == first_id
    assert manifest["provider_auth_circuit_incident_status"] == first_status


def test_reasoning_tokens_are_included_in_output_metering(tmp_path: Path) -> None:
    current = state(tmp_path)
    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 1000
    )
    call = current.record(
        request_id,
        {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 610},
    )
    assert call["input_tokens"] == 100
    assert call["output_tokens"] == 510


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": 1.5, "completion_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": "1"},
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 1.5},
        {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 6},
    ],
)
def test_provider_token_usage_requires_exact_consistent_integers(
    tmp_path: Path, usage: dict
) -> None:
    current = state(tmp_path)
    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )
    with pytest.raises(budget_proxy.GuardViolation, match="token usage|inconsistent"):
        current.record(request_id, usage)
    assert request_id in current.value["in_flight"]
    assert current.ledger["calls"] == []


def test_restart_with_in_flight_request_opens_circuit(tmp_path: Path) -> None:
    current = state(tmp_path)
    current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 100)
    restarted = state(tmp_path)
    assert restarted.value["circuit_open"] is True
    assert "ambiguous" in restarted.value["reason"]


def test_record_crash_after_ledger_commit_repairs_without_second_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    request_id, reservation_item = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )
    expected_manifest = budget_proxy.reservation_manifest(
        {request_id: reservation_item}, configured.prices
    )
    real_atomic_json = budget_proxy.atomic_json

    state_failures = 0

    def crash_before_state_commit(path, value):
        nonlocal state_failures
        if path == current.state_path and state_failures == 0:
            state_failures += 1
            raise RuntimeError("simulated state commit crash")
        real_atomic_json(path, value)

    monkeypatch.setattr(budget_proxy, "atomic_json", crash_before_state_commit)
    with pytest.raises(RuntimeError, match="simulated state commit crash"):
        current.record(
            request_id,
            {"prompt_tokens": 50, "completion_tokens": 25},
        )
    saved_ledger = json.loads(current.ledger_path.read_text())
    saved_state = json.loads(current.state_path.read_text())
    assert len(saved_ledger["calls"]) == 1
    assert request_id in saved_state["in_flight"]

    # This is the handler's exact post-record exception transition.  A
    # transient state-write error can be followed by a successful circuit
    # write before the process restarts.
    current.open_circuit(
        "ambiguous upstream failure: RuntimeError",
        keep_request=True,
        request_id=request_id,
    )
    assert json.loads(current.state_path.read_text())["circuit_open"] is True

    monkeypatch.setattr(budget_proxy, "atomic_json", real_atomic_json)
    restarted = state(tmp_path, configured)
    assert restarted.health()["in_flight"] == 0
    assert restarted.health()["circuit_open"] is False
    assert restarted.value["last_record_commit_repair"]["request_ids"] == [
        request_id
    ]
    assert restarted.value["last_record_commit_repair"][
        "cleared_exact_record_ambiguity"
    ] is True
    assert restarted.value["last_record_commit_repair"]["manifest_sha256"] == (
        expected_manifest["sha256"]
    )
    assert restarted.value["last_record_commit_repair"]["reservations"] == (
        expected_manifest["reservations"]
    )
    assert restarted.value["last_record_commit_repair"]["total_max_usd"] == (
        expected_manifest["total_max_usd"]
    )
    assert restarted.value["last_record_commit_repair"]["result"] == (
        "record_commits_repaired_and_drained"
    )
    assert len(restarted.ledger["calls"]) == 1
    assert restarted.ledger["provider_spent_usd"] == saved_ledger[
        "provider_spent_usd"
    ]


def test_discard_write_failure_keeps_reservation_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = state(tmp_path)
    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )

    def fail_state_write(path, value):
        raise RuntimeError("simulated discard write failure")

    monkeypatch.setattr(budget_proxy, "atomic_json", fail_state_write)
    with pytest.raises(RuntimeError, match="discard write failure"):
        current.discard(request_id)
    assert request_id in current.value["in_flight"]


def test_guard_configuration_mismatch_fails_closed(tmp_path: Path) -> None:
    state(tmp_path)
    with pytest.raises(SystemExit, match="guard"):
        state(tmp_path, settings(qwen_monthly_limit_usd=4))


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("at", "", "timestamp"),
        ("started_at", "2026-01-01T00:00:00", "timestamp"),
        ("provider_call_recorded", False, "provenance"),
        ("estimated_from_reservation", "false", "provenance"),
        ("reservation_manifest_sha256", "not-a-digest", "provenance"),
    ],
)
def test_persisted_provider_call_provenance_fails_closed(
    tmp_path: Path, field: str, bad_value: object, message: str
) -> None:
    current = state(tmp_path)
    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )
    current.record(
        request_id, {"prompt_tokens": 80, "completion_tokens": 120}
    )
    persisted = json.loads(current.ledger_path.read_text())
    persisted["calls"][0][field] = bad_value
    current.ledger_path.write_text(json.dumps(persisted))
    with pytest.raises(SystemExit, match=message):
        state(tmp_path)


def _historical_call(
    request_id: str,
    *,
    minute: int,
    reconciled: bool = False,
) -> dict[str, object]:
    started = datetime(2026, 7, 13, 10, minute, tzinfo=timezone.utc)
    input_tokens = 80 + minute
    output_tokens = 120 + minute
    value: dict[str, object] = {
        "at": (started + timedelta(seconds=1)).isoformat(),
        "started_at": started.isoformat(),
        "request_id": request_id,
        "endpoint": "chat",
        "model": budget_proxy.HINDSIGHT_MODEL,
        "scope": "hindsight_backfill",
        "period": "all",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": budget_proxy._token_cost(
            settings().prices[budget_proxy.HINDSIGHT_MODEL],
            input_tokens,
            output_tokens,
        ),
        "provider_reported_cost_usd": None,
        "estimated_from_reservation": reconciled,
    }
    if reconciled:
        value["ambiguous_upstream_reconciled"] = True
    return value


def _write_historical_ledger(
    current: budget_proxy.BudgetState,
    calls: list[dict[str, object]],
    *,
    version: int = budget_proxy.LEGACY_LEDGER_VERSION,
) -> None:
    ledger = json.loads(current.ledger_path.read_text())
    ledger["version"] = version
    ledger["calls"] = calls
    ledger["provider_spent_usd"] = float(
        ledger["initial_provider_spend_usd"]
    ) + sum(float(value["cost_usd"]) for value in calls)
    current.ledger_path.write_text(json.dumps(ledger))


def test_production_historical_ledger_starts_and_only_allows_an_initial_prefix(
    tmp_path: Path,
) -> None:
    created = state(tmp_path)
    calls = [
        _historical_call(
            "10000000-0000-4000-8000-000000000001", minute=1
        ),
        _historical_call(
            "10000000-0000-4000-8000-000000000002",
            minute=2,
            reconciled=True,
        ),
        _historical_call(
            "10000000-0000-4000-8000-000000000003", minute=3
        ),
        _historical_call(
            "10000000-0000-4000-8000-000000000004",
            minute=4,
            reconciled=True,
        ),
        _historical_call(
            "10000000-0000-4000-8000-000000000005", minute=5
        ),
    ]
    _write_historical_ledger(created, calls)

    adopted = state(tmp_path)
    assert adopted.ledger["calls"] == calls
    assert adopted.ledger["version"] == budget_proxy.LEGACY_LEDGER_VERSION

    request_id, _ = adopted.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )
    adopted.record(
        request_id, {"prompt_tokens": 80, "completion_tokens": 120}
    )
    restarted = state(tmp_path)
    assert restarted.ledger["calls"][-1]["provider_call_recorded"] is True

    persisted = json.loads(restarted.ledger_path.read_text())
    late_legacy = _historical_call(
        "10000000-0000-4000-8000-000000000006", minute=6
    )
    persisted["calls"].append(late_legacy)
    persisted["provider_spent_usd"] += late_legacy["cost_usd"]
    restarted.ledger_path.write_text(json.dumps(persisted))
    with pytest.raises(SystemExit, match="provenance"):
        state(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda call: call.update({"unexpected": None}),
        lambda call: call.update({"estimated_from_reservation": "false"}),
    ],
)
def test_legacy_provider_call_requires_the_exact_pinned_schema(
    tmp_path: Path, mutation
) -> None:
    created = state(tmp_path)
    call = _historical_call(
        "20000000-0000-4000-8000-000000000001", minute=1
    )
    mutation(call)
    _write_historical_ledger(created, [call])
    with pytest.raises(SystemExit, match="provenance"):
        state(tmp_path)


def test_version_two_ledger_never_accepts_a_legacy_shaped_call(
    tmp_path: Path,
) -> None:
    created = state(tmp_path)
    call = _historical_call(
        "30000000-0000-4000-8000-000000000001", minute=1
    )
    _write_historical_ledger(
        created, [call], version=budget_proxy.LEDGER_VERSION
    )
    with pytest.raises(SystemExit, match="provenance"):
        state(tmp_path)


def test_ledger_and_state_are_mode_600(tmp_path: Path) -> None:
    current = state(tmp_path)
    assert (tmp_path / "ledger.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "ledger.json").read_text())["version"] == 2
    assert current.health()["circuit_open"] is False
    assert current.health()["status"] == "maintenance"
    assert current.health()["hindsight_backfill_limit_usd"] == 30
    assert current.health()["hindsight_llm_max_concurrent"] == 3
    assert current.health()["provider_max_in_flight"] == 6


def test_final_maintenance_hold_is_exact_drained_and_idempotent(
    tmp_path: Path,
) -> None:
    current = state(tmp_path)
    with pytest.raises(budget_proxy.GuardViolation, match="reason is not exact"):
        current.enter_final_maintenance_hold("some other hold")

    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 100
    )
    with pytest.raises(budget_proxy.BudgetViolation, match="zero in-flight"):
        current.enter_final_maintenance_hold(
            budget_proxy.FINAL_MAINTENANCE_HOLD_REASON
        )
    current.discard(request_id)

    first = current.enter_final_maintenance_hold(
        budget_proxy.FINAL_MAINTENANCE_HOLD_REASON
    )
    second = current.enter_final_maintenance_hold(
        budget_proxy.FINAL_MAINTENANCE_HOLD_REASON
    )
    assert second == first
    health = current.health()
    assert health["circuit_open"] is True
    assert health["circuit_reason"] == budget_proxy.FINAL_MAINTENANCE_HOLD_REASON
    assert health["in_flight"] == 0
    assert health["final_maintenance_hold"] == first


def test_final_maintenance_hold_does_not_mask_an_incident(tmp_path: Path) -> None:
    current = state(tmp_path)
    current.open_circuit("provider returned 401", keep_request=False)
    with pytest.raises(budget_proxy.BudgetViolation, match="existing circuit"):
        current.enter_final_maintenance_hold(
            budget_proxy.FINAL_MAINTENANCE_HOLD_REASON
        )
    assert current.health()["circuit_reason"] == "provider returned 401"


def test_final_maintenance_hold_cannot_reopen_after_state_drift(
    tmp_path: Path,
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    current.enter_final_maintenance_hold(
        budget_proxy.FINAL_MAINTENANCE_HOLD_REASON
    )
    persisted = json.loads(current.state_path.read_text())
    persisted["circuit_open"] = False
    persisted.pop("reason", None)
    current.state_path.write_text(json.dumps(persisted))

    with pytest.raises(SystemExit, match="final maintenance hold"):
        state(tmp_path, configured)
    assert json.loads(current.state_path.read_text())["final_maintenance_hold"]


@pytest.mark.parametrize("missing_key", ["circuit_open", "in_flight"])
def test_missing_mandatory_state_key_fails_closed(
    tmp_path: Path, missing_key: str
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    persisted = json.loads(current.state_path.read_text())
    persisted.pop(missing_key)
    current.state_path.write_text(json.dumps(persisted))
    with pytest.raises(SystemExit, match="circuit bit|reservation set"):
        state(tmp_path, configured)


def test_final_maintenance_hold_endpoint_requires_proxy_auth(tmp_path: Path) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    server = budget_proxy.BudgetProxyServer(
        ("127.0.0.1", 0), current, configured
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/admin/maintenance-hold"
    try:
        unauthorized = budget_proxy.httpx.post(
            url,
            json={"reason": budget_proxy.FINAL_MAINTENANCE_HOLD_REASON},
            timeout=5,
        )
        assert unauthorized.status_code == 401
        assert current.health()["circuit_open"] is False

        inference_key = budget_proxy.httpx.post(
            url,
            headers={"Authorization": "Bearer proxy-secret"},
            json={"reason": budget_proxy.FINAL_MAINTENANCE_HOLD_REASON},
            timeout=5,
        )
        assert inference_key.status_code == 401
        assert current.health()["circuit_open"] is False

        wrong_reason = budget_proxy.httpx.post(
            url,
            headers={"Authorization": "Bearer admin-secret"},
            json={"reason": "not the final hold"},
            timeout=5,
        )
        assert wrong_reason.status_code == 400
        assert current.health()["circuit_open"] is False

        accepted = budget_proxy.httpx.post(
            url,
            headers={"Authorization": "Bearer admin-secret"},
            json={"reason": budget_proxy.FINAL_MAINTENANCE_HOLD_REASON},
            timeout=5,
        )
        assert accepted.status_code == 200
        assert accepted.json()["reason"] == (
            budget_proxy.FINAL_MAINTENANCE_HOLD_REASON
        )
        assert current.health()["circuit_open"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_reservation_manifest_endpoint_is_authenticated_exact_restart_proof(
    tmp_path: Path,
) -> None:
    configured = settings()
    current = state(tmp_path, configured)
    completed_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )
    completed_call = current.record(
        completed_id, {"prompt_tokens": 80, "completion_tokens": 120}
    )
    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )
    current.value["last_provider_auth_reset"] = {
        "reset_token_sha256": "a" * 64
    }
    current.value["last_ambiguous_reconciliation"] = {
        "manifest_sha256": "b" * 64,
        "reservation_ids": ["older-request"],
        "charged_maximum_usd": 0.5,
        "result": "maximum_charged_and_drained",
    }
    current.save_state()
    expected = budget_proxy.reservation_manifest(
        current.value["in_flight"], configured.prices
    )

    server = budget_proxy.BudgetProxyServer(
        ("127.0.0.1", 0), current, configured
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/admin/reservations"
    try:
        unauthorized = budget_proxy.httpx.get(url, timeout=5)
        assert unauthorized.status_code == 401
        inference_key = budget_proxy.httpx.get(
            url,
            headers={"Authorization": "Bearer proxy-secret"},
            timeout=5,
        )
        assert inference_key.status_code == 401

        response = budget_proxy.httpx.get(
            url,
            headers={"Authorization": "Bearer admin-secret"},
            timeout=5,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["sha256"] == expected["sha256"]
        assert body["total_max_usd"] == expected["total_max_usd"]
        assert body["health"] == current.health()
        assert body["health"]["in_flight"] == len(body["reservations"])
        assert [item["request_id"] for item in body["reservations"]] == [
            request_id
        ]
        assert body["last_provider_auth_reset"]["reset_token_sha256"] == (
            "a" * 64
        )
        assert body["last_ambiguous_reconciliation"]["manifest_sha256"] == (
            "b" * 64
        )
        assert body["provider_failure_history"] == []
        assert body["provider_call_history"] == [completed_call]
        assert body["provider_call_history"][0]["request_id"] == completed_id
        assert body["provider_ledger_version"] == budget_proxy.LEDGER_VERSION
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("transition", ["reserve", "record"])
def test_admin_snapshot_serializes_health_and_manifest_across_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    current = state(tmp_path)
    request_id = None
    if transition == "record":
        request_id, _ = current.reserve(
            "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
        )

    original_manifest = budget_proxy.reservation_manifest
    snapshot_entered = threading.Event()
    release_snapshot = threading.Event()
    transition_started = threading.Event()
    gate_once = True

    def gated_manifest(in_flight, prices):
        nonlocal gate_once
        if gate_once and threading.current_thread().name == "budget-snapshot":
            gate_once = False
            snapshot_entered.set()
            assert release_snapshot.wait(timeout=5)
        return original_manifest(in_flight, prices)

    monkeypatch.setattr(budget_proxy, "reservation_manifest", gated_manifest)
    snapshots: list[dict] = []
    errors: list[BaseException] = []

    def take_snapshot() -> None:
        try:
            snapshots.append(current.exact_reservation_manifest())
        except BaseException as exc:  # pragma: no cover - assertion transport
            errors.append(exc)

    def mutate() -> None:
        try:
            transition_started.set()
            if transition == "reserve":
                current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 200)
            else:
                assert request_id is not None
                current.record(
                    request_id,
                    {
                        "prompt_tokens": 80,
                        "completion_tokens": 120,
                        "total_tokens": 200,
                    },
                )
        except BaseException as exc:  # pragma: no cover - assertion transport
            errors.append(exc)

    snapshot_thread = threading.Thread(target=take_snapshot, name="budget-snapshot")
    mutation_thread = threading.Thread(target=mutate, name="budget-transition")
    snapshot_thread.start()
    assert snapshot_entered.wait(timeout=5)
    mutation_thread.start()
    assert transition_started.wait(timeout=5)
    # The mutation cannot cross the snapshot's lock-held serialization point.
    mutation_thread.join(timeout=0.05)
    assert mutation_thread.is_alive()
    release_snapshot.set()
    snapshot_thread.join(timeout=5)
    mutation_thread.join(timeout=5)

    assert not snapshot_thread.is_alive()
    assert not mutation_thread.is_alive()
    assert errors == []
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    expected_before = 0 if transition == "reserve" else 1
    assert snapshot["health"]["in_flight"] == expected_before
    assert len(snapshot["reservations"]) == expected_before
    assert snapshot["health"]["in_flight"] == len(snapshot["reservations"])
    assert current.health()["in_flight"] == 1 - expected_before
    # The digest remains bound to the list returned by the same snapshot even
    # if the live state changed immediately after the lock was released.
    encoded = json.dumps(
        snapshot["reservations"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert snapshot["sha256"] == budget_proxy.hashlib.sha256(encoded).hexdigest()


def test_provider_capacity_returns_retryable_429_without_opening_circuit(
    tmp_path: Path,
) -> None:
    configured = settings(provider_egress_enabled=True, provider_max_in_flight=1)
    current = state(tmp_path, configured)
    current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 100)
    server = budget_proxy.BudgetProxyServer(
        ("127.0.0.1", 0), current, configured
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = budget_proxy.httpx.post(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            headers={"Authorization": "Bearer proxy-secret"},
            json={
                "model": budget_proxy.HINDSIGHT_MODEL,
                "messages": [{"role": "user", "content": "capacity"}],
                "max_completion_tokens": 1,
                "reasoning_effort": "low",
                "include_reasoning": False,
            },
            timeout=5,
        )
        assert response.status_code == 429
        assert response.headers["retry-after"] == "2"
        assert current.health()["circuit_open"] is False
        assert current.health()["in_flight"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("17", "17"),
        ("Wed, 21 Oct 2037 07:28:00 GMT", None),
        ("1e300", None),
        ("nan", None),
        ("not-a-delay", None),
        ("17\r\nInjected: yes", None),
    ],
)
def test_retry_after_validation(value: str, expected: str | None) -> None:
    assert budget_proxy.validated_retry_after(value) == expected


def test_near_http_date_retry_after_is_accepted() -> None:
    value = budget_proxy.email.utils.format_datetime(
        datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True
    )
    assert budget_proxy.validated_retry_after(value) == value


@pytest.mark.parametrize("status_code", [429, 503])
def test_upstream_retryable_failure_preserves_retry_after_without_opening_circuit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    configured = settings(provider_egress_enabled=True)
    current = state(tmp_path, configured)
    server = budget_proxy.BudgetProxyServer(
        ("127.0.0.1", 0), current, configured
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_post = budget_proxy.httpx.post
    upstream_headers = {}

    def routed_post(url, *args, **kwargs):
        if str(url).startswith(configured.provider_base_url):
            upstream_headers.update(kwargs["headers"])
            return budget_proxy.httpx.Response(
                status_code,
                headers={
                    "content-type": "application/json",
                    "retry-after": "19",
                },
                json={"error": {"message": "rate limited"}},
                request=budget_proxy.httpx.Request("POST", str(url)),
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    try:
        response = real_post(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            headers={"Authorization": "Bearer proxy-secret"},
            json={
                "model": budget_proxy.HINDSIGHT_MODEL,
                "messages": [{"role": "user", "content": "retry"}],
                "max_completion_tokens": 1,
                "reasoning_effort": "low",
                "include_reasoning": False,
            },
            timeout=5,
        )
        assert response.status_code == status_code
        assert response.headers["retry-after"] == "19"
        assert upstream_headers["x-agent-id"] == (
            budget_proxy.FUGU_HINDSIGHT_AGENT_ID
        )
        assert len(upstream_headers["x-session-id"]) == 36
        assert current.health()["circuit_open"] is False
        assert current.health()["in_flight"] == 0
        failure = current.value["provider_failure_history"][-1]
        assert failure["request_id"] == upstream_headers["x-session-id"]
        assert failure["fugu_session_id"] == upstream_headers["x-session-id"]
        assert failure["fugu_agent_id"] == (
            budget_proxy.FUGU_HINDSIGHT_AGENT_ID
        )
        assert failure["status_code"] == status_code
        assert failure["retry_after"] == "19"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_commit_logging_failure_cannot_reopen_budget_circuit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings(provider_egress_enabled=True)
    current = state(tmp_path, configured)
    server = budget_proxy.BudgetProxyServer(
        ("127.0.0.1", 0), current, configured
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_post = budget_proxy.httpx.post

    def routed_post(url, *args, **kwargs):
        if str(url).startswith(configured.provider_base_url):
            return budget_proxy.httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    "choices": [],
                },
                request=budget_proxy.httpx.Request("POST", str(url)),
            )
        return real_post(url, *args, **kwargs)

    def closed_stdout(*args, **kwargs):
        raise BrokenPipeError("stdout closed during restart")

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    monkeypatch.setattr("builtins.print", closed_stdout)
    provenance = "hindsight-recovery-" + "a" * 24 + "-gpt_oss"
    try:
        response = real_post(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            headers={
                "Authorization": "Bearer proxy-secret",
                "X-Recovery-Provenance": provenance,
            },
            json={
                "model": budget_proxy.HINDSIGHT_MODEL,
                "messages": [{"role": "user", "content": "commit"}],
                "max_completion_tokens": 1,
                "reasoning_effort": "low",
                "include_reasoning": False,
            },
            timeout=5,
        )
        assert response.status_code == 200
        assert current.health()["circuit_open"] is False
        assert current.health()["in_flight"] == 0
        assert len(current.ledger["calls"]) == 1
        assert current.ledger["calls"][0][
            "request_provenance_sha256"
        ] == budget_proxy.hashlib.sha256(provenance.encode()).hexdigest()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_malformed_recovery_provenance_is_rejected_before_reservation(
    tmp_path: Path,
) -> None:
    configured = settings(provider_egress_enabled=True)
    current = state(tmp_path, configured)
    server = budget_proxy.BudgetProxyServer(
        ("127.0.0.1", 0), current, configured
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = budget_proxy.httpx.post(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            headers={
                "Authorization": "Bearer proxy-secret",
                "X-Recovery-Provenance": "not-an-exact-controller-route",
            },
            json={
                "model": budget_proxy.HINDSIGHT_MODEL,
                "messages": [{"role": "user", "content": "blocked"}],
                "max_completion_tokens": 1,
            },
            timeout=5,
        )
        assert response.status_code == 400
        assert current.health()["in_flight"] == 0
        assert current.ledger["calls"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("status_code", "message", "expected"),
    [
        (402, "unrelated wording", True),
        (403, "Insufficient credit balance for this request", True),
        (429, "credits exhausted", False),
        (429, "rate limit exceeded", False),
        (401, "invalid API key", False),
        (503, "temporary upstream outage", False),
    ],
)
def test_credit_exhaustion_classifier_is_narrow(
    status_code: int, message: str, expected: bool
) -> None:
    body = json.dumps({"error": {"message": message}}).encode()
    assert budget_proxy.provider_credit_exhausted(status_code, body) is expected


def test_observation_mode_keeps_monetary_limits_as_telemetry(
    tmp_path: Path,
) -> None:
    configured = settings(
        budget_observation_mode=True,
        provider_total_limit_usd=0.00005,
        provider_safety_margin_usd=0.00001,
        hindsight_backfill_limit_usd=0.00001,
    )
    current = state(tmp_path, configured)
    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 100
    )
    current.record(
        request_id, {"prompt_tokens": 100, "completion_tokens": 100}
    )

    health = current.health()
    assert health["budget_observation_mode"] is True
    assert health["provider_limit_exceeded"] is True
    assert health["hindsight_backfill_limit_exceeded"] is True
    assert health["circuit_open"] is False

    restarted = state(tmp_path, configured)
    assert restarted.health()["status"] == "maintenance"
    assert restarted.health()["circuit_open"] is False


def test_observation_mode_restart_maximum_charges_unchanged_reservation(
    tmp_path: Path,
) -> None:
    configured = settings(budget_observation_mode=True)
    current = state(tmp_path, configured)
    request_id, item = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )
    expected_cost = float(item["reservation"]["cost_usd"])

    restarted = state(tmp_path, configured)
    assert restarted.health()["in_flight"] == 0
    assert restarted.health()["circuit_open"] is False
    assert restarted.ledger["calls"][-1]["request_id"] == request_id
    assert restarted.ledger["calls"][-1][
        "ambiguous_upstream_reconciled"
    ] is True
    assert restarted.value["last_ambiguous_reconciliation"][
        "charged_maximum_usd"
    ] == expected_cost


def test_observation_mode_still_enforces_provider_concurrency(
    tmp_path: Path,
) -> None:
    configured = settings(
        budget_observation_mode=True, provider_max_in_flight=2
    )
    current = state(tmp_path, configured)
    current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 100)
    current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 100)
    with pytest.raises(
        budget_proxy.ProviderCapacityViolation, match="concurrent"
    ):
        current.reserve("chat", budget_proxy.HINDSIGHT_MODEL, 100, 100)
    assert current.health()["in_flight"] == 2


def test_observation_mode_removes_legacy_final_maintenance_hold(
    tmp_path: Path,
) -> None:
    current = state(tmp_path, settings())
    current.enter_final_maintenance_hold(
        budget_proxy.FINAL_MAINTENANCE_HOLD_REASON
    )

    observed = state(
        tmp_path, settings(budget_observation_mode=True)
    )
    assert observed.health()["circuit_open"] is False
    assert observed.health()["final_maintenance_hold"] is None
    assert observed.value["last_observation_mode_circuit_clear"][
        "previous_reason"
    ] == budget_proxy.FINAL_MAINTENANCE_HOLD_REASON


@pytest.mark.parametrize(
    "legacy_reason", ["provider returned 401", "provider returned 402"]
)
def test_observation_mode_clears_legacy_auth_holds(
    tmp_path: Path, legacy_reason: str
) -> None:
    guarded = settings()
    current = state(tmp_path, guarded)
    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 100
    )
    status_code = int(legacy_reason.rsplit(" ", 1)[1])
    current.record_provider_failure(
        request_id,
        status_code,
        retry_after=None,
        circuit_reason=legacy_reason,
    )

    observed = state(
        tmp_path, settings(budget_observation_mode=True)
    )
    assert observed.health()["circuit_open"] is False
    assert "provider_auth_circuit_incident_request_id" not in observed.value
    assert "provider_auth_circuit_incident_status" not in observed.value
    assert observed.value["last_observation_mode_circuit_clear"][
        "previous_reason"
    ] == legacy_reason


@pytest.mark.parametrize(
    ("status_code", "message", "expected_circuit"),
    [
        (401, "invalid API key", False),
        (429, "rate limited", False),
        (429, "credits exhausted", False),
        (503, "temporary outage", False),
        (402, "payment provider response", True),
        (403, "insufficient funds", True),
    ],
)
def test_observation_mode_only_credit_failures_open_circuit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    message: str,
    expected_circuit: bool,
) -> None:
    configured = settings(
        provider_egress_enabled=True, budget_observation_mode=True
    )
    current = state(tmp_path, configured)
    server = budget_proxy.BudgetProxyServer(
        ("127.0.0.1", 0), current, configured
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_post = budget_proxy.httpx.post

    def routed_post(url, *args, **kwargs):
        if str(url).startswith(configured.provider_base_url):
            return budget_proxy.httpx.Response(
                status_code,
                headers={
                    "content-type": "application/json",
                    "retry-after": "11",
                },
                json={"error": {"message": message}},
                request=budget_proxy.httpx.Request("POST", str(url)),
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    try:
        response = real_post(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            headers={"Authorization": "Bearer proxy-secret"},
            json={
                "model": budget_proxy.HINDSIGHT_MODEL,
                "messages": [{"role": "user", "content": "observe"}],
                "max_completion_tokens": 1,
                "reasoning_effort": "low",
                "include_reasoning": False,
            },
            timeout=5,
        )
        assert response.status_code == status_code
        if status_code in {429, 503}:
            assert response.headers["retry-after"] == "11"
        assert current.health()["circuit_open"] is expected_circuit
        assert current.health()["in_flight"] == 0
        if expected_circuit:
            assert current.health()["circuit_reason"] == (
                budget_proxy.CREDIT_EXHAUSTED_CIRCUIT_REASON
            )
            restarted = state(tmp_path, configured)
            assert restarted.health()["circuit_open"] is True
            assert restarted.health()["circuit_reason"] == (
                budget_proxy.CREDIT_EXHAUSTED_CIRCUIT_REASON
            )
        else:
            assert current.health()["circuit_reason"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_observation_mode_transport_ambiguity_is_maximum_charged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings(
        provider_egress_enabled=True, budget_observation_mode=True
    )
    current = state(tmp_path, configured)
    server = budget_proxy.BudgetProxyServer(
        ("127.0.0.1", 0), current, configured
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_post = budget_proxy.httpx.post

    def routed_post(url, *args, **kwargs):
        if str(url).startswith(configured.provider_base_url):
            raise budget_proxy.httpx.ReadTimeout("ambiguous provider timeout")
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    try:
        response = real_post(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            headers={"Authorization": "Bearer proxy-secret"},
            json={
                "model": budget_proxy.HINDSIGHT_MODEL,
                "messages": [{"role": "user", "content": "timeout"}],
                "max_completion_tokens": 1,
                "reasoning_effort": "low",
                "include_reasoning": False,
            },
            timeout=5,
        )
        assert response.status_code == 502
        assert current.health()["circuit_open"] is False
        assert current.health()["in_flight"] == 0
        assert len(current.ledger["calls"]) == 1
        assert current.ledger["calls"][0][
            "ambiguous_upstream_reconciled"
        ] is True
        assert current.value["last_ambiguous_reconciliation"][
            "newly_charged_usd"
        ] > 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_observation_mode_retries_swallowed_settlement_before_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings(
        provider_egress_enabled=True,
        budget_observation_mode=True,
        provider_max_in_flight=1,
    )
    current = state(tmp_path, configured)
    server = budget_proxy.BudgetProxyServer(
        ("127.0.0.1", 0), current, configured
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_post = budget_proxy.httpx.post
    real_atomic_json = budget_proxy.atomic_json
    failed_state_commit = False

    def routed_post(url, *args, **kwargs):
        if str(url).startswith(configured.provider_base_url):
            raise budget_proxy.httpx.ReadTimeout("ambiguous provider timeout")
        return real_post(url, *args, **kwargs)

    def fail_first_state_drain(path, value):
        nonlocal failed_state_commit
        if (
            path == current.state_path
            and not failed_state_commit
            and value.get("in_flight") == {}
            and json.loads(current.ledger_path.read_text()).get("calls")
        ):
            failed_state_commit = True
            raise OSError("transient state replace failure")
        return real_atomic_json(path, value)

    monkeypatch.setattr(budget_proxy.httpx, "post", routed_post)
    monkeypatch.setattr(budget_proxy, "atomic_json", fail_first_state_drain)
    payload = {
        "model": budget_proxy.HINDSIGHT_MODEL,
        "messages": [{"role": "user", "content": "timeout"}],
        "max_completion_tokens": 1,
        "reasoning_effort": "low",
        "include_reasoning": False,
    }
    try:
        first = real_post(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            headers={"Authorization": "Bearer proxy-secret"},
            json=payload,
            timeout=5,
        )
        assert first.status_code == 502
        assert failed_state_commit is True
        assert current.health()["in_flight"] == 1

        # The second reserve must settle the ended first request before the
        # max-in-flight=1 check. It reaches the provider instead of returning a
        # local capacity 429, and its own ambiguity is settled normally.
        second = real_post(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            headers={"Authorization": "Bearer proxy-secret"},
            json=payload,
            timeout=5,
        )
        assert second.status_code == 502
        assert current.health()["circuit_open"] is False
        assert current.health()["in_flight"] == 0
        calls = current.ledger["calls"]
        assert len(calls) == 2
        assert len({call["request_id"] for call in calls}) == 2
        assert all(call["ambiguous_upstream_reconciled"] is True for call in calls)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_observation_mode_restart_repairs_ledger_first_state_second_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings(
        budget_observation_mode=True, provider_max_in_flight=1
    )
    current = state(tmp_path, configured)
    request_id, item = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 200
    )
    expected_cost = float(item["reservation"]["cost_usd"])
    real_atomic_json = budget_proxy.atomic_json
    failed_state_commit = False

    def fail_first_state_drain(path, value):
        nonlocal failed_state_commit
        if (
            path == current.state_path
            and not failed_state_commit
            and value.get("in_flight") == {}
        ):
            failed_state_commit = True
            raise OSError("crash after ledger replace")
        return real_atomic_json(path, value)

    monkeypatch.setattr(budget_proxy, "atomic_json", fail_first_state_drain)
    with pytest.raises(OSError, match="crash after ledger replace"):
        current.maximum_charge_ambiguous(request_id)

    durable_ledger = json.loads(current.ledger_path.read_text())
    durable_state = json.loads(current.state_path.read_text())
    assert [call["request_id"] for call in durable_ledger["calls"]] == [request_id]
    assert request_id in durable_state["in_flight"]

    # Startup recognizes the already committed maximum charge and performs
    # only the missing state drain. It must not append or charge the request a
    # second time.
    restarted = state(tmp_path, configured)
    assert restarted.health()["in_flight"] == 0
    assert restarted.health()["circuit_open"] is False
    assert [call["request_id"] for call in restarted.ledger["calls"]] == [request_id]
    assert restarted.ledger["provider_spent_usd"] == pytest.approx(
        configured.initial_provider_spend_usd + expected_cost
    )
