#!/usr/bin/env bash
# Deploy one worker in the dedicated Stage-A Indexer signer cluster.
#
# This wrapper intentionally has no Matrix-cluster fallback and no action that writes
# IndexerRegistry. Safe-owned app-id admission is a human approval boundary:
# `prepare` emits exact Safe transaction JSON, then `finish` waits for that transaction
# before binding, starting, registering, and validating the warmed candidate.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"

NODE="${1:?usage: indexer-ha-replica-node.sh <replica-name> <action>}"
ACTION="${2:-prepare}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/indexer-ha-replica-node.yaml}"
CLUSTER_STATE="${INDEXER_HA_CLUSTER_STATE:-$LOGDIR/indexer-ha-cluster.state}"
REPLICA_STATE="$LOGDIR/generic-node-${NODE}.state"
SAFE_PAYLOAD="$LOGDIR/indexer-ha-safe-admission-${NODE}.json"
MEASURED_COMPOSE_NAME="${INDEXER_HA_COMPOSE_NAME:-attestmesh-indexer-ha-replica}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
ZERO_ADDRESS=0x0000000000000000000000000000000000000000
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000
ENTRY_POINT_V07=0x0000000071727De22E5E9d8BAf0edAc6f37da032
DSTACK_REGISTER_SELECTOR=0x537d491c
STAGE_A_SIDECAR_DIGEST=cc8aaa13ae356de28f2e02df777adc56754a9b52e7b81e718e755969e8804943
STAGE_A_INDEXER_DIGEST=0d7cbbdb049e1c7606d169ea69f890cf05d3384bc1777c5dac3e1237ebaaa68c
STAGE_A_DSTACK_FACET_CODEHASH=0x91c3c31fabe7d7c55924bd46873bcb46960c1e5db5fb1e322fc8fb2f1ad76563
STAGE_A_MEMBER_IMPL_CODEHASH=0xadc979a69cd23526776858fefe6ed0c9e143e7ffbe2f81d715b2ea84468f0f22

export COMPOSE GATEWAY_DOMAIN
export BOX_COMPOSE_NAME="$MEASURED_COMPOSE_NAME"
export BOX_VCPU="${BOX_VCPU:-4}" BOX_MEM="${BOX_MEM:-8192}" BOX_DISK="${BOX_DISK:-80}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}"
export BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

[[ "$NODE" =~ ^[a-zA-Z0-9._-]+$ ]] || die "replica name contains unsupported characters"
[[ "$MEASURED_COMPOSE_NAME" =~ ^[a-zA-Z0-9._-]+$ ]] \
  || die "INDEXER_HA_COMPOSE_NAME contains unsupported characters"

_tools() {
  local tool
  for tool in cast curl jq ssh scp; do
    command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
  done
}

_require_reviewed_images() {
  local sidecar_count indexer_count
  [ -s "$COMPOSE" ] || die "missing compose file: $COMPOSE"
  sidecar_count=$(grep -Ec \
    "^[[:space:]]*image:[[:space:]]*ghcr.io/attestmesh/cluster-mesh-agent@sha256:${STAGE_A_SIDECAR_DIGEST}([[:space:]]|$)" \
    "$COMPOSE" || true)
  indexer_count=$(grep -Ec \
    "^[[:space:]]*image:[[:space:]]*ghcr.io/attestmesh/attestmesh-indexer@sha256:${STAGE_A_INDEXER_DIGEST}([[:space:]]|$)" \
    "$COMPOSE" || true)
  [ "$sidecar_count" = 1 ] && [ "$indexer_count" = 2 ] \
    || die "compose must use the reviewed Stage-A sidecar and Indexer OCI digests before cluster state, deploy, admission, or start"
}

_address() {
  local label="$1" value="$2"
  [[ "$value" =~ ^0x[0-9a-fA-F]{40}$ ]] || die "$label must be a 20-byte 0x address"
  [ "${value,,}" != "$ZERO_ADDRESS" ] || die "$label must be nonzero"
}

_bytes32() {
  local label="$1" value="$2"
  [[ "$value" =~ ^0x[0-9a-fA-F]{64}$ ]] || die "$label must be a 32-byte 0x value"
  [ "${value,,}" != "$ZERO32" ] || die "$label must be nonzero"
}

_state_value() {
  local file="$1" key="$2"
  sed -n "s/^${key}=//p" "$file" 2>/dev/null | tail -1
}

