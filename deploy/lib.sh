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

# Confirm a known transaction hash without ever resubmitting it. An optional log
# file keeps the receipt path next to the submission output.
confirm_transaction() {
  local label="${1:?label required}" rpc_url="${2:?rpc URL required}" tx_hash="${3:?transaction hash required}"
  local lf="${4:-$LOGDIR/${label}.$(ts).log}" waiter
  waiter="${RECEIPT_WAITER:-$LIB_DIR/wait-for-receipt.mjs}"
  [[ "$tx_hash" =~ ^0x[0-9a-fA-F]{64}$ ]] || { log "✗ ${label} returned an invalid transaction hash"; return 75; }
  log "… ${label} submitted tx=$tx_hash; awaiting receipt"
  if node "$waiter" "$tx_hash" "$rpc_url" >>"$lf" 2>&1; then
    log "✔ ${label} confirmed tx=$tx_hash"
    return 0
  fi
  log "✗ ${label} receipt confirmation FAILED; transaction will NOT be resubmitted. Last 25 lines:"
  tail -25 "$lf" | sed 's/^/    | /' >&2
  return 75
}

# Box-side sends keep their private key on the box and tee the async hash into a
# local deployment log. Confirm that hash from the coordinator immediately after
# SSH returns. The glob is scoped to one node/action and the newest log is the
# one just written by the caller.
confirm_latest_transaction() {
  local label="${1:?label required}" rpc_url="${2:?rpc URL required}" log_glob="${3:?log glob required}"
  local lf tx_hash
  # Deliberate glob expansion: log_glob is constructed by our deploy scripts.
  # shellcheck disable=SC2086
  lf=$(ls -1t $log_glob 2>/dev/null | head -1)
  [ -n "$lf" ] || { log "✗ ${label}: transaction log not found"; return 75; }
  tx_hash=$(grep -oE '0x[0-9a-fA-F]{64}' "$lf" | tail -1)
  [ -n "$tx_hash" ] || { log "✗ ${label}: no transaction hash in $lf"; return 75; }
  confirm_transaction "$label" "$rpc_url" "$tx_hash" "$lf"
}

# Submit once and confirm the exact transaction receipt. Return 75 after a hash
# has been published but confirmation fails, so callers never resubmit an
# ambiguous transaction. Ordinary pre-hash failures remain safe to retry.
send_confirmed() {
  local label="${1:?label required}" rpc_url="${2:?rpc URL required}" private_key="${3:?private key required}"
  shift 3
  local lf="$LOGDIR/${label}.$(ts).log" out rc tx_hash
  log "▶ ${label}: cast send $(printf '%s ' "$@") --rpc-url $rpc_url --private-key <redacted>"
  log "  └ log: $lf"
  if out=$(cast send "$@" --async --rpc-url "$rpc_url" --private-key "$private_key" 2>&1); then
    printf '%s\n' "$out" >"$lf"
  else
    rc=$?
    printf '%s\n' "$out" >"$lf"
    tx_hash=$(printf '%s\n' "$out" | grep -oE '0x[0-9a-fA-F]{64}' | tail -1)
    if [ -n "$tx_hash" ]; then
      log "⚠ ${label} returned rc=${rc} after publishing tx=$tx_hash; confirming instead of resubmitting"
      confirm_transaction "$label" "$rpc_url" "$tx_hash" "$lf"
      return
    fi
    log "✗ ${label} submission FAILED (rc=${rc}). Last 25 lines:"
    tail -25 "$lf" | sed 's/^/    | /' >&2
    return "$rc"
  fi
  tx_hash=$(printf '%s\n' "$out" | grep -oE '0x[0-9a-fA-F]{64}' | tail -1)
  if [ -z "$tx_hash" ]; then
    log "✗ ${label} returned no transaction hash; refusing to guess whether it was submitted"
    return 75
  fi
  confirm_transaction "$label" "$rpc_url" "$tx_hash" "$lf"
}

# Standard deployer send: refresh the nonce and retry once only when submission
# failed before a transaction hash was returned.
send_with_nonce_retry() {
  local label="${1:?label required}"; shift
  local nonce rc
  nonce=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL") || return
  send_confirmed "$label" "$RPC_URL" "$PRIVATE_KEY" "$@" --nonce "$nonce" && return 0
  rc=$?
  [ "$rc" -ne 75 ] || return "$rc"
  log "↻ $label: refetching nonce + retrying pre-submission failure"
  sleep 4
  nonce=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL") || return
  send_confirmed "${label}-retry" "$RPC_URL" "$PRIVATE_KEY" "$@" --nonce "$nonce"
}
