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

# Resolve an authenticated box-local Base proxy URL without copying its token
# into the repository. Callers keep the URL in memory and seal it directly into
# a CVM environment over stdin.
box_local_rpc_url() {
  local box_host="${1:?box host required}" alias="${2:?rpc alias required}"
  local base="${BOX_LOCAL_RPC_BASE_URL:-http://10.0.100.1:8545}"
  local keys="${BOX_LOCAL_RPC_KEYS_FILE:-/srv/data/base-node/proxyd/keys.env}"
  local token
  case "$alias" in
    *[!a-zA-Z0-9_-]*) die "invalid local RPC alias: $alias" ;;
  esac
  token=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$box_host" \
    "sudo awk -F= -v a='$alias' '\$1 == a { print \$2; exit }' '$keys'" 2>/dev/null) \
    || die "could not read local RPC alias '$alias' from $box_host"
  [ -n "$token" ] || die "local RPC alias '$alias' is absent from $keys"
  printf '%s/%s\n' "${base%/}" "$token"
}

# run_step <name> <cmd...> : log start, tee output to its own logfile, fail loud with a tail.
run_step() {
  local name="$1"; shift
  local lf="$LOGDIR/${name}.$(ts).log"
  # Redact key material from the echoed command line (full output still goes to
  # the per-step logfile, which is gitignored — but the console echo travels).
  log "▶ ${name}: $(printf '%s ' "$@" | sed -E 's/(--private-key|--api-key|--token)[= ]+[^ ]+/\1 <redacted>/g')"
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

# A successful addComposeHash receipt can become visible through the public RPC
# before the KMS chain watcher has consumed the block.  Keep the live VM on its
# old compose until the on-chain readback succeeds and the KMS policy reader has
# had time to catch up.  Live Base-mainnet evidence on 2026-07-11 showed a newly
# mined hash taking roughly six minutes to become boot-eligible at the KMS, so
# the default deliberately leaves additional margin.
settle_compose_hash_for_kms() {
  local cluster="${1:?cluster required}" compose_hash="${2#0x}"
  local settle_seconds="${COMPOSE_ALLOWLIST_SETTLE_SECONDS:-600}"
  local allowed="" elapsed=0 step

  for _ in $(seq 1 30); do
    allowed=$(cast call "$cluster" 'allowedComposeHashes(bytes32)(bool)' \
      "0x$compose_hash" --rpc-url "$RPC_URL" 2>/dev/null || true)
    [ "$allowed" = true ] && break
    sleep 2
  done
  [ "$allowed" = true ] || die "compose hash 0x$compose_hash is not visible on-chain; refusing to stop the VM"

  log "compose hash 0x$compose_hash is on-chain; waiting ${settle_seconds}s for KMS visibility before stopping the VM"
  while [ "$elapsed" -lt "$settle_seconds" ]; do
    step=15
    [ $((settle_seconds - elapsed)) -lt "$step" ] && step=$((settle_seconds - elapsed))
    sleep "$step"
    elapsed=$((elapsed + step))
    log "KMS allowlist settle: ${elapsed}/${settle_seconds}s"
  done
}
