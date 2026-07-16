#!/usr/bin/env bash
# confidential-sandboxes sandboxd node in the isolated Cluster 2.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: sandboxd-node.sh <node-name> [deploy|prime|bind|start|verify-health|smoke|update|retire-previous|replace|rollback|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/sandboxd-node.yaml}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
WEBHOST_STATE="${WEBHOST_STATE:-$LOGDIR/webhost-node-open-webhost.state}"
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/sandboxd.env}"
CLOUDFLARE_SYNCLAVE_TOML="${CLOUDFLARE_SYNCLAVE_TOML:-$HOME/.attestmesh/cloudflare-synclave-net.toml}"
APP_DOMAIN="${APP_DOMAIN:-sandbox.synclave.net}"
# A CreateVm replacement has a fresh encrypted data disk. This one-time switch is accepted only
# after the old authenticated API proves it has no live sandboxes; it explicitly authorizes
# discarding pre-production tombstones/test history. Future stateful replacements need migration.
ALLOW_EMPTY_STATE_RESET="${ALLOW_EMPTY_STATE_RESET:-0}"

export BOX_VCPU="${BOX_VCPU:-8}" BOX_MEM="${BOX_MEM:-16384}" BOX_DISK="${BOX_DISK:-300}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

STATE="$LOGDIR/sandboxd-node-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000

_save() {
  local tmp="${STATE}.tmp.$$"
  umask 077
  if ! cat > "$tmp" <<EOF
X=${X:-}
H=${H:-}
VM_ID=${VM_ID:-}
DEPLOY_PHASE=${DEPLOY_PHASE:-}
UPDATE_PHASE=${UPDATE_PHASE:-}
UPDATE_VM_ID=${UPDATE_VM_ID:-}
UPDATE_H=${UPDATE_H:-}
UPDATE_PREVIOUS_H=${UPDATE_PREVIOUS_H:-}
PREVIOUS_VM_ID=${PREVIOUS_VM_ID:-}
PREVIOUS_RETIRE_PHASE=${PREVIOUS_RETIRE_PHASE:-}
PREVIOUS_RETIRE_APP_ID=${PREVIOUS_RETIRE_APP_ID:-}
PREVIOUS_RETIRE_CURRENT_VM_ID=${PREVIOUS_RETIRE_CURRENT_VM_ID:-}
PREVIOUS_RETIRE_VM_ID=${PREVIOUS_RETIRE_VM_ID:-}
PREVIOUS_RETIRE_CURRENT_H=${PREVIOUS_RETIRE_CURRENT_H:-}
PREVIOUS_RETIRE_CURRENT_VCPU=${PREVIOUS_RETIRE_CURRENT_VCPU:-}
PREVIOUS_RETIRE_CURRENT_MEM=${PREVIOUS_RETIRE_CURRENT_MEM:-}
PREVIOUS_RETIRE_CURRENT_DISK=${PREVIOUS_RETIRE_CURRENT_DISK:-}
PREVIOUS_RETIRE_H=${PREVIOUS_RETIRE_H:-}
PREVIOUS_RETIRE_VCPU=${PREVIOUS_RETIRE_VCPU:-}
PREVIOUS_RETIRE_MEM=${PREVIOUS_RETIRE_MEM:-}
PREVIOUS_RETIRE_DISK=${PREVIOUS_RETIRE_DISK:-}
REPLACEMENT_VM_ID=${REPLACEMENT_VM_ID:-}
REPLACEMENT_OLD_VM_ID=${REPLACEMENT_OLD_VM_ID:-}
REPLACEMENT_H=${REPLACEMENT_H:-}
REPLACEMENT_PREVIOUS_H=${REPLACEMENT_PREVIOUS_H:-}
REPLACEMENT_PHASE=${REPLACEMENT_PHASE:-}
REPLACEMENT_VCPU=${REPLACEMENT_VCPU:-}
REPLACEMENT_MEM=${REPLACEMENT_MEM:-}
REPLACEMENT_DISK=${REPLACEMENT_DISK:-}
REPLACEMENT_STATE_RESET_APPROVED=${REPLACEMENT_STATE_RESET_APPROVED:-}
CLUSTER=${CLUSTER:-}
MEMBER_IMPL=${MEMBER_IMPL:-}
GATEWAY_URL=${GATEWAY_URL:-}
GATEWAY_DOMAIN=${GATEWAY_DOMAIN:-}
EOF
  then
    rm -f "$tmp"
    die "could not write deployment state journal $tmp"
  fi
  chmod 600 "$tmp" || { rm -f "$tmp"; die "could not protect deployment state journal $tmp"; }
  # Rename gives atomic readers; syncfs before and after it makes the phase marker a real
  # persistence barrier before any non-idempotent VMM operation. Without this, a host power loss
  # after CreateVm could roll the journal back and permit a duplicate allocation on retry.
  sync -f "$tmp" || { rm -f "$tmp"; die "could not persist deployment state journal $tmp"; }
  mv -f "$tmp" "$STATE" || { rm -f "$tmp"; die "could not install deployment state journal $STATE"; }
  sync -f "$(dirname "$STATE")" || die "could not persist deployment state directory"
}

_load() { [ -f "$STATE" ] && source "$STATE" || true; }
ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }

_ensure_secrets() {
  local zone_token=""
  mkdir -p "$(dirname "$SECRETS_FILE")"
  if [ ! -f "$SECRETS_FILE" ]; then
    umask 077
    cat > "$SECRETS_FILE" <<EOF
SANDBOX_DAEMON_TOKEN=$(openssl rand -hex 32)
EOF
    log "generated sandboxd secrets at $SECRETS_FILE"
  fi
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  [ -n "${SANDBOX_DAEMON_TOKEN:-}" ] || die "SANDBOX_DAEMON_TOKEN missing in $SECRETS_FILE"
  # The pinned Caddy build uses CLOUDFLARE_API_TOKEN for DNS-01. Prefer the explicitly Synclave-
  # scoped value, then the protected operator token file. Feed the same zone-scoped credential to
  # both measured env names so this node never falls back to an unrelated Cloudflare zone token.
  zone_token="${CLOUDFLARE_SYNCLAVE_API_TOKEN:-${CLOUDFLARE_API_TOKEN:-}}"
  if [ -z "$zone_token" ] && [ -f "$CLOUDFLARE_SYNCLAVE_TOML" ]; then
    zone_token=$(sed -nE 's/^api_token *= *"?([^" ]+)"?.*/\1/p' "$CLOUDFLARE_SYNCLAVE_TOML" | head -1)
  fi
  [ -n "$zone_token" ] || die "Cloudflare synclave.net DNS token missing (expected $CLOUDFLARE_SYNCLAVE_TOML)"
  CLOUDFLARE_API_TOKEN="$zone_token"
  CLOUDFLARE_SYNCLAVE_API_TOKEN="$zone_token"
}

_require_env() {
  local wh_cluster wh_member_impl indexer
  wh_cluster=$(sed -nE 's/^CLUSTER=(.*)$/\1/p' "$WEBHOST_STATE" 2>/dev/null | tail -1)
  wh_member_impl=$(sed -nE 's/^MEMBER_IMPL=(.*)$/\1/p' "$WEBHOST_STATE" 2>/dev/null | tail -1)
  CLUSTER="${CLUSTER:-$wh_cluster}"
  MEMBER_IMPL="${MEMBER_IMPL:-$wh_member_impl}"
  CLUSTER="${CLUSTER:?missing CLUSTER (expected in $WEBHOST_STATE)}"
  MEMBER_IMPL="${MEMBER_IMPL:?missing MEMBER_IMPL (expected in $WEBHOST_STATE)}"
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  [ -s "$COMPOSE" ] || die "missing compose file: $COMPOSE"
  [ "$APP_DOMAIN" = "sandbox.synclave.net" ] || die "APP_DOMAIN must be the dedicated sandbox.synclave.net zone"
  _ensure_secrets
}

send_seq() {
  local label="$1"; shift
  send_with_nonce_retry "$label" "$@"
}

