#!/usr/bin/env bash
# Indexer bring-up — standardized, logged. The indexer is SHARED infrastructure:
# ONE instance serves every cluster (it discovers them all via the factory) and is
# not tied to any single cluster or network — do NOT deploy one per cluster. It is
# an attested dstack service, NOT a cluster member: a stock dstack app (no Path A
# upgrade, no cluster gating), so updates are plain `phala deploy --cvm-id`. What
# ties it to the system is on-chain: the operator registers (endpoint, codeId,
# Ed25519 pubkey) in the per-chain IndexerRegistry; every sidecar discovers it from
# there and verifies each push against that pubkey.
#
#   source deploy/env.sh \
#     && deploy/indexer.sh <name> [ensure|all|env-file|deploy|register|verify|update]
#
# Subcommands:
#   ensure    no-op if the registry already has an endpoint (the shared indexer is
#             up); otherwise deploy+register+verify — the cluster-workflow entry
#   env-file  build the sealed env (chain + RPC + registry addrs + ghcr pull creds)
#   deploy    phala-deploy the CVM (stock dstack app on base KMS)
#   register  read the boot-derived pubkey from CVM logs + the compose hash, then
#             IndexerRegistry.setIndexer((endpoint, codeId, pubKey, updatedAt))
#   verify    poll /healthz via the gateway + read back IndexerRegistry.current()
#   update    roll a new compose/image onto the existing CVM (no cluster gate)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR KMS_CONTRACT
npx --yes phala status 2>&1 | grep -qiE "logged in" || die "phala not logged in (run: npx phala login)"

NAME="${1:?usage: indexer.sh <name> [all|env-file|deploy|register|verify|update]}"
NODE_ID="${NODE_ID:-26}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/indexer-1.yaml}"
ENV_FILE="${ENV_FILE:-/tmp/attestmesh-${NAME}.env}"
STATE="$LOGDIR/indexer-${NAME}.state"           # persists CVM_ID / APP_ID across subcommands
GW_DOMAIN="${GATEWAY_DOMAIN:-dstack-base-prod5.phala.network}"
REGISTRY=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json")
FACTORY=$(jq -r .clusterDiamondFactory "$ROOT/contracts/script/deployments/${CHAIN_ID}.json")

_save() { printf 'CVM_ID=%s\nAPP_ID=%s\n' "$CVM_ID" "$APP_ID" > "$STATE"; }
_load() { [ -f "$STATE" ] && source "$STATE" || true; }

