#!/usr/bin/env bash
# Deploy an AttestMesh Indexer candidate as a member of the main C3 cluster on the
# self-hosted dstack box. `candidate` leaves IndexerRegistry unchanged so the node
# can be verified before an Indexer-LB blue/green cutover; `all` retains the legacy
# direct-registration behavior.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:-attestmesh-indexer-c3}"
ACTION="${2:-all}"
ACTION_ARG="${3:-}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/indexer-member-node.yaml}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
INDEXER_GRPC_GATEWAY_PORT="${INDEXER_GRPC_GATEWAY_PORT:-50052}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
REGISTRY=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json")
STATE="$LOGDIR/generic-node-${NODE}.state"
REGISTER_SIGNATURE='dstack_register((bytes32,bytes32,bytes,bytes,bytes,bytes,bytes,string),address,bytes32,bytes32)'

export COMPOSE GATEWAY_DOMAIN
export BOX_VCPU="${BOX_VCPU:-4}" BOX_MEM="${BOX_MEM:-8192}" BOX_DISK="${BOX_DISK:-80}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

_default_cluster_env() {
  if [ -z "${CLUSTER:-}" ] || [ -z "${MEMBER_IMPL:-}" ]; then
    [ -f "$MATRIX_STATE" ] || die "missing Matrix cluster state: $MATRIX_STATE"
    CLUSTER="${CLUSTER:-$(grep '^CLUSTER=' "$MATRIX_STATE" | cut -d= -f2-)}"
    MEMBER_IMPL="${MEMBER_IMPL:-$(grep '^MEMBER_IMPL=' "$MATRIX_STATE" | cut -d= -f2-)}"
  fi
  export CLUSTER MEMBER_IMPL
}

