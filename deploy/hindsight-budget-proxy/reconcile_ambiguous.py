#!/usr/bin/env python3
"""Explicitly reconcile ambiguous proxy reservations at their maximum cost."""

from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from budget_proxy import (
    FINAL_MAINTENANCE_HOLD_REASON,
    GuardViolation,
    RECOVERY_PROBE_SUCCESS_HOLD_REASON,
    atomic_json,
    configured_budget_guard,
    exact_record_commit_matches,
    exact_recovery_auth_incident,
    reservation_manifest,
    validate_ledger_invariants,
    validate_state_invariants,
)


def _reconciliation_call(
    request_id: str, item: dict[str, Any], *, at: str
) -> dict[str, Any]:
    reservation = dict(item.get("reservation") or {})
    call = {
        "at": at,
        "started_at": item.get("started_at"),
        "request_id": request_id,
        "endpoint": item.get("endpoint"),
        "model": item.get("model"),
        "scope": item.get("scope"),
        "period": item.get("period"),
        "request_provenance_sha256": item.get(
            "request_provenance_sha256"
        ),
        "input_tokens": int(reservation.get("input_tokens") or 0),
        "output_tokens": int(reservation.get("output_tokens") or 0),
        "cost_usd": float(reservation.get("cost_usd") or 0),
        "provider_reported_cost_usd": None,
        "estimated_from_reservation": True,
        "ambiguous_upstream_reconciled": True,
    }
    if item.get("recovery_probe_route") is not None:
        call["recovery_probe_route"] = item["recovery_probe_route"]
        call["recovery_auth_incident_request_id"] = item[
            "recovery_auth_incident_request_id"
        ]
        call["recovery_gate_sha256"] = item["recovery_gate_sha256"]
    return call


def _same_reconciliation_call(
    existing: dict[str, Any], expected: dict[str, Any]
) -> bool:
    exact_fields = (
        "started_at",
        "request_id",
        "endpoint",
        "model",
        "scope",
        "period",
        "request_provenance_sha256",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "provider_reported_cost_usd",
        "estimated_from_reservation",
        "ambiguous_upstream_reconciled",
        "recovery_probe_route",
        "recovery_auth_incident_request_id",
        "recovery_gate_sha256",
    )
    return all(existing.get(name) == expected.get(name) for name in exact_fields)