# The catch-up floor: the factory's deploy block (no clusters exist before it).
# Binary search getCode over [0, head] — ~26 RPC round-trips.
_factory_deploy_block() {
  local lo=0 hi mid code
  hi=$(cast block-number --rpc-url "$RPC_URL") || die "cast block-number failed"
  while [ "$lo" -lt "$hi" ]; do
    mid=$(( (lo + hi) / 2 ))
    code=$(cast code "$FACTORY" --block "$mid" --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$code" ] && [ "$code" != "0x" ]; then hi=$mid; else lo=$((mid + 1)); fi
  done
  echo "$lo"
}

_build_env_file() {
  local guser gtok start
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep -E '^\s*token\s*=' "$HOME/.teesql/ghcr-pull.toml" | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$REGISTRY" ] && [ -n "$FACTORY" ] && [ -n "$gtok" ] || die "could not assemble sealed env (registry/factory/ghcr creds)"
  start="${INDEXER_START_BLOCK:-$(_factory_deploy_block)}"
  log "catch-up floor (factory deploy block): $start"
  cat > "$ENV_FILE" <<EOF
CHAIN_ID=${CHAIN_ID}
RPC_URL=${RPC_URL}
INDEXER_REGISTRY_ADDR=${REGISTRY}
CLUSTER_DIAMOND_FACTORY_ADDR=${FACTORY}
INDEXER_CODE_ID=
INDEXER_START_BLOCK=${start}
DSTACK_DOCKER_REGISTRY=ghcr.io
DSTACK_DOCKER_USERNAME=${guser}
DSTACK_DOCKER_PASSWORD=${gtok}
EOF
  log "built sealed env → $ENV_FILE (keys: $(grep -oE '^[A-Z_]+' "$ENV_FILE" | tr '\n' ' '))"
}

deploy_cvm() {
  [ -f "$ENV_FILE" ] || _build_env_file
  local lf="$LOGDIR/indexer-deploy-${NAME}.$(ts).log"
  log "▶ phala deploy (stock dstack app) name=$NAME compose=$COMPOSE node-id=$NODE_ID"
  npx --yes phala deploy --kms base --kms-contract "$KMS_CONTRACT" \
    --name "$NAME" --compose "$COMPOSE" -e "$ENV_FILE" --node-id "$NODE_ID" \
    --private-key "$PRIVATE_KEY" --rpc-url "$RPC_URL" \
    --ssh-pubkey "$HOME/.ssh/id_ed25519.pub" 2>&1 | tee "$lf"
  CVM_ID=$(grep -iE 'CVM ID:' "$lf" | awk '{print $NF}' | tr -d '[:space:]')
  APP_ID=$(grep -iE 'App ID:' "$lf" | awk '{print $NF}' | tr -d '[:space:]')
  [ -n "$CVM_ID" ] && [ -n "$APP_ID" ] || die "could not parse CVM ID / App ID from phala deploy"
  APP_ID="${APP_ID#0x}"
  _save
  log "✔ deployed CVM=$CVM_ID app_id=$APP_ID"
}

# Wait for boot, scrape the derived Ed25519 pubkey from the logs, and register.
register() {
  _load; [ -n "${CVM_ID:-}" ] && [ -n "${APP_ID:-}" ] || die "no CVM state; run 'deploy' first"
  local pubkey="" i
  for i in $(seq 1 40); do
    pubkey=$(npx --yes phala cvms logs "$CVM_ID" 2>/dev/null \
      | grep -oE 'pubkey[^0-9a-fx]*0x[0-9a-fA-F]{64}' | grep -oE '0x[0-9a-fA-F]{64}' | head -1)
    [ -n "$pubkey" ] && break
    log "… waiting for indexer boot to log its signing pubkey (attempt $i/40)"
    sleep 10
  done
  [ -n "$pubkey" ] || die "indexer never logged its signing pubkey — check: phala cvms logs $CVM_ID"
  local hash; hash=$(npx --yes phala cvms get "$CVM_ID" --json 2>/dev/null \
    | grep -oiE '"compose_hash"[[:space:]]*:[[:space:]]*"[0-9a-fA-F]+"' | head -1 \
    | sed -E 's/.*"([0-9a-fA-F]+)".*/\1/')
  [ -n "$hash" ] || die "could not read compose_hash for CVM $CVM_ID"
  local endpoint="${INDEXER_ENDPOINT:-https://${APP_ID}-50051.${GW_DOMAIN}}"
  log "registering: endpoint=$endpoint codeId=0x$hash pubKey=$pubkey"
  run_step "setIndexer-${NAME}" cast send "$REGISTRY" \
    "setIndexer((string,bytes32,bytes32,uint64))" "($endpoint,0x$hash,$pubkey,$(date +%s))" \
    --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY"
}

verify() {
  _load; [ -n "${APP_ID:-}" ] || die "no CVM state; run 'deploy' first"
  local i body
  for i in $(seq 1 40); do
    body=$(curl -sm 8 "https://${APP_ID}-9090.${GW_DOMAIN}/healthz" 2>/dev/null)
    if [ -n "$body" ]; then
      log "✔ indexer healthz: $body"
      log "registry current(): $(cast call "$REGISTRY" 'current()(string,bytes32,bytes32,uint64)' --rpc-url "$RPC_URL")"
      return 0
    fi
    log "… indexer health not reachable yet (attempt $i/40)"
    sleep 15
  done
  die "indexer health endpoint never came up — check: phala cvms logs $CVM_ID"
}

update_cvm() {
  _load; [ -n "${CVM_ID:-}" ] || die "no CVM state; run 'deploy' first"
  [ -f "$ENV_FILE" ] || _build_env_file
  run_step "indexer-update-${NAME}" npx --yes phala deploy \
    --cvm-id "$CVM_ID" --compose "$COMPOSE" -e "$ENV_FILE"
  run_step "indexer-restart-${NAME}" npx --yes phala cvms restart "$CVM_ID"
}

# The shared-infra entry: if SOME indexer is already registered on this chain,
# every cluster (and every network this indexer watches) reuses it — no-op.
ensure() {
  local cur; cur=$(cast call "$REGISTRY" 'current()(string,bytes32,bytes32,uint64)' --rpc-url "$RPC_URL" 2>/dev/null | head -1 | tr -d '"' )
  if [ -n "$cur" ] && [ "$cur" != "0x" ]; then
    log "shared indexer already registered (endpoint=$cur) — nothing to do"
    return 0
  fi
  deploy_cvm; register; verify
}

log "=== Indexer bring-up: $NAME ==="
case "${2:-ensure}" in
  ensure)   ensure ;;
  env-file) _build_env_file ;;
  deploy)   deploy_cvm ;;
  register) register ;;
  verify)   verify ;;
  update)   update_cvm ;;
  all)      deploy_cvm; register; verify ;;
  *) die "usage: indexer.sh <name> [ensure|all|env-file|deploy|register|verify|update]" ;;
esac
