#!/usr/bin/env bash
# Stable public HAProxy front door for AttestMesh Indexer blue/green cutovers.
#
# The LB is deployed once as a C3 member. Candidate Indexers are deployed with
# `indexer-member-node.sh <name> candidate`; this driver then performs a two-phase
# switch around the IndexerRegistry signing-key update:
#
#   deploy/indexer-lb-node.sh attestmesh-indexer-lb all attestmesh-indexer-c3-green
#   deploy/indexer-lb-node.sh attestmesh-indexer-lb switch attestmesh-indexer-c3-next
#
# The stable registry endpoint is the LB app gateway on :50052. The control API is
# mesh-only on :50053. Existing gRPC streams continue during prepare/registry update;
# commit swaps the backend and closes them so sidecars re-read the new registry key.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: indexer-lb-node.sh <node-name> [setup|preflight|deploy|prime|bind|start|register-direct|verify|verify-health|verify-lb|active|abort|switch|update|stop|all] [indexer-node-or-ip]}"
ACTION="${2:-all}"
TARGET="${3:-}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/indexer-lb-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GENERIC_STATE="$LOGDIR/generic-node-${NODE}.state"
LB_STATE="$LOGDIR/indexer-lb-node-${NODE}.state"
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/indexer-lb.env}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
REGISTRY="$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json")"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000

export BOX_VCPU="${BOX_VCPU:-1}" BOX_MEM="${BOX_MEM:-1536}" BOX_DISK="${BOX_DISK:-12}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}"
export BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }
ssh_mesh() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$MESH_SSH_HOST" "$@"; }

_state_value() {
  local file="$1" key="$2"
  sed -n "s/^${key}=//p" "$file" 2>/dev/null | tail -1
}

_load_generic() {
  [ -f "$GENERIC_STATE" ] || die "missing LB node state: $GENERIC_STATE"
  # shellcheck disable=SC1090
  source "$GENERIC_STATE"
  [ -n "${X:-}" ] && [ -n "${H:-}" ] && [ -n "${VM_ID:-}" ] \
    || die "LB state is missing X/H/VM_ID: $GENERIC_STATE"
}

_load_lb() { [ -f "$LB_STATE" ] && source "$LB_STATE" || true; }

_save_lb() {
  umask 077
  cat >"$LB_STATE" <<EOF
UPDATED_AT=$(ts)
MESH_IP=${MESH_IP:-}
ACTIVE_BACKEND=${ACTIVE_BACKEND:-}
ACTIVE_BACKEND_NODE=${ACTIVE_BACKEND_NODE:-}
ACTIVE_PUBKEY=${ACTIVE_PUBKEY:-}
ACTIVE_CODE_ID=${ACTIVE_CODE_ID:-}
STABLE_ENDPOINT=${STABLE_ENDPOINT:-}
EOF
}

_default_cluster_env() {
  if [ -z "${CLUSTER:-}" ] || [ -z "${MEMBER_IMPL:-}" ]; then
    [ -f "$MATRIX_STATE" ] || die "missing cluster state: $MATRIX_STATE"
    CLUSTER="${CLUSTER:-$(_state_value "$MATRIX_STATE" CLUSTER)}"
    MEMBER_IMPL="${MEMBER_IMPL:-$(_state_value "$MATRIX_STATE" MEMBER_IMPL)}"
  fi
  [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] \
    || die "could not resolve CLUSTER/MEMBER_IMPL"
  [ -n "${KMS_ROOT:-}" ] || die "missing KMS_ROOT (source deploy/env.sh)"
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$REGISTRY}"
  [ -n "${BUNDLER_URL:-}" ] || die "missing BUNDLER_URL"
  [ -n "${GAS_POLICY_ID:-}" ] || die "missing GAS_POLICY_ID"
  export CLUSTER MEMBER_IMPL KMS_ROOT INDEXER_REGISTRY_ADDR GATEWAY_DOMAIN
}