_validate_cluster_values() {
  require CHAIN_ID RPC_URL CLUSTER DSTACK_FACET MEMBER_IMPL INDEXER_COMPOSE_HASH INDEXER_CLUSTER_OWNER KMS_ROOT
  [[ "$CHAIN_ID" =~ ^[1-9][0-9]*$ ]] || die "CHAIN_ID must be a positive decimal integer"
  _address CLUSTER "$CLUSTER"
  _address DSTACK_FACET "$DSTACK_FACET"
  _address MEMBER_IMPL "$MEMBER_IMPL"
  _address INDEXER_CLUSTER_OWNER "$INDEXER_CLUSTER_OWNER"
  _address KMS_ROOT "$KMS_ROOT"
  _bytes32 INDEXER_COMPOSE_HASH "$INDEXER_COMPOSE_HASH"
  [[ "$MEASURED_COMPOSE_NAME" =~ ^[a-zA-Z0-9._-]+$ ]] \
    || die "INDEXER_HA_COMPOSE_NAME contains unsupported characters"
}

_verify_safe_owner() {
  local code threshold
  code=$(cast code "$INDEXER_CLUSTER_OWNER" --rpc-url "$RPC_URL") \
    || die "cannot read INDEXER_CLUSTER_OWNER code"
  [ "$code" != 0x ] || die "INDEXER_CLUSTER_OWNER must be a deployed Safe contract, not an EOA"
  threshold=$(cast call "$INDEXER_CLUSTER_OWNER" 'getThreshold()(uint256)' \
    --rpc-url "$RPC_URL" 2>/dev/null) \
    || die "INDEXER_CLUSTER_OWNER does not expose Safe getThreshold()"
  [[ "$threshold" =~ ^[0-9]+$ ]] && [ "$threshold" -gt 0 ] \
    || die "INDEXER_CLUSTER_OWNER returned an invalid Safe threshold"
}

_verify_cluster_policy() {
  local actual_owner solidstate_owner actual_dstack_facet main_cluster chain accept_calldata
  local dstack_codehash member_codehash
  _validate_cluster_values
  chain=$(cast chain-id --rpc-url "$RPC_URL") || die "RPC_URL is unavailable"
  [ "$chain" = "$CHAIN_ID" ] || die "RPC chain $chain does not match CHAIN_ID=$CHAIN_ID"
  cast code "$CLUSTER" --rpc-url "$RPC_URL" | grep -Eq '^0x[0-9a-fA-F]{4,}$' \
    || die "dedicated CLUSTER has no code"
  cast code "$DSTACK_FACET" --rpc-url "$RPC_URL" | grep -Eq '^0x[0-9a-fA-F]{4,}$' \
    || die "DSTACK_FACET has no code"
  cast code "$MEMBER_IMPL" --rpc-url "$RPC_URL" | grep -Eq '^0x[0-9a-fA-F]{4,}$' \
    || die "MEMBER_IMPL has no code"
  dstack_codehash=$(cast codehash "$DSTACK_FACET" --rpc-url "$RPC_URL") \
    || die "cannot read DSTACK_FACET runtime code hash"
  [ "${dstack_codehash,,}" = "$STAGE_A_DSTACK_FACET_CODEHASH" ] \
    || die "DSTACK_FACET runtime code hash $dstack_codehash is not the reviewed Stage-A build"
  member_codehash=$(cast codehash "$MEMBER_IMPL" --rpc-url "$RPC_URL") \
    || die "cannot read MEMBER_IMPL runtime code hash"
  [ "${member_codehash,,}" = "$STAGE_A_MEMBER_IMPL_CODEHASH" ] \
    || die "MEMBER_IMPL runtime code hash $member_codehash is not the reviewed Stage-A build"
  actual_dstack_facet=$(cast call "$CLUSTER" 'facetAddress(bytes4)(address)' \
    "$DSTACK_REGISTER_SELECTOR" --rpc-url "$RPC_URL") \
    || die "CLUSTER does not expose ERC-2535 facetAddress(bytes4)"
  [ "${actual_dstack_facet,,}" = "${DSTACK_FACET,,}" ] \
    || die "CLUSTER dstack_register facet $actual_dstack_facet does not match prepared Path-A DSTACK_FACET"
  actual_owner=$(cast call "$CLUSTER" 'clusterOwner()(address)' --rpc-url "$RPC_URL") \
    || die "CLUSTER does not expose clusterOwner()"
  [ "${actual_owner,,}" = "${INDEXER_CLUSTER_OWNER,,}" ] \
    || die "cluster owner $actual_owner does not match INDEXER_CLUSTER_OWNER"
  solidstate_owner=$(cast call "$CLUSTER" 'owner()(address)' --rpc-url "$RPC_URL") \
    || die "CLUSTER does not expose SolidState owner()"
  if [ "${solidstate_owner,,}" != "${INDEXER_CLUSTER_OWNER,,}" ]; then
    accept_calldata=$(cast calldata 'acceptOwnership()')
    printf 'SAFE_ADDRESS=%s\nSAFE_TARGET=%s\nSAFE_VALUE=0\nSAFE_CALLDATA=%s\n' \
      "$INDEXER_CLUSTER_OWNER" "$CLUSTER" "$accept_calldata" >&2
    die "Safe must execute acceptOwnership() on the dedicated CLUSTER before save-cluster-state or replica preparation"
  fi
  [ "$(cast call "$CLUSTER" 'allowAnyDevice()(bool)' --rpc-url "$RPC_URL")" = false ] \
    || die "dedicated signer cluster must set allowAnyDevice=false"
  [ "$(cast call "$CLUSTER" 'requireTcbUpToDate()(bool)' --rpc-url "$RPC_URL")" = true ] \
    || die "dedicated signer cluster must require an up-to-date TCB"
  [ "$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' \
      "$INDEXER_COMPOSE_HASH" --rpc-url "$RPC_URL")" = true ] \
    || die "INDEXER_COMPOSE_HASH is not allowlisted on the dedicated cluster"
  [ "$(cast call "$CLUSTER" 'allowedKmsRoots(address)(bool)' \
      "$KMS_ROOT" --rpc-url "$RPC_URL")" = true ] \
    || die "KMS_ROOT is not allowlisted on the dedicated cluster"
  if [ -f "$MATRIX_STATE" ]; then
    main_cluster="$(_state_value "$MATRIX_STATE" CLUSTER)"
    if [ -n "$main_cluster" ] && [ "${main_cluster,,}" = "${CLUSTER,,}" ]; then
      die "dedicated Indexer CLUSTER must not be the Matrix/general C3 cluster"
    fi
  fi
  _verify_safe_owner
}

