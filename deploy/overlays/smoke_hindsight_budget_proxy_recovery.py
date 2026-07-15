#!/usr/bin/env python3
"""Verify the exact reviewed recovery helpers inside a built proxy image."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


RUNTIME_FILES = {
    "deploy/hindsight-budget-proxy/budget_proxy.py": "/opt/attestmesh/budget_proxy.py",
    "deploy/hindsight-budget-proxy/reconcile_ambiguous.py": "/opt/attestmesh/reconcile_ambiguous.py",
    "deploy/hindsight-budget-proxy/reset_provider_auth.py": "/opt/attestmesh/reset_provider_auth.py",
    "deploy/hindsight-budget-proxy/control_marker.py": "/opt/attestmesh/control_marker.py",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    args = parser.parse_args()
    expected = {
        runtime: sha256(args.repo_root / source)
        for source, runtime in RUNTIME_FILES.items()
    }
    runtime_paths = " ".join(expected)
    helpers = " ".join(
        (
            "/opt/attestmesh/reconcile_ambiguous.py",
            "/opt/attestmesh/reset_provider_auth.py",
            "/opt/attestmesh/control_marker.py",
        )
    )
    script = (
        f"sha256sum {runtime_paths}\n"
        f"for helper in {helpers}; do "
        "/app/api/.venv/bin/python \"$helper\" --help >/dev/null; done"
    )
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            args.image,
            "-ec",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    actual: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] in expected:
            actual[parts[1]] = parts[0]
    if actual != expected:
        raise SystemExit(
            "runtime recovery helper hash mismatch: "
            + json.dumps({"expected": expected, "actual": actual}, sort_keys=True)
        )
    image_id = subprocess.run(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(
        json.dumps(
            {"image": args.image, "image_id": image_id, "files": actual},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
