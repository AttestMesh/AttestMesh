#!/usr/bin/env bash
# Standardized node bring-up routine (Track D) — the most order-sensitive / repeated
# routine (one run per node), so it is the prime smithers-orchestrated loop.
# Order: predict member addr → owner seeds app_id → build/push sidecar image →
#        phala deploy CVM (app_id = member addr) → wait boot → verify on-chain registration.
#
#   source deploy/env.sh && deploy/node.sh <node-name>
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"; require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: node.sh <node-name>}"
RECEIPT="$ROOT/contracts/script/deployments/${CHAIN_ID}.json"
CLUSTER="${CLUSTER:-$(jq -r '.cluster // empty' "$RECEIPT" 2>/dev/null)}"
[ -n "$CLUSTER" ] || CLUSTER="${ATTESTMESH_CLUSTER:?set ATTESTMESH_CLUSTER=<diamond addr> (or add .cluster to the receipt)}"
MEMBER_FACTORY=$(jq -r .clusterMemberFactory "$RECEIPT")
SALT=$(cast keccak "attestmesh-member-${NODE}")

require_phala() {
  if [ -z "${PHALA_CLOUD_API_KEY:-}" ]; then
    log "✗ PHALA_CLOUD_API_KEY is not set — cannot deploy a CVM (Track D5)."
    log "  UNBLOCK: write the Phala Cloud API key to ~/.teesql/phala-cloud-api.key"
    log "           (or 'export PHALA_CLOUD_API_KEY=<key>' before sourcing deploy/env.sh),"
    log "           then re-run. Everything up to this point does not need it."
    die "Phala auth required for node bring-up"
  fi
  log "phala auth: $(PHALA_CLOUD_API_KEY="$PHALA_CLOUD_API_KEY" npx --yes phala status 2>&1 | grep -iE 'logged in|authenticated|account' | head -1 || echo 'status unknown')"
}

# 1. Predict the member address (this IS the dstack app_id the operator allowlists + the CVM boots as).
predict() {
  MEMBER=$(cast call "$MEMBER_FACTORY" "predictMemberAddress(address,bytes32)(address)" "$CLUSTER" "$SALT" --rpc-url "$RPC_URL")
  log "node=$NODE  predicted member (app_id) = $MEMBER  (cluster=$CLUSTER)"
}

# 2. Deploy the member contract + owner pre-approves its app_id at the boot gate (cold-start fix).
member_and_appid() {
  export CLUSTER MEMBER_SALT="$SALT"
  run_step "deploy-member-${NODE}" bash -c "cd '$ROOT/contracts' && forge script script/DeployMember.s.sol:DeployMember --rpc-url '$RPC_URL' --broadcast"
  bash "$HERE/onchain.sh" seed-appid "$CLUSTER" "$MEMBER"
}

# 3. Build + push the sidecar OCI image and the node compose.
#    MILESTONE-A DEPENDENCY: requires the sidecar bring-up wiring (state::run + the
#    DstackRuntime KMS-chain method) and a Dockerfile/compose, which are not built yet.
build_image() {
  if [ ! -f "$ROOT/sidecar/Dockerfile" ]; then
    log "⚠ sidecar/Dockerfile missing — image build is pending milestone-A (wire state::run + DstackRuntime KMS-chain method). Skipping build."
    return 9
  fi
  run_step "build-sidecar-${NODE}" bash -c "cd '$ROOT/sidecar' && docker build -t attestmesh-sidecar:latest ."
  # run_step "push-sidecar-${NODE}" docker push ...   # registry per ~/.teesql/ghcr-pull.toml
}

# 4. Deploy the CVM on Phala with app_id = member address.
deploy_cvm() {
  require_phala
  # phala deploy reads the compose; app_id is set to $MEMBER. Compose/CVM authoring is
  # milestone-A (needs the sidecar image). This is the standardized invocation point.
  log "▶ phala deploy CVM for node=$NODE app_id=$MEMBER (compose pending sidecar image)"
  # run_step "phala-deploy-${NODE}" npx phala deploy --name "attestmesh-${NODE}" --compose deploy/compose/${NODE}.yaml ...
  log "⚠ CVM deploy not yet runnable — needs the sidecar image (build_image) first."
  return 9
}

# 5. Verify the node registered on-chain (the moment the real KMS proof is validated).
verify_registration() {
  local mc; mc=$(cast call "$CLUSTER" "memberCount()(uint256)" --rpc-url "$RPC_URL")
  local id; id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$MEMBER" --rpc-url "$RPC_URL")
  log "memberCount=$mc  memberIdOf($MEMBER)=$id"
  [ "$id" != "0x0000000000000000000000000000000000000000000000000000000000000000" ] \
    && log "✔ node $NODE registered (the real KMS proof verified on-chain)" \
    || log "… node $NODE not yet registered"
}

log "=== node bring-up: $NODE ==="
predict
case "${2:-all}" in
  predict) ;;
  member)  member_and_appid ;;
  image)   build_image ;;
  cvm)     member_and_appid; build_image; deploy_cvm ;;
  verify)  verify_registration ;;
  all)     member_and_appid; build_image; deploy_cvm; verify_registration ;;
esac
