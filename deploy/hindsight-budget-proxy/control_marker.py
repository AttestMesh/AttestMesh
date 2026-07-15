#!/usr/bin/env python3
"""Atomically persist a payload-free recovery-control roll marker."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from budget_proxy import GuardViolation, atomic_json


CONTROL_FILENAMES = {
    "reconcile": "reconcile-control.json",
    "auth_reset": "auth-reset-control.json",
}


def write_control_marker(
    directory: str | Path,
    *,
    control: str,
    nonce: str,
    roll_sha256: str,
    enabled: bool,
    secret_present: bool,
) -> dict[str, Any]:
    if control not in CONTROL_FILENAMES:
        raise GuardViolation("recovery control marker kind is invalid")
    if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise GuardViolation("recovery control marker nonce is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", roll_sha256) is None:
        raise GuardViolation("recovery control marker roll fingerprint is invalid")
    if type(enabled) is not bool or type(secret_present) is not bool:
        raise GuardViolation("recovery control marker bits are malformed")
    if enabled is not secret_present:
        raise GuardViolation("recovery control marker enable/secret bits disagree")
    value = {
        "version": 2,
        "control": control,
        "nonce": nonce,
        "roll_sha256": roll_sha256,
        "enabled": enabled,
        "secret_present": secret_present,
        "completed": True,
    }
    atomic_json(Path(directory) / CONTROL_FILENAMES[control], value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--control", choices=sorted(CONTROL_FILENAMES), required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--roll-sha256", required=True)
    parser.add_argument("--enabled", choices=("0", "1"), required=True)
    parser.add_argument("--secret-present", choices=("0", "1"), required=True)
    args = parser.parse_args()
    value = write_control_marker(
        args.directory,
        control=args.control,
        nonce=args.nonce,
        roll_sha256=args.roll_sha256,
        enabled=args.enabled == "1",
        secret_present=args.secret_present == "1",
    )
    print(
        "recovery control marker persisted: "
        f"control={value['control']} enabled={int(value['enabled'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
