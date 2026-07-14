#!/usr/bin/env python3
"""Explicitly reconcile ambiguous proxy reservations at their maximum cost."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from budget_proxy import atomic_json


def reconcile(
    ledger_path: str | Path,
    state_path: str | Path,
    *,
    acknowledgement: bool,
) -> dict[str, Any]:
    if not acknowledgement:
        raise ValueError("maximum-spend acknowledgement is required")

    ledger_file = Path(ledger_path)
    state_file = Path(state_path)
    ledger = json.loads(ledger_file.read_text())
    state = json.loads(state_file.read_text())
    in_flight = state.get("in_flight") or {}
    if not in_flight:
        raise ValueError("proxy has no ambiguous reservations")

    now = datetime.now(timezone.utc).isoformat()
    calls = ledger.setdefault("calls", [])
    charged = 0.0
    for request_id, item in sorted(in_flight.items()):
        reservation = item.get("reservation") or {}
        cost = float(reservation.get("cost_usd") or 0)
        charged += cost
        calls.append(
            {
                "at": now,
                "started_at": item.get("started_at"),
                "request_id": request_id,
                "endpoint": item.get("endpoint"),
                "model": item.get("model"),
                "scope": item.get("scope"),
                "period": item.get("period"),
                "input_tokens": int(reservation.get("input_tokens") or 0),
                "output_tokens": int(reservation.get("output_tokens") or 0),
                "cost_usd": cost,
                "provider_reported_cost_usd": None,
                "estimated_from_reservation": True,
                "ambiguous_upstream_reconciled": True,
            }
        )

    ledger["provider_spent_usd"] = float(ledger.get("provider_spent_usd") or 0) + charged
    state["in_flight"] = {}
    state["circuit_open"] = False
    state.pop("reason", None)
    state["last_ambiguous_reconciliation"] = {
        "at": now,
        "requests": len(in_flight),
        "charged_maximum_usd": charged,
    }
    atomic_json(ledger_file, ledger)
    atomic_json(state_file, state)
    return {
        "requests": len(in_flight),
        "charged_maximum_usd": charged,
        "provider_spent_usd": ledger["provider_spent_usd"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--acknowledge-max-spend", action="store_true")
    parser.add_argument(
        "--if-needed",
        action="store_true",
        help="exit successfully without changing state when no ambiguous reservation exists",
    )
    args = parser.parse_args()
    if args.if_needed:
        state_path = Path(args.state)
        if not state_path.exists():
            print(json.dumps({"reconciled": False, "reason": "state file absent"}, sort_keys=True))
            return 0
        state = json.loads(state_path.read_text())
        if not (state.get("in_flight") or {}):
            print(
                json.dumps(
                    {"reconciled": False, "reason": "no ambiguous reservation"},
                    sort_keys=True,
                )
            )
            return 0
    result = reconcile(
        args.ledger,
        args.state,
        acknowledgement=args.acknowledge_max_spend,
    )
    result["reconciled"] = True
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
