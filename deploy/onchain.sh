#!/usr/bin/env bash
# Standardized on-chain deploy routine (Track A) — ordered, idempotent, logged.
# Subcommands run the specific-ordering sequence; smithers (deploy/workflows/deploy.tsx)
# sequences these as durable steps, or run directly:
#   source deploy/env.sh && deploy/onchain.sh all
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"; require PRIVATE_KEY RPC_URL CHAIN_ID KMS_ROOT COMPOSE_HASH DEPLOYER_ADDR

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

# seed-appid <cluster> <memberAddr>: owner pre-approves an app_id so the KMS gate admits
# the CVM at first boot (the cold-start fix). Owner-gas (not sponsored).
seed_appid() {
  local cluster="$1" member="$2"
  run_step "seed-appid-${member}" cast send "$cluster" "addAllowedAppId(address)" "$member" --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY"
  log "allowedAppIds[$member] = $(cast call "$cluster" 'allowedAppIds(address)(bool)' "$member" --rpc-url "$RPC_URL")"
}

# patha-upgrade <cluster>: diamond-cut the cluster's DstackFacet to the Path A build (dstack_register
# accepts owner-allowlisted app_ids) + deploy the Path A ClusterMember impl (the UUPS upgrade target
# for dstack-provisioned app proxies). One-time per cluster; needs the deployer to be the diamond's
# solidstate owner (the script acceptsOwnership if it is the nominee). Prints the new facet + impl.
patha_upgrade() {
  local cluster="${1:?usage: onchain.sh patha-upgrade <cluster>}"
  export CLUSTER="$cluster"
  run_step "patha-upgrade-${cluster}" bash -c "cd '$ROOT/contracts' && forge script script/UpgradeDstackFacetPathA.s.sol:UpgradeDstackFacetPathA --rpc-url '$RPC_URL' --broadcast"
}

case "${1:-all}" in
  preflight) preflight ;;
  infra)     preflight; infra ;;
  cluster)   cluster "${2:-attestmesh-1}" ;;
  patha-upgrade) patha_upgrade "$2" ;;
  seed-appid) seed_appid "$2" "$3" ;;
  all)       preflight; infra; cluster "${2:-attestmesh-1}" ;;
  *) die "usage: onchain.sh {preflight|infra|cluster [name]|patha-upgrade <cluster>|seed-appid <cluster> <member>|all}" ;;
esac