_ensure_secrets() {
  umask 077
  mkdir -p "$(dirname "$SECRETS_FILE")"
  if [ ! -s "$SECRETS_FILE" ]; then
    cat >"$SECRETS_FILE" <<EOF
# Indexer LB mesh-control secret — generated $(date -u +%FT%TZ).
INDEXER_LB_ADMIN_KEY=ilb_$(openssl rand -hex 32)
EOF
    log "generated Indexer LB control secret -> $SECRETS_FILE"
  fi
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  [ -n "${INDEXER_LB_ADMIN_KEY:-}" ] || die "INDEXER_LB_ADMIN_KEY missing in $SECRETS_FILE"
}

_seal_lb_env() {
  _ensure_secrets
  local initial="${INDEXER_LB_INITIAL_BACKEND:-}"
  if [ -n "$initial" ] && ! [[ "$initial" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    initial="$(_backend_ip "$initial")"
  fi
  APP_ENV_B64="$({
    printf 'INDEXER_LB_ADMIN_KEY=%s\n' "$INDEXER_LB_ADMIN_KEY"
    printf 'INDEXER_LB_INITIAL_BACKEND=%s\n' "$initial"
  } | base64 | tr -d '\n')"
  export APP_ENV_B64
}

generic() {
  local action="$1"
  _default_cluster_env
  _seal_lb_env
  COMPOSE="$COMPOSE" MATRIX_STATE="$MATRIX_STATE" "$HERE/generic-node.sh" "$NODE" "$action"
}

_member_mesh_ip() {
  local app="$1" cluster="$2" member_id raw
  member_id=$(cast call "$cluster" "memberIdOf(address)(bytes32)" "$app" --rpc-url "$RPC_URL" 2>/dev/null) \
    || return 1
  [ -n "$member_id" ] && [ "$member_id" != "$ZERO32" ] || return 1
  raw=$(cast call "$cluster" "meshIpOf(bytes32)(uint32)" "$member_id" --rpc-url "$RPC_URL" 2>/dev/null) \
    || return 1
  python3 - "$raw" <<'PY'
import ipaddress, sys
print(ipaddress.IPv4Address(int(sys.argv[1], 0)))
PY
}

_discover_mesh_ip() {
  _load_lb
  [ -n "${MESH_IP:-}" ] && return 0
  _load_generic
  _default_cluster_env
  MESH_IP="$(_member_mesh_ip "$X" "$CLUSTER")" || return 1
  _save_lb
}

