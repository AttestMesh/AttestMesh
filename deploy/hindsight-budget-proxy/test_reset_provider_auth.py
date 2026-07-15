from __future__ import annotations

import copy
import json
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

import budget_proxy
import reconcile_ambiguous
import reset_provider_auth
from reset_provider_auth import reset_provider_auth_circuit


def settings(**overrides) -> budget_proxy.Settings:
    base = budget_proxy.Settings(
        provider_base_url="https://provider.invalid/v1",
        provider_api_key="provider-secret",
        proxy_api_key="proxy-secret",
        admin_api_key="admin-secret",
        provider_egress_enabled=True,
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
        initial_provider_spend_usd=0.0,
    )
    return replace(base, **overrides)


def new_state(tmp_path: Path) -> tuple[budget_proxy.BudgetState, budget_proxy.Settings]:
    configured = settings()
    return (
        budget_proxy.BudgetState(
            tmp_path / "ledger.json", tmp_path / "state.json", configured
        ),
        configured,
    )


def begin_auth_episode(current: budget_proxy.BudgetState, status: int = 401) -> str:
    request_id, _ = current.reserve(
        "chat", budget_proxy.HINDSIGHT_MODEL, 100, 1
    )
    current.record_provider_failure(
        request_id,
        status,
        retry_after=None,
        circuit_reason=f"provider returned {status}",
    )
    return request_id


def succeed_probe(
    current: budget_proxy.BudgetState,
    incident_id: str,
    route: str,
    marker: str,
) -> str:
    spec, replay = current.reserve_recovery_probe(
        route=route,
        provenance=f"hindsight-recovery-{marker * 24}-{route}",
        auth_incident_request_id=incident_id,
        agent_circuit_open=True,
        pocket_circuit_open=True,
        provider_in_flight=0,
        projected_full_cost_usd=26.43,
    )
    assert replay is None
    call = current.record(
        str(spec["request_id"]),
        {"prompt_tokens": 1, "completion_tokens": 1 if route == "gpt_oss" else 0},
    )
    return str(call["request_id"])


def complete_auth_episode(
    current: budget_proxy.BudgetState, status: int = 401
) -> tuple[str, dict[str, str]]:
    incident_id = begin_auth_episode(current, status)
    return incident_id, {
        "gpt_oss": succeed_probe(current, incident_id, "gpt_oss", "a"),
        "qwen": succeed_probe(current, incident_id, "qwen", "b"),
    }


def reset(
    current: budget_proxy.BudgetState,
    configured: budget_proxy.Settings,
    token: str = "one-shot-a",
):
    return reset_provider_auth_circuit(
        current.state_path,
        current.ledger_path,
        acknowledgement=True,
        reset_token=token,
        expected_guard=configured.guard_fingerprint(),
        expected_initial_provider_spend_usd=configured.initial_provider_spend_usd,
        provider_max_in_flight=configured.provider_max_in_flight,
    )


def test_reset_requires_and_records_both_exact_paid_route_proofs(tmp_path: Path) -> None:
    current, configured = new_state(tmp_path)
    incident_id, request_ids = complete_auth_episode(current)

    result = reset(current, configured)

    assert result["reset"] is True
    assert result["auth_incident_request_id"] == incident_id
    assert result["probe_request_ids"] == request_ids
    saved = json.loads(current.state_path.read_text())
    assert saved["circuit_open"] is False
    assert saved["in_flight"] == {}
    assert "reason" not in saved
    assert "provider_auth_recovery" not in saved
    assert "provider_auth_circuit_incident_request_id" not in saved
    assert "provider_auth_circuit_incident_status" not in saved
    proof = saved["last_provider_auth_reset"]
    assert proof["previous_reason"] == "provider returned 401"
    assert proof["credential_validated_by_exact_paid_probes"] is True
    assert set(proof["probe_successes"]) == {"gpt_oss", "qwen"}
    assert len(saved["consumed_provider_auth_reset_tokens"]) == 1
    assert current.state_path.stat().st_mode & 0o777 == 0o600


def test_reset_rejects_missing_route_success(tmp_path: Path) -> None:
    current, configured = new_state(tmp_path)
    incident_id = begin_auth_episode(current)
    succeed_probe(current, incident_id, "gpt_oss", "c")
    with pytest.raises(ValueError, match="both exact GPT-OSS and Qwen"):
        reset(current, configured)


def test_maximum_charged_ambiguity_is_not_a_success_proof(tmp_path: Path) -> None:
    current, configured = new_state(tmp_path)
    incident_id = begin_auth_episode(current)
    succeed_probe(current, incident_id, "gpt_oss", "d")
    spec, replay = current.reserve_recovery_probe(
        route="qwen",
        provenance="hindsight-recovery-" + "e" * 24 + "-qwen",
        auth_incident_request_id=incident_id,
        agent_circuit_open=True,
        pocket_circuit_open=True,
        provider_in_flight=0,
        projected_full_cost_usd=26.43,
    )
    assert replay is None
    current.open_circuit(
        "ambiguous upstream failure: recovery probe ReadTimeout",
        request_id=str(spec["request_id"]),
    )
    manifest = current.exact_reservation_manifest()
    reconcile_ambiguous.reconcile(
        current.ledger_path,
        current.state_path,
        acknowledgement=True,
        expected_manifest_sha256=str(manifest["sha256"]),
        expected_guard=configured.guard_fingerprint(),
        expected_initial_provider_spend_usd=configured.initial_provider_spend_usd,
        provider_max_in_flight=configured.provider_max_in_flight,
    )
    restarted = budget_proxy.BudgetState(
        current.ledger_path, current.state_path, configured
    )
    with pytest.raises(ValueError, match="both exact GPT-OSS and Qwen"):
        reset(restarted, configured)