save_cluster_state() {
  _tools
  _require_reviewed_images
  _verify_cluster_policy
  verify_rendered_hash
  if [ -e "$CLUSTER_STATE" ] && [ "${FORCE:-0}" != 1 ]; then
    die "$CLUSTER_STATE already exists; set FORCE=1 only for an intentional replacement"
  fi
  umask 077
  mkdir -p "$(dirname "$CLUSTER_STATE")"
  local tmp="${CLUSTER_STATE}.tmp.$$"
  cat >"$tmp" <<EOF
UPDATED_AT=$(ts)
CHAIN_ID=$CHAIN_ID
CLUSTER=$CLUSTER
DSTACK_FACET=$DSTACK_FACET
MEMBER_IMPL=$MEMBER_IMPL
INDEXER_COMPOSE_HASH=${INDEXER_COMPOSE_HASH,,}
INDEXER_CLUSTER_OWNER=$INDEXER_CLUSTER_OWNER
KMS_ROOT=$KMS_ROOT
MEASURED_COMPOSE_NAME=$MEASURED_COMPOSE_NAME
EOF
  mv "$tmp" "$CLUSTER_STATE"
  log "saved dedicated Indexer cluster state -> $CLUSTER_STATE"
}

_load_cluster_state() {
  [ -f "$CLUSTER_STATE" ] \
    || die "missing dedicated state $CLUSTER_STATE; run save-cluster-state with explicit values"
  local line key value required
  declare -A state=() seen=()
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || die "blank line in dedicated cluster state: $CLUSTER_STATE"
    [[ "$line" == *=* ]] || die "malformed line in dedicated cluster state: $CLUSTER_STATE"
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      UPDATED_AT|CHAIN_ID|CLUSTER|DSTACK_FACET|MEMBER_IMPL|INDEXER_COMPOSE_HASH|INDEXER_CLUSTER_OWNER|KMS_ROOT|MEASURED_COMPOSE_NAME) ;;
      *) die "unknown dedicated cluster state field '$key' in $CLUSTER_STATE" ;;
    esac
    [ -z "${seen[$key]+present}" ] \
      || die "duplicate dedicated cluster state field '$key' in $CLUSTER_STATE"
    seen[$key]=1
    state[$key]="$value"
  done <"$CLUSTER_STATE"
  for required in UPDATED_AT CHAIN_ID CLUSTER DSTACK_FACET MEMBER_IMPL INDEXER_COMPOSE_HASH INDEXER_CLUSTER_OWNER KMS_ROOT MEASURED_COMPOSE_NAME; do
    [ -n "${seen[$required]+present}" ] \
      || die "dedicated cluster state is missing $required: $CLUSTER_STATE"
  done
  [[ "${state[UPDATED_AT]}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
    || die "dedicated cluster state UPDATED_AT is malformed"
  CHAIN_ID="${state[CHAIN_ID]}"
  CLUSTER="${state[CLUSTER]}"
  DSTACK_FACET="${state[DSTACK_FACET]}"
  MEMBER_IMPL="${state[MEMBER_IMPL]}"
  INDEXER_COMPOSE_HASH="${state[INDEXER_COMPOSE_HASH]}"
  INDEXER_CLUSTER_OWNER="${state[INDEXER_CLUSTER_OWNER]}"
  KMS_ROOT="${state[KMS_ROOT]}"
  MEASURED_COMPOSE_NAME="${state[MEASURED_COMPOSE_NAME]}"
  [ "${MEASURED_COMPOSE_NAME:-}" = "$BOX_COMPOSE_NAME" ] \
    || die "cluster state compose name differs from INDEXER_HA_COMPOSE_NAME=$BOX_COMPOSE_NAME"
  _validate_cluster_values
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$(
    jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json"
  )}"
  require INDEXER_REGISTRY_ADDR GATEWAY_DOMAIN
  _address INDEXER_REGISTRY_ADDR "$INDEXER_REGISTRY_ADDR"
  export CHAIN_ID CLUSTER DSTACK_FACET MEMBER_IMPL INDEXER_COMPOSE_HASH INDEXER_CLUSTER_OWNER KMS_ROOT
  export INDEXER_REGISTRY_ADDR GATEWAY_DOMAIN
}