_load_state() {
  local mode owner value state_cluster state_impl state_gateway
  [ -f "$STATE" ] || die "missing state $STATE; run deploy/all first"
  mode=$(stat -c '%a' "$STATE") || die "cannot stat state $STATE"
  owner=$(stat -c '%u' "$STATE") || die "cannot read state owner $STATE"
  [ "$owner" = "$(id -u)" ] || die "state is not owned by the current operator: $STATE"
  [ $((8#$mode & 077)) -eq 0 ] || die "state must not be group/world accessible: $STATE"

  value=$(_state_field X) || die "state must contain exactly one X field"
  [[ "$value" =~ ^0x[0-9a-fA-F]{40}$ ]] || die "state X is not an address"
  X="$value"
  value=$(_state_field H) || die "state must contain exactly one H field"
  value="${value#0x}"
  [[ "$value" =~ ^[0-9a-fA-F]{64}$ ]] || die "state H is not a 32-byte hash"
  H="${value,,}"
  value=$(_state_field VM_ID) || die "state must contain exactly one VM_ID field"
  [[ "$value" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$ ]] \
    || die "state VM_ID contains unsupported characters"
  VM_ID="$value"

  state_cluster=$(_state_field CLUSTER) || die "state must contain exactly one CLUSTER field"
  state_impl=$(_state_field MEMBER_IMPL) || die "state must contain exactly one MEMBER_IMPL field"
  state_gateway=$(_state_field GATEWAY_DOMAIN) || die "state must contain exactly one GATEWAY_DOMAIN field"
  if [ -n "$state_cluster" ]; then
    [[ "$state_cluster" =~ ^0x[0-9a-fA-F]{40}$ ]] || die "state CLUSTER is not an address"
    CLUSTER="$state_cluster"
  fi
  if [ -n "$state_impl" ]; then
    [[ "$state_impl" =~ ^0x[0-9a-fA-F]{40}$ ]] || die "state MEMBER_IMPL is not an address"
    MEMBER_IMPL="$state_impl"
  fi
  if [ -n "$state_gateway" ]; then
    [[ "$state_gateway" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?$ ]] \
      || die "state GATEWAY_DOMAIN contains unsupported characters"
    GATEWAY_DOMAIN="$state_gateway"
  fi
}

_state_field() {
  local key="$1"
  awk -F= -v key="$key" '
    $1 == key { count += 1; value = substr($0, length($1) + 2) }
    END {
      if (count != 1) exit 1
      print value
    }
  ' "$STATE"
}

_app_host_prefix() {
  printf '%s' "${X#0x}" | tr 'A-Z' 'a-z'
}

_current_registry_cluster_count() {
  local endpoint status
  endpoint=$(cast call "$REGISTRY" 'current()(string,bytes32,bytes32,uint64)' \
    --json --rpc-url "$RPC_URL" 2>/dev/null | jq -er '.[0] | select(length > 0)') || return 1
  status=$(curl -fsS --max-time 12 "${endpoint/-50052./-9090.}/status" 2>/dev/null) \
    || return 1
  echo "$status" | jq -er '.readModel.clusterCount | select(type == "number" and . > 0)'
}

send_seq() {
  local label="$1"; shift
  send_with_nonce_retry "$label" "$@"
}

_pubkey_from_serial() {
  _load_state
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" \
    "sudo sed 's/\\x1b\\[[0-9;]*m//g' /srv/data/dstack/vm/$VM_ID/serial.log /srv/data/dstack/vm/$VM_ID/serial.history.log 2>/dev/null" \
    | grep -E 'derived indexer signing identity' \
    | grep -oE '0x[0-9a-fA-F]{64}' \
    | tail -1
}

_pubkey_from_http() {
  _load_state
  local host
  host="$(_app_host_prefix)-9090.${GATEWAY_DOMAIN}"
  curl -fsm 8 "https://${host}/status" 2>/dev/null \
    | jq -r 'select(.pubKey != null) | .pubKey' \
    | grep -E '^0x[0-9a-fA-F]{64}$' \
    | tail -1
}

register_indexer() {
  _load_state
  local pubkey endpoint
  pubkey="$(_pubkey_from_http || true)"
  [ -n "$pubkey" ] || pubkey="$(_pubkey_from_serial)"
  [ -n "$pubkey" ] || die "indexer pubkey not found via /status or CVM serial logs; check VM_ID=$VM_ID"
  endpoint="https://$(_app_host_prefix)-${INDEXER_GRPC_GATEWAY_PORT}.${GATEWAY_DOMAIN}"
  log "registering C3 indexer endpoint=$endpoint codeId=0x${H#0x}"
  send_with_nonce_retry "setIndexer-${NODE}" "$REGISTRY" \
    "setIndexer((string,bytes32,bytes32,uint64))" \
    "($endpoint,0x${H#0x},$pubkey,$(date +%s))"
}

_direct_register_payload() {
  _load_state
  local host payload
  host="$(_app_host_prefix)-9092.${GATEWAY_DOMAIN}"
  payload=$(curl -fsm 8 "https://${host}/registration-calldata" 2>/dev/null || true)
  if [ -n "$payload" ]; then
    printf '%s\n' "$payload"
    return 0
  fi

  ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" \
    "sudo sed 's/\\x1b\\[[0-9;]*m//g' /srv/data/dstack/vm/$VM_ID/serial.log /srv/data/dstack/vm/$VM_ID/serial.history.log 2>/dev/null" \
    | grep 'ATTESTMESH_DIRECT_REGISTER ' \
    | sed 's/^.*ATTESTMESH_DIRECT_REGISTER //' \
    | tail -1
}

_validate_registration_payload() {
  local payload="$1" decoded calldata cluster member code_id x_pub wg_pub
  local decoded_member decoded_code decoded_x decoded_wg expected_code
  echo "$payload" | jq -e '
    type == "object"
    and (.cluster | type == "string" and test("^0x[0-9a-fA-F]{40}$"))
    and (.member | type == "string" and test("^0x[0-9a-fA-F]{40}$"))
    and (.codeId | type == "string" and test("^0x[0-9a-fA-F]{64}$"))
    and (.xPubKey | type == "string" and test("^0x[0-9a-fA-F]{64}$"))
    and (.wgPubKey | type == "string" and test("^0x[0-9a-fA-F]{64}$"))
    and (.calldata | type == "string" and test("^0x537d491c[0-9a-fA-F]+$"))
  ' >/dev/null || die "registration helper returned an invalid payload schema"

  cluster=$(echo "$payload" | jq -r '.cluster | ascii_downcase')
  member=$(echo "$payload" | jq -r '.member | ascii_downcase')
  code_id=$(echo "$payload" | jq -r '.codeId | ascii_downcase')
  x_pub=$(echo "$payload" | jq -r '.xPubKey | ascii_downcase')
  wg_pub=$(echo "$payload" | jq -r '.wgPubKey | ascii_downcase')
  calldata=$(echo "$payload" | jq -r .calldata)
  [ "$cluster" = "${CLUSTER,,}" ] \
    || die "registration helper emitted cluster=$cluster, expected CLUSTER=${CLUSTER,,}"
  [ "$member" = "${X,,}" ] \
    || die "registration helper emitted member=$member, expected X=${X,,}"
  expected_code="${X,,}000000000000000000000000"
  [ "$code_id" = "$expected_code" ] \
    || die "registration helper codeId=$code_id is not bytes32(bytes20(X))=$expected_code"

  decoded=$(cast decode-calldata --json "$REGISTER_SIGNATURE" "$calldata") \
    || die "registration helper calldata is not canonical dstack_register ABI"
  echo "$decoded" | jq -e '
    type == "array"
    and length == 4
    and (.[0] | type == "array" and length == 8)
    and (.[0][0] | type == "string")
    and (.[1] | type == "string")
    and (.[2] | type == "string")
    and (.[3] | type == "string")
  ' >/dev/null || die "decoded dstack_register calldata has an unexpected shape"
  decoded_code=$(echo "$decoded" | jq -r '.[0][0] | ascii_downcase')
  decoded_member=$(echo "$decoded" | jq -r '.[1] | ascii_downcase')
  decoded_x=$(echo "$decoded" | jq -r '.[2] | ascii_downcase')
  decoded_wg=$(echo "$decoded" | jq -r '.[3] | ascii_downcase')
  [ "$decoded_code" = "$code_id" ] \
    || die "calldata proof codeId does not match the verified payload codeId"
  [ "$decoded_member" = "$member" ] \
    || die "calldata member does not match the verified payload member"
  [ "$decoded_x" = "$x_pub" ] \
    || die "calldata xPubKey does not match the verified payload xPubKey"
  [ "$decoded_wg" = "$wg_pub" ] \
    || die "calldata wgPubKey does not match the verified payload wgPubKey"
  printf '%s\n' "$calldata"
}

register_member_direct() {
  _load_state
  _default_cluster_env
  local zero id payload calldata member
  zero=0x0000000000000000000000000000000000000000000000000000000000000000
  id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null || true)
  if [ -n "$id" ] && [ "$id" != "$zero" ]; then
    log "generic node already registered: memberId=$id"
    return 0
  fi

  for i in $(seq 1 60); do
    payload="$(_direct_register_payload || true)"
    if [ -n "$payload" ] && echo "$payload" | jq -e '.calldata and .member' >/dev/null 2>&1; then
      member=$(echo "$payload" | jq -r .member)
      calldata=$(_validate_registration_payload "$payload")
      log "▶ direct dstack_register for $NODE member=$member via operator tx"
      send_seq "direct-dstack-register-${NODE}" "$CLUSTER" --data "$calldata"
      return 0
    fi
    log "… waiting for registration helper calldata ($i/60)"
    sleep 5
  done
  die "registration helper calldata not found in CVM serial logs"
}

validate_registration_file() {
  local file="${1:?usage: indexer-member-node.sh <name> validate-registration-payload <payload.json>}" payload
  [ -f "$file" ] || die "registration payload file not found: $file"
  _load_state
  _default_cluster_env
  payload=$(<"$file")
  _validate_registration_payload "$payload" >/dev/null
  log "registration payload is bound to cluster=$CLUSTER member=$X and canonical dstack_register calldata"
}

verify_http() {
  _load_state
  local host i body status min_clusters attempts
  host="$(_app_host_prefix)-9090.${GATEWAY_DOMAIN}"
  min_clusters="${INDEXER_MIN_CLUSTER_COUNT:-}"
  if [ -z "$min_clusters" ]; then
    min_clusters="$(_current_registry_cluster_count || true)"
    min_clusters="${min_clusters:-1}"
  fi
  # A restarted blue/green color rebuilds its volatile factory + cluster read
  # model before opening gRPC. Keep the deployment gate alive for that pre-warm.
  attempts="${INDEXER_VERIFY_ATTEMPTS:-180}"
  for i in $(seq 1 "$attempts"); do
    body=$(curl -fsm 8 "https://${host}/mesh/health" 2>/dev/null || true)
    status=$(curl -fsm 8 "https://${host}/status" 2>/dev/null || true)
    if [ -n "$body" ] && echo "$status" | jq -e \
      --argjson min "$min_clusters" \
      '.health.ok == true and .readModel.clusterCount >= $min and (.readModel.atBlock | type == "number")' \
      >/dev/null 2>&1; then
      log "✔ indexer caught up with a populated read model: https://${host}/status"
      printf '%s\n' "$body" | jq '{clusterCount, memberCount, atBlock}' 2>/dev/null || true
      return 0
    fi
    log "… indexer HTTP/read model not ready ($i/$attempts)"
    sleep 10
  done
  die "indexer did not become healthy with at least $min_clusters indexed cluster(s)"
}

verify_registry() {
  log "registry current(): $(cast call "$REGISTRY" 'current()(string,bytes32,bytes32,uint64)' --rpc-url "$RPC_URL")"
}

generic() {
  _default_cluster_env
  "$HERE/generic-node.sh" "$NODE" "$1"
}

candidate() {
  generic preflight
  generic deploy
  generic prime
  generic bind
  generic start
  register_member_direct
  generic verify
  verify_http
}

log "=== C3 AttestMesh indexer member: $NODE ==="
case "$ACTION" in
  preflight|deploy|prime|bind|start|verify|update|stop) generic "$ACTION" ;;
  register-member-direct) register_member_direct ;;
  validate-registration-payload) validate_registration_file "$ACTION_ARG" ;;
  register) register_indexer ;;
  verify-http) verify_http ;;
  verify-registry) verify_registry ;;
  candidate) candidate ;;
  all)
    candidate
    register_indexer
    verify_registry
    ;;
  setup)
    generic deploy
    generic prime
    generic bind
    generic start
    register_member_direct
    generic verify
    register_indexer
    verify_registry
    ;;
  *) die "usage: indexer-member-node.sh [name] [preflight|deploy|prime|bind|start|verify|update|stop|register-member-direct|validate-registration-payload|register|verify-http|verify-registry|candidate|setup|all] [payload.json]" ;;
esac
