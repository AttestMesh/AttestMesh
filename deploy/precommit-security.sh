#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
exec python3 "$ROOT/deploy/precommit_security.py" --repo "$ROOT"
