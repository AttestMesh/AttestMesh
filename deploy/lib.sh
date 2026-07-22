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

# Resolve an authenticated box-local Base Reth proxy URL without copying its
# token to the repository. Callers keep the URL in memory and seal it directly
# into the CVM environment.
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

# Wait until the exact compose allowlist state is visible through the
# box-local KMS RPC before stopping a healthy VM for an upgrade. The caller has
# already confirmed the allowlist transaction through the public RPC; an exact
# `true` result from the local RPC proves that its own chain view includes the
# new hash even when its reported head trails the public RPC. The eth_call is
# bound to the local head, and two consecutive reads are required.
wait_box_local_allowlist_propagation() {
  local box_host="${1:?box host required}"
  local local_rpc="${2:?box-local RPC required}"
  local cluster="${3:?cluster required}"
  local compose_hash="${4:?compose hash required}"
  local timeout_seconds="${6:-${ALLOWLIST_PROPAGATION_TIMEOUT_SECONDS:-300}}"
  local timeout_seconds="${6:-300}"
  local calldata deadline result local_block allowed consecutive=0
  calldata=$(cast calldata 'allowedComposeHashes(bytes32)' "0x${compose_hash#0x}") \
    || die "could not encode local allowlist proof call"
  deadline=$(( $(date +%s) + timeout_seconds ))
  log "waiting for box-local KMS RPC to prove the exact compose allowlist"
  while [ "$(date +%s)" -lt "$deadline" ]; do
    result=$(
      {
        printf 'export LOCAL_RPC=%q\n' "$local_rpc"
        printf 'export CLUSTER=%q\n' "$cluster"
        printf 'export CALLDATA=%q\n' "$calldata"
        cat <<'SCRIPT'
python3 - <<'PY'
import json
import os
import urllib.request

url = os.environ["LOCAL_RPC"]

def rpc(method, params):
    request = urllib.request.Request(
        url,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    if value.get("error"):
        raise RuntimeError(value["error"])
    return value["result"]

head_hex = rpc("eth_blockNumber", [])
head = int(head_hex, 16)
raw = rpc(
    "eth_call",
    [{"to": os.environ["CLUSTER"], "data": os.environ["CALLDATA"]}, head_hex],
)
print(json.dumps({"block": head, "allowed": int(raw, 16) == 1}))
PY
SCRIPT
      } | ssh -o BatchMode=yes -o ConnectTimeout=8 "$box_host" "sudo bash -s" 2>/dev/null
    ) || result=''
    local_block=$(printf '%s\n' "$result" | jq -r '.block // -1' 2>/dev/null || printf '%s' -1)
    allowed=$(printf '%s\n' "$result" | jq -r '.allowed // false' 2>/dev/null || printf '%s' false)
    if [ "$allowed" = true ]; then
      consecutive=$((consecutive + 1))
      if [ "$consecutive" -ge 2 ]; then
        log "✔ box-local KMS RPC proves compose allowlist at block=$local_block"
        return 0
      fi
    else
      consecutive=0
    fi
    sleep 5
  done
  die "box-local KMS RPC did not converge on the exact compose allowlist; old VM left running"
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

# Refuse to roll when the files that shape a deployment drift from origin/main.
#
# The 2026-07-22 synclave outage came from rolling with a checkout parked on a stale feature
# branch: its synclave-node.sh still carried the pre-broker hostname defaults (app./sandbox.
# subdomains instead of the bare zone), so the sealed env tripped the app's
# APP_DOMAIN === SANDBOX_APPS_DOMAIN check and every post-refactor image crash-looped on boot.
# origin/main had been correct the whole time. The same drift had already bitten once that day
# via the compose's renamed CLOUDFLARE_DNS_* keys. Treat it as a hard error, not a convention:
# commit the change to main first, then roll from a checkout that matches it.
#
# ALLOW_ROLL_SOURCE_DRIFT=1 overrides (emergencies only; the drift is logged loudly).
verify_roll_source_matches_main() {
  local root="${1:?repo root required}"; shift
  [ "$#" -gt 0 ] || die "verify_roll_source_matches_main: no paths given"
  git -C "$root" rev-parse --git-dir >/dev/null 2>&1 \
    || die "roll source is not a git checkout: $root"
  git -C "$root" fetch --quiet origin main \
    || die "could not fetch origin/main to verify the roll source"
  local drifted="" p
  for p in "$@"; do
    git -C "$root" diff --quiet FETCH_HEAD -- "$p" 2>/dev/null \
      || drifted="${drifted}  - ${p}"$'\n'
  done
  if [ -z "$drifted" ]; then
    log "✔ roll source matches origin/main"
    return 0
  fi
  if [ "${ALLOW_ROLL_SOURCE_DRIFT:-}" = "1" ]; then
    log "⚠ roll source DIFFERS from origin/main — ALLOW_ROLL_SOURCE_DRIFT=1 override in effect:"
    printf '%s' "$drifted" >&2
    return 0
  fi
  die "roll source differs from origin/main — refusing to deploy stale deploy files:
${drifted}  Commit and push the change to main, then roll from a checkout at origin/main.
  (ALLOW_ROLL_SOURCE_DRIFT=1 overrides — emergencies only.)"
}