_runtime_env() {
  require PRIVATE_KEY DEPLOYER_ADDR RPC_URL GAS_POLICY_ID
  [ -n "${GAS_POLICY_ID//[[:space:]]/}" ] || die "GAS_POLICY_ID must not be blank"
  local rpc="${RPC_URL%/}" guest_rpc="${CVM_RPC_URL:-$RPC_URL}" bundler_compare
  GUEST_BUNDLER_URL="${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
  bundler_compare="${GUEST_BUNDLER_URL%/}"
  guest_rpc="${guest_rpc%/}"
  [ -n "$GUEST_BUNDLER_URL" ] || die "CVM_BUNDLER_URL/BUNDLER_URL must not be blank"
  [ "$bundler_compare" != "$rpc" ] || die "sealed guest BUNDLER_URL must not equal RPC_URL"
  [ "$bundler_compare" != "$guest_rpc" ] \
    || die "sealed guest BUNDLER_URL must not equal CVM_RPC_URL"
  export GUEST_BUNDLER_URL
}

_bundler_rpc() {
  local method="$1"
  curl -fsS --max-time 15 -H 'content-type: application/json' \
    --data "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$method\",\"params\":[]}" \
    "$GUEST_BUNDLER_URL"
}

preflight_bundler() {
  local entries chain expected
  _runtime_env
  entries="$(_bundler_rpc eth_supportedEntryPoints)" \
    || die "sealed guest BUNDLER_URL did not answer eth_supportedEntryPoints"
  echo "$entries" | jq -e --arg entry "${ENTRY_POINT_V07,,}" \
    '.result
     | type == "array"
       and length > 0
       and all(.[]; test("^0x[0-9a-fA-F]{40}$"))
       and (map(ascii_downcase) | index($entry) != null)' \
    >/dev/null \
    || die "sealed guest BUNDLER_URL does not support the sidecar's canonical v0.7 EntryPoint $ENTRY_POINT_V07"
  chain="$(_bundler_rpc eth_chainId)" || die "sealed guest BUNDLER_URL did not answer eth_chainId"
  chain=$(echo "$chain" | jq -er '.result | select(type == "string")') \
    || die "sealed guest BUNDLER_URL returned an invalid chain id"
  expected=$(printf '0x%x' "$CHAIN_ID")
  [ "${chain,,}" = "$expected" ] \
    || die "sealed guest BUNDLER_URL chain id $chain does not match CHAIN_ID=$CHAIN_ID"
  log "ERC-4337 bundler preflight passed (canonical v0.7 entry point + chain id; gas policy present)"
}

preflight_guest_rpc() {
  local guest_rpc="${CVM_RPC_URL:-$RPC_URL}" chain
  chain=$(cast chain-id --rpc-url "$guest_rpc") \
    || die "sealed CVM_RPC_URL is unavailable"
  [ "$chain" = "$CHAIN_ID" ] \
    || die "sealed CVM_RPC_URL chain $chain does not match CHAIN_ID=$CHAIN_ID"
  log "sealed guest RPC preflight passed (chain id $chain)"
}

