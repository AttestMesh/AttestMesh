#!/usr/bin/env bash
# Standardized on-chain deploy routine (Track A) — ordered, idempotent, logged.
# Subcommands run the specific-ordering sequence; smithers (deploy/workflows/deploy.tsx)
# sequences these as durable steps, or run directly:
#   source deploy/env.sh && deploy/onchain.sh all
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"; require PRIVATE_KEY RPC_URL CHAIN_ID KMS_ROOT DEPLOYER_ADDR

RECEIPT="$ROOT/contracts/script/deployments/${CHAIN_ID}.json"

preflight() {
  local d; d=$(cast wallet address --private-key "$PRIVATE_KEY" 2>/dev/null)
  [ "${d,,}" = "${DEPLOYER_ADDR,,}" ] || die "deployer key mismatch ($d != $DEPLOYER_ADDR)"
  log "preflight ok: deployer=$d chain=$(cast chain-id --rpc-url "$RPC_URL") balance=$(cast from-wei "$(cast balance "$d" --rpc-url "$RPC_URL")")ETH"
}

# infra: idempotent — skip if the receipt's factory already has code on-chain.
infra() {
  if [ -f "$RECEIPT" ]; then
    local f; f=$(jq -r .clusterDiamondFactory "$RECEIPT" 2>/dev/null)
    if [ -n "$f" ] && [ "$(cast code "$f" --rpc-url "$RPC_URL" 2>/dev/null)" != "0x" ]; then
      log "infra already deployed (factory $f has code) — skipping. Set FORCE=1 to redeploy."
      [ "${FORCE:-0}" = "1" ] || return 0
    fi
  fi
  run_step deploy-infra bash -c "cd '$ROOT/contracts' && forge script script/DeployInfra.s.sol:DeployInfra --rpc-url '$RPC_URL' --broadcast"
  log "infra receipt: $RECEIPT"; jq . "$RECEIPT" >&2
}

# cluster: deploy a ClusterDiamond from a generated config (KMS root + compose hash seeded).
cluster() {
  local name="${1:-attestmesh-1}"
  require COMPOSE_HASH
  export CLUSTER_FACTORY MEMBER_FACTORY CLUSTER_CONFIG
  CLUSTER_FACTORY=$(jq -r .clusterDiamondFactory "$RECEIPT"); MEMBER_FACTORY=$(jq -r .clusterMemberFactory "$RECEIPT")
  CLUSTER_CONFIG="script/clusters/${name}.json"
  local salt; salt=$(cast keccak "attestmesh-cluster-${name}")
  cat > "$ROOT/contracts/$CLUSTER_CONFIG" <<JSON
{ "clusterOwner": "$DEPLOYER_ADDR", "kmsRootSigner": "$KMS_ROOT",
  "initialComposeHashes": ["$COMPOSE_HASH"], "initialDeviceIds": [],
  "allowAnyDevice": true, "requireTcbUpToDate": false,
  "meshCidrIp": 168624128, "meshCidrPrefix": 16, "salt": "$salt" }
JSON
  run_step "deploy-cluster-${name}" bash -c "cd '$ROOT/contracts' && forge script script/DeployCluster.s.sol:DeployCluster --rpc-url '$RPC_URL' --broadcast"
}

