#!/usr/bin/env python3
"""Explicitly clear a non-ambiguous provider-auth circuit after key validation."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from budget_proxy import (
    GuardViolation,
    atomic_json,
    configured_budget_guard,
    exact_recovery_auth_incident,
    successful_recovery_probe_calls,
    validate_ledger_invariants,
    validate_state_invariants,
)


RESETTABLE_REASONS = {"provider returned 401", "provider returned 402"}


def reset_provider_auth_circuit(
    state_path: str | Path,
    ledger_path: str | Path,
    *,
    acknowledgement: bool,
    reset_token: str,
    expected_guard: dict[str, Any] | None = None,
    expected_initial_provider_spend_usd: float | None = None,
    provider_max_in_flight: int = 6,
) -> dict[str, Any]:
    """Clear only after both exact, real provider-auth route probes succeed."""

    if not acknowledgement:
        raise ValueError("validated-provider-credential acknowledgement is required")
    if not reset_token:
        raise ValueError("a non-empty one-shot reset token is required")

    state_file = Path(state_path)
    ledger_file = Path(ledger_path)
    state = json.loads(state_file.read_text())
    ledger = json.loads(ledger_file.read_text())
    try:
        validate_ledger_invariants(
            ledger,
            expected_guard=expected_guard,
            expected_initial_provider_spend_usd=(
                expected_initial_provider_spend_usd
            ),
        )
        validate_state_invariants(
            state,
            prices=ledger["guard"]["prices"],
            provider_max_in_flight=provider_max_in_flight,
        )
    except GuardViolation as exc:
        raise ValueError(str(exc)) from exc
    if not state.get("circuit_open"):
        raise ValueError("proxy circuit is not open")
    if state.get("in_flight"):
        raise ValueError("proxy has ambiguous in-flight reservations")

    reason = str(state.get("reason") or "")
    if reason not in RESETTABLE_REASONS:
        raise ValueError(f"circuit reason is not a provider-auth failure: {reason!r}")

    active = state.get("provider_auth_recovery")
    if not isinstance(active, dict):
        raise ValueError("active provider-auth recovery proof is absent")
    incident_request_id = str(active.get("auth_incident_request_id") or "")
    try:
        incident = exact_recovery_auth_incident(state, incident_request_id)
    except GuardViolation as exc:
        raise ValueError(str(exc)) from exc
    if (
        active.get("auth_incident_status") != incident.get("status_code")
        or active.get("auth_incident_at") != incident.get("at")
        or state.get("provider_auth_circuit_incident_request_id")
        != incident_request_id
        or state.get("provider_auth_circuit_incident_status")
        != incident.get("status_code")
        or reason != f"provider returned {incident['status_code']}"
    ):
        raise ValueError("active provider-auth recovery root incident drifted")
    try:
        successes = successful_recovery_probe_calls(
            ledger, incident_request_id
        )
    except GuardViolation as exc:
        raise ValueError(str(exc)) from exc
    if set(successes) != {"gpt_oss", "qwen"}:
        raise ValueError(
            "both exact GPT-OSS and Qwen paid recovery probes must succeed"
        )
    incident_at = datetime.fromisoformat(str(incident["at"]))
    for route, call in successes.items():
        recorded_at = datetime.fromisoformat(str(call["at"]))
        if recorded_at < incident_at:
            raise ValueError(
                f"{route} recovery probe predates its auth incident"
            )

    token_sha256 = hashlib.sha256(reset_token.encode()).hexdigest()
    consumed = state.setdefault("consumed_provider_auth_reset_tokens", [])
    if token_sha256 in consumed:
        raise ValueError("one-shot reset token has already been consumed")

    reset_at = datetime.now(timezone.utc).isoformat()
    state["circuit_open"] = False
    state.pop("reason", None)
    state["last_provider_auth_reset"] = {
        "at": reset_at,
        "previous_reason": reason,
        "credential_validated_by_exact_paid_probes": True,
        "auth_incident_request_id": incident_request_id,
        "auth_incident_status": int(incident["status_code"]),
        "probe_successes": {
            route: {
                "request_id": str(call["request_id"]),
                "request_provenance_sha256": str(
                    call["request_provenance_sha256"]
                ),
                "recovery_gate_sha256": str(call["recovery_gate_sha256"]),
                "cost_usd": float(call["cost_usd"]),
            }
            for route, call in sorted(successes.items())
        },
        "reset_token_sha256": token_sha256,
    }
    state.pop("provider_auth_recovery", None)
    state.pop("provider_auth_circuit_incident_request_id", None)
    state.pop("provider_auth_circuit_incident_status", None)
    consumed.append(token_sha256)
    try:
        validate_state_invariants(
            state,
            prices=ledger["guard"]["prices"],
            provider_max_in_flight=provider_max_in_flight,
        )
    except GuardViolation as exc:
        raise ValueError(str(exc)) from exc
    atomic_json(state_file, state)
    return {
        "reset": True,
        "previous_reason": reason,
        "at": reset_at,
        "auth_incident_request_id": incident_request_id,
        "probe_request_ids": {
            route: str(call["request_id"])
            for route, call in sorted(successes.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--provider-max-in-flight", required=True, type=int)
    parser.add_argument(
        "--acknowledge-provider-credential-validated", action="store_true"
    )
    parser.add_argument("--reset-token", required=True)
    parser.add_argument(
        "--if-needed",
        action="store_true",
        help="exit successfully when the circuit is already closed",
    )
    args = parser.parse_args()

    expected_guard, expected_initial_spend = configured_budget_guard()
    if args.if_needed:
        state_path = Path(args.state)
        ledger_path = Path(args.ledger)
        if not state_path.exists():
            raise ValueError(
                "provider-auth reset state is absent; refusing an unproven no-op"
            )
        if not ledger_path.exists():
            raise ValueError(
                "provider-auth reset ledger is absent; refusing an unproven no-op"
            )
        state = json.loads(state_path.read_text())
        ledger = json.loads(ledger_path.read_text())
        try:
            validate_ledger_invariants(
                ledger,
                expected_guard=expected_guard,
                expected_initial_provider_spend_usd=expected_initial_spend,
            )
            validate_state_invariants(
                state,
                prices=ledger["guard"]["prices"],
                provider_max_in_flight=args.provider_max_in_flight,
            )
        except GuardViolation as exc:
            raise ValueError(str(exc)) from exc
        if not state.get("circuit_open"):
            token_sha256 = hashlib.sha256(args.reset_token.encode()).hexdigest()
            previous = dict(state.get("last_provider_auth_reset") or {})
            incident_request_id = str(
                previous.get("auth_incident_request_id") or ""
            )
            try:
                incident = exact_recovery_auth_incident(
                    state, incident_request_id
                )
                successes = successful_recovery_probe_calls(
                    ledger, incident_request_id
                )
            except GuardViolation as exc:
                raise ValueError(str(exc)) from exc
            expected_proofs = {
                route: {
                    "request_id": str(call["request_id"]),
                    "request_provenance_sha256": str(
                        call["request_provenance_sha256"]
                    ),
                    "recovery_gate_sha256": str(call["recovery_gate_sha256"]),
                    "cost_usd": float(call["cost_usd"]),
                }
                for route, call in sorted(successes.items())
            }
            if (
                previous.get("reset_token_sha256") != token_sha256
                or previous.get("credential_validated_by_exact_paid_probes")
                is not True
                or previous.get("auth_incident_status")
                != incident.get("status_code")
                or previous.get("previous_reason")
                != f"provider returned {incident['status_code']}"
                or expected_proofs.keys() != {"gpt_oss", "qwen"}
                or previous.get("probe_successes") != expected_proofs
            ):
                raise ValueError(
                    "closed circuit lacks the exact completed provider-auth reset proof"
                )
            print(
                json.dumps(
                    {
                        "reset": False,
                        "reason": "matching provider-auth reset already completed",
                        "auth_incident_request_id": previous.get(
                            "auth_incident_request_id"
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 0

    result = reset_provider_auth_circuit(
        args.state,
        args.ledger,
        acknowledgement=args.acknowledge_provider_credential_validated,
        reset_token=args.reset_token,
        expected_guard=expected_guard,
        expected_initial_provider_spend_usd=expected_initial_spend,
        provider_max_in_flight=args.provider_max_in_flight,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
