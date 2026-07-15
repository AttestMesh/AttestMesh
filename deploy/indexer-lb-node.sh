#!/usr/bin/env bash
# Stable public HAProxy front door for AttestMesh Indexer blue/green cutovers and
# Stage A shared-identity worker pools.
#
# The LB is deployed once as a C3 member. Candidate Indexers are deployed with
# `indexer-member-node.sh <name> candidate`; this driver then performs a two-phase
# switch around the IndexerRegistry signing-key update:
#
#   deploy/indexer-lb-node.sh attestmesh-indexer-lb all attestmesh-indexer-c3-green
#   deploy/indexer-lb-node.sh attestmesh-indexer-lb switch attestmesh-indexer-c3-next
#   deploy/indexer-lb-node.sh attestmesh-indexer-lb switch indexer-ha-r1,indexer-ha-r2
#
# The stable registry endpoint is the LB app gateway on :50052. The control API is
# mesh-only on :50053. Existing gRPC streams continue during prepare/registry update;
# commit swaps the backend/pool and closes them so sidecars re-read the registry.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: indexer-lb-node.sh <node-name> [setup|preflight|deploy|prime|bind|start|register-direct|verify|verify-health|verify-lb|active|assert-drained|abort|recover|switch|update|stop|all] [target-or-operation-id]}"
ACTION="${2:-all}"
TARGET="${3:-}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/indexer-lb-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GENERIC_STATE="$LOGDIR/generic-node-${NODE}.state"
LB_STATE="$LOGDIR/indexer-lb-node-${NODE}.state"
TXN_STATE="$LOGDIR/indexer-lb-transaction-${NODE}.json"
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/indexer-lb.env}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
REGISTRY="$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json")"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000
ZERO_ADDRESS=0x0000000000000000000000000000000000000000
MAX_INDEXER_BACKENDS=8

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
  local state_cluster
  [ -f "$GENERIC_STATE" ] || die "missing LB node state: $GENERIC_STATE"
  # State is written from remote MCP responses. Read only fixed keys and treat
  # every value as untrusted data; never execute the file as shell syntax.
  X="$(_validate_address "$(_state_value "$GENERIC_STATE" X)" "LB X")"
  _validate_bytes32 "$(_state_value "$GENERIC_STATE" H)" "LB H" >/dev/null
  VM_ID="$(_validate_vm_id "$(_state_value "$GENERIC_STATE" VM_ID)" "LB VM_ID")"
  state_cluster="$(_state_value "$GENERIC_STATE" CLUSTER)"
  if [ -n "$state_cluster" ]; then
    CLUSTER="$(_validate_address "$state_cluster" "LB CLUSTER")"
  fi
}

_load_lb() {
  [ -f "$LB_STATE" ] || return 0
  MESH_IP="$(_state_value "$LB_STATE" MESH_IP)"
  ACTIVE_BACKEND="$(_state_value "$LB_STATE" ACTIVE_BACKEND)"
  ACTIVE_BACKEND_NODE="$(_state_value "$LB_STATE" ACTIVE_BACKEND_NODE)"
  ACTIVE_BACKENDS="$(_state_value "$LB_STATE" ACTIVE_BACKENDS)"
  ACTIVE_BACKEND_NODES="$(_state_value "$LB_STATE" ACTIVE_BACKEND_NODES)"
  ACTIVE_PUBKEY="$(_state_value "$LB_STATE" ACTIVE_PUBKEY)"
  ACTIVE_CODE_ID="$(_state_value "$LB_STATE" ACTIVE_CODE_ID)"
  ACTIVE_INDEXER_CLUSTER="$(_state_value "$LB_STATE" ACTIVE_INDEXER_CLUSTER)"
  ACTIVE_MEMBER_IDS="$(_state_value "$LB_STATE" ACTIVE_MEMBER_IDS)"
  STABLE_ENDPOINT="$(_state_value "$LB_STATE" STABLE_ENDPOINT)"
}

