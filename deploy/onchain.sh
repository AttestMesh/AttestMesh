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
SAFE_SINGLETON="${SAFE_SINGLETON:-0x29fcB43b46531BcA003ddC8FCB67FFE91900C762}"
SAFE_PROXY_FACTORY="${SAFE_PROXY_FACTORY:-0x4e1DCf7AD4e460CfD30791CCC4F9c8a4f820ec67}"
SAFE_FALLBACK_HANDLER="${SAFE_FALLBACK_HANDLER:-0xfd0732Dc9E303f09fCEf3a7388Ad10A83459Ec99}"

preflight() {
  local d; d=$(cast wallet address --private-key "$PRIVATE_KEY" 2>/dev/null)
  [ "${d,,}" = "${DEPLOYER_ADDR,,}" ] || die "deployer key mismatch ($d != $DEPLOYER_ADDR)"
  log "preflight ok: deployer=$d chain=$(cast chain-id --rpc-url "$RPC_URL") balance=$(cast from-wei "$(cast balance "$d" --rpc-url "$RPC_URL")")ETH"
}

safe() {
  local name="${1:-andrew-xyn-pg}" out safe_addr
  export SAFE_SINGLETON SAFE_PROXY_FACTORY SAFE_FALLBACK_HANDLER
  SAFE_SALT_NONCE="${SAFE_SALT_NONCE:-$(cast --to-dec "$(cast keccak "attestmesh-safe-${name}")")}"; export SAFE_SALT_NONCE
  [ "$(cast call "$SAFE_SINGLETON" 'VERSION()(string)' --rpc-url "$RPC_URL" 2>/dev/null)" = '"1.4.1"' ] \
    || die "canonical SafeL2 1.4.1 singleton unavailable"
  out=$(cd "$ROOT/contracts" && forge script script/DeploySoleSignerSafe.s.sol:DeploySoleSignerSafe \
    --rpc-url "$RPC_URL" --broadcast 2>&1) || die "Safe deployment failed: $out"
  printf '%s\n' "$out" >&2
  safe_addr=$(sed -nE 's/.*Safe deployed: (0x[0-9A-Fa-f]{40}).*/\1/p' <<<"$out" | tail -1)
  [ -n "$safe_addr" ] || die "could not parse Safe address"
  [ "$(cast call "$safe_addr" 'getThreshold()(uint256)' --rpc-url "$RPC_URL" | awk '{print $1}')" = 1 ] || die "Safe threshold mismatch"
  [ "$(cast call "$safe_addr" 'getOwners()(address[])' --rpc-url "$RPC_URL" | tr -d '[] ' | tr A-Z a-z)" = "${DEPLOYER_ADDR,,}" ] \
    || die "Safe owner mismatch"
  printf 'SAFE=%s\n' "$safe_addr"
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
  local owner="${CLUSTER_OWNER:-$DEPLOYER_ADDR}"
  cat > "$ROOT/contracts/$CLUSTER_CONFIG" <<JSON
{ "clusterOwner": "$owner", "kmsRootSigner": "$KMS_ROOT",
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
  send_with_nonce_retry "seed-appid-${member}" "$cluster" "addAllowedAppId(address)" "$member"
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
  safe)      preflight; safe "${2:-andrew-xyn-pg}" ;;
  cluster)   cluster "${2:-attestmesh-1}" ;;
  patha-upgrade) patha_upgrade "$2" ;;
  seed-appid) seed_appid "$2" "$3" ;;
  all)       preflight; infra; cluster "${2:-attestmesh-1}" ;;
  *) die "usage: onchain.sh {preflight|infra|safe [name]|cluster [name]|patha-upgrade <cluster>|seed-appid <cluster> <member>|all}" ;;
esac