# indexer-cluster: create the dedicated signer cluster used by the active-active
# Indexer pool. This intentionally has a stricter boot policy than the general
# development cluster helper above: the CSK becomes the fleet-wide envelope
# signing key, so admitting a device is equivalent to admitting a signer.
#
# Required env:
#   INDEXER_COMPOSE_HASH      exact rendered dstack compose hash shared by replicas
#   INDEXER_DEVICE_IDS_JSON   JSON array of explicitly approved bytes32 device ids
#   INDEXER_CLUSTER_OWNER     org Safe that owns both cluster policy surfaces
indexer_cluster() {
  local name="${1:-attestmesh-indexer-ha}"
  require INDEXER_COMPOSE_HASH INDEXER_DEVICE_IDS_JSON INDEXER_CLUSTER_OWNER
  echo "$name" | grep -Eq '^[a-zA-Z0-9._-]+$' \
    || die "indexer cluster name may contain only letters, digits, dot, underscore, and dash"
  echo "$INDEXER_COMPOSE_HASH" | grep -Eq '^0x[0-9a-fA-F]{64}$' \
    || die "INDEXER_COMPOSE_HASH must be a bytes32 hex value"
  [ "$INDEXER_COMPOSE_HASH" != "0x$(printf '0%.0s' {1..64})" ] \
    || die "INDEXER_COMPOSE_HASH must be nonzero"
  echo "$INDEXER_DEVICE_IDS_JSON" | jq -e \
    '. as $ids
     | type == "array"
       and length > 0
       and all(.[];
         test("^0x[0-9a-fA-F]{64}$")
         and ascii_downcase != ("0x" + ("0" * 64)))
       and (($ids | map(ascii_downcase) | unique | length) == ($ids | length))' \
    >/dev/null || die "INDEXER_DEVICE_IDS_JSON must be a non-empty bytes32 JSON array"
  echo "$INDEXER_CLUSTER_OWNER" | grep -Eq '^0x[0-9a-fA-F]{40}$' \
    || die "INDEXER_CLUSTER_OWNER must be an address"
  [ "${INDEXER_CLUSTER_OWNER,,}" != "0x$(printf '0%.0s' {1..40})" ] \
    || die "INDEXER_CLUSTER_OWNER must be nonzero"
  echo "$KMS_ROOT" | grep -Eq '^0x[0-9a-fA-F]{40}$' \
    || die "KMS_ROOT must be an address"
  [ "${KMS_ROOT,,}" != "0x$(printf '0%.0s' {1..40})" ] \
    || die "KMS_ROOT must be nonzero"

  export CLUSTER_FACTORY MEMBER_FACTORY CLUSTER_CONFIG
  CLUSTER_FACTORY=$(jq -r .clusterDiamondFactory "$RECEIPT")
  MEMBER_FACTORY=$(jq -r .clusterMemberFactory "$RECEIPT")
  CLUSTER_CONFIG="script/clusters/${name}.json"
  local salt; salt=$(cast keccak "attestmesh-indexer-cluster-${name}")
  jq -n \
    --arg owner "$INDEXER_CLUSTER_OWNER" \
    --arg kms "$KMS_ROOT" \
    --arg compose "$INDEXER_COMPOSE_HASH" \
    --argjson devices "$INDEXER_DEVICE_IDS_JSON" \
    --arg salt "$salt" \
    '{clusterOwner:$owner, kmsRootSigner:$kms,
      initialComposeHashes:[$compose], initialDeviceIds:$devices,
      allowAnyDevice:false, requireTcbUpToDate:true,
      meshCidrIp:169279488, meshCidrPrefix:16, salt:$salt}' \
    > "$ROOT/contracts/$CLUSTER_CONFIG"
  run_step "deploy-indexer-cluster-${name}" bash -c \
    "cd '$ROOT/contracts' && forge script script/DeployCluster.s.sol:DeployCluster --rpc-url '$RPC_URL' --broadcast" \
    || die "dedicated Indexer cluster deployment failed"
  log "dedicated Indexer cluster deployed with closed device policy; config=$CLUSTER_CONFIG"
}

# seed-appid <cluster> <memberAddr>: owner pre-approves an app_id so the KMS gate admits
# the CVM at first boot (the cold-start fix). Owner-gas (not sponsored).
seed_appid() {
  local cluster="$1" member="$2"
  send_with_nonce_retry "seed-appid-${member}" "$cluster" "addAllowedAppId(address)" "$member"
  log "allowedAppIds[$member] = $(cast call "$cluster" 'allowedAppIds(address)(bool)' "$member" --rpc-url "$RPC_URL")"
}

