#!/usr/bin/env python3
"""Explicitly clear a non-ambiguous provider-auth circuit after key validation."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from budget_proxy import atomic_json


RESETTABLE_REASONS = {"provider returned 401", "provider returned 402"}


def reset_provider_auth_circuit(
    state_path: str | Path,
    *,
    acknowledgement: bool,
    reset_token: str,
) -> dict[str, Any]:
    """Clear only a proven, reservation-free provider authentication failure."""

    if not acknowledgement:
        raise ValueError("validated-provider-credential acknowledgement is required")
    if not reset_token:
        raise ValueError("a non-empty one-shot reset token is required")

    state_file = Path(state_path)
    state = json.loads(state_file.read_text())
    if not state.get("circuit_open"):
        raise ValueError("proxy circuit is not open")
    if state.get("in_flight"):
        raise ValueError("proxy has ambiguous in-flight reservations")

    reason = str(state.get("reason") or "")
    if reason not in RESETTABLE_REASONS:
        raise ValueError(f"circuit reason is not a provider-auth failure: {reason!r}")

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
        "credential_validated_out_of_band": True,
        "reset_token_sha256": token_sha256,
    }
    consumed.append(token_sha256)
    atomic_json(state_file, state)
    return {"reset": True, "previous_reason": reason, "at": reset_at}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
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

    if args.if_needed:
        state_path = Path(args.state)
        if not state_path.exists():
            print(json.dumps({"reset": False, "reason": "state file absent"}, sort_keys=True))
            return 0
        state = json.loads(state_path.read_text())
        if not state.get("circuit_open"):
            print(json.dumps({"reset": False, "reason": "circuit already closed"}, sort_keys=True))
            return 0

    result = reset_provider_auth_circuit(
        args.state,
        acknowledgement=args.acknowledge_provider_credential_validated,
        reset_token=args.reset_token,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