_generic() {
  local action="$1" allow_gateway_drift=0
  case "$action" in
    cleanup|stop) allow_gateway_drift=1 ;;
  esac
  COMPOSE="$COMPOSE" BOX_COMPOSE_NAME="$BOX_COMPOSE_NAME" \
    ALLOW_GENERIC_GATEWAY_DRIFT="$allow_gateway_drift" \
    REQUIRE_GUEST_CONFIG_FINGERPRINT=1 STRICT_GENERIC_STATE_BINDINGS=1 \
    "$HERE/generic-node.sh" "$NODE" "$action"
}

rendered_hash() {
  require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR
  local hash
  hash=$(_generic hash | grep -oE '^[0-9a-fA-F]{64}$' | tail -1)
  [ -n "$hash" ] || die "could not render the shared replica compose hash"
  printf '0x%s\n' "${hash,,}"
}

verify_rendered_hash() {
  local actual
  actual=$(rendered_hash)
  [ "$actual" = "${INDEXER_COMPOSE_HASH,,}" ] \
    || die "rendered compose hash $actual does not match signer-cluster hash $INDEXER_COMPOSE_HASH"
  log "rendered replica hash matches signer cluster: $actual"
}

preflight() {
  _tools
  _load_cluster_state
  _runtime_env
  _require_reviewed_images
  preflight_guest_rpc
  preflight_bundler
  _verify_cluster_policy
  verify_rendered_hash
  _generic preflight
}

_load_replica_state() {
  local drift_mode="${1:-enforce-config}"
  local expected_cluster="$CLUSTER" expected_impl="$MEMBER_IMPL"
  local expected_kms="$KMS_ROOT" expected_hash="${INDEXER_COMPOSE_HASH,,}"
  local actual_hash current_guest_hash line key value required
  declare -A state=() seen=()
  [ -f "$REPLICA_STATE" ] || die "missing replica state $REPLICA_STATE; run deploy first"
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || die "blank line in replica state: $REPLICA_STATE"
    [[ "$line" == *=* ]] || die "malformed line in replica state: $REPLICA_STATE"
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      STATE_SCHEMA|UPDATED_AT|STATE_PHASE|X|H|VM_ID|CLUSTER|MEMBER_IMPL|KMS_ROOT|GATEWAY_DOMAIN|GUEST_CONFIG_SHA256) ;;
      *) die "unknown replica state field '$key' in $REPLICA_STATE" ;;
    esac
    [ -z "${seen[$key]+present}" ] || die "duplicate replica state field '$key' in $REPLICA_STATE"
    seen[$key]=1
    state[$key]="$value"
  done <"$REPLICA_STATE"
  for required in STATE_SCHEMA UPDATED_AT STATE_PHASE X H VM_ID CLUSTER MEMBER_IMPL KMS_ROOT GATEWAY_DOMAIN GUEST_CONFIG_SHA256; do
    [ -n "${seen[$required]+present}" ] || die "replica state is missing $required: $REPLICA_STATE"
  done
  [ "${state[STATE_SCHEMA]}" = 2 ] || die "replica state must use schema 2"
  [[ "${state[UPDATED_AT]}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
    || die "replica state UPDATED_AT is malformed"
  case "${state[STATE_PHASE]}" in
    preflighted|deployed-stopped|primed|bound|started|registered|cleaned) ;;
    *) die "replica state STATE_PHASE is invalid: ${state[STATE_PHASE]}" ;;
  esac
  X="${state[X]}"
  H="${state[H]}"
  VM_ID="${state[VM_ID]}"
  _address X "$X"
  _bytes32 H "0x${H#0x}"
  [[ "$VM_ID" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$ ]] \
    || die "replica state VM_ID contains unsupported characters"
  [[ "${state[GATEWAY_DOMAIN]}" =~ ^[a-zA-Z0-9._:-]+$ ]] \
    || die "replica state GATEWAY_DOMAIN contains unsupported characters"
  [ "${state[CLUSTER],,}" = "${expected_cluster,,}" ] \
    || die "replica state belongs to cluster ${state[CLUSTER]}, expected $expected_cluster"
  [ "${state[MEMBER_IMPL],,}" = "${expected_impl,,}" ] \
    || die "replica state MEMBER_IMPL differs from dedicated cluster state"
  [ "${state[KMS_ROOT],,}" = "${expected_kms,,}" ] \
    || die "replica state KMS_ROOT differs from dedicated cluster state"
  if [ "$drift_mode" != allow-config-drift ]; then
    [ "${state[GATEWAY_DOMAIN]}" = "$GATEWAY_DOMAIN" ] \
      || die "replica state GATEWAY_DOMAIN differs from the requested gateway"
  fi
  actual_hash="0x${H#0x}"
  actual_hash="${actual_hash,,}"
  [ "$actual_hash" = "$expected_hash" ] \
    || die "replica compose hash $actual_hash differs from dedicated cluster state"
  [[ "${state[GUEST_CONFIG_SHA256]}" =~ ^[0-9a-fA-F]{64}$ ]] \
    || die "replica state GUEST_CONFIG_SHA256 is malformed"
  if [ "$drift_mode" != allow-config-drift ]; then
    current_guest_hash=$(_generic guest-config-sha256 | grep -oE '^[0-9a-fA-F]{64}$' | tail -1)
    [ -n "$current_guest_hash" ] || die "could not fingerprint the current sealed guest configuration"
    [ "${state[GUEST_CONFIG_SHA256],,}" = "${current_guest_hash,,}" ] \
      || die "sealed guest configuration drifted since replica deployment; redeploy instead of continuing"
  fi
  H="${H#0x}"
  INDEXER_COMPOSE_HASH="$expected_hash"
}