_save_lb() {
  umask 077
  cat >"$LB_STATE" <<EOF
UPDATED_AT=$(ts)
MESH_IP=${MESH_IP:-}
ACTIVE_BACKEND=${ACTIVE_BACKEND:-}
ACTIVE_BACKEND_NODE=${ACTIVE_BACKEND_NODE:-}
ACTIVE_BACKENDS=${ACTIVE_BACKENDS:-${ACTIVE_BACKEND:-}}
ACTIVE_BACKEND_NODES=${ACTIVE_BACKEND_NODES:-${ACTIVE_BACKEND_NODE:-}}
ACTIVE_PUBKEY=${ACTIVE_PUBKEY:-}
ACTIVE_CODE_ID=${ACTIVE_CODE_ID:-}
ACTIVE_INDEXER_CLUSTER=${ACTIVE_INDEXER_CLUSTER:-}
ACTIVE_MEMBER_IDS=${ACTIVE_MEMBER_IDS:-}
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

_require_protocol_v3_fleet_confirmation() {
  [ "${INDEXER_PROTOCOL_V3_FLEET_CONFIRMED:-}" = 1 ] \
    || die "shared Indexer pools require INDEXER_PROTOCOL_V3_FLEET_CONFIRMED=1 after every production sidecar has durable protocol-v3 exact-cursor support"
}

_seal_lb_env() {
  _ensure_secrets
  local initial="${INDEXER_LB_INITIAL_BACKEND:-}" target ip require_bridge=0 pinned_pubkey
  local -a initial_targets=() initial_ips=()
  local -A seen_initial_ips=()
  if [ -n "$initial" ]; then
    [[ "$initial" != ,* && "$initial" != *, && "$initial" != *,,* ]] \
      || die "INDEXER_LB_INITIAL_BACKEND contains an empty pool member"
    IFS=',' read -r -a initial_targets <<<"$initial"
    [ "${#initial_targets[@]}" -le "$MAX_INDEXER_BACKENDS" ] \
      || die "INDEXER_LB_INITIAL_BACKEND supports at most $MAX_INDEXER_BACKENDS members"
    if [ "${#initial_targets[@]}" -ge 2 ]; then
      _require_protocol_v3_fleet_confirmation
      require_bridge=1
      pinned_pubkey="${INDEXER_BACKEND_PUBKEY:-}"
      pinned_pubkey="${pinned_pubkey,,}"
      echo "$pinned_pubkey" | grep -Eq '^0x[0-9a-f]{64}$' \
        || die "a shared initial pool requires pinned INDEXER_BACKEND_PUBKEY"
      [ "$pinned_pubkey" != "$ZERO32" ] \
        || die "INDEXER_BACKEND_PUBKEY must be nonzero"
    fi
    for target in "${initial_targets[@]}"; do
      target="$(_trim "$target")"
      [ -n "$target" ] || die "INDEXER_LB_INITIAL_BACKEND contains an empty pool member"
      ip="$(_backend_ip "$target" "$require_bridge")" \
        || die "could not resolve initial backend $target"
      [ -z "${seen_initial_ips[$ip]:-}" ] \
        || die "INDEXER_LB_INITIAL_BACKEND resolves duplicate address $ip"
      seen_initial_ips[$ip]=1
      initial_ips+=("$ip")
    done
    initial=$(IFS=,; echo "${initial_ips[*]}")
  fi
  APP_ENV_B64="$({
    printf 'INDEXER_LB_ADMIN_KEY=%s\n' "$INDEXER_LB_ADMIN_KEY"
    printf 'INDEXER_LB_INITIAL_BACKEND=%s\n' "$initial"
    printf 'INDEXER_LB_CLUSTER=%s\n' "$CLUSTER"
    printf 'INDEXER_LB_PINNED_PUBKEY=%s\n' "${INDEXER_BACKEND_PUBKEY:-}"
    printf 'INDEXER_PROTOCOL_V3_FLEET_CONFIRMED=%s\n' "${INDEXER_PROTOCOL_V3_FLEET_CONFIRMED:-}"
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
  raw=$(cast call "$cluster" "meshIpOf(bytes32)(uint32)" "$member_id" \
    --json --rpc-url "$RPC_URL" 2>/dev/null | jq -er '.[0]') || return 1
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
  [[ "$target" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] \
    || die "backend node name must be a safe token of at most 128 characters"
  printf '%s\n' "$LOGDIR/generic-node-${target}.state"
}

_validate_vm_id() {
  local value="$1" field="${2:-VM_ID}"
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] \
    || die "$field must be a safe token of at most 128 characters"
  printf '%s\n' "$value"
}

_validate_address() {
  local value="$1" field="$2"
  [[ "$value" =~ ^0x[0-9a-fA-F]{40}$ ]] \
    || die "$field must be a 20-byte 0x address"
  [ "${value,,}" != "$ZERO_ADDRESS" ] || die "$field must be nonzero"
  printf '%s\n' "${value,,}"
}

_validate_bytes32() {
  local value="$1" field="$2"
  value="0x${value#0x}"
  [[ "$value" =~ ^0x[0-9a-fA-F]{64}$ ]] \
    || die "$field must be a 32-byte 0x hex value"
  [ "${value,,}" != "$ZERO32" ] || die "$field must be nonzero"
  printf '%s\n' "${value,,}"
}

_validate_private_ipv4() {
  local value="$1"
  python3 - "$value" <<'PY'
import ipaddress, sys
try:
    ip = ipaddress.IPv4Address(sys.argv[1])
except ipaddress.AddressValueError as exc:
    raise SystemExit(f"invalid backend IPv4 address: {exc}")
if not ip.is_private:
    raise SystemExit("backend must be a private bridge or mesh IPv4 address")
print(ip)
PY
}

_trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s\n' "$value"
}

