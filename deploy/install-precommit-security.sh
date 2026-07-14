#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT/deploy"

if ! command -v bun >/dev/null 2>&1; then
  echo "Bun is required to install the pinned Smithers runtime." >&2
  exit 1
fi
if ! command -v codex >/dev/null 2>&1; then
  echo "Codex CLI is required for the gpt-5.6-sol/ultra review." >&2
  exit 1
fi

bun install --frozen-lockfile
version="$(bun "$ROOT/deploy/node_modules/smithers-orchestrator/src/bin/smithers.js" --version)"
if [[ "$version" != "0.22.0" ]]; then
  echo "Expected smithers-orchestrator 0.22.0, found $version." >&2
  exit 1
fi

git -C "$ROOT" config core.hooksPath .githooks
echo "Installed AttestMesh staged-security hook via core.hooksPath=.githooks"
echo "Codex authentication status:"
codex login status