_allowlist_compose_hash() {
  local hash="${1#0x}" label="${2:-sandboxd-addHash-${NODE}}" allowed
  [[ "$hash" =~ ^[0-9a-fA-F]{64}$ ]] || die "invalid compose hash: $1"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$hash" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" = true ]; then
    log "compose hash already allowlisted: 0x$hash"
    return 0
  fi
  send_seq "$label" "$CLUSTER" "addComposeHash(bytes32)" "0x$hash" \
    || die "addComposeHash failed — refusing to mutate a VM onto an unallowlisted compose"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$hash" --rpc-url "$RPC_URL" 2>/dev/null)
  [ "$allowed" = true ] || die "compose hash is still not allowlisted after transaction: 0x$hash"
}

_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok public_base
  local remote_dir remote_compose remote_helper lock_key lock_file helper_sha remote_helper_sha
  local helper_command cleanup_command remote_script remote_command rc
  local -a helper_args extra_args
  extra_args=("${@:4}")
  case "$mode" in
    deploy|hash)
      helper_args=("$mode")
      ;;
    create-replacement|inventory-app)
      helper_args=("$mode" "$app_id")
      ;;
    start|stop|describe)
      helper_args=("$mode" "$vm_id")
      ;;
    update|upgrade-stopped|checked-start|retire-previous)
      helper_args=("$mode" "$app_id" "$vm_id" "${extra_args[@]}")
      ;;
    *)
      die "unsupported sandboxd box helper mode: $mode"
      ;;
  esac
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  public_base="${GATEWAY_URL:-}"

  # Never reuse a remote compose/helper pathname: a concurrent invocation must not be able to
  # replace an input between the hash gate and VMM mutation. mktemp plus umask creates an
  # root-owned 0700 directory; both the remote EXIT trap and this caller attempt cleanup.
  remote_dir=$(ssh_box "sudo bash -c 'umask 077; dir=\$(mktemp -d /tmp/sandboxd-node.XXXXXXXX) && chmod 0700 \"\$dir\" && printf \"%s\\\\n\" \"\$dir\"'") \
    || die "could not allocate unique remote sandboxd helper directory"
  [[ "$remote_dir" =~ ^/tmp/sandboxd-node\.[A-Za-z0-9]+$ ]] \
    || die "remote sandboxd helper returned an unsafe temporary path"
  remote_compose="$remote_dir/compose.yaml"
  remote_helper="$remote_dir/sandboxd-node-box.py"
  helper_sha=$(sha256sum "$HERE/sandboxd-node-box.py" | awk '{print $1}') \
    || { ssh_box "sudo rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true; die "could not hash local sandboxd helper"; }
  # Stream directly through sudo into the root-owned directory. There is no user-writable remote
  # staging window in which another BOX_HOST login could rewrite the compose or executable helper.
  if ! ssh_box "sudo install -o root -g root -m 0400 /dev/stdin '$remote_compose'" < "$COMPOSE" \
    || ! ssh_box "sudo install -o root -g root -m 0400 /dev/stdin '$remote_helper'" < "$HERE/sandboxd-node-box.py"; then
    ssh_box "sudo rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true
    die "could not stage unique remote sandboxd helper inputs"
  fi
  remote_helper_sha=$(ssh_box "sudo sha256sum '$remote_helper'" | awk '{print $1}') \
    || { ssh_box "sudo rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true; die "could not verify remote sandboxd helper"; }
  [ "$remote_helper_sha" = "$helper_sha" ] \
    || { ssh_box "sudo rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true; die "remote sandboxd helper integrity mismatch"; }

  # Every mode for this one sandboxd node uses the same remote lock. Mixing a node lock for generic
  # StopVm/StartVm with an app lock for update would allow the two mutation paths to race.
  lock_key="${NODE//[^a-zA-Z0-9_.-]/_}"
  lock_key="${lock_key,,}"
  [ -n "$lock_key" ] || lock_key=sandboxd
  lock_file="/run/lock/sandboxd-$lock_key.lock"
  printf -v helper_command '%q ' "$BOX_PY" "$remote_helper" "${helper_args[@]}"
  printf -v cleanup_command 'rm -rf -- %q' "$remote_dir"
  printf -v remote_script \
    'set -euo pipefail; set -a; . /dev/stdin; set +a; trap %q EXIT; command -v flock >/dev/null; flock -w 60 %q %s' \
    "$cleanup_command" "$lock_file" "$helper_command"
  printf -v remote_command \
    'sudo BOX_NAME=%q BOX_COMPOSE=%q BOX_VCPU=%q BOX_MEM=%q BOX_DISK=%q BOX_PORTS=%q BOX_GATEWAY_ENABLED=%q BOX_NET_MODE=%q bash -c %q' \
    "$NODE" "$remote_compose" "$BOX_VCPU" "$BOX_MEM" "$BOX_DISK" "$BOX_PORTS" \
    "$BOX_GATEWAY_ENABLED" "$BOX_NET_MODE" "$remote_script"
  {
    printf 'E_CHAIN_ID=%q\n' "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n' "${CVM_RPC_URL:-$RPC_URL}"
    printf 'E_BUNDLER_URL=%q\n' "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
    printf 'E_GAS_POLICY_ID=%q\n' "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "$INDEXER_REGISTRY_ADDR"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_CLUSTER=%q\n' "$CLUSTER"
    printf 'E_PEER_ENVELOPE_FALLBACK=%q\n' "true"
    printf 'E_SANDBOX_DAEMON_TOKEN=%q\n' "$SANDBOX_DAEMON_TOKEN"
    printf 'E_PUBLIC_BASE_URL=%q\n' "$public_base"
    printf 'E_ONCHAIN_CLUSTER_DIAMOND=%q\n' "$CLUSTER"
    printf 'E_APP_DOMAIN=%q\n' "$APP_DOMAIN"
    printf 'E_CLOUDFLARE_API_TOKEN=%q\n' "$CLOUDFLARE_API_TOKEN"
    printf 'E_CLOUDFLARE_SYNCLAVE_API_TOKEN=%q\n' "$CLOUDFLARE_SYNCLAVE_API_TOKEN"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "$remote_command"
  rc=$?
  # The remote trap is authoritative; this idempotent cleanup also covers an SSH disconnect before
  # bash installed that trap.
  ssh_box "sudo rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true
  return "$rc"
}

deploy_cvm() {
  _load; _require_env
  [ -z "${DEPLOY_PHASE:-}" ] \
    || die "initial deploy outcome is ambiguous ($DEPLOY_PHASE); reconcile contract/VMM state manually"
  [ -z "${X:-}" ] && [ -z "${VM_ID:-}" ] \
    || die "state already contains app/VM identity; use replace instead of allocating a duplicate"
  [ -z "${REPLACEMENT_PHASE:-}" ] \
    || die "an unfinished replacement exists in $STATE"
  DEPLOY_PHASE=creating
  _save
  log "▶ box deploy_app sandboxd node=$NODE compose=$COMPOSE (stopped)"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  GATEWAY_URL=$(echo "$j" | jq -r .gateway_url)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  [ -n "$VM_ID" ] && [ "$VM_ID" != null ] || die "could not parse VM id; initial deploy remains ambiguous"
  DEPLOY_PHASE=
  _save
  log "✔ deployed sandboxd app_id=$X compose_hash=$H vm=$VM_ID gateway=$GATEWAY_URL"
}

prime_gate() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  _allowlist_compose_hash "$H" "sandboxd-addHash-${NODE}"
  if [ "$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)" != true ]; then
    send_seq "sandboxd-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  else
    log "app id already allowlisted"
  fi
  log "✔ cluster gate primed for sandboxd app_id=$X"
}

bind_member() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind sandboxd X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/sandboxd-bind-${NODE}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --async --rpc-url $BOX_RPC --private-key "\$KEY"
SCRIPT
  confirm_latest_transaction "sandboxd-bind-${NODE}" "$RPC_URL" "$LOGDIR/sandboxd-bind-${NODE}.*.log" || die "bind transaction not confirmed"
  local c=""
  for _ in 1 2 3 4 5 6 7 8; do
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    [ "${c,,}" = "${CLUSTER,,}" ] && break
    sleep 2
  done
  log "X.cluster()=$c (expect $CLUSTER)"
  [ "${c,,}" = "${CLUSTER,,}" ] || die "bind did not stick (X.cluster()=$c)"
  log "✔ bound sandboxd app X -> $CLUSTER"
}

start_cvm() {
  _load; _require_env
  [ -n "${VM_ID:-}" ] || die "need VM_ID (run deploy first)"
  log "▶ start sandboxd VM vm=$VM_ID"
  _box_run start "" "$VM_ID" || die "start failed"
  log "✔ start requested for sandboxd vm=$VM_ID"
}

_wait_health() {
  local attempts="${1:-45}" delay="${2:-10}" expected_hash="${3:-${H:-}}" \
    allow_legacy="${4:-0}" i code body
  [ -n "${GATEWAY_URL:-}" ] || return 1
  [ -n "${X:-}" ] && [ -n "$expected_hash" ] || return 1
  body=$(mktemp)
  for i in $(seq 1 "$attempts"); do
    code=$(curl -sS -o "$body" -w '%{http_code}' --max-time 10 "$GATEWAY_URL/healthz" 2>/dev/null || true)
    if [ "$code" = 200 ] && jq -e \
      --arg app "$X" --arg hash "$expected_hash" --arg allow_legacy "$allow_legacy" '
      .ok == true and
      (
        (
          (((.app_id // "") | ascii_downcase | ltrimstr("0x")) ==
           ($app | ascii_downcase | ltrimstr("0x"))) and
          (((.compose_hash // "") | ascii_downcase | ltrimstr("0x")) ==
           ($hash | ascii_downcase | ltrimstr("0x")))
        ) or
        ($allow_legacy == "1" and (.app_id == null) and (.compose_hash == null))
      )
    ' "$body" >/dev/null 2>&1; then
      log "✔ sandboxd health proves expected app and compose: $GATEWAY_URL/healthz"
      cat "$body"
      rm -f "$body"
      return 0
    fi
    log "… sandboxd health not ready ($i/$attempts, code=${code:-000})"
    sleep "$delay"
  done
  [ ! -s "$body" ] || { log "last sandboxd health response:"; cat "$body"; }
  rm -f "$body"
  return 1
}

_approve_empty_state_reset() {
  local body count
  [ "$ALLOW_EMPTY_STATE_RESET" = "1" ] \
    || die "replacement uses a fresh encrypted disk; set ALLOW_EMPTY_STATE_RESET=1 only for an empty pre-production node"
  body=$(curl -fsS --max-time 15 \
    -H "Authorization: Bearer $SANDBOX_DAEMON_TOKEN" \
    "$GATEWAY_URL/_api/sandboxes") \
    || die "could not prove old sandboxd state is empty"
  count=$(echo "$body" | jq -er '.sandboxes | length') \
    || die "old sandbox list response is invalid"
  [ "$count" -eq 0 ] \
    || die "old sandboxd has $count live sandbox(es); refusing fresh-disk replacement without migration"
  REPLACEMENT_STATE_RESET_APPROVED=1
  log "✔ old API has zero live sandboxes; explicit pre-production state reset recorded"
}

verify_health() {
  _load; _require_env
  [ -n "${GATEWAY_URL:-}" ] || die "need GATEWAY_URL (run deploy first)"
  _wait_health 45 10 || die "sandboxd health did not become ready at $GATEWAY_URL/healthz"
}

retire_previous_cvm() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${PREVIOUS_VM_ID:-}" ] \
    || die "retire-previous requires exact current and previous VM ids in $STATE"
  [ "$VM_ID" != "$PREVIOUS_VM_ID" ] \
    || die "refusing to retire the recorded current VM"
  [ "${SANDBOXD_RETIRE_PREVIOUS_VM_ID:-}" = "$PREVIOUS_VM_ID" ] \
    || die "set SANDBOXD_RETIRE_PREVIOUS_VM_ID to exact recorded predecessor $PREVIOUS_VM_ID after reviewing it"
  [ -z "${REPLACEMENT_PHASE:-}" ] \
    || die "cannot retire a predecessor during replacement phase $REPLACEMENT_PHASE"

  local current_expected current j previous previous_j out result
  case "${UPDATE_PHASE:-}" in
    "")
      current_expected="${H:-}"
      ;;
    upgraded)
      [ "${UPDATE_VM_ID:-}" = "$VM_ID" ] && [ -n "${UPDATE_H:-}" ] \
        || die "upgraded update journal does not identify the current VM/hash"
      current_expected="$UPDATE_H"
      ;;
    *)
      die "cannot retire a predecessor during ambiguous update phase ${UPDATE_PHASE:-<empty>}"
      ;;
  esac
  current_expected="${current_expected#0x}"
  current_expected="${current_expected#0X}"
  [[ "$current_expected" =~ ^[0-9a-fA-F]{64}$ ]] \
    || die "current compose hash is invalid during predecessor retirement"

  case "${PREVIOUS_RETIRE_PHASE:-}" in
    "")
      current=$(_box_run describe "" "$VM_ID") \
        || die "could not inspect current VM before predecessor retirement"
      j=$(echo "$current" | grep '"vm_id"' | tail -1)
      echo "$j" | jq -e \
        --arg vm "$VM_ID" --arg app "$X" --arg hash "$current_expected" \
        --arg vcpu "$BOX_VCPU" --arg memory "$BOX_MEM" --arg disk "$BOX_DISK" '
        .found == true and .vm_id == $vm and
        (((.app_id // "") | ascii_downcase | ltrimstr("0x")) ==
         ($app | ascii_downcase | ltrimstr("0x"))) and
        (((.compose_hash // "") | ascii_downcase | ltrimstr("0x")) ==
         ($hash | ascii_downcase | ltrimstr("0x"))) and
        (.vcpu | type == "number" and floor == . and . == ($vcpu | tonumber)) and
        (.memory | type == "number" and floor == . and . == ($memory | tonumber)) and
        (.disk_size | type == "number" and floor == . and . == ($disk | tonumber)) and
        (((.status // "") | ascii_downcase) as $status |
          ($status == "running" or $status == "started"))' >/dev/null \
        || die "current VM identity/hash/profile/status is not exact before predecessor retirement"

      previous=$(_box_run describe "" "$PREVIOUS_VM_ID") \
        || die "could not inspect recorded predecessor VM"
      previous_j=$(echo "$previous" | grep '"vm_id"' | tail -1)
      echo "$previous_j" | jq -e \
        --arg vm "$PREVIOUS_VM_ID" --arg app "$X" '
        .found == true and .vm_id == $vm and
        (((.app_id // "") | ascii_downcase | ltrimstr("0x")) ==
         ($app | ascii_downcase | ltrimstr("0x"))) and
        (((.status // "") | ascii_downcase) as $status |
          ($status == "stopped" or $status == "exited")) and
        ((.compose_hash // "") | type == "string" and test("^[0-9a-fA-F]{64}$")) and
        (.vcpu | type == "number" and floor == . and . > 0) and
        (.memory | type == "number" and floor == . and . > 0) and
        (.disk_size | type == "number" and floor == . and . > 0)' >/dev/null \
        || die "recorded predecessor is not an exact stopped same-app VM"

      PREVIOUS_RETIRE_VM_ID="$PREVIOUS_VM_ID"
      PREVIOUS_RETIRE_APP_ID="$X"
      PREVIOUS_RETIRE_CURRENT_VM_ID="$VM_ID"
      PREVIOUS_RETIRE_CURRENT_H="${current_expected,,}"
      PREVIOUS_RETIRE_CURRENT_VCPU="$BOX_VCPU"
      PREVIOUS_RETIRE_CURRENT_MEM="$BOX_MEM"
      PREVIOUS_RETIRE_CURRENT_DISK="$BOX_DISK"
      PREVIOUS_RETIRE_H=$(echo "$previous_j" | jq -er '.compose_hash | ascii_downcase')
      PREVIOUS_RETIRE_VCPU=$(echo "$previous_j" | jq -er '.vcpu | tostring')
      PREVIOUS_RETIRE_MEM=$(echo "$previous_j" | jq -er '.memory | tostring')
      PREVIOUS_RETIRE_DISK=$(echo "$previous_j" | jq -er '.disk_size | tostring')
      PREVIOUS_RETIRE_PHASE=prepared
      # Persist every exact deletion precondition before the irreversible RemoveVm. A retry can
      # safely distinguish a not-yet-applied removal from an applied removal with a stale journal.
      _save
      ;;
    prepared|removing)
      ;;
    *)
      die "unknown predecessor retirement phase $PREVIOUS_RETIRE_PHASE"
      ;;
  esac

  [ "${PREVIOUS_RETIRE_APP_ID,,}" = "${X,,}" ] \
    && [ "$PREVIOUS_RETIRE_CURRENT_VM_ID" = "$VM_ID" ] \
    && [ "$PREVIOUS_RETIRE_VM_ID" = "$PREVIOUS_VM_ID" ] \
    && [ "${PREVIOUS_RETIRE_CURRENT_H,,}" = "${current_expected,,}" ] \
    || die "predecessor retirement journal no longer matches current deployment state"
  [[ "${PREVIOUS_RETIRE_H:-}" =~ ^[0-9a-fA-F]{64}$ ]] \
    && [[ "${PREVIOUS_RETIRE_CURRENT_VCPU:-}" =~ ^[1-9][0-9]*$ ]] \
    && [[ "${PREVIOUS_RETIRE_CURRENT_MEM:-}" =~ ^[1-9][0-9]*$ ]] \
    && [[ "${PREVIOUS_RETIRE_CURRENT_DISK:-}" =~ ^[1-9][0-9]*$ ]] \
    && [[ "${PREVIOUS_RETIRE_VCPU:-}" =~ ^[1-9][0-9]*$ ]] \
    && [[ "${PREVIOUS_RETIRE_MEM:-}" =~ ^[1-9][0-9]*$ ]] \
    && [[ "${PREVIOUS_RETIRE_DISK:-}" =~ ^[1-9][0-9]*$ ]] \
    || die "predecessor retirement journal is incomplete"

  if [ "$PREVIOUS_RETIRE_PHASE" = prepared ]; then
    PREVIOUS_RETIRE_PHASE=removing
    _save
  fi
  out=$(_box_run retire-previous "$X" "$VM_ID" "$PREVIOUS_RETIRE_CURRENT_H" \
    "$PREVIOUS_RETIRE_VM_ID" "$PREVIOUS_RETIRE_H" \
    "$PREVIOUS_RETIRE_CURRENT_VCPU" "$PREVIOUS_RETIRE_CURRENT_MEM" \
    "$PREVIOUS_RETIRE_CURRENT_DISK" \
    "$PREVIOUS_RETIRE_VCPU" "$PREVIOUS_RETIRE_MEM" "$PREVIOUS_RETIRE_DISK") \
    || die "checked predecessor removal failed; durable retirement journal retained"
  echo "$out"
  result=$(echo "$out" | grep '"current_vm_id"' | tail -1)
  echo "$result" | jq -e \
    --arg app "$X" --arg current "$VM_ID" --arg previous "$PREVIOUS_RETIRE_VM_ID" '
    (((.app_id // "") | ascii_downcase | ltrimstr("0x")) ==
     ($app | ascii_downcase | ltrimstr("0x"))) and
    .current_vm_id == $current and .previous_vm_id == $previous and
    (.already_removed | type == "boolean")' >/dev/null \
    || die "checked predecessor removal returned an unexpected identity; journal retained"

  PREVIOUS_VM_ID=
  PREVIOUS_RETIRE_PHASE=
  PREVIOUS_RETIRE_APP_ID=
  PREVIOUS_RETIRE_CURRENT_VM_ID=
  PREVIOUS_RETIRE_VM_ID=
  PREVIOUS_RETIRE_CURRENT_H=
  PREVIOUS_RETIRE_CURRENT_VCPU=
  PREVIOUS_RETIRE_CURRENT_MEM=
  PREVIOUS_RETIRE_CURRENT_DISK=
  PREVIOUS_RETIRE_H=
  PREVIOUS_RETIRE_VCPU=
  PREVIOUS_RETIRE_MEM=
  PREVIOUS_RETIRE_DISK=
  _save
  log "✔ retired obsolete predecessor; current same-app VM is unique: $VM_ID"
}

update_member() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  [ -z "${REPLACEMENT_PHASE:-}" ] || die "cannot update during replacement phase $REPLACEMENT_PHASE"
  local nh out j current current_h current_status target_h recovery_h recorded_h
  local failed_target_rebase_h
  recorded_h="${H:-}"
  recorded_h="${recorded_h#0x}"
  recorded_h="${recorded_h#0X}"
  [[ "$recorded_h" =~ ^[0-9a-fA-F]{64}$ ]] \
    || die "recorded compose hash is invalid: ${H:-<missing>}"
  current=$(_box_run describe "" "$VM_ID") || die "could not read current VM resources"
  j=$(echo "$current" | grep '"vm_id"' | tail -1)
  echo "$j" | jq -e \
    --arg vm "$VM_ID" --arg vcpu "$BOX_VCPU" --arg memory "$BOX_MEM" \
    --arg disk "$BOX_DISK" --arg app "$X" \
    '.found == true and
     .vm_id == $vm and
     (.vcpu | tonumber) == ($vcpu | tonumber) and
     (.memory | tonumber) == ($memory | tonumber) and
     (.disk_size | tonumber) == ($disk | tonumber) and
     (((.app_id // "") | ascii_downcase | ltrimstr("0x")) ==
      ($app | ascii_downcase | ltrimstr("0x")))' >/dev/null \
    || die "current VM differs from the exact measured profile; use a reviewed replacement (never in-place autoscale)"
  current_h=$(echo "$j" | jq -er '.compose_hash | ascii_downcase | ltrimstr("0x")') \
    || die "current VM has no measured compose hash"
  current_status=$(echo "$j" | jq -er '(.status // "") | ascii_downcase') \
    || die "current VM has no status"
  if [ -z "${UPDATE_PHASE:-}" ]; then
    _inventory_exact_single "$VM_ID" steady \
      || die "recorded VM is not the sole steady same-app VM before update"
  else
    # A crash after StopVm may leave the exact journaled VM terminal. Require that same VM to remain
    # the sole full inventory entry in either state; dormant duplicates are still ambiguity.
    case "$current_status" in
      running|started)
        _inventory_exact_single "$VM_ID" steady \
          || die "same-app inventory is ambiguous during running update recovery"
        ;;
      stopped|exited)
        _inventory_exact_single "$VM_ID" terminal \
          || die "same-app inventory is ambiguous during stopped update recovery"
        ;;
      *)
        die "current VM is in a transitional/unknown state during update recovery: $current_status"
        ;;
    esac
  fi
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"

  if [ -z "${UPDATE_PHASE:-}" ]; then
    if [ "${current_h,,}" != "${recorded_h,,}" ]; then
      recovery_h="${SANDBOXD_UPDATE_RECOVERY_FROM_HASH:-}"
      recovery_h="${recovery_h#0x}"
      [[ "$recovery_h" =~ ^[0-9a-fA-F]{64}$ ]] \
        && [ "${recovery_h,,}" = "${current_h,,}" ] \
        || die "VM compose 0x$current_h differs from recorded 0x${H#0x}; set SANDBOXD_UPDATE_RECOVERY_FROM_HASH to that exact inspected hash only after reviewing the interrupted update"
      log "⚠ explicitly recovering the inspected unjournaled compose 0x$current_h"
    fi
    UPDATE_VM_ID="$VM_ID"
    UPDATE_PREVIOUS_H="$current_h"
    UPDATE_H="$nh"
    UPDATE_PHASE=prepared
    # Persist both hashes before StopVm/UpgradeApp. A retry can now distinguish not-started,
    # applied, and ambiguous outcomes without blindly mutating the VM a second time.
    _save
  else
    [ "$UPDATE_PHASE" = prepared ] || [ "$UPDATE_PHASE" = mutating ] \
      || [ "$UPDATE_PHASE" = upgraded ] \
      || die "unknown in-place update phase $UPDATE_PHASE"
    [ -n "${UPDATE_VM_ID:-}" ] && [ -n "${UPDATE_H:-}" ] \
      && [ -n "${UPDATE_PREVIOUS_H:-}" ] \
      || die "in-place update journal is incomplete"
    [[ "$UPDATE_H" =~ ^[0-9a-fA-F]{64}$ ]] \
      && [[ "$UPDATE_PREVIOUS_H" =~ ^[0-9a-fA-F]{64}$ ]] \
      || die "in-place update journal contains an invalid compose hash"
    [ "$UPDATE_VM_ID" = "$VM_ID" ] \
      || die "in-place update journal names VM $UPDATE_VM_ID, not recorded VM $VM_ID"
    if [ "$UPDATE_PHASE" = upgraded ] && [ "${UPDATE_H,,}" != "${nh,,}" ]; then
      # A target that reached UpgradeApp but failed its health gate may need a second measured
      # release. Rebase only from the exact journaled/read-back failed hash, and only after proving
      # that hash is not currently healthy. H remains the last-known-good hash throughout; the
      # failed target becomes UPDATE_PREVIOUS_H so normal update recovery can still distinguish
      # not-started, applied, and ambiguous outcomes after the rebase is durably recorded.
      failed_target_rebase_h="${SANDBOXD_UPDATE_FAILED_TARGET_REBASE_FROM_HASH:-}"
      failed_target_rebase_h="${failed_target_rebase_h#0x}"
      failed_target_rebase_h="${failed_target_rebase_h#0X}"
      [[ "$failed_target_rebase_h" =~ ^[0-9a-fA-F]{64}$ ]] \
        && [ "${failed_target_rebase_h,,}" = "${UPDATE_H,,}" ] \
        && [ "${failed_target_rebase_h,,}" = "${current_h,,}" ] \
        || die "unfinished update target changed; to rebase an unhealthy upgraded target, set SANDBOXD_UPDATE_FAILED_TARGET_REBASE_FROM_HASH to exact journaled/read-back hash 0x$UPDATE_H"
      if _wait_health 3 2 "$UPDATE_H"; then
        die "journaled update target 0x$UPDATE_H currently proves healthy; refusing failed-target rebase"
      fi

      # Health probing can take ten seconds. Re-read every VMM invariant and the same-app inventory
      # immediately before the atomic journal transition so a concurrent VM/inventory change
      # cannot be authorized by stale evidence.
      current=$(_box_run describe "" "$VM_ID") \
        || die "could not re-inspect failed update target before rebase"
      j=$(echo "$current" | grep '"vm_id"' | tail -1)
      echo "$j" | jq -e \
        --arg vm "$VM_ID" --arg target "$UPDATE_H" --arg vcpu "$BOX_VCPU" \
        --arg memory "$BOX_MEM" --arg disk "$BOX_DISK" --arg app "$X" \
        '.found == true and
         .vm_id == $vm and
         (((.compose_hash // "") | ascii_downcase | ltrimstr("0x")) ==
          ($target | ascii_downcase | ltrimstr("0x"))) and
         (.vcpu | tonumber) == ($vcpu | tonumber) and
         (.memory | tonumber) == ($memory | tonumber) and
         (.disk_size | tonumber) == ($disk | tonumber) and
         (((.app_id // "") | ascii_downcase | ltrimstr("0x")) ==
          ($app | ascii_downcase | ltrimstr("0x")))' >/dev/null \
        || die "failed update target identity/resource/hash changed before rebase"
      current_status=$(echo "$j" | jq -er '(.status // "") | ascii_downcase') \
        || die "failed update target has no status before rebase"
      if [ "$current_status" = stopped ] || [ "$current_status" = exited ]; then
        _inventory_exact_single "$VM_ID" terminal \
          || die "failed update target is not the sole stopped same-app VM; refusing rebase"
      else
        [ "$current_status" = running ] || [ "$current_status" = started ] \
          || die "failed update target has a transitional/unknown status; refusing rebase"
        _inventory_exact_single "$VM_ID" steady \
          || die "failed update target is not the sole steady same-app VM; refusing rebase"
      fi

      UPDATE_PREVIOUS_H="$UPDATE_H"
      UPDATE_H="$nh"
      UPDATE_PHASE=prepared
      # This is the authorization boundary: persist the exact failed and hotfix hashes before
      # allowlisting or performing any VMM mutation. H deliberately remains last-known-good.
      _save
      current_h="$UPDATE_PREVIOUS_H"
      log "⚠ durably rebased unhealthy update target 0x$UPDATE_PREVIOUS_H onto hotfix 0x$UPDATE_H"
    else
      [ "${UPDATE_H,,}" = "${nh,,}" ] \
        || die "measured compose changed during unfinished update (journal=0x$UPDATE_H current=0x$nh)"
    fi
  fi

  # Verify the KMS gate before any StopVm/UpgradeApp or recovery start. An unallowlisted hash
  # cannot unseal at boot.
  _allowlist_compose_hash "$UPDATE_H" "sandboxd-update-addHash-${NODE}"

  if [ "$UPDATE_PHASE" = prepared ]; then
    UPDATE_PHASE=mutating
    _save
  fi

  if [ "$UPDATE_PHASE" = mutating ]; then
    if [ "${current_h,,}" = "${UPDATE_PREVIOUS_H,,}" ]; then
      out=$(_box_run update "$X" "$VM_ID" "$UPDATE_PREVIOUS_H" "$UPDATE_H" \
        "$BOX_VCPU" "$BOX_MEM" "$BOX_DISK") \
        || die "in-place update failed; durable update journal retained for exact readback recovery"
      echo "$out"
      j=$(echo "$out" | grep '"app_id"' | tail -1)
      target_h=$(echo "$j" | jq -er '.compose_hash | ascii_downcase | ltrimstr("0x")') \
        || die "in-place update returned no compose hash; durable update journal retained"
      [ "${target_h,,}" = "${UPDATE_H,,}" ] \
        || die "in-place update returned unexpected compose hash $target_h (expected $UPDATE_H); durable update journal retained"
    elif [ "${current_h,,}" = "${UPDATE_H,,}" ]; then
      log "in-place update already reached its target before journal completion"
      if [ "$current_status" = stopped ] || [ "$current_status" = exited ]; then
        _inventory_matches none \
          || die "target VM is stopped but another same-app VM is active; refusing recovery start"
        _box_run checked-start "$X" "$VM_ID" "$UPDATE_H" \
          "$BOX_VCPU" "$BOX_MEM" "$BOX_DISK" >/dev/null \
          || die "target VM is stopped and recovery start failed"
      fi
    else
      die "in-place update outcome is ambiguous: VM hash 0x$current_h is neither previous 0x$UPDATE_PREVIOUS_H nor target 0x$UPDATE_H"
    fi

    current=$(_box_run describe "" "$VM_ID") \
      || die "could not read back updated VM; update journal retained"
    j=$(echo "$current" | grep '"vm_id"' | tail -1)
    echo "$j" | jq -e --arg target "$UPDATE_H" \
      '.found == true and
       (((.compose_hash // "") | ascii_downcase | ltrimstr("0x")) ==
        ($target | ascii_downcase | ltrimstr("0x")))' >/dev/null \
      || die "VMM did not read back target compose; update journal retained"
    UPDATE_PHASE=upgraded
    _save
  fi

  [ "$UPDATE_PHASE" = upgraded ] || die "in-place update did not reach upgraded phase"
  current=$(_box_run describe "" "$VM_ID") \
    || die "could not inspect upgraded VM; update journal retained"
  j=$(echo "$current" | grep '"vm_id"' | tail -1)
  echo "$j" | jq -e \
    --arg vm "$VM_ID" --arg target "$UPDATE_H" --arg vcpu "$BOX_VCPU" \
    --arg memory "$BOX_MEM" --arg disk "$BOX_DISK" --arg app "$X" \
    '.found == true and
     .vm_id == $vm and
     (((.compose_hash // "") | ascii_downcase | ltrimstr("0x")) ==
      ($target | ascii_downcase | ltrimstr("0x"))) and
     (.vcpu | tonumber) == ($vcpu | tonumber) and
     (.memory | tonumber) == ($memory | tonumber) and
     (.disk_size | tonumber) == ($disk | tonumber) and
     (((.app_id // "") | ascii_downcase | ltrimstr("0x")) ==
      ($app | ascii_downcase | ltrimstr("0x")))' >/dev/null \
    || die "upgraded VM identity/resource/hash readback failed; update journal retained"
  current_status=$(echo "$j" | jq -er '(.status // "") | ascii_downcase')
  if [ "$current_status" = stopped ] || [ "$current_status" = exited ]; then
    _inventory_matches none \
      || die "upgraded VM is stopped but another same-app VM is active; refusing recovery start"
    _box_run checked-start "$X" "$VM_ID" "$UPDATE_H" \
      "$BOX_VCPU" "$BOX_MEM" "$BOX_DISK" >/dev/null \
      || die "upgraded VM recovery start failed; update journal retained"
  fi
  _wait_inventory "$VM_ID" 30 2 \
    || die "updated VM is not the only active same-app VM; update journal retained"
  # Do not commit the target hash until the exact measured app is healthy and unique.
  _wait_health 45 10 "$UPDATE_H" \
    || die "updated VM did not prove target health; update journal retained"
  _inventory_exact_single "$VM_ID" steady \
    || die "same-app inventory changed or gained a dormant duplicate during update health gate; update journal retained"
  H="$UPDATE_H"
  UPDATE_PHASE=
  UPDATE_VM_ID=
  UPDATE_H=
  UPDATE_PREVIOUS_H=
  _save
  log "✔ sandboxd update complete and healthy vm=$VM_ID compose_hash=$H"
}

_validate_replacement_vm() {
  local vm_id="${1:?replacement VM id required}" expected_hash="${2:-}" out j
  out=$(_box_run describe "" "$vm_id") || return 1
  j=$(echo "$out" | grep '"vm_id"' | tail -1)
  [ -n "$j" ] || { log "replacement describe returned no JSON: $out"; return 1; }
  if ! echo "$j" | jq -e \
    --arg vcpu "$REPLACEMENT_VCPU" --arg memory "$REPLACEMENT_MEM" --arg disk "$REPLACEMENT_DISK" \
    --arg app "$X" --arg expected_hash "$expected_hash" \
    '.found == true and
     (.vcpu | tonumber) == ($vcpu | tonumber) and
     (.memory | tonumber) == ($memory | tonumber) and
     (.disk_size | tonumber) == ($disk | tonumber) and
     (((.app_id // "") | ascii_downcase | ltrimstr("0x")) ==
       ($app | ascii_downcase | ltrimstr("0x"))) and
     (((.status // "") | ascii_downcase) as $status |
       ($status == "stopped" or $status == "exited")) and
     ($expected_hash == "" or
       (((.compose_hash // "") | ascii_downcase) == ($expected_hash | ascii_downcase)))' >/dev/null; then
    log "replacement VM identity/resource/stopped readback mismatch: $j"
    return 1
  fi
  log "✔ replacement VM is stopped with the expected app id and ${REPLACEMENT_VCPU} vCPU / ${REPLACEMENT_MEM} MiB / ${REPLACEMENT_DISK} GiB: $vm_id"
}

_resume_replacement_rebase() {
  local out
  [ "$REPLACEMENT_PHASE" = rebasing ] || die "replacement rebase called from $REPLACEMENT_PHASE"
  [ -n "${REPLACEMENT_PREVIOUS_H:-}" ] || die "replacement rebase lacks its previous compose hash"
  _inventory_matches "$REPLACEMENT_OLD_VM_ID" \
    || die "old VM is not the only active same-app VM during stopped replacement rebase"

  if _validate_replacement_vm "$REPLACEMENT_VM_ID" "$REPLACEMENT_H"; then
    log "replacement rebase was already applied before journal completion"
  else
    # A readback of the exact previous hash proves UpgradeApp did not take effect, so retrying the
    # same stopped-only mutation is safe. Any third hash is ambiguous and fails closed.
    _validate_replacement_vm "$REPLACEMENT_VM_ID" "$REPLACEMENT_PREVIOUS_H" \
      || die "replacement rebase outcome is ambiguous; VM hash is neither previous nor target"
    out=$(_box_run upgrade-stopped "$X" "$REPLACEMENT_VM_ID") \
      || die "stopped replacement UpgradeApp failed or became ambiguous"
    echo "$out"
    _validate_replacement_vm "$REPLACEMENT_VM_ID" "$REPLACEMENT_H" \
      || die "stopped replacement did not read back the target compose hash"
  fi
  REPLACEMENT_PREVIOUS_H=
  REPLACEMENT_PHASE=created
  _save
}

_inventory_matches() {
  # Treat every same-app state except exact stopped/exited as active. This deliberately counts
  # creating/starting/stopping/unknown as active so an intermediate or novel VMM state can never be
  # mistaken for proof that it is safe to start the other copy of the app identity.
  local expected="${1:?expected active VM id or none}" out j
  out=$(_box_run inventory-app "$X") || return 1
  j=$(echo "$out" | grep '"vms"' | tail -1)
  [ -n "$j" ] || { log "VMM app inventory returned no JSON: $out"; return 1; }
  echo "$j" | jq -e --arg expected "$expected" '
    def terminal:
      ((. // "") | ascii_downcase) as $status |
      ($status == "stopped" or $status == "exited");
    [.vms[] | select((.status | terminal) | not)] as $active |
    if $expected == "none" then
      ($active | length) == 0
    else
      (($active | length) == 1 and $active[0].vm_id == $expected)
    end
  ' >/dev/null
}

_inventory_exact_single() {
  local expected="${1:?expected VM id}" expected_state="${2:?expected steady or terminal}" out j
  [ "$expected_state" = steady ] || [ "$expected_state" = terminal ] || return 1
  out=$(_box_run inventory-app "$X") || return 1
  j=$(echo "$out" | grep '"vms"' | tail -1)
  [ -n "$j" ] || { log "VMM app inventory returned no JSON: $out"; return 1; }
  echo "$j" | jq -e --arg expected "$expected" --arg expected_state "$expected_state" '
    def terminal:
      ((. // "") | ascii_downcase) as $status |
      ($status == "stopped" or $status == "exited");
    def steady:
      ((. // "") | ascii_downcase) as $status |
      ($status == "running" or $status == "started");
    (.vms | type) == "array" and
    (.vms | length) == 1 and
    .vms[0].vm_id == $expected and
    (if $expected_state == "terminal" then
       (.vms[0].status | terminal)
     else
       (.vms[0].status | steady)
     end)
  ' >/dev/null
}

_wait_inventory() {
  local expected="${1:?expected active VM id or none}" attempts="${2:-20}" delay="${3:-2}"
  local i
  for i in $(seq 1 "$attempts"); do
    _inventory_matches "$expected" && return 0
    sleep "$delay"
  done
  return 1
}

_rollback_replacement() {
  local reason="${1:-replacement failed}"
  local return_after_rollback="${2:-0}" health_recovered=1
  log "⚠ replacement cutover failed; rolling back without starting two copies: $reason"
  REPLACEMENT_PHASE=rolling-back
  _save

  # Prove the replacement is stopped before starting the old VM. If this cannot be proved, fail
  # closed: availability is preferable to two CVMs concurrently presenting the same app identity.
  if ! _box_run stop "" "$REPLACEMENT_VM_ID" >/dev/null; then
    REPLACEMENT_PHASE=rollback-failed
    _save
    die "rollback could not prove replacement VM $REPLACEMENT_VM_ID stopped; old VM remains stopped/unknown"
  fi
  if _inventory_matches "$REPLACEMENT_OLD_VM_ID"; then
    log "old VM is already the only active copy; no rollback start is needed"
  elif _inventory_matches none; then
    _box_run start "" "$REPLACEMENT_OLD_VM_ID" >/dev/null || {
      REPLACEMENT_PHASE=rollback-failed
      _save
      die "replacement is stopped, but rollback could not restart old VM $REPLACEMENT_OLD_VM_ID"
    }
  else
    REPLACEMENT_PHASE=rollback-failed
    _save
    die "rollback inventory found another active same-app VM; refusing to start the old VM"
  fi
  _wait_inventory "$REPLACEMENT_OLD_VM_ID" 30 2 || {
    REPLACEMENT_PHASE=rollback-failed
    _save
    die "rollback could not prove the old VM is the only active same-app VM"
  }
  REPLACEMENT_PHASE=rolled-back
  _save
  if ! _wait_health 18 10 "$H" 1; then
    health_recovered=0
    log "⚠ old VM restart was requested but gateway health has not recovered; replacement state is retained"
  fi
  if [ "$return_after_rollback" = 1 ]; then
    [ "$health_recovered" = 1 ] \
      || die "replacement is stopped and old-only inventory is restored, but old gateway health did not recover"
    log "✔ operator rollback restored old VM $REPLACEMENT_OLD_VM_ID; replacement remains stopped for retry"
    return 0
  fi
  die "replacement rolled back to old VM $REPLACEMENT_OLD_VM_ID: $reason"
}

rollback_cvm() {
  _load; _require_env
  [ -n "${REPLACEMENT_PHASE:-}" ] || die "no replacement journal exists to roll back"
  [ -n "${REPLACEMENT_VM_ID:-}" ] && [ -n "${REPLACEMENT_OLD_VM_ID:-}" ] \
    || die "replacement journal lacks the exact old/new VM identities"
  _rollback_replacement "operator-requested rollback from phase $REPLACEMENT_PHASE" 1
}

_resume_replacement() {
  [ -n "${REPLACEMENT_VM_ID:-}" ] || die "replacement phase $REPLACEMENT_PHASE has no VM id; refusing a duplicate CreateVm"
  [ -n "${REPLACEMENT_OLD_VM_ID:-}" ] || die "replacement state has no old VM id"
  [ -n "${REPLACEMENT_H:-}" ] || die "replacement state has no compose hash"
  [ -n "${REPLACEMENT_VCPU:-}" ] && [ -n "${REPLACEMENT_MEM:-}" ] && [ -n "${REPLACEMENT_DISK:-}" ] \
    || die "replacement state has no immutable resource target"

  while :; do
    case "$REPLACEMENT_PHASE" in
      rolled-back)
        # Retry the already-created, stopped replacement; never allocate another VM implicitly.
        log "retrying retained replacement VM $REPLACEMENT_VM_ID"
        REPLACEMENT_PHASE=created
        _save
        ;;
      rebasing)
        _resume_replacement_rebase
        ;;
      creating)
        die "replacement CreateVm outcome is ambiguous and has no recorded VM id; inspect the VMM before retrying"
        ;;
      created)
        _validate_replacement_vm "$REPLACEMENT_VM_ID" "$REPLACEMENT_H" \
          || _rollback_replacement "replacement VM resource validation failed"
        _inventory_matches "$REPLACEMENT_OLD_VM_ID" \
          || _rollback_replacement "old VM is not the only active same-app VM before cutover"
        log "▶ stopping old sandboxd VM $REPLACEMENT_OLD_VM_ID"
        _box_run stop "" "$REPLACEMENT_OLD_VM_ID" >/dev/null \
          || _rollback_replacement "old VM could not be stopped"
        _inventory_matches none \
          || _rollback_replacement "another same-app VM remained active after stopping old"
        REPLACEMENT_PHASE=old-stopped
        _save
        ;;
      old-stopped)
        _inventory_matches none \
          || _rollback_replacement "same-app inventory is not quiescent before replacement start"
        _validate_replacement_vm "$REPLACEMENT_VM_ID" "$REPLACEMENT_H" \
          || _rollback_replacement "replacement stopped-state/identity changed before start"
        log "▶ starting replacement sandboxd VM $REPLACEMENT_VM_ID"
        _box_run start "" "$REPLACEMENT_VM_ID" >/dev/null \
          || _rollback_replacement "replacement VM start request failed"
        REPLACEMENT_PHASE=replacement-started
        _save
        ;;
      replacement-started)
        _wait_inventory "$REPLACEMENT_VM_ID" 30 2 \
          || _rollback_replacement "replacement is not the only active same-app VM"
        _wait_health 45 10 "$REPLACEMENT_H" \
          || _rollback_replacement "replacement gateway identity/health failed"
        _inventory_matches "$REPLACEMENT_VM_ID" \
          || _rollback_replacement "same-app inventory changed during replacement health gate"
        REPLACEMENT_PHASE=verified
        _save
        ;;
      verified)
        PREVIOUS_VM_ID="$REPLACEMENT_OLD_VM_ID"
        VM_ID="$REPLACEMENT_VM_ID"
        H="$REPLACEMENT_H"
        REPLACEMENT_VM_ID=
        REPLACEMENT_OLD_VM_ID=
        REPLACEMENT_H=
        REPLACEMENT_PREVIOUS_H=
        REPLACEMENT_PHASE=
        REPLACEMENT_VCPU=
        REPLACEMENT_MEM=
        REPLACEMENT_DISK=
        REPLACEMENT_STATE_RESET_APPROVED=
        _save
        log "✔ replacement cutover complete: active=$VM_ID previous-stopped=$PREVIOUS_VM_ID compose_hash=$H"
        return 0
        ;;
      rolling-back|rollback-failed)
        _rollback_replacement "resuming an interrupted rollback"
        ;;
      *)
        die "unknown replacement phase: ${REPLACEMENT_PHASE:-<empty>}"
        ;;
    esac
  done
}

replace_cvm() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] \
    || die "need existing X/VM_ID/CLUSTER in $STATE"
  [ -n "${GATEWAY_URL:-}" ] || die "need the existing gateway URL in $STATE"

  if [ -z "${REPLACEMENT_PHASE:-}" ]; then
    [ -z "${PREVIOUS_VM_ID:-}" ] \
      || die "retire recorded predecessor $PREVIOUS_VM_ID before allocating another replacement"
    _inventory_exact_single "$VM_ID" steady \
      || die "current VM must be the sole steady same-app inventory entry before replacement allocation"
  fi

  local nh out j new_vm returned_x returned_h
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute replacement compose hash"
  _allowlist_compose_hash "$nh" "sandboxd-replace-addHash-${NODE}"
  [ "$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)" = true ] \
    || die "existing app id is not allowlisted; refusing replacement: $X"

  if [ -n "${REPLACEMENT_PHASE:-}" ]; then
    [ "${REPLACEMENT_OLD_VM_ID:-}" = "$VM_ID" ] \
      || die "active VM changed during replacement (recorded=$REPLACEMENT_OLD_VM_ID current=$VM_ID)"
    [ "${REPLACEMENT_VCPU:-}/${REPLACEMENT_MEM:-}/${REPLACEMENT_DISK:-}" = "$BOX_VCPU/$BOX_MEM/$BOX_DISK" ] \
      || die "replacement resource target changed during cutover; refusing to mix provisioning profiles"
    [ "${REPLACEMENT_STATE_RESET_APPROVED:-}" = "1" ] \
      || die "replacement journal lacks the explicit empty-state reset approval"
    if [ "$REPLACEMENT_PHASE" = rolled-back ] && \
       { [ "${REPLACEMENT_H:-}" != "$nh" ] || [ -n "${REPLACEMENT_PREVIOUS_H:-}" ]; }; then
      # The failed replacement is already the explicitly provisioned 8/16/300 VM. Reuse it by
      # installing the corrected compose while it remains stopped; never allocate a duplicate and
      # never start it alongside the healthy old VM. The two hashes make a crash during UpgradeApp
      # reconcilable from VMM readback.
      _inventory_matches "$VM_ID" \
        || die "old VM is not the only active same-app VM before replacement rebase"
      if [ "${REPLACEMENT_H:-}" != "$nh" ]; then
        [ -z "${REPLACEMENT_PREVIOUS_H:-}" ] \
          || die "replacement journal contains conflicting previous and target compose hashes"
        _validate_replacement_vm "$REPLACEMENT_VM_ID" "$REPLACEMENT_H" \
          || die "rolled-back replacement identity or recorded compose hash changed"
        REPLACEMENT_PREVIOUS_H="$REPLACEMENT_H"
        REPLACEMENT_H="$nh"
      fi
      REPLACEMENT_PHASE=rebasing
      _save
    else
      [ "${REPLACEMENT_H:-}" = "$nh" ] \
        || die "measured compose changed during replacement (recorded=$REPLACEMENT_H current=$nh); refusing to mix builds"
    fi
    _resume_replacement
    return
  fi

  # Only a brand-new replacement allocation requires the recorded old VM to be the sole active
  # instance.  Once the durable journal has advanced, each phase has its own exact inventory gate:
  # after old-stopped there must be none, and after replacement-started only the new VM may run.
  # Requiring the old VM here unconditionally would make a crash at either cutover point impossible
  # to resume and could strand the service offline.
  _inventory_matches "$VM_ID" \
    || die "recorded VM is not the only active same-app VM; refusing replacement allocation"

  REPLACEMENT_OLD_VM_ID="$VM_ID"
  REPLACEMENT_H="$nh"
  REPLACEMENT_VCPU="$BOX_VCPU"
  REPLACEMENT_MEM="$BOX_MEM"
  REPLACEMENT_DISK="$BOX_DISK"
  _approve_empty_state_reset
  REPLACEMENT_PHASE=creating
  _save
  log "▶ creating stopped ${BOX_VCPU} vCPU / ${BOX_MEM} MiB / ${BOX_DISK} GiB replacement for app $X"
  out=$(_box_run create-replacement "$X") \
    || die "replacement CreateVm failed or became ambiguous; state prevents an automatic duplicate retry"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  [ -n "$j" ] || die "replacement CreateVm returned no parseable result; state remains ambiguous"
  new_vm=$(echo "$j" | jq -r .vm_id)
  returned_x=$(echo "$j" | jq -r .app_id)
  returned_h=$(echo "$j" | jq -r .compose_hash)
  [ -n "$new_vm" ] && [ "$new_vm" != null ] && [ "$new_vm" != "$VM_ID" ] \
    || die "replacement returned an invalid/reused VM id; state remains ambiguous"
  [ "${returned_x,,}" = "${X,,}" ] || die "replacement app id changed unexpectedly; state remains ambiguous"
  [ "${returned_h,,}" = "${nh,,}" ] || die "replacement compose hash mismatch; state remains ambiguous"
  REPLACEMENT_VM_ID="$new_vm"
  REPLACEMENT_PHASE=created
  _save
  _resume_replacement
}

smoke() {
  _load; _require_env
  [ -z "${REPLACEMENT_PHASE:-}" ] || die "cannot smoke-test during replacement phase $REPLACEMENT_PHASE"
  [ -n "${GATEWAY_URL:-}" ] || die "need GATEWAY_URL"
  _wait_health 3 5 || die "smoke target does not prove the recorded app/compose identity"
  TOK="$SANDBOX_DAEMON_TOKEN" GW="$GATEWAY_URL" python3 - <<'PY'
import hashlib, json, os, subprocess, sys, time

tok = os.environ["TOK"]
gw = os.environ["GW"].rstrip("/")
image = "ghcr.io/attestmesh/synclave-workloads@sha256:eeeab97469edf54f2d5b9582a0a1c6b49866af931573324919a3dcc6b23a0b4e"
request_suffix = f"{int(time.time())}-{os.getpid()}"
idempotency_key = f"release-smoke-{request_suffix}"
payload = {
    "owner": "ops:release-smoke",
    "org_id": "ops",
    "sandbox_handle": f"release-{request_suffix}",
    "runtime": {
        "kind": "runsc",
        "image": image,
        "command": "/bin/sh",
        "args": ["-c", "sleep 3600"],
    },
    "resources": {"cpu_millis": 1000, "memory_mb": 1024, "pids": 256, "disk_mb": 10240},
}

def request(method, url, body=None, *, auth=False, headers=None):
    args = ["curl", "-sS", "--max-time", "45", "-X", method, "-w", "\n%{http_code}"]
    if auth:
        args += ["-H", f"Authorization: Bearer {tok}"]
    for name, value in (headers or {}).items():
        args += ["-H", f"{name}: {value}"]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body)]
    proc = subprocess.run([*args, url], text=True, capture_output=True, timeout=50)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"curl exited {proc.returncode}")
    response, code = proc.stdout.rsplit("\n", 1)
    return int(code), response

sid = None
capacity_before = None
try:
    code, raw = request("GET", f"{gw}/_api/capacity", auth=True)
    if code != 200:
        raise RuntimeError(f"capacity preflight returned HTTP {code}")
    capacity_before = json.loads(raw)

    create_headers = {"Idempotency-Key": idempotency_key}
    code, raw = request(
        "POST", f"{gw}/_api/sandboxes", payload, auth=True, headers=create_headers
    )
    if code != 201:
        raise RuntimeError(f"create returned HTTP {code}: {raw[:500]}")
    created = json.loads(raw)
    sid = created.get("id")
    if not sid or created.get("status") != "running":
        raise RuntimeError("create did not return a running sandbox identity")

    code, raw = request(
        "POST", f"{gw}/_api/sandboxes", payload, auth=True, headers=create_headers
    )
    if code != 201:
        raise RuntimeError(f"idempotent replay returned HTTP {code}: {raw[:500]}")
    replayed = json.loads(raw)
    if replayed.get("id") != sid:
        raise RuntimeError("idempotent replay returned a different sandbox identity")

    code, raw = request("GET", f"{gw}/_api/capacity", auth=True)
    if code != 200:
        raise RuntimeError(f"capacity readback returned HTTP {code}")
    capacity_during = json.loads(raw)
    expected_resources = payload["resources"]
    before_committed = capacity_before["committed"]
    during_committed = capacity_during["committed"]
    single_capacity_commit = (
        capacity_during["sandbox_count"] == capacity_before["sandbox_count"] + 1
        and all(
            during_committed[name] == before_committed[name] + value
            for name, value in expected_resources.items()
        )
    )

    code, raw = request("GET", f"{gw}/s/{sid}/attestation")
    if code != 200:
        raise RuntimeError(f"attestation returned HTTP {code}")
    att = json.loads(raw)
    events = att.get("event_log") or []
    if isinstance(events, str):
        events = json.loads(events)
    report_data = att.get("report_data") or ""
    expected = hashlib.sha256(
        b"cs-attest-v1" + bytes.fromhex(sid) + bytes.fromhex(att["manifest_hash"])
    ).hexdigest()
    checks = {
        "quote_present": bool(att.get("quote")),
        "report_data_bound": report_data.startswith(expected),
        "create_event_present": any(
            isinstance(event, dict) and event.get("event") == "cs.sandbox.create"
            for event in events
        ),
        "image_pinned": (att.get("manifest", {}).get("runtime", {}).get("image") == image),
        "idempotent_replay": replayed.get("id") == sid,
        "single_capacity_commit": single_capacity_commit,
    }
    print("smoke", json.dumps({"sandbox_id": sid[:12] + "…", "checks": checks}, indent=2))
    if not all(checks.values()):
        raise RuntimeError("structural attestation smoke failed")

    # Production policy must reject arbitrary exec even for the authenticated control caller.
    code, _ = request(
        "POST", f"{gw}/_api/sandboxes/{sid}/exec", {"command": "true"}, auth=True
    )
    if code != 400:
        raise RuntimeError(f"disabled exec returned HTTP {code}, expected 400")
except Exception as error:
    print(f"smoke failed: {error}", file=sys.stderr)
    sys.exit_code = 1
else:
    sys.exit_code = 0
finally:
    if sid:
        try:
            code, _ = request("DELETE", f"{gw}/_api/sandboxes/{sid}", auth=True)
            if code != 200:
                raise RuntimeError(f"cleanup returned HTTP {code}")
            code, raw = request("GET", f"{gw}/_api/capacity", auth=True)
            if code != 200:
                raise RuntimeError(f"cleanup capacity readback returned HTTP {code}")
            capacity_after = json.loads(raw)
            if capacity_before is None or (
                capacity_after["sandbox_count"] != capacity_before["sandbox_count"]
                or capacity_after["committed"] != capacity_before["committed"]
            ):
                raise RuntimeError("cleanup did not restore the original capacity commitment")
        except Exception as cleanup_error:
            print(f"smoke cleanup failed: {cleanup_error}", file=sys.stderr)
            sys.exit_code = 1
sys.exit(sys.exit_code)
PY
}

case "$ACTION" in
  deploy|prime|bind|start|verify-health|smoke|update|retire-previous|replace|rollback|setup|all)
    command -v flock >/dev/null 2>&1 || die "flock is required for duplicate-safe deployment"
    exec {DEPLOY_LOCK_FD}>"${STATE}.deployment.lock" \
      || die "could not open deployment lock for $STATE"
    flock -n "$DEPLOY_LOCK_FD" \
      || die "another sandboxd deployment process already holds the state lock"
    ;;
esac

if [ "$ACTION" != retire-previous ]; then
  _load
  [ -z "${PREVIOUS_RETIRE_PHASE:-}" ] \
    || die "predecessor retirement is unfinished ($PREVIOUS_RETIRE_PHASE); resume retire-previous before any other deployment action"
fi

log "=== confidential-sandboxes node: $NODE ==="
case "$ACTION" in
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  start) start_cvm ;;
  verify-health) verify_health ;;
  smoke) smoke ;;
  update) update_member ;;
  retire-previous) retire_previous_cvm ;;
  replace) replace_cvm ;;
  rollback) rollback_cvm ;;
  setup) deploy_cvm; prime_gate; bind_member; start_cvm ;;
  all) deploy_cvm; prime_gate; bind_member; start_cvm; verify_health; smoke ;;
  *) die "usage: sandboxd-node.sh <node-name> [deploy|prime|bind|start|verify-health|smoke|update|retire-previous|replace|rollback|setup|all]" ;;
esac