def reconcile(
    ledger_path: str | Path,
    state_path: str | Path,
    *,
    acknowledgement: bool,
    expected_manifest_sha256: str,
    expected_guard: dict[str, Any] | None = None,
    expected_initial_provider_spend_usd: float | None = None,
    provider_max_in_flight: int = 6,
) -> dict[str, Any]:
    if not acknowledgement:
        raise ValueError("maximum-spend acknowledgement is required")

    ledger_file = Path(ledger_path)
    state_file = Path(state_path)
    ledger = json.loads(ledger_file.read_text())
    state = json.loads(state_file.read_text())
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
    if state.get("final_maintenance_hold") or str(state.get("reason") or "") == (
        FINAL_MAINTENANCE_HOLD_REASON
    ):
        raise ValueError("final maintenance hold forbids ambiguity reconciliation")
    in_flight = state.get("in_flight") or {}
    if not in_flight:
        last = dict(state.get("last_ambiguous_reconciliation") or {})
        record_repair = dict(state.get("last_record_commit_repair") or {})
        if last.get("manifest_sha256") == expected_manifest_sha256:
            return {
                "requests": int(last["requests"]),
                "reservation_ids": list(last["reservation_ids"]),
                "record_commit_repaired_ids": list(
                    last.get("record_commit_repaired_ids") or []
                ),
                "maximum_charged_ids": list(last.get("maximum_charged_ids") or []),
                "manifest_sha256": str(last["manifest_sha256"]),
                "charged_maximum_usd": float(last["charged_maximum_usd"]),
                "newly_charged_usd": 0.0,
                "provider_spent_usd": float(ledger.get("provider_spent_usd") or 0),
                "replayed": True,
                "result": str(last.get("result") or "maximum_charged_and_drained"),
            }
        if record_repair.get("manifest_sha256") == expected_manifest_sha256:
            return {
                "requests": int(record_repair["requests"]),
                "reservation_ids": list(record_repair["request_ids"]),
                "record_commit_repaired_ids": list(record_repair["request_ids"]),
                "maximum_charged_ids": [],
                "manifest_sha256": str(record_repair["manifest_sha256"]),
                "charged_maximum_usd": 0.0,
                "newly_charged_usd": 0.0,
                "provider_spent_usd": float(ledger.get("provider_spent_usd") or 0),
                "replayed": True,
                "result": "record_commits_repaired_and_drained",
            }
        raise ValueError("proxy has no matching ambiguous reservation manifest")

    manifest = reservation_manifest(in_flight, ledger["guard"]["prices"])
    if manifest["sha256"] != expected_manifest_sha256:
        raise ValueError("ambiguous reservation manifest changed")

    if state["circuit_open"] is False:
        # The only permitted closed+reserved shape is a crash after the
        # reservation commit and before the proxy persisted its ambiguity
        # circuit. Full ledger and state validation above happens before this
        # non-spending materialization, so unrelated provenance corruption can
        # never be converted into an authorized maximum charge.
        state_candidate = copy.deepcopy(state)
        state_candidate["circuit_open"] = True
        state_candidate["reason"] = (
            "operator-authorized stale reservation reconciliation"
        )
        atomic_json(state_file, state_candidate)
        state = state_candidate
    else:
        reason = str(state.get("reason") or "")
        approved_reason = (
            reason == "ambiguous upstream failure"
            or reason.startswith("ambiguous upstream failure:")
            or reason
            == "ambiguous in-flight requests found after proxy restart"
            or reason
            == "operator-authorized stale reservation reconciliation"
        )
        if not approved_reason:
            raise ValueError(
                "only an exact open ambiguity circuit may be reconciled"
            )

    now = datetime.now(timezone.utc).isoformat()
    ledger_candidate = copy.deepcopy(ledger)
    calls = ledger_candidate.setdefault("calls", [])
    charged = 0.0
    newly_charged = 0.0
    record_commit_repaired_ids: list[str] = []
    maximum_charged_ids: list[str] = []
    for request_id, item in sorted(in_flight.items()):
        expected = _reconciliation_call(request_id, item, at=now)
        all_existing = [
            call
            for call in calls
            if call.get("request_id") == request_id
        ]
        normal = [
            call
            for call in all_existing
            if call.get("ambiguous_upstream_reconciled") is not True
        ]
        if normal:
            price_guard = dict(
                dict(ledger_candidate["guard"])["prices"][str(item["model"])]
            )
            prices = (
                float(price_guard["input_usd_per_m"]),
                float(price_guard["output_usd_per_m"]),
            )
            if len(normal) == 1 and exact_record_commit_matches(
                normal[0], request_id, item, prices=prices
            ):
                record_commit_repaired_ids.append(request_id)
                continue
            raise ValueError(
                f"reservation already has a non-reconciliation ledger row for {request_id}"
            )
        existing = [
            call
            for call in all_existing
            if call.get("ambiguous_upstream_reconciled") is True
        ]
        if len(existing) > 1:
            raise ValueError(f"duplicate reconciliation ledger rows for {request_id}")
        if existing:
            if not _same_reconciliation_call(existing[0], expected):
                raise ValueError(
                    f"reconciliation ledger provenance drift for {request_id}"
                )
        else:
            calls.append(expected)
            newly_charged += float(expected["cost_usd"])
        maximum_charged_ids.append(request_id)
        charged += float(expected["cost_usd"])

    ledger_candidate["provider_spent_usd"] = (
        float(ledger_candidate.get("provider_spent_usd") or 0) + newly_charged
    )
    validate_ledger_invariants(
        ledger_candidate,
        expected_guard=expected_guard,
        expected_initial_provider_spend_usd=(
            expected_initial_provider_spend_usd
        ),
    )
    # Ledger first: after a crash here, the unchanged state manifest is replayed
    # and exact request IDs prevent a duplicate charge.
    atomic_json(ledger_file, ledger_candidate)

    reservation_ids = [
        str(value["request_id"]) for value in manifest["reservations"]
    ]
    state_candidate = copy.deepcopy(state)
    state_candidate["in_flight"] = {}
    repaired_recovery_probes = [
        request_id
        for request_id in record_commit_repaired_ids
        if in_flight[request_id].get("recovery_probe_route") is not None
    ]
    reconciled_recovery_probes = [
        request_id
        for request_id in maximum_charged_ids
        if in_flight[request_id].get("recovery_probe_route") is not None
    ]
    recovered_probe_ids = repaired_recovery_probes + reconciled_recovery_probes
    if recovered_probe_ids:
        try:
            incident_ids = {
                str(in_flight[request_id]["recovery_auth_incident_request_id"])
                for request_id in recovered_probe_ids
            }
            if len(incident_ids) != 1:
                raise ValueError("recovered probe reservations span auth incidents")
            incident = exact_recovery_auth_incident(
                state_candidate, incident_ids.pop()
            )
            state_candidate["circuit_open"] = True
            state_candidate["reason"] = (
                "provider returned " + str(incident["status_code"])
            )
        except (GuardViolation, ValueError, KeyError):
            state_candidate["circuit_open"] = True
            state_candidate["reason"] = RECOVERY_PROBE_SUCCESS_HOLD_REASON
    else:
        state_candidate["circuit_open"] = False
        state_candidate.pop("reason", None)
    result_kind = (
        "maximum_charged_and_drained"
        if not record_commit_repaired_ids
        else (
            "record_commits_repaired_and_drained"
            if not maximum_charged_ids
            else "record_commits_repaired_and_maximum_charged"
        )
    )
    if record_commit_repaired_ids:
        state_candidate["last_record_commit_repair"] = {
            "at": now,
            "requests": len(in_flight),
            "request_ids": record_commit_repaired_ids,
            "reservation_ids": reservation_ids,
            "reservations": manifest["reservations"],
            "manifest_sha256": manifest["sha256"],
            "total_max_usd": manifest["total_max_usd"],
            "cleared_exact_record_ambiguity": not repaired_recovery_probes,
            "result": result_kind,
        }
    if maximum_charged_ids:
        state_candidate["last_ambiguous_reconciliation"] = {
            "at": now,
            "requests": len(in_flight),
            "reservation_ids": reservation_ids,
            "record_commit_repaired_ids": record_commit_repaired_ids,
            "maximum_charged_ids": maximum_charged_ids,
            "reservations": manifest["reservations"],
            "manifest_sha256": manifest["sha256"],
            "total_max_usd": manifest["total_max_usd"],
            "charged_maximum_usd": charged,
            "provider_spent_usd": ledger_candidate["provider_spent_usd"],
            "result": result_kind,
        }
    atomic_json(state_file, state_candidate)
    return {
        "requests": len(in_flight),
        "reservation_ids": reservation_ids,
        "record_commit_repaired_ids": record_commit_repaired_ids,
        "maximum_charged_ids": maximum_charged_ids,
        "manifest_sha256": manifest["sha256"],
        "charged_maximum_usd": charged,
        "newly_charged_usd": newly_charged,
        "provider_spent_usd": ledger_candidate["provider_spent_usd"],
        "replayed": newly_charged == 0,
        "result": result_kind,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--acknowledge-max-spend", action="store_true")
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--provider-max-in-flight", required=True, type=int)
    parser.add_argument(
        "--if-needed",
        action="store_true",
        help="exit successfully without changing state when no ambiguous reservation exists",
    )
    args = parser.parse_args()
    if args.if_needed:
        state_path = Path(args.state)
        if not state_path.exists():
            raise ValueError(
                "ambiguity reconciliation state is absent; refusing an unproven no-op"
            )
    expected_guard, expected_initial_spend = configured_budget_guard()
    result = reconcile(
        args.ledger,
        args.state,
        acknowledgement=args.acknowledge_max_spend,
        expected_manifest_sha256=args.expected_manifest_sha256,
        expected_guard=expected_guard,
        expected_initial_provider_spend_usd=expected_initial_spend,
        provider_max_in_flight=args.provider_max_in_flight,
    )
    result["reconciled"] = True
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