_backend_ip() {
  local target="$1" require_bridge="${2:-0}" state vm app cluster ip
  if [[ "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    _validate_private_ipv4 "$target"
    return 0
  fi
  state="$(_backend_state "$target")"
  [ -f "$state" ] || die "missing Indexer candidate state: $state"
  if [ "$require_bridge" = 1 ]; then
    [ "${INDEXER_LB_BACKEND_MODE:-bridge}" = bridge ] \
      || die "shared named workers require same-host bridge routing; mesh mode is only valid for legacy single-C3 candidates"
    vm="$(_validate_vm_id "$(_state_value "$state" VM_ID)" "backend VM_ID")"
    ip="$(_bridge_ip_for_vm "$vm" 2>/dev/null || true)"
    [ -n "$ip" ] \
      || die "shared worker $target has no same-host bridge address; pass an explicitly private routable IP instead"
    ip="$(_validate_private_ipv4 "$ip")"
    printf '%s\n' "$ip"
    return 0
  fi
  if [ "${INDEXER_LB_BACKEND_MODE:-bridge}" = bridge ]; then
    vm="$(_validate_vm_id "$(_state_value "$state" VM_ID)" "backend VM_ID")"
    ip="$(_bridge_ip_for_vm "$vm" 2>/dev/null || true)"
    if [ -n "$ip" ]; then
      ip="$(_validate_private_ipv4 "$ip")"
      printf '%s\n' "$ip"
      return 0
    fi
  fi
  app="$(_validate_address "$(_state_value "$state" X)" "backend X")"
  cluster="$(_state_value "$state" CLUSTER)"
  cluster="${cluster:-${CLUSTER:-}}"
  cluster="$(_validate_address "$cluster" "backend CLUSTER")"
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

_current_registry_cluster_count() {
  local value endpoint status
  value=$(cast call "$REGISTRY" 'current()(string,bytes32,bytes32,uint64)' \
    --json --rpc-url "$RPC_URL" 2>/dev/null) || return 1
  endpoint=$(echo "$value" | jq -er '.[0] | select(length > 0)') || return 1
  status=$(curl -fsS --max-time 12 "${endpoint/-50052./-9090.}/status" 2>/dev/null) \
    || return 1
  echo "$status" | jq -er '.readModel.clusterCount | select(type == "number" and . > 0)'
}

_backend_metadata() {
  local target="$1" ip="$2" mode="${3:-single}" state status health min_clusters
  local expected_code expected_cluster
  min_clusters="${INDEXER_MIN_CLUSTER_COUNT:-}"
  if [ -z "$min_clusters" ]; then
    min_clusters="$(_current_registry_cluster_count || true)"
    min_clusters="${min_clusters:-1}"
  fi
  status="$(_backend_http "$ip" /status)" || die "candidate status unavailable at $ip:9090"
  BACKEND_PUBKEY=$(echo "$status" | jq -r '.pubKey // empty' | tr 'A-F' 'a-f')
  echo "$BACKEND_PUBKEY" | grep -Eq '^0x[0-9a-f]{64}$' \
    || die "candidate returned invalid pubKey: ${BACKEND_PUBKEY:-<empty>}"
  echo "$status" | jq -e --argjson min "$min_clusters" \
    '.readModel.clusterCount >= $min and (.readModel.atBlock | type == "number")' >/dev/null \
    || die "candidate read model is not caught up or has fewer than $min_clusters cluster(s): $status"

  if [ "$mode" = shared ]; then
    echo "$status" | jq -e \
      '.identityMode == "cluster-shared"
       and .health.rpcReachable == true
       and (.health.chainHeadLagBlocks | type == "number")
       and .health.chainHeadLagBlocks >= 0
       and .health.chainHeadLagBlocks < 10' >/dev/null \
      || die "shared candidate must be cluster-shared, RPC-reachable, and fewer than 10 blocks behind: $status"

    BACKEND_CODE_ID=$(echo "$status" | jq -r '.codeId // empty' | tr 'A-F' 'a-f')
    BACKEND_INDEXER_CLUSTER=$(echo "$status" | jq -r '.indexerCluster // empty' | tr 'A-F' 'a-f')
    BACKEND_MEMBER_ID=$(echo "$status" | jq -r '.servingMemberId // empty' | tr 'A-F' 'a-f')
    echo "$BACKEND_CODE_ID" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "shared candidate returned invalid codeId: ${BACKEND_CODE_ID:-<empty>}"
    [ "$BACKEND_CODE_ID" != "$ZERO32" ] \
      || die "shared candidate returned a zero codeId"
    echo "$BACKEND_INDEXER_CLUSTER" | grep -Eq '^0x[0-9a-f]{40}$' \
      || die "shared candidate returned invalid indexerCluster: ${BACKEND_INDEXER_CLUSTER:-<empty>}"
    [ "$BACKEND_INDEXER_CLUSTER" != "$ZERO_ADDRESS" ] \
      || die "shared candidate returned a zero indexerCluster"
    echo "$BACKEND_MEMBER_ID" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "shared candidate returned invalid servingMemberId: ${BACKEND_MEMBER_ID:-<empty>}"
    [ "$BACKEND_MEMBER_ID" != "$ZERO32" ] \
      || die "shared candidate returned an empty servingMemberId"

    if [[ "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
      expected_code="${INDEXER_BACKEND_CODE_ID:-}"
      expected_cluster="${INDEXER_HA_CLUSTER:-${INDEXER_CLUSTER_ADDR:-}}"
      [ -n "$expected_code" ] \
        || die "literal shared backend IP requires INDEXER_BACKEND_CODE_ID"
      [ -n "$expected_cluster" ] \
        || die "literal shared backend IP requires INDEXER_HA_CLUSTER"
    else
      state="$(_backend_state "$target")"
      expected_code="$(_validate_bytes32 "$(_state_value "$state" H)" "backend H")"
      expected_cluster="$(_validate_address "$(_state_value "$state" CLUSTER)" "backend CLUSTER")"
    fi
    expected_code="0x${expected_code#0x}"
    expected_code="${expected_code,,}"
    expected_cluster="0x${expected_cluster#0x}"
    expected_cluster="${expected_cluster,,}"
    echo "$expected_code" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "invalid expected shared candidate compose/code id: $expected_code"
    [ "$expected_code" != "$ZERO32" ] \
      || die "shared candidate compose/code id must be nonzero"
    echo "$expected_cluster" | grep -Eq '^0x[0-9a-f]{40}$' \
      || die "invalid expected dedicated Indexer cluster: $expected_cluster"
    [ "$expected_cluster" != "$ZERO_ADDRESS" ] \
      || die "dedicated Indexer cluster must be nonzero"
    [ "$BACKEND_CODE_ID" = "$expected_code" ] \
      || die "candidate /status codeId=$BACKEND_CODE_ID, expected $expected_code"
    [ "$BACKEND_INDEXER_CLUSTER" = "$expected_cluster" ] \
      || die "candidate /status indexerCluster=$BACKEND_INDEXER_CLUSTER, expected $expected_cluster"
    [ "$POOL_PINNED_PUBKEY" = "$BACKEND_PUBKEY" ] \
      || die "pinned INDEXER_BACKEND_PUBKEY does not match shared candidate /status"
    export BACKEND_PUBKEY BACKEND_CODE_ID BACKEND_INDEXER_CLUSTER BACKEND_MEMBER_ID
    return 0
  fi

  health="$(_backend_http "$ip" /healthz)" || die "candidate $target is not healthy at $ip:9090"
  echo "$health" | jq -e '.status == "ok"' >/dev/null \
    || die "candidate health is not ok: $health"
  echo "$status" | jq -e '.health.ok == true' >/dev/null \
    || die "candidate /status reports unhealthy: $status"
  if [[ "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    BACKEND_CODE_ID="${INDEXER_BACKEND_CODE_ID:-}"
    [ -n "$BACKEND_CODE_ID" ] || die "literal backend IP requires INDEXER_BACKEND_CODE_ID"
    if [ -n "${INDEXER_BACKEND_PUBKEY:-}" ] && [ "${INDEXER_BACKEND_PUBKEY,,}" != "$BACKEND_PUBKEY" ]; then
      die "INDEXER_BACKEND_PUBKEY does not match candidate /status"
    fi
  else
    state="$(_backend_state "$target")"
    BACKEND_CODE_ID="$(_validate_bytes32 "$(_state_value "$state" H)" "backend H")"
  fi
  BACKEND_CODE_ID="0x${BACKEND_CODE_ID#0x}"
  BACKEND_CODE_ID="${BACKEND_CODE_ID,,}"
  echo "$BACKEND_CODE_ID" | grep -Eq '^0x[0-9a-fA-F]{64}$' \
    || die "invalid candidate compose/code id: $BACKEND_CODE_ID"
  BACKEND_INDEXER_CLUSTER=""
  BACKEND_MEMBER_ID=""
  export BACKEND_PUBKEY BACKEND_CODE_ID BACKEND_INDEXER_CLUSTER BACKEND_MEMBER_ID
}

_resolve_switch_pool() {
  local requested="$1" target backend mode=single item
  local -a raw_targets=()
  local -A seen_targets=() seen_backends=() seen_members=()
  POOL_TARGETS=()
  POOL_BACKENDS=()
  POOL_MEMBER_IDS=()
  POOL_PUBKEY=""
  POOL_CODE_ID=""
  POOL_CLUSTER=""
  POOL_PINNED_PUBKEY=""
  POOL_SHARED_HA=0

  [[ "$requested" != ,* && "$requested" != *, && "$requested" != *,,* ]] \
    || die "backend pool contains an empty member"
  IFS=',' read -r -a raw_targets <<<"$requested"
  [ "${#raw_targets[@]}" -le "$MAX_INDEXER_BACKENDS" ] \
    || die "backend pool supports at most $MAX_INDEXER_BACKENDS members"
  if [ "${#raw_targets[@]}" -gt 1 ]; then
    mode=shared
    POOL_SHARED_HA=1
    _require_protocol_v3_fleet_confirmation
    POOL_PINNED_PUBKEY="${INDEXER_BACKEND_PUBKEY:-}"
    POOL_PINNED_PUBKEY="${POOL_PINNED_PUBKEY,,}"
    echo "$POOL_PINNED_PUBKEY" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "a shared HA pool requires pinned INDEXER_BACKEND_PUBKEY"
    [ "$POOL_PINNED_PUBKEY" != "$ZERO32" ] \
      || die "INDEXER_BACKEND_PUBKEY must be nonzero"
  fi
  [ "$mode" = single ] || [ "${#raw_targets[@]}" -ge 2 ] \
    || die "a shared HA pool requires at least two backends"

  for item in "${raw_targets[@]}"; do
    target="$(_trim "$item")"
    [ -n "$target" ] || die "backend pool contains an empty member"
    [ -z "${seen_targets[$target]:-}" ] || die "backend pool repeats target $target"
    seen_targets[$target]=1
    if [ "$mode" = shared ]; then
      backend="$(_backend_ip "$target" 1)" \
        || die "could not resolve shared candidate backend $target"
    else
      backend="$(_backend_ip "$target")" \
        || die "could not resolve candidate backend $target"
    fi
    [ -z "${seen_backends[$backend]:-}" ] \
      || die "backend pool resolves more than one target to $backend"
    seen_backends[$backend]=1
    _backend_metadata "$target" "$backend" "$mode"

    if [ -z "$POOL_PUBKEY" ]; then
      POOL_PUBKEY="$BACKEND_PUBKEY"
      POOL_CODE_ID="$BACKEND_CODE_ID"
      POOL_CLUSTER="$BACKEND_INDEXER_CLUSTER"
    else
      [ "$BACKEND_PUBKEY" = "$POOL_PUBKEY" ] \
        || die "shared HA backends do not expose the same pubKey"
      [ "$BACKEND_CODE_ID" = "$POOL_CODE_ID" ] \
        || die "shared HA backends do not expose the same nonzero codeId"
      [ "$BACKEND_INDEXER_CLUSTER" = "$POOL_CLUSTER" ] \
        || die "shared HA backends do not belong to the same dedicated Indexer cluster"
    fi
    if [ "$mode" = shared ]; then
      [ -z "${seen_members[$BACKEND_MEMBER_ID]:-}" ] \
        || die "shared HA backends repeat servingMemberId $BACKEND_MEMBER_ID"
      seen_members[$BACKEND_MEMBER_ID]=1
      POOL_MEMBER_IDS+=("$BACKEND_MEMBER_ID")
    fi
    POOL_TARGETS+=("$target")
    POOL_BACKENDS+=("$backend")
  done

  if [ "$mode" = shared ]; then
    [ "${POOL_CLUSTER,,}" != "${CLUSTER,,}" ] \
      || die "shared Indexer workers require a dedicated cluster; pool cluster equals the LB/C3 cluster $CLUSTER"
  fi
  POOL_TARGETS_CSV=$(IFS=,; echo "${POOL_TARGETS[*]}")
  POOL_BACKENDS_CSV=$(IFS=,; echo "${POOL_BACKENDS[*]}")
  POOL_MEMBER_IDS_CSV=$(IFS=,; echo "${POOL_MEMBER_IDS[*]}")
}

_control_get() {
  local path="$1"
  _ensure_secrets
  _discover_mesh_ip || die "LB is not registered with a mesh IP"
  ssh_mesh "INDEXER_LB_ADMIN_KEY=$(printf '%q' "$INDEXER_LB_ADMIN_KEY") INDEXER_LB_URL=$(printf '%q' "http://$MESH_IP:50053$path") bash -s" <<'SCRIPT'
curl -fsS --max-time 15 \
  -H "Authorization: Bearer $INDEXER_LB_ADMIN_KEY" \
  "$INDEXER_LB_URL"
SCRIPT
}

_control_post() {
  local path="$1" payload="$2"
  _ensure_secrets
  _discover_mesh_ip || die "LB is not registered with a mesh IP"
  ssh_mesh "INDEXER_LB_ADMIN_KEY=$(printf '%q' "$INDEXER_LB_ADMIN_KEY") INDEXER_LB_PAYLOAD=$(printf '%q' "$payload") INDEXER_LB_URL=$(printf '%q' "http://$MESH_IP:50053$path") bash -s" <<'SCRIPT'
curl -fsS --max-time 150 \
  -H "Authorization: Bearer $INDEXER_LB_ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary "$INDEXER_LB_PAYLOAD" \
  "$INDEXER_LB_URL"
SCRIPT
}

send_seq() {
  local label="$1"; shift
  send_with_nonce_retry "$label" "$@"
}

_registry_owner_preflight() {
  local owner
  owner=$(cast call "$REGISTRY" 'owner()(address)' --rpc-url "$RPC_URL" 2>/dev/null | tr 'A-F' 'a-f') \
    || die "could not read IndexerRegistry.owner()"
  [ "$owner" = "${DEPLOYER_ADDR,,}" ] \
    || die "IndexerRegistry owner=$owner but configured deployer=${DEPLOYER_ADDR,,}; this driver does not yet emit Safe transactions"
}

_set_registry() {
  local label="$1" endpoint="$2" code_id="$3" pubkey="$4" updated_at="$5"
  _registry_owner_preflight
  send_seq "$label" "$REGISTRY" \
    "setIndexer((string,bytes32,bytes32,uint64))" \
    "($endpoint,$code_id,$pubkey,$updated_at)"
}

_atomic_json_write() {
  local path="$1" value="$2"
  python3 - "$path" "$value" <<'PY'
import json, os, sys
path, raw = sys.argv[1], sys.argv[2]
value = json.loads(raw)
tmp = path + ".tmp"
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(value, fh, sort_keys=True)
    fh.write("\n")
    fh.flush()
    os.fsync(fh.fileno())
os.chmod(tmp, 0o600)
os.replace(tmp, path)
fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(fd)
finally:
    os.close(fd)
PY
}

_txn_update_phase() {
  local phase="$1" value
  [ -s "$TXN_STATE" ] || die "missing transaction journal: $TXN_STATE"
  value=$(jq -c --arg phase "$phase" --argjson at "$(date +%s)" \
    '.phase=$phase | .phase_updated_at=$at' "$TXN_STATE")
  _atomic_json_write "$TXN_STATE" "$value"
}

_txn_clear() {
  python3 - "$TXN_STATE" <<'PY'
import os, sys
path = sys.argv[1]
try:
    os.unlink(path)
except FileNotFoundError:
    raise SystemExit(0)
fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
try:
    os.fsync(fd)
finally:
    os.close(fd)
PY
}

_operation_status() {
  local operation_id="$1"
  _control_get "/operation?operation_id=$operation_id" | jq -er '.status'
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
  printf 'https://%s-50052.%s\n' "${X#0x}" "$GATEWAY_DOMAIN"
}

switch_backend() {
  local requested="${1:-${TARGET:-}}" stable before old_endpoint old_code old_pub old_updated
  local control_before old_pool_json old_members_json old_active_pub old_active_code old_active_cluster old_active_operation
  local restore_pub restore_code prepare_payload prepare_out commit_out restore_payload restore_out
  local pool_json targets_json members_json shared_label operation_id operation_status journal
  [ -n "$requested" ] || die "usage: $0 $NODE switch <indexer-node-or-ip[,indexer-node-or-ip...]>"
  [ ! -s "$TXN_STATE" ] \
    || die "unfinished Indexer LB transaction journal exists; run '$0 $NODE recover' first: $TXN_STATE"
  _load_generic
  _default_cluster_env
  _ensure_secrets
  _registry_owner_preflight
  _discover_mesh_ip || die "LB mesh IP unavailable"
  _resolve_switch_pool "$requested"
  stable="$(_stable_endpoint)"
  before="$(_registry_snapshot)" || die "could not snapshot IndexerRegistry.current()"
  old_endpoint=$(echo "$before" | jq -r '.[0]')
  old_code=$(echo "$before" | jq -r '.[1]' | tr 'A-F' 'a-f')
  old_pub=$(echo "$before" | jq -r '.[2]' | tr 'A-F' 'a-f')
  old_updated=$(echo "$before" | jq -r '.[3]')
  control_before="$(_control_get /active)" || die "could not snapshot active LB pool"
  old_pool_json=$(echo "$control_before" | jq -ce '
    if (.active_backends | type) == "array" then
      [.active_backends[] | select(type == "string" and length > 0)]
    elif ((.active_backend // "") | length) > 0 then
      [(.active_backend | split(",")[]) | select(length > 0)]
    else [] end
  ') || die "LB returned invalid active pool state: $control_before"
  old_members_json=$(echo "$control_before" | jq -ce '
    if (.active_members | type) == "array" then .active_members else [] end
  ') || die "LB returned invalid active member state: $control_before"
  old_active_pub=$(echo "$control_before" | jq -r '.active_pubkey // empty' | tr 'A-F' 'a-f')
  old_active_code=$(echo "$control_before" | jq -r '.active_code_id // empty' | tr 'A-F' 'a-f')
  old_active_cluster=$(echo "$control_before" | jq -r '.active_cluster // empty' | tr 'A-F' 'a-f')
  old_active_operation=$(echo "$control_before" | jq -r '.active_operation_id // empty' | tr 'A-F' 'a-f')
  restore_pub="$old_active_pub"
  restore_code="$old_active_code"
  if [ "$(echo "$old_pool_json" | jq 'length')" -gt 0 ]; then
    [ -n "$restore_pub" ] || restore_pub="$old_pub"
    [ -n "$restore_code" ] || restore_code="$old_code"
  fi

  operation_id=$(openssl rand -hex 32)
  pool_json=$(printf '%s\n' "${POOL_BACKENDS[@]}" | jq -Rsc 'split("\n") | map(select(length > 0))')
  targets_json=$(printf '%s\n' "${POOL_TARGETS[@]}" | jq -Rsc 'split("\n") | map(select(length > 0))')
  members_json=$(printf '%s\n' "${POOL_MEMBER_IDS[@]}" | jq -Rsc 'split("\n") | map(select(length > 0))')

  if [ "$POOL_SHARED_HA" = 1 ]; then
    prepare_payload=$(jq -nc \
      --argjson backends "$pool_json" \
      --arg pubkey "$POOL_PUBKEY" \
      --arg code "$POOL_CODE_ID" \
      --arg cluster "$POOL_CLUSTER" \
      --arg operation "$operation_id" \
      '{backends:$backends, expected_pubkey:$pubkey, expected_code_id:$code, expected_cluster:$cluster, protocol_v3_fleet_confirmed:true, operation_id:$operation}')
    shared_label="shared pool nodes=$POOL_TARGETS_CSV backends=$POOL_BACKENDS_CSV cluster=$POOL_CLUSTER members=$POOL_MEMBER_IDS_CSV"
  else
    prepare_payload=$(jq -nc \
      --arg backend "${POOL_BACKENDS[0]}" \
      --arg pubkey "$POOL_PUBKEY" \
      --arg code "$POOL_CODE_ID" \
      --arg operation "$operation_id" \
      '{backend:$backend, expected_pubkey:$pubkey, expected_code_id:$code, operation_id:$operation}')
    shared_label="candidate=${POOL_TARGETS[0]} backend=${POOL_BACKENDS[0]}"
  fi

  restore_payload=$(jq -nc \
    --arg operation "$operation_id" \
    --argjson backends "$old_pool_json" \
    --arg pubkey "$restore_pub" \
    --arg code "$restore_code" \
    --arg cluster "$old_active_cluster" \
    --arg active_operation "$old_active_operation" \
    --argjson members "$old_members_json" \
    '{operation_id:$operation, backends:$backends, active_pubkey:$pubkey, active_code_id:$code, active_cluster:$cluster, active_members:$members, active_operation_id:$active_operation}')
  journal=$(jq -nc \
    --arg operation "$operation_id" \
    --arg requested "$requested" \
    --arg stable "$stable" \
    --arg old_endpoint "$old_endpoint" \
    --arg old_code "$old_code" \
    --arg old_pub "$old_pub" \
    --arg old_updated "$old_updated" \
    --arg new_code "$POOL_CODE_ID" \
    --arg new_pub "$POOL_PUBKEY" \
    --arg cluster "$POOL_CLUSTER" \
    --argjson targets "$targets_json" \
    --argjson backends "$pool_json" \
    --argjson members "$members_json" \
    --argjson prepare "$prepare_payload" \
    --argjson restore "$restore_payload" \
    --argjson created "$(date +%s)" \
    --arg shared "$POOL_SHARED_HA" \
    '{operation_id:$operation, phase:"created", created_at:$created, requested:$requested, stable_endpoint:$stable, old_registry:{endpoint:$old_endpoint,code_id:$old_code,pubkey:$old_pub,updated_at:$old_updated}, new_registry:{endpoint:$stable,code_id:$new_code,pubkey:$new_pub}, pool:{targets:$targets,backends:$backends,members:$members,pubkey:$new_pub,code_id:$new_code,cluster:$cluster,shared:($shared == "1")}, prepare_payload:$prepare, restore_payload:$restore}')
  _atomic_json_write "$TXN_STATE" "$journal"
  log "▶ preparing Indexer LB $NODE $shared_label pubkey=$POOL_PUBKEY"
  if ! prepare_out="$(_control_post /prepare "$prepare_payload" 2>&1)"; then
    operation_status="$(_operation_status "$operation_id" 2>/dev/null || true)"
    if [ "$operation_status" = prepared ]; then
      if _control_post /abort "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" >/dev/null 2>&1; then
        _txn_clear
      fi
    elif [ "$operation_status" = unknown ]; then
      _txn_clear
    fi
    die "LB prepare failed or response was uncertain (journal retained unless safely aborted): $prepare_out"
  fi
  [ "$(echo "$prepare_out" | jq -r '.operation_id // empty')" = "$operation_id" ] \
    || die "LB prepare returned the wrong operation_id; recover using $TXN_STATE"
  _txn_update_phase prepared
  echo "$prepare_out" | tee "$LOGDIR/indexer-lb-prepare-${NODE}.$(ts).log"

  _txn_update_phase registry-intent
  if ! _control_post /intent "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" >/dev/null; then
    die "could not durably mark registry intent; LB remains paused and recovery journal is $TXN_STATE"
  fi
  log "▶ updating IndexerRegistry at unchanged stable endpoint=$stable codeId=$POOL_CODE_ID"
  if ! _set_registry "indexer-lb-registry-${NODE}" "$stable" "$POOL_CODE_ID" "$POOL_PUBKEY" "$(date +%s)"; then
    if ! restore_out="$(_control_post /restore "$restore_payload" 2>&1)"; then
      die "registry update failed and old pool restore failed; frontends remain paused and journal is $TXN_STATE: $restore_out"
    fi
    _txn_clear
    die "registry update failed; previous LB pool restored and frontends reopened"
  fi
  _txn_update_phase registry-updated

  log "▶ committing Indexer LB data-plane pool and reconnecting subscribers"
  if ! commit_out="$(_control_post /commit "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" 2>&1)"; then
    operation_status="$(_operation_status "$operation_id" 2>/dev/null || true)"
    if [ "$operation_status" = committed ]; then
      commit_out=$(jq -nc --arg operation "$operation_id" \
        '{operation_id:$operation, committed_after_uncertain_response:true}')
    elif [ -n "$operation_status" ] && [ "$operation_status" != unknown ]; then
      log "commit failed; rolling the registry tuple back before restoring the previous data-plane pool"
      if ! _set_registry "indexer-lb-registry-rollback-${NODE}" "$old_endpoint" "$old_code" "$old_pub" "$(date +%s)"; then
        die "LB commit failed AND registry rollback failed; frontends remain paused and journal is $TXN_STATE: $commit_out"
      fi
      if ! restore_out="$(_control_post /restore "$restore_payload" 2>&1)"; then
        die "LB commit failed; previous registry tuple was restored with a fresh timestamp but pool restore failed: $restore_out"
      fi
      _txn_clear
      die "LB commit failed; previous registry tuple and LB pool were restored: $commit_out"
    else
      die "LB commit outcome is uncertain; no rollback was attempted. Run '$0 $NODE recover' using $TXN_STATE: $commit_out"
    fi
  fi
  _txn_update_phase committed
  echo "$commit_out" | tee "$LOGDIR/indexer-lb-commit-${NODE}.$(ts).log"

  ACTIVE_BACKEND="${POOL_BACKENDS[0]}"
  ACTIVE_BACKEND_NODE="${POOL_TARGETS[0]}"
  ACTIVE_BACKENDS="$POOL_BACKENDS_CSV"
  ACTIVE_BACKEND_NODES="$POOL_TARGETS_CSV"
  ACTIVE_PUBKEY="$POOL_PUBKEY"
  ACTIVE_CODE_ID="$POOL_CODE_ID"
  ACTIVE_INDEXER_CLUSTER="$POOL_CLUSTER"
  ACTIVE_MEMBER_IDS="$POOL_MEMBER_IDS_CSV"
  STABLE_ENDPOINT="$stable"
  _save_lb
  verify_lb
  _txn_clear
  log "✔ Indexer LB active $shared_label; keep the old worker(s) running until subscriber diagnostics reconnect"
}

_finalize_recovered_transaction() {
  local first_backend first_target
  first_backend=$(jq -r '.pool.backends[0]' "$TXN_STATE")
  first_target=$(jq -r '.pool.targets[0]' "$TXN_STATE")
  ACTIVE_BACKEND="$first_backend"
  ACTIVE_BACKEND_NODE="$first_target"
  ACTIVE_BACKENDS=$(jq -r '.pool.backends | join(",")' "$TXN_STATE")
  ACTIVE_BACKEND_NODES=$(jq -r '.pool.targets | join(",")' "$TXN_STATE")
  ACTIVE_PUBKEY=$(jq -r '.pool.pubkey' "$TXN_STATE")
  ACTIVE_CODE_ID=$(jq -r '.pool.code_id' "$TXN_STATE")
  ACTIVE_INDEXER_CLUSTER=$(jq -r '.pool.cluster' "$TXN_STATE")
  ACTIVE_MEMBER_IDS=$(jq -r '.pool.members | join(",")' "$TXN_STATE")
  STABLE_ENDPOINT=$(jq -r '.stable_endpoint' "$TXN_STATE")
  _save_lb
  verify_lb
  _txn_clear
  log "✔ recovered and finalized committed Indexer LB transaction"
}

recover_transaction() {
  local operation_id status registry current_endpoint current_code current_pub
  local new_endpoint new_code new_pub old_endpoint old_code old_pub restore_payload commit_out
  [ -s "$TXN_STATE" ] || die "no unfinished Indexer LB transaction journal: $TXN_STATE"
  _load_generic
  _default_cluster_env
  _ensure_secrets
  _discover_mesh_ip || die "LB mesh IP unavailable"
  _registry_owner_preflight
  operation_id=$(jq -er '.operation_id' "$TXN_STATE") \
    || die "transaction journal has no operation_id: $TXN_STATE"
  status="$(_operation_status "$operation_id")" \
    || die "could not determine controller operation status; leaving journal untouched"
  registry="$(_registry_snapshot)" || die "could not read IndexerRegistry.current()"
  current_endpoint=$(echo "$registry" | jq -r '.[0]')
  current_code=$(echo "$registry" | jq -r '.[1]' | tr 'A-F' 'a-f')
  current_pub=$(echo "$registry" | jq -r '.[2]' | tr 'A-F' 'a-f')
  new_endpoint=$(jq -r '.new_registry.endpoint' "$TXN_STATE")
  new_code=$(jq -r '.new_registry.code_id' "$TXN_STATE" | tr 'A-F' 'a-f')
  new_pub=$(jq -r '.new_registry.pubkey' "$TXN_STATE" | tr 'A-F' 'a-f')
  old_endpoint=$(jq -r '.old_registry.endpoint' "$TXN_STATE")
  old_code=$(jq -r '.old_registry.code_id' "$TXN_STATE" | tr 'A-F' 'a-f')
  old_pub=$(jq -r '.old_registry.pubkey' "$TXN_STATE" | tr 'A-F' 'a-f')
  restore_payload=$(jq -c '.restore_payload' "$TXN_STATE")

  if [ "$status" = committed ] \
    && [ "$current_endpoint" = "$new_endpoint" ] \
    && [ "$current_code" = "$new_code" ] \
    && [ "$current_pub" = "$new_pub" ]; then
    _finalize_recovered_transaction
    return 0
  fi

  if [ "$status" = unknown ] \
    && [ "$current_endpoint" = "$old_endpoint" ] \
    && [ "$current_code" = "$old_code" ] \
    && [ "$current_pub" = "$old_pub" ]; then
    _txn_clear
    log "✔ cleared a transaction that never reached the controller or registry"
    return 0
  fi

  if [ "$current_endpoint" = "$old_endpoint" ] \
    && [ "$current_code" = "$old_code" ] \
    && [ "$current_pub" = "$old_pub" ]; then
    if [ "$status" = prepared ]; then
      _control_post /abort "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" >/dev/null \
        || die "controller abort failed; journal retained"
    else
      _control_post /restore "$restore_payload" >/dev/null \
        || die "controller old-pool restore failed; journal retained"
    fi
    _txn_clear
    log "✔ recovered the previous registry tuple and LB pool"
    return 0
  fi

  if [ "$current_endpoint" = "$new_endpoint" ] \
    && [ "$current_code" = "$new_code" ] \
    && [ "$current_pub" = "$new_pub" ] \
    && [ "$status" != unknown ]; then
    if commit_out="$(_control_post /commit "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" 2>&1)" \
      || [ "$(_operation_status "$operation_id" 2>/dev/null || true)" = committed ]; then
      _txn_update_phase committed
      _finalize_recovered_transaction
      return 0
    fi
    log "forward recovery failed; restoring the previous registry tuple with a fresh timestamp"
    _set_registry "indexer-lb-registry-recover-rollback-${NODE}" \
      "$old_endpoint" "$old_code" "$old_pub" "$(date +%s)" \
      || die "recovery registry rollback failed; journal retained: $commit_out"
    _control_post /restore "$restore_payload" >/dev/null \
      || die "registry rolled back but LB pool restore failed; journal retained"
    _txn_clear
    die "forward recovery failed; previous registry tuple and LB pool were restored"
  fi

  die "registry/controller state does not match either journal generation; no mutation performed"
}

verify_sidecar_health() {
  _load_generic
  local bridge i body
  for i in $(seq 1 40); do
    bridge="$(_bridge_ip_for_vm "$VM_ID" 2>/dev/null || true)"
    if [ -z "$bridge" ]; then
      log "… waiting for Indexer LB bridge neighbor after VM start ($i/40)"
      sleep 10
      continue
    fi
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
  _default_cluster_env
  _discover_mesh_ip || die "LB mesh IP unavailable"
  local control stable status registry_value endpoint registry_pub registry_code active_pub active_code active_cluster
  local backend_status expected_member unique_members i
  local -a active_backends=() active_members=()
  control="$(_control_get /active)" || die "LB control API unavailable"
  echo "$control" | jq -e '.haproxy_socket == true' >/dev/null \
    || die "LB control response does not report HAProxy socket: $control"
  mapfile -t active_backends < <(echo "$control" | jq -r '.active_backends[]? // empty')
  if [ "${#active_backends[@]}" -eq 0 ]; then
    mapfile -t active_backends < <(echo "$control" | jq -r \
      '(.active_backend // "") | split(",")[] | select(length > 0)')
  fi
  mapfile -t active_members < <(echo "$control" | jq -r '.active_members[]? // empty' | tr 'A-F' 'a-f')
  active_pub=$(echo "$control" | jq -r '.active_pubkey // empty' | tr 'A-F' 'a-f')
  active_code=$(echo "$control" | jq -r '.active_code_id // empty' | tr 'A-F' 'a-f')
  active_cluster=$(echo "$control" | jq -r '.active_cluster // empty' | tr 'A-F' 'a-f')
  if [ -n "$active_pub" ]; then
    stable="$(_stable_endpoint)"
    status=$(curl -fsS --max-time 15 "${stable/-50052./-9090.}/status") \
      || die "stable Indexer HTTP endpoint unavailable"
    [ "$(echo "$status" | jq -r '.pubKey // empty' | tr 'A-F' 'a-f')" = "${active_pub,,}" ] \
      || die "stable endpoint pubkey does not match LB active pool"
    registry_value="$(_registry_snapshot)" || die "could not read IndexerRegistry.current()"
    endpoint=$(echo "$registry_value" | jq -r '.[0]')
    registry_code=$(echo "$registry_value" | jq -r '.[1]' | tr 'A-F' 'a-f')
    registry_pub=$(echo "$registry_value" | jq -r '.[2]' | tr 'A-F' 'a-f')
    [ "$endpoint" = "$stable" ] || die "registry endpoint=$endpoint, expected stable LB endpoint=$stable"
    [ "$registry_pub" = "${active_pub,,}" ] || die "registry pubkey does not match LB active pool"
    if [ -n "$active_code" ]; then
      [ "$registry_code" = "$active_code" ] \
        || die "registry codeId does not match LB active pool"
    fi
  fi

  if [ "${#active_backends[@]}" -ge 2 ]; then
    echo "$active_pub" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "shared active pool has an invalid pubkey"
    echo "$active_code" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "shared active pool has an invalid codeId"
    [ "$active_code" != "$ZERO32" ] || die "shared active pool has a zero codeId"
    echo "$active_cluster" | grep -Eq '^0x[0-9a-f]{40}$' \
      || die "shared active pool has an invalid dedicated cluster"
    [ "$active_cluster" != "$ZERO_ADDRESS" ] || die "shared active pool has a zero cluster"
    [ "$active_cluster" != "${CLUSTER,,}" ] \
      || die "shared active pool is using the LB/C3 cluster instead of a dedicated Indexer cluster"
    [ "${#active_members[@]}" -eq "${#active_backends[@]}" ] \
      || die "shared active pool member IDs do not match its backend count"
    unique_members=$(printf '%s\n' "${active_members[@]}" | sort -u | wc -l | tr -d ' ')
    [ "$unique_members" -eq "${#active_members[@]}" ] \
      || die "shared active pool contains duplicate serving member IDs"

    for i in "${!active_backends[@]}"; do
      expected_member="${active_members[$i]}"
      backend_status="$(_backend_http "${active_backends[$i]}" /status)" \
        || die "shared active backend ${active_backends[$i]} status is unavailable"
      echo "$backend_status" | jq -e \
        --arg pubkey "$active_pub" \
        --arg code "$active_code" \
        --arg cluster "$active_cluster" \
        --arg member "$expected_member" '
          .identityMode == "cluster-shared"
          and ((.pubKey // "") | ascii_downcase) == $pubkey
          and ((.codeId // "") | ascii_downcase) == $code
          and ((.indexerCluster // "") | ascii_downcase) == $cluster
          and ((.servingMemberId // "") | ascii_downcase) == $member
          and .health.ok == true
          and .health.grpcAccepting == true
          and .health.rpcReachable == true
        ' >/dev/null \
        || die "shared active backend ${active_backends[$i]} identity or serving health diverged: $backend_status"
    done
  fi
  echo "$control" | jq \
    '{active_backend,active_backends,active_pubkey,active_code_id,active_cluster,active_members,prepared,haproxy_socket}'
  log "✔ Indexer LB control=$MESH_IP:50053 grpc=$(_stable_endpoint)"
}

active() { _control_get /active | jq; }

assert_drained() {
  local target="${1:-}" backend control operation_id pinned_operation payload
  [ -n "$target" ] \
    || die "usage: $0 $NODE assert-drained <indexer-worker-node-or-private-ip>"
  [[ "$target" != *,* ]] || die "assert-drained accepts exactly one worker"
  backend="$(_backend_ip "$target" 1)" \
    || die "could not resolve worker to assert drained: $target"
  control="$(_control_get /active)" || die "could not read active LB generation"
  operation_id=$(echo "$control" | jq -er '.active_operation_id | select(type == "string" and test("^[0-9a-f]{64}$"))') \
    || die "LB has no tokenized active generation; perform a successful switch before stopping workers"
  pinned_operation="${INDEXER_LB_ACTIVE_OPERATION_ID:-}"
  if [ -n "$pinned_operation" ]; then
    pinned_operation="${pinned_operation,,}"
    [[ "$pinned_operation" =~ ^[0-9a-f]{64}$ ]] \
      || die "INDEXER_LB_ACTIVE_OPERATION_ID must be 32 bytes of lowercase hex"
    [ "$pinned_operation" = "$operation_id" ] \
      || die "active LB operation changed: expected $pinned_operation, got $operation_id"
  fi
  payload=$(jq -nc \
    --arg operation "$operation_id" \
    --arg backend "$backend" \
    '{operation_id:$operation,backend:$backend}')
  _control_post /assert-drained "$payload" | jq
}

abort_prepare() {
  local operation_id="${1:-}" journal_operation=""
  if [ -s "$TXN_STATE" ]; then
    journal_operation=$(jq -er '.operation_id | select(type == "string" and test("^[0-9a-f]{64}$"))' "$TXN_STATE") \
      || die "transaction journal contains an invalid operation_id: $TXN_STATE"
  fi
  if [ -z "$operation_id" ]; then
    operation_id="$journal_operation"
  elif [ -n "$journal_operation" ] && [ "${operation_id,,}" != "$journal_operation" ]; then
    die "refusing to abort operation $operation_id while local journal tracks $journal_operation"
  fi
  operation_id="${operation_id,,}"
  [ -n "$operation_id" ] \
    || die "abort requires an operation_id or local transaction journal"
  [[ "$operation_id" =~ ^[0-9a-f]{64}$ ]] \
    || die "abort operation_id must be 32 bytes of lowercase hex"
  _control_post /abort "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" | jq
  [ ! -s "$TXN_STATE" ] || _txn_clear
}

log "=== AttestMesh Indexer LB node: $NODE ==="
case "$ACTION" in
  setup) _ensure_secrets ;;
  preflight|deploy|prime|bind|start|register-direct|verify) generic "$ACTION" ;;
  verify-health) verify_sidecar_health ;;
  verify-lb) verify_lb ;;
  active) active ;;
  assert-drained) assert_drained "$TARGET" ;;
  abort) abort_prepare "$TARGET" ;;
  recover) recover_transaction ;;
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
  *) die "usage: $0 <node-name> [setup|preflight|deploy|prime|bind|start|register-direct|verify|verify-health|verify-lb|active|assert-drained|abort|recover|switch|update|stop|all] [target-or-operation-id]" ;;
esac