# patha-upgrade <cluster>: legacy EOA-owner flow. Diamond-cut the cluster's DstackFacet to the
# Path A build (dstack_register accepts owner-allowlisted app_ids) + deploy the Path A ClusterMember
# impl. Safe-owned clusters must use patha-safe-prepare below; this command cannot act as a Safe.
patha_upgrade() {
  local cluster="${1:?usage: onchain.sh patha-upgrade <cluster>}"
  export CLUSTER="$cluster"
  run_step "patha-upgrade-${cluster}" bash -c "cd '$ROOT/contracts' && forge script script/UpgradeDstackFacetPathA.s.sol:UpgradeDstackFacetPathA --rpc-url '$RPC_URL' --broadcast"
}

# patha-safe-prepare <cluster> <safe>: deploy the Path A facet/member implementation from the
# configured broadcaster, then emit an exact target/value/calldata bundle for separate Safe
# review and execution. This command NEVER submits the diamondCut and fails unless the Safe has
# already accepted the cluster's solidstate ownership.
patha_safe_prepare() {
  local cluster="${1:?usage: onchain.sh patha-safe-prepare <cluster> <safe>}"
  local safe="${2:?usage: onchain.sh patha-safe-prepare <cluster> <safe>}"
  local actual_chain accepted_owner policy_owner bundle_rel bundle_abs cluster_code safe_code
  local new_facet member_impl current_facet current_register_facet calldata_hash
  local new_facet_code member_impl_code

  echo "$cluster" | grep -Eq '^0x[0-9a-fA-F]{40}$' \
    || die "patha-safe-prepare cluster must be an address"
  echo "$safe" | grep -Eq '^0x[0-9a-fA-F]{40}$' \
    || die "patha-safe-prepare Safe must be an address"
  [ "${cluster,,}" != "0x$(printf '0%.0s' {1..40})" ] \
    || die "patha-safe-prepare cluster must be nonzero"
  [ "${safe,,}" != "0x$(printf '0%.0s' {1..40})" ] \
    || die "patha-safe-prepare Safe must be nonzero"

  actual_chain=$(cast chain-id --rpc-url "$RPC_URL") \
    || die "could not read RPC chain id"
  [ "$actual_chain" = "$CHAIN_ID" ] \
    || die "RPC chain mismatch ($actual_chain != configured $CHAIN_ID)"
  cluster_code=$(cast code "$cluster" --rpc-url "$RPC_URL") \
    || die "could not read cluster code: $cluster"
  [ "$cluster_code" != "0x" ] || die "cluster has no code: $cluster"
  safe_code=$(cast code "$safe" --rpc-url "$RPC_URL") \
    || die "could not read Safe code: $safe"
  [ "$safe_code" != "0x" ] || die "Safe has no code: $safe"

  accepted_owner=$(cast call "$cluster" 'owner()(address)' --rpc-url "$RPC_URL") \
    || die "could not read cluster solidstate owner"
  [ "${accepted_owner,,}" = "${safe,,}" ] \
    || die "Safe has not accepted solidstate ownership ($accepted_owner != $safe)"
  policy_owner=$(cast call "$cluster" 'clusterOwner()(address)' --rpc-url "$RPC_URL") \
    || die "could not read cluster policy owner"
  [ "${policy_owner,,}" = "${safe,,}" ] \
    || die "Safe does not own cluster policy ($policy_owner != $safe)"
  cast call "$safe" 'getThreshold()(uint256)' --rpc-url "$RPC_URL" >/dev/null \
    || die "expected owner does not expose the Safe threshold surface"
  cast call "$safe" 'getOwners()(address[])' --rpc-url "$RPC_URL" >/dev/null \
    || die "expected owner does not expose the Safe owners surface"

  bundle_rel="script/deployments/${CHAIN_ID}-patha-safe-${cluster,,}.json"
  bundle_abs="$ROOT/contracts/$bundle_rel"
  rm -f "$bundle_abs"
  export CLUSTER="$cluster" EXPECTED_SAFE_OWNER="$safe" PATHA_BUNDLE_FILE="$bundle_rel"

  if ! run_step "patha-safe-prepare-${cluster}" bash -c \
    "cd '$ROOT/contracts' && forge script script/PrepareDstackFacetPathASafe.s.sol:PrepareDstackFacetPathASafe --rpc-url '$RPC_URL' --broadcast --slow"; then
    rm -f "$bundle_abs"
    die "Path-A Safe preparation failed; no Safe transaction bundle was produced"
  fi

  [ -f "$bundle_abs" ] || die "Path-A Safe preparation returned without a bundle"
  jq -e --arg cluster "$cluster" --arg safe "$safe" --arg chain "$CHAIN_ID" '
      .schemaVersion == 1
      and (.chainId | tostring) == $chain
      and (.cluster | ascii_downcase) == ($cluster | ascii_downcase)
      and (.target | ascii_downcase) == ($cluster | ascii_downcase)
      and (.safeOwner | ascii_downcase) == ($safe | ascii_downcase)
      and .value == 0
      and ((.data | ascii_downcase) | test("^0x1f931c1c[0-9a-f]*$"))
      and (.calldataHash | test("^0x[0-9a-fA-F]{64}$"))
      and (.dstackFacet | test("^0x[0-9a-fA-F]{40}$"))
      and (.clusterMemberImplementation | test("^0x[0-9a-fA-F]{40}$"))
      and (.currentDstackFacet | test("^0x[0-9a-fA-F]{40}$"))
    ' "$bundle_abs" >/dev/null || die "Path-A Safe bundle failed structural validation"

  new_facet=$(jq -r .dstackFacet "$bundle_abs")
  member_impl=$(jq -r .clusterMemberImplementation "$bundle_abs")
  current_facet=$(jq -r .currentDstackFacet "$bundle_abs")
  new_facet_code=$(cast code "$new_facet" --rpc-url "$RPC_URL") \
    || die "could not read prepared DstackFacet deployment: $new_facet"
  [ "$new_facet_code" != "0x" ] \
    || die "prepared DstackFacet deployment has no code: $new_facet"
  member_impl_code=$(cast code "$member_impl" --rpc-url "$RPC_URL") \
    || die "could not read prepared ClusterMember implementation: $member_impl"
  [ "$member_impl_code" != "0x" ] \
    || die "prepared ClusterMember implementation has no code: $member_impl"
  current_register_facet=$(cast call "$cluster" 'facetAddress(bytes4)(address)' \
    0x537d491c --rpc-url "$RPC_URL") \
    || die "could not verify current dstack_register facet"
  [ "${current_register_facet,,}" = "${current_facet,,}" ] \
    || die "cluster selector topology changed while preparing the Safe bundle"
  calldata_hash=$(cast keccak "$(jq -r .data "$bundle_abs")") \
    || die "could not hash Path-A Safe calldata"
  [ "${calldata_hash,,}" = "$(jq -r '.calldataHash | ascii_downcase' "$bundle_abs")" ] \
    || die "Path-A Safe bundle calldata hash mismatch"

  log "Path-A implementations deployed; the cluster diamondCut has NOT been submitted"
  log "review the exact Safe transaction bundle: $bundle_abs"
  jq '{target, value, data, dstackFacet, clusterMemberImplementation, currentDstackFacet, calldataHash}' \
    "$bundle_abs" >&2
}

case "${1:-all}" in
  preflight) preflight ;;
  infra)     preflight; infra ;;
  cluster)   cluster "${2:-attestmesh-1}" ;;
  indexer-cluster) preflight; indexer_cluster "${2:-attestmesh-indexer-ha}" ;;
  patha-upgrade) patha_upgrade "$2" ;;
  patha-safe-prepare) preflight; patha_safe_prepare "${2:-}" "${3:-}" ;;
  seed-appid) seed_appid "$2" "$3" ;;
  all)       preflight; infra; cluster "${2:-attestmesh-1}" ;;
  *) die "usage: onchain.sh {preflight|infra|cluster [name]|indexer-cluster [name]|patha-upgrade <cluster>|patha-safe-prepare <cluster> <safe>|seed-appid <cluster> <member>|all}" ;;
esac