_bridge_ip_for_vm() {
  local vm_id="$1"
  ssh_box "sudo VMID='$vm_id' bash -s" <<'SCRIPT'
set -u
MAC=$(ps axww -o args | grep -F "/srv/data/dstack/vm/$VMID/" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2 || true)
[ -n "$MAC" ] || exit 1
IP=$(ip neigh show dev dstack-br0 | grep -i "$MAC" | grep -oE '^10\.[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
[ -n "$IP" ] || exit 1
echo "$IP"
SCRIPT
}

_backend_state() {
  local target="$1"
  printf '%s\n' "$LOGDIR/generic-node-${target}.state"
}

_backend_ip() {
  local target="$1" state vm app cluster ip
  if [[ "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    printf '%s\n' "$target"
    return 0
  fi
  state="$(_backend_state "$target")"
  [ -f "$state" ] || die "missing Indexer candidate state: $state"
  if [ "${INDEXER_LB_BACKEND_MODE:-bridge}" = bridge ]; then
    vm="$(_state_value "$state" VM_ID)"
    ip="$(_bridge_ip_for_vm "$vm" 2>/dev/null || true)"
    if [ -n "$ip" ]; then
      printf '%s\n' "$ip"
      return 0
    fi
  fi
  app="$(_state_value "$state" X)"
  cluster="$(_state_value "$state" CLUSTER)"
  cluster="${cluster:-${CLUSTER:-}}"
  _member_mesh_ip "$app" "$cluster"
}

_backend_http() {
  local ip="$1" path="$2" out
  out=$(ssh_box "curl -fsS --max-time 12 'http://$ip:9090$path'" 2>/dev/null) && {
    printf '%s\n' "$out"
    return 0
  }
  ssh_mesh "curl -fsS --max-time 12 'http://$ip:9090$path'"
}

_backend_metadata() {
  local target="$1" ip="$2" state status health min_clusters
  min_clusters="${INDEXER_MIN_CLUSTER_COUNT:-1}"
  health="$(_backend_http "$ip" /healthz)" || die "candidate $target is not healthy at $ip:9090"
  echo "$health" | jq -e '.status == "ok"' >/dev/null \
    || die "candidate health is not ok: $health"
  status="$(_backend_http "$ip" /status)" || die "candidate status unavailable at $ip:9090"
  BACKEND_PUBKEY=$(echo "$status" | jq -r '.pubKey // empty' | tr 'A-F' 'a-f')
  echo "$BACKEND_PUBKEY" | grep -Eq '^0x[0-9a-f]{64}$' \
    || die "candidate returned invalid pubKey: ${BACKEND_PUBKEY:-<empty>}"
  echo "$status" | jq -e '.health.ok == true' >/dev/null \
    || die "candidate /status reports unhealthy: $status"
  echo "$status" | jq -e --argjson min "$min_clusters" \
    '.readModel.clusterCount >= $min and (.readModel.atBlock | type == "number")' >/dev/null \
    || die "candidate read model is not caught up or has fewer than $min_clusters cluster(s): $status"
  if [[ "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    BACKEND_CODE_ID="${INDEXER_BACKEND_CODE_ID:-}"
    [ -n "$BACKEND_CODE_ID" ] || die "literal backend IP requires INDEXER_BACKEND_CODE_ID"
    if [ -n "${INDEXER_BACKEND_PUBKEY:-}" ] && [ "${INDEXER_BACKEND_PUBKEY,,}" != "$BACKEND_PUBKEY" ]; then
      die "INDEXER_BACKEND_PUBKEY does not match candidate /status"
    fi
  else
    state="$(_backend_state "$target")"
    BACKEND_CODE_ID="$(_state_value "$state" H)"
  fi
  BACKEND_CODE_ID="0x${BACKEND_CODE_ID#0x}"
  echo "$BACKEND_CODE_ID" | grep -Eq '^0x[0-9a-fA-F]{64}$' \
    || die "invalid candidate compose/code id: $BACKEND_CODE_ID"
  export BACKEND_PUBKEY BACKEND_CODE_ID
}

_control_get() {
  local path="$1"
  _discover_mesh_ip || die "LB is not registered with a mesh IP"
  ssh_mesh "curl -fsS --max-time 12 'http://$MESH_IP:50053$path'"
}

_control_post() {
  local path="$1" payload="$2"
  _ensure_secrets
  _discover_mesh_ip || die "LB is not registered with a mesh IP"
  ssh_mesh "INDEXER_LB_ADMIN_KEY=$(printf '%q' "$INDEXER_LB_ADMIN_KEY") INDEXER_LB_PAYLOAD=$(printf '%q' "$payload") INDEXER_LB_URL=$(printf '%q' "http://$MESH_IP:50053$path") bash -s" <<'SCRIPT'
curl -fsS --max-time 30 \
  -H "Authorization: Bearer $INDEXER_LB_ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary "$INDEXER_LB_PAYLOAD" \
  "$INDEXER_LB_URL"
SCRIPT
}

send_seq() {
  local label="$1"; shift
  local nonce
  nonce=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL")
  run_step "$label" cast send "$@" --nonce "$nonce" --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY" \
    && return 0
  log "↻ $label: refetching nonce + retrying"
  sleep 4
  nonce=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL")
  run_step "${label}-retry" cast send "$@" --nonce "$nonce" --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY"
}

_set_registry() {
  local label="$1" endpoint="$2" code_id="$3" pubkey="$4" updated_at="$5"
  send_seq "$label" "$REGISTRY" \
    "setIndexer((string,bytes32,bytes32,uint64))" \
    "($endpoint,$code_id,$pubkey,$updated_at)"
}

_registry_snapshot() {
  local value
  value=$(cast call "$REGISTRY" 'current()(string,bytes32,bytes32,uint64)' --json --rpc-url "$RPC_URL") \
    || return 1
  echo "$value" | jq -e 'type == "array" and length >= 4' >/dev/null || return 1
  printf '%s\n' "$value"
}

_stable_endpoint() {
  _load_generic
  printf 'https://%s-50052.%s\n' "$(printf '%s' "${X#0x}" | tr 'A-Z' 'a-z')" "$GATEWAY_DOMAIN"
}

switch_backend() {
  local target="${1:-${TARGET:-}}" backend stable before old_endpoint old_code old_pub old_updated
  local prepare_payload prepare_out commit_out rollback_ok=0
  [ -n "$target" ] || die "usage: $0 $NODE switch <indexer-node-or-ip>"
  _load_generic
  _default_cluster_env
  _ensure_secrets
  _discover_mesh_ip || die "LB mesh IP unavailable"
  backend="$(_backend_ip "$target")" || die "could not resolve candidate backend $target"
  _backend_metadata "$target" "$backend"
  stable="$(_stable_endpoint)"
  before="$(_registry_snapshot)" || die "could not snapshot IndexerRegistry.current()"
  old_endpoint=$(echo "$before" | jq -r '.[0]')
  old_code=$(echo "$before" | jq -r '.[1]')
  old_pub=$(echo "$before" | jq -r '.[2]')
  old_updated=$(echo "$before" | jq -r '.[3]')

  prepare_payload=$(jq -nc --arg backend "$backend" --arg pubkey "$BACKEND_PUBKEY" \
    '{backend:$backend, expected_pubkey:$pubkey}')
  log "▶ preparing Indexer LB $NODE candidate=$target backend=$backend pubkey=$BACKEND_PUBKEY"
  if ! prepare_out="$(_control_post /prepare "$prepare_payload" 2>&1)"; then
    _control_post /abort '{}' >/dev/null 2>&1 || true
    die "LB prepare failed: $prepare_out"
  fi
  echo "$prepare_out" | tee "$LOGDIR/indexer-lb-prepare-${NODE}.$(ts).log"

  log "▶ updating IndexerRegistry to stable endpoint=$stable candidate codeId=$BACKEND_CODE_ID"
  if ! _set_registry "indexer-lb-registry-${NODE}" "$stable" "$BACKEND_CODE_ID" "$BACKEND_PUBKEY" "$(date +%s)"; then
    _control_post /abort '{}' >/dev/null 2>&1 || true
    die "registry update failed; LB remains on the previous backend"
  fi

  log "▶ committing Indexer LB data-plane switch and reconnecting subscribers"
  if ! commit_out="$(_control_post /commit '{}' 2>&1)"; then
    log "commit failed; restoring previous registry record before reopening the LB"
    if _set_registry "indexer-lb-registry-rollback-${NODE}" "$old_endpoint" "$old_code" "$old_pub" "$old_updated"; then
      rollback_ok=1
    fi
    _control_post /abort '{}' >/dev/null 2>&1 || true
    [ "$rollback_ok" = 1 ] || die "LB commit failed AND registry rollback failed: $commit_out"
    die "LB commit failed; previous registry record restored: $commit_out"
  fi
  echo "$commit_out" | tee "$LOGDIR/indexer-lb-commit-${NODE}.$(ts).log"

  ACTIVE_BACKEND="$backend"
  ACTIVE_BACKEND_NODE="$target"
  ACTIVE_PUBKEY="$BACKEND_PUBKEY"
  ACTIVE_CODE_ID="$BACKEND_CODE_ID"
  STABLE_ENDPOINT="$stable"
  _save_lb
  verify_lb
  log "✔ Indexer LB active backend=$target ($backend); keep the old candidate running until subscriber diagnostics reconnect"
}

verify_sidecar_health() {
  _load_generic
  local bridge i body
  bridge="$(_bridge_ip_for_vm "$VM_ID")" || die "could not resolve LB bridge IP"
  for i in $(seq 1 40); do
    # A long-lived v1 cluster can contain permanently registered, dead test
    # members. A brand-new member may therefore never latch full convergence even
    # though its mesh route and CSK are ready. The authenticated control request in
    # `switch_backend` is the end-to-end mesh proof; here require its prerequisites
    # without treating an intentional HTTP 503 as an absent response.
    body=$(ssh_box "curl -sS --max-time 8 'http://$bridge:9091/healthz'" 2>/dev/null || true)
    if echo "$body" | jq -e '.csk_acquired == true and (.live_peers // 0) > 0' >/dev/null 2>&1; then
      log "✔ Indexer LB sidecar mesh-ready (health remains convergence+CSK): $body"
      return 0
    fi
    log "… Indexer LB sidecar mesh prerequisites unavailable ($i/40)"
    sleep 10
  done
  die "Indexer LB sidecar never acquired its CSK and a live mesh peer"
}

verify_lb() {
  _load_generic
  _load_lb
  _discover_mesh_ip || die "LB mesh IP unavailable"
  local control stable status endpoint registry_pub active_pub
  control="$(_control_get /active)" || die "LB control API unavailable"
  echo "$control" | jq -e '.haproxy_socket == true' >/dev/null \
    || die "LB control response does not report HAProxy socket: $control"
  active_pub=$(echo "$control" | jq -r '.active_pubkey // empty')
  if [ -n "$active_pub" ]; then
    stable="$(_stable_endpoint)"
    status=$(curl -fsS --max-time 15 "${stable/-50052./-9090.}/status") \
      || die "stable Indexer HTTP endpoint unavailable"
    [ "$(echo "$status" | jq -r '.pubKey // empty' | tr 'A-F' 'a-f')" = "${active_pub,,}" ] \
      || die "stable endpoint pubkey does not match LB active backend"
    endpoint=$(cast call "$REGISTRY" 'current()(string,bytes32,bytes32,uint64)' --json --rpc-url "$RPC_URL" | jq -r '.[0]')
    registry_pub=$(cast call "$REGISTRY" 'current()(string,bytes32,bytes32,uint64)' --json --rpc-url "$RPC_URL" | jq -r '.[2]' | tr 'A-F' 'a-f')
    [ "$endpoint" = "$stable" ] || die "registry endpoint=$endpoint, expected stable LB endpoint=$stable"
    [ "$registry_pub" = "${active_pub,,}" ] || die "registry pubkey does not match LB active backend"
  fi
  echo "$control" | jq '{active_backend,active_pubkey,prepared,haproxy_socket}'
  log "✔ Indexer LB control=$MESH_IP:50053 grpc=$(_stable_endpoint)"
}

active() { _control_get /active | jq; }
abort_prepare() { _control_post /abort '{}' | jq; }

log "=== AttestMesh Indexer LB node: $NODE ==="
case "$ACTION" in
  setup) _ensure_secrets ;;
  preflight|deploy|prime|bind|start|register-direct|verify) generic "$ACTION" ;;
  verify-health) verify_sidecar_health ;;
  verify-lb) verify_lb ;;
  active) active ;;
  abort) abort_prepare ;;
  switch) switch_backend "$TARGET" ;;
  update) generic update; verify_sidecar_health; verify_lb ;;
  stop) generic stop ;;
  all)
    _ensure_secrets
    generic all
    verify_sidecar_health
    _discover_mesh_ip || die "LB failed to acquire a mesh IP"
    switch_backend "${TARGET:-${INDEXER_LB_INITIAL_INDEXER:-attestmesh-indexer-c3-green}}"
    ;;
  *) die "usage: $0 <node-name> [setup|preflight|deploy|prime|bind|start|register-direct|verify|verify-health|verify-lb|active|abort|switch|update|stop|all] [indexer-node-or-ip]" ;;
esac
