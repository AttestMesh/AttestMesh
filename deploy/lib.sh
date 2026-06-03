#!/usr/bin/env bash
# Shared logging + step helpers for AttestMesh deploy routines.
# Every step tees full output to a timestamped per-step logfile under deploy/logs/
# so that on a re-run you can see exactly what failed (goal directive: add logging
# into anything that gives trouble).

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="${LOGDIR:-$LIB_DIR/logs}"
mkdir -p "$LOGDIR"

ts()  { date -u +%Y%m%dT%H%M%SZ; }
log() { printf '[%s] %s\n' "$(ts)" "$*" | tee -a "$LOGDIR/deploy.log" >&2; }
die() { log "FATAL: $*"; exit 1; }

# require <VAR> ... : fail loudly if any named env var is empty.
require() {
  local miss=0 v
  for v in "$@"; do
    if [ -z "${!v:-}" ]; then log "MISSING required env: $v"; miss=1; fi
  done
  [ "$miss" -eq 0 ] || die "missing required environment (source deploy/env.sh first)"
}

# run_step <name> <cmd...> : log start, tee output to its own logfile, fail loud with a tail.
run_step() {
  local name="$1"; shift
  local lf="$LOGDIR/${name}.$(ts).log"
  log "▶ ${name}: $*"
  log "  └ log: $lf"
  if "$@" >"$lf" 2>&1; then
    log "✔ ${name}"
    return 0
  else
    local rc=$?
    log "✗ ${name} FAILED (rc=${rc}). Last 25 lines:"
    tail -25 "$lf" | sed 's/^/    | /' >&2
    return "$rc"
  fi
}