deploy_replica() {
  preflight
  _generic deploy
  # generic-node persists CLUSTER/H/VM_ID under the unique NODE name; LB named
  # backend resolution consumes that exact state without any Matrix fallback.
  _load_replica_state
  log "deployed stopped shared replica app=$X vm=$VM_ID measured_name=$BOX_COMPOSE_NAME"
}

safe_admission() {
  _load_cluster_state
  _require_reviewed_images
  _verify_cluster_policy
  local dedicated_cluster="$CLUSTER" calldata created
  _load_replica_state
  calldata=$(cast calldata 'addAllowedAppId(address)' "$X")
  created=$(( $(date +%s) * 1000 ))
  umask 077
  jq -n \
    --arg version "1.0" \
    --arg chainId "$CHAIN_ID" \
    --argjson createdAt "$created" \
    --arg safe "$INDEXER_CLUSTER_OWNER" \
    --arg name "Admit $NODE to dedicated Indexer signer cluster" \
    --arg to "$dedicated_cluster" \
    --arg data "$calldata" \
    --arg appId "$X" \
    --arg composeHash "0x${H#0x}" \
    '{version:$version, chainId:$chainId, createdAt:$createdAt,
      meta:{name:$name,description:"Safe approval required before the replica may boot",
            txBuilderVersion:"1.18.0",createdFromSafeAddress:$safe,
            checksum:"",appId:$appId,composeHash:$composeHash},
      transactions:[{to:$to,value:"0",data:$data,
                     contractMethod:null,contractInputsValues:null}]}' \
    >"$SAFE_PAYLOAD"
  printf 'SAFE_ADDRESS=%s\nSAFE_TARGET=%s\nSAFE_VALUE=0\nSAFE_CALLDATA=%s\nSAFE_JSON=%s\n' \
    "$INDEXER_CLUSTER_OWNER" "$dedicated_cluster" "$calldata" "$SAFE_PAYLOAD"
  log "Safe-ready app-id admission emitted; do not start $NODE before it is executed"
}

wait_admission() {
  _load_cluster_state
  local dedicated_cluster="$CLUSTER" attempts="${INDEXER_ADMISSION_ATTEMPTS:-180}" i allowed
  _load_replica_state
  [[ "$attempts" =~ ^[0-9]+$ ]] && [ "$attempts" -gt 0 ] \
    || die "INDEXER_ADMISSION_ATTEMPTS must be a positive integer"
  for i in $(seq 1 "$attempts"); do
    allowed=$(cast call "$dedicated_cluster" 'allowedAppIds(address)(bool)' "$X" \
      --rpc-url "$RPC_URL" 2>/dev/null || true)
    if [ "$allowed" = true ]; then
      _verify_cluster_policy
      log "Safe admission verified on-chain for $NODE app=$X"
      return 0
    fi
    log "waiting for Safe app-id admission ($i/$attempts)"
    sleep 5
  done
  die "Safe did not admit app $X to cluster $dedicated_cluster"
}

bind_replica() {
  _load_cluster_state
  local dedicated_cluster="$CLUSTER" current
  _load_replica_state
  wait_admission
  current=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null || true)
  if [ "${current,,}" = "${dedicated_cluster,,}" ]; then
    log "replica app is already bound to dedicated cluster"
    return 0
  fi
  _generic bind
}