def test_reset_rejects_success_bound_to_another_root_incident(tmp_path: Path) -> None:
    current, configured = new_state(tmp_path)
    complete_auth_episode(current)
    ledger = copy.deepcopy(current.ledger)
    ledger["calls"][-1]["recovery_auth_incident_request_id"] = str(uuid.uuid4())
    budget_proxy.atomic_json(current.ledger_path, ledger)
    with pytest.raises(ValueError, match="both exact GPT-OSS and Qwen"):
        reset(current, configured)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("in_flight", "in-flight reservation"),
        ("wrong_reason", "not a provider-auth"),
        ("closed", "not open"),
        ("missing_active", "active provider-auth recovery proof is absent"),
    ],
)
def test_reset_fails_closed_for_other_states(
    tmp_path: Path, mutation: str, match: str
) -> None:
    current, configured = new_state(tmp_path)
    complete_auth_episode(current)
    value = copy.deepcopy(current.value)
    if mutation == "in_flight":
        # A real reservation shape is needed so invariant validation reaches
        # the reset's explicit ambiguity gate.
        request_id = str(uuid.uuid4())
        value["in_flight"][request_id] = {
            "model": budget_proxy.HINDSIGHT_MODEL,
            "endpoint": "chat",
            "scope": "hindsight_backfill",
            "period": "all",
            "started_at": value["provider_auth_recovery"]["auth_incident_at"],
            "reservation": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "cost_usd": 0.00000075,
            },
        }
    elif mutation == "wrong_reason":
        value["reason"] = "local budget exceeded"
    elif mutation == "closed":
        value["circuit_open"] = False
        value.pop("reason", None)
        value.pop("provider_auth_recovery", None)
        value.pop("provider_auth_circuit_incident_request_id", None)
        value.pop("provider_auth_circuit_incident_status", None)
    else:
        value.pop("provider_auth_recovery", None)
    budget_proxy.atomic_json(current.state_path, value)
    with pytest.raises(ValueError, match=match):
        reset(current, configured)


def test_reset_requires_explicit_acknowledgement(tmp_path: Path) -> None:
    current, configured = new_state(tmp_path)
    complete_auth_episode(current, 402)
    with pytest.raises(ValueError, match="acknowledgement"):
        reset_provider_auth_circuit(
            current.state_path,
            current.ledger_path,
            acknowledgement=False,
            reset_token="one-shot-c",
            expected_guard=configured.guard_fingerprint(),
            expected_initial_provider_spend_usd=0.0,
            provider_max_in_flight=6,
        )


def test_if_needed_refuses_absent_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reset_provider_auth.py",
            "--state",
            str(tmp_path / "absent-state.json"),
            "--ledger",
            str(tmp_path / "absent-ledger.json"),
            "--provider-max-in-flight",
            "6",
            "--acknowledge-provider-credential-validated",
            "--reset-token",
            "one-shot",
            "--if-needed",
        ],
    )
    with pytest.raises(ValueError, match="state is absent"):
        reset_provider_auth.main()


def test_if_needed_refuses_absent_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"
    budget_proxy.atomic_json(
        state_path, {"circuit_open": False, "in_flight": {}}
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reset_provider_auth.py",
            "--state",
            str(state_path),
            "--ledger",
            str(tmp_path / "absent-ledger.json"),
            "--provider-max-in-flight",
            "6",
            "--acknowledge-provider-credential-validated",
            "--reset-token",
            "one-shot",
            "--if-needed",
        ],
    )
    with pytest.raises(ValueError, match="ledger is absent"):
        reset_provider_auth.main()


def test_if_needed_replays_only_exact_ledger_bound_completed_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current, configured = new_state(tmp_path)
    complete_auth_episode(current)
    reset(current, configured, "replay-token")
    argv = [
        "reset_provider_auth.py",
        "--state",
        str(current.state_path),
        "--ledger",
        str(current.ledger_path),
        "--provider-max-in-flight",
        "6",
        "--acknowledge-provider-credential-validated",
        "--reset-token",
        "replay-token",
        "--if-needed",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert reset_provider_auth.main() == 0

    value = json.loads(current.state_path.read_text())
    value["last_provider_auth_reset"]["probe_successes"]["qwen"][
        "cost_usd"
    ] += 1
    budget_proxy.atomic_json(current.state_path, value)
    with pytest.raises(ValueError, match="completed provider-auth reset proof"):
        reset_provider_auth.main()


def test_reset_token_is_one_shot_across_auth_episodes(tmp_path: Path) -> None:
    current, configured = new_state(tmp_path)
    complete_auth_episode(current)
    reset(current, configured, "single-use")

    restarted = budget_proxy.BudgetState(
        current.ledger_path, current.state_path, configured
    )
    second_incident = begin_auth_episode(restarted, 402)
    with pytest.raises(
        budget_proxy.GuardViolation, match="incident lineage drift"
    ):
        restarted.reserve_recovery_probe(
            route="gpt_oss",
            provenance="hindsight-recovery-" + "a" * 24 + "-gpt_oss",
            auth_incident_request_id=second_incident,
            agent_circuit_open=True,
            pocket_circuit_open=True,
            provider_in_flight=0,
            projected_full_cost_usd=26.43,
        )
    succeed_probe(restarted, second_incident, "gpt_oss", "c")
    succeed_probe(restarted, second_incident, "qwen", "d")
    with pytest.raises(ValueError, match="already been consumed"):
        reset(restarted, configured, "single-use")
