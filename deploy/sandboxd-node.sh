#!/usr/bin/env bash
# confidential-sandboxes sandboxd node in the isolated Cluster 2.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: sandboxd-node.sh <node-name> [deploy|prime|bind|start|verify-health|smoke|update|replace|rollback|all]}"
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
PREVIOUS_VM_ID=${PREVIOUS_VM_ID:-}
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
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  public_base="${GATEWAY_URL:-}"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/sandboxd-node-box.py" "$BOX_HOST:/tmp/sandboxd-node-box.py"
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
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/sandboxd-node-box.py $mode $app_id $vm_id'"
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

update_member() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  [ -z "${REPLACEMENT_PHASE:-}" ] || die "cannot update during replacement phase $REPLACEMENT_PHASE"
  local nh out j current
  current=$(_box_run describe "" "$VM_ID") || die "could not read current VM resources"
  j=$(echo "$current" | grep '"vm_id"' | tail -1)
  echo "$j" | jq -e \
    --arg vcpu "$BOX_VCPU" --arg memory "$BOX_MEM" --arg disk "$BOX_DISK" \
    '.found == true and
     (.vcpu | tonumber) >= ($vcpu | tonumber) and
     (.memory | tonumber) >= ($memory | tonumber) and
     (.disk_size | tonumber) >= ($disk | tonumber)' >/dev/null \
    || die "current VM is below the measured capacity profile; use replace (never in-place autoscale)"
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  # Verify the KMS gate before StopVm/UpgradeApp. An unallowlisted hash cannot unseal at boot.
  _allowlist_compose_hash "$nh" "sandboxd-update-addHash-${NODE}"
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  _save
  log "✔ sandboxd update complete vm=$VM_ID"
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
image = "ghcr.io/dmvt/cs-sandbox-base@sha256:8ccfb22336a73e28b7fd8bef024d355ec5673d70d09a6099ad5094836f65e9d3"
payload = {
    "owner": "ops:release-smoke",
    "org_id": "ops",
    "sandbox_handle": f"release-{int(time.time())}-{os.getpid()}",
    "runtime": {"kind": "runsc", "image": image},
    "resources": {"cpu_millis": 100, "memory_mb": 128, "pids": 128, "disk_mb": 16},
}

def request(method, url, body=None, *, auth=False):
    args = ["curl", "-sS", "--max-time", "45", "-X", method, "-w", "\n%{http_code}"]
    if auth:
        args += ["-H", f"Authorization: Bearer {tok}"]
    if body is not None:
        args += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body)]
    proc = subprocess.run([*args, url], text=True, capture_output=True, timeout=50)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"curl exited {proc.returncode}")
    response, code = proc.stdout.rsplit("\n", 1)
    return int(code), response

sid = None
try:
    code, raw = request("POST", f"{gw}/_api/sandboxes", payload, auth=True)
    if code != 201:
        raise RuntimeError(f"create returned HTTP {code}: {raw[:500]}")
    created = json.loads(raw)
    sid = created.get("id")
    if not sid or created.get("status") != "running":
        raise RuntimeError("create did not return a running sandbox identity")

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
        except Exception as cleanup_error:
            print(f"smoke cleanup failed: {cleanup_error}", file=sys.stderr)
            sys.exit_code = 1
sys.exit(sys.exit_code)
PY
}

case "$ACTION" in
  deploy|prime|bind|start|verify-health|smoke|update|replace|rollback|setup|all)
    command -v flock >/dev/null 2>&1 || die "flock is required for duplicate-safe deployment"
    exec {DEPLOY_LOCK_FD}>"${STATE}.deployment.lock" \
      || die "could not open deployment lock for $STATE"
    flock -n "$DEPLOY_LOCK_FD" \
      || die "another sandboxd deployment process already holds the state lock"
    ;;
esac

log "=== confidential-sandboxes node: $NODE ==="
case "$ACTION" in
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  start) start_cvm ;;
  verify-health) verify_health ;;
  smoke) smoke ;;
  update) update_member ;;
  replace) replace_cvm ;;
  rollback) rollback_cvm ;;
  setup) deploy_cvm; prime_gate; bind_member; start_cvm ;;
  all) deploy_cvm; prime_gate; bind_member; start_cvm; verify_health; smoke ;;
  *) die "usage: sandboxd-node.sh <node-name> [deploy|prime|bind|start|verify-health|smoke|update|replace|rollback|setup|all]" ;;
esac