start_replica() {
  _load_cluster_state
  local dedicated_cluster="$CLUSTER" current
  _load_replica_state
  _runtime_env
  _require_reviewed_images
  preflight_guest_rpc
  preflight_bundler
  wait_admission
  current=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null || true)
  [ "${current,,}" = "${dedicated_cluster,,}" ] \
    || die "replica app is not bound to the dedicated cluster; run bind first"
  _generic start
}

register_direct() {
  _load_cluster_state
  _load_replica_state
  _generic register-direct
}

verify_member() {
  _load_cluster_state
  _load_replica_state
  _generic verify
}

verify_candidate() {
  _load_cluster_state
  local dedicated_cluster="$CLUSTER" expected_hash expected_member host attempts min_clusters i status=""
  _load_replica_state
  expected_hash="0x${H#0x}"
  expected_member=$(cast call "$dedicated_cluster" 'memberIdOf(address)(bytes32)' "$X" \
    --rpc-url "$RPC_URL") || die "cannot resolve serving member id"
  _bytes32 servingMemberId "$expected_member"
  host="$(printf '%s' "${X#0x}" | tr '[:upper:]' '[:lower:]')-9090.${GATEWAY_DOMAIN}"
  attempts="${INDEXER_VERIFY_ATTEMPTS:-180}"
  min_clusters="${INDEXER_MIN_CLUSTER_COUNT:-1}"
  [[ "$attempts" =~ ^[0-9]+$ ]] && [ "$attempts" -gt 0 ] \
    || die "INDEXER_VERIFY_ATTEMPTS must be a positive integer"
  [[ "$min_clusters" =~ ^[0-9]+$ ]] && [ "$min_clusters" -gt 0 ] \
    || die "INDEXER_MIN_CLUSTER_COUNT must be a positive integer"
  for i in $(seq 1 "$attempts"); do
    status=$(curl -fsm 10 "https://${host}/status" 2>/dev/null || true)
    if echo "$status" | jq -e \
      --arg code "${expected_hash,,}" \
      --arg cluster "${dedicated_cluster,,}" \
      --arg member "${expected_member,,}" \
      --argjson chain "$CHAIN_ID" \
      --argjson min "$min_clusters" '
        .chainId == $chain
        and .identityMode == "cluster-shared"
        and ((.codeId // "") | ascii_downcase) == $code
        and ((.indexerCluster // "") | ascii_downcase) == $cluster
        and ((.servingMemberId // "") | ascii_downcase) == $member
        and ((.pubKey // "") | test("^0x[0-9a-fA-F]{64}$"))
        and .pubKey != "0x0000000000000000000000000000000000000000000000000000000000000000"
        and .health.rpcReachable == true
        and (.health.grpcAccepting | type == "boolean")
        and (.health.chainHeadLagBlocks | type == "number")
        and .health.chainHeadLagBlocks >= 0
        and .health.chainHeadLagBlocks < 10
        and (.readModel.atBlock | type == "number")
        and (.readModel.clusterCount | type == "number")
        and .readModel.clusterCount >= $min
        and (.readModel.memberCount | type == "number")
        and .readModel.memberCount > 0
      ' >/dev/null 2>&1; then
      echo "$status" | jq \
        '{pubKey,codeId,identityMode,indexerCluster,servingMemberId,health,readModel}'
      log "warmed shared candidate verified (grpcAccepting may remain false until registry rotation)"
      return 0
    fi
    log "waiting for exact shared identity and caught-up read model ($i/$attempts)"
    sleep 10
  done
  die "shared candidate failed exact identity/readiness validation: ${status:-no status}"
}

_assert_lb_drained() {
  local operation="${INDEXER_LB_ACTIVE_OPERATION_ID:-}" proof
  [ -n "${INDEXER_LB_NODE:-}" ] \
    || die "registered worker stop requires explicit INDEXER_LB_NODE"
  [[ "$INDEXER_LB_NODE" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$ ]] \
    || die "INDEXER_LB_NODE must be a safe node name of at most 128 characters"
  [[ "$operation" =~ ^[0-9a-f]{64}$ ]] \
    || die "registered worker stop requires INDEXER_LB_ACTIVE_OPERATION_ID as exactly 64 lowercase hex characters"
  proof=$(INDEXER_LB_ACTIVE_OPERATION_ID="$operation" \
    "$HERE/indexer-lb-node.sh" "$INDEXER_LB_NODE" assert-drained "$NODE") \
    || die "authenticated Indexer LB drain proof failed; refusing to stop registered worker $NODE"
  echo "$proof" | jq -e --arg operation "$operation" '
    .drained == true
    and .operation_id == $operation
    and (.active_backends | type == "array")
  ' >/dev/null \
    || die "Indexer LB returned an invalid or mismatched drain proof; refusing to stop registered worker $NODE"
  log "authenticated LB drain proof accepted for worker=$NODE active_operation_id=$operation"
}

stop_replica() {
  _load_cluster_state
  _load_replica_state allow-config-drift
  local member_id
  member_id=$(cast call "$CLUSTER" 'memberIdOf(address)(bytes32)' "$X" \
    --rpc-url "$RPC_URL") || die "cannot verify worker membership before stop"
  [[ "$member_id" =~ ^0x[0-9a-fA-F]{64}$ ]] \
    || die "worker membership query returned a malformed member id"
  if [ "${member_id,,}" != "$ZERO32" ]; then
    _assert_lb_drained
  else
    log "worker never registered and cannot have opened shared gRPC; LB drain confirmation is not required"
  fi
  # The dedicated cluster is Safe-owned. Generic cleanup is used only for its
  # idempotent VM stop and is forbidden from attempting an EOA allowlist write.
  FORCE_CLEANUP=1 SKIP_APP_ALLOWLIST_CLEANUP=1 _generic stop
  log "worker VM is proven stopped or gone; Safe app admission remains unchanged"
}

bootstrap_help() {
  cat <<EOF
Dedicated cluster handoff (state path: $CLUSTER_STATE):
  1. Compute INDEXER_COMPOSE_HASH with: $0 $NODE hash
  2. Run deploy/onchain.sh indexer-cluster and capture its "Cluster deployed:" address.
  3. Have INDEXER_CLUSTER_OWNER execute acceptOwnership() on that cluster.
  4. Prepare the exact Safe-owned Path-A cut (this only deploys implementations):
       deploy/onchain.sh patha-safe-prepare "\$CLUSTER" "\$INDEXER_CLUSTER_OWNER"
     Review and execute the bundle's target/value/calldata through the Safe, then extract:
       PATHA_BUNDLE="contracts/script/deployments/\${CHAIN_ID}-patha-safe-\${CLUSTER,,}.json"
       DSTACK_FACET=\$(jq -r .dstackFacet "\$PATHA_BUNDLE")
       MEMBER_IMPL=\$(jq -r .clusterMemberImplementation "\$PATHA_BUNDLE")
  5. Persist the exact handoff with:
     CLUSTER=<Cluster-deployed-address> DSTACK_FACET=<new-DstackFacet> \\
     MEMBER_IMPL=<new-ClusterMember-impl> \\
     INDEXER_COMPOSE_HASH=<hash> INDEXER_CLUSTER_OWNER=<Safe> KMS_ROOT=<root> \\
     $0 $NODE save-cluster-state

The persisted schema is CHAIN_ID, CLUSTER, DSTACK_FACET, MEMBER_IMPL,
INDEXER_COMPOSE_HASH, INDEXER_CLUSTER_OWNER, KMS_ROOT, and
MEASURED_COMPOSE_NAME. Replica actions never fall back to Matrix state, and
verify that dstack_register resolves to the exact persisted Path-A facet.
EOF
}

prepare() {
  preflight
  local vm
  vm="$(_state_value "$REPLICA_STATE" VM_ID)"
  if [ -n "$vm" ]; then
    _load_replica_state
    log "reusing existing stopped replica state vm=$VM_ID"
  else
    _generic deploy
    _load_replica_state
  fi
  safe_admission
}

finish() {
  _load_cluster_state
  _runtime_env
  preflight_bundler
  bind_replica
  start_replica
  register_direct
  verify_member
  verify_candidate
  log "shared Indexer candidate ready; IndexerRegistry was not modified"
}

log "=== dedicated Indexer HA replica: $NODE action=$ACTION ==="
case "$ACTION" in
  hash) rendered_hash ;;
  save-cluster-state) save_cluster_state ;;
  verify-cluster) _tools; _load_cluster_state; _verify_cluster_policy ;;
  preflight) preflight ;;
  deploy) deploy_replica ;;
  safe-admission) safe_admission ;;
  wait-admission) wait_admission ;;
  bind) bind_replica ;;
  start) start_replica ;;
  register-direct) register_direct ;;
  verify-member) verify_member ;;
  verify-candidate) verify_candidate ;;
  stop|cleanup) stop_replica ;;
  bootstrap-help|help) bootstrap_help ;;
  prepare) prepare ;;
  finish|candidate) finish ;;
  *)
    die "usage: $0 <replica-name> {hash|save-cluster-state|verify-cluster|preflight|deploy|safe-admission|wait-admission|bind|start|register-direct|verify-member|verify-candidate|stop|bootstrap-help|prepare|finish}"
    ;;
esac
