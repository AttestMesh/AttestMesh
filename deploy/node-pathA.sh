#!/usr/bin/env bash
# Path A node bring-up (dstack base KMS) — standardized, order-sensitive, logged.
#
# Base KMS only mints an app_id it provisioned, so the member contract cannot be a
# factory-predicted address; it must BE the app_id. Order matters:
#   1. phala deploy a STOCK DstackApp (phala mints the app_id X) running our node compose.
#   2. PRIME the cluster gate (addComposeHash + addAllowedAppId) BEFORE upgrading, so the
#      KMS key-release passes whether it reads the stock gate or (post-upgrade) the cluster.
#   3. UPGRADE the stock proxy X to the ClusterMember impl + reinitialize it to the cluster.
#   4. The in-CVM sidecar self-discovers X from /Info, finds the cluster, and self-registers
#      via a sponsored UserOp. `verify` polls the chain for that.
#
#   source deploy/env.sh \
#     && CLUSTER=<diamond> MEMBER_IMPL=<pathA ClusterMember impl> ENV_FILE=<sealed env> \
#        deploy/node-pathA.sh <node-name> [all|deploy|prime|upgrade|verify]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR KMS_CONTRACT CLUSTER MEMBER_IMPL
# Phala auth is a stored `phala login` session (device-flow), not an env var.
npx --yes phala status 2>&1 | grep -qiE "logged in" || die "phala not logged in (run: npx phala login)"

NODE="${1:?usage: node-pathA.sh <node-name> [all|setup|deploy|prime|upgrade|verify|env-file]}"
NODE_ID="${NODE_ID:-26}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/${NODE}.yaml}"
# Sealed env (Alchemy + ghcr secrets) — never committed; auto-built by env-file/deploy if absent.
ENV_FILE="${ENV_FILE:-/tmp/attestmesh-${NODE}.env}"
STATE="$LOGDIR/node-pathA-${NODE}.state"   # persists CVM_ID / X across subcommands
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000

_save() { printf 'CVM_ID=%s\nX=%s\n' "$CVM_ID" "$X" > "$STATE"; }
_load() { [ -f "$STATE" ] && source "$STATE" || true; }

# Build the sealed env file phala encrypts (-e): node config + ghcr pull-creds (so dstack's
# pre-launch can docker-login the private image). MEMBER_CONTRACT is intentionally omitted —
# the sidecar self-discovers its app_id from /Info (Path A).
_build_env_file() {
  local indexer guser gtok
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json")
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep -E '^\s*token\s*=' "$HOME/.teesql/ghcr-pull.toml" | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$indexer" ] && [ -n "$gtok" ] || die "could not assemble sealed env (indexer/ghcr creds)"
  cat > "$ENV_FILE" <<EOF
CHAIN_ID=${CHAIN_ID}
RPC_URL=${RPC_URL}
BUNDLER_URL=${BUNDLER_URL:-$RPC_URL}
GAS_POLICY_ID=${GAS_POLICY_ID:-}
INDEXER_REGISTRY_ADDR=${indexer}
DSTACK_DOCKER_REGISTRY=ghcr.io
DSTACK_DOCKER_USERNAME=${guser}
DSTACK_DOCKER_PASSWORD=${gtok}
EOF
  log "built sealed env → $ENV_FILE (keys: $(grep -oE '^[A-Z_]+' "$ENV_FILE" | tr '\n' ' '))"
}

# send_seq <label> <to> <sig> <args...> : cast send with an explicit, locally-incremented
# nonce. Back-to-back sends otherwise race the RPC's lagging pending-nonce, yielding
# "replacement transaction underpriced" / "nonce too low". The nonce is fetched fresh on the
# first send of an invocation, then incremented in-process (only on success).
NEXT_NONCE=""
send_seq() {
  local label="$1"; shift
  [ -n "$NEXT_NONCE" ] || NEXT_NONCE=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL")
  run_step "$label" cast send "$@" --nonce "$NEXT_NONCE" \
    --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY" \
    && NEXT_NONCE=$((NEXT_NONCE + 1))
}

# 1. Deploy a stock DstackApp CVM; phala mints the app_id (no --custom-app-id on base KMS).
deploy_cvm() {
  [ -f "$ENV_FILE" ] || _build_env_file
  local lf="$LOGDIR/pathA-deploy-${NODE}.$(ts).log"
  log "▶ phala deploy (base KMS, stock) node=$NODE compose=$COMPOSE node-id=$NODE_ID"
  npx --yes phala deploy --kms base --kms-contract "$KMS_CONTRACT" \
    --name "$NODE" --compose "$COMPOSE" -e "$ENV_FILE" --node-id "$NODE_ID" \
    --private-key "$PRIVATE_KEY" --rpc-url "$RPC_URL" \
    --ssh-pubkey "$HOME/.ssh/id_ed25519.pub" 2>&1 | tee "$lf"
  CVM_ID=$(grep -iE 'CVM ID:' "$lf" | awk '{print $NF}' | tr -d '[:space:]')
  local appid; appid=$(grep -iE 'App ID:' "$lf" | awk '{print $NF}' | tr -d '[:space:]')
  [ -n "$CVM_ID" ] && [ -n "$appid" ] || die "could not parse CVM ID / App ID from phala deploy"
  X="0x${appid#0x}"
  _save
  log "✔ deployed CVM=$CVM_ID app_id(X)=$X"
}

# 2. Prime the cluster boot gate BEFORE upgrading (compose hash + app_id allowlist).
prime_gate() {
  _load; [ -n "${CVM_ID:-}" ] && [ -n "${X:-}" ] || die "no CVM state; run 'deploy' first"
  local hash; hash=$(npx --yes phala cvms get "$CVM_ID" --json 2>/dev/null \
    | grep -oiE '"compose_hash"[[:space:]]*:[[:space:]]*"[0-9a-fA-F]+"' | head -1 \
    | sed -E 's/.*"([0-9a-fA-F]+)".*/\1/')
  [ -n "$hash" ] || die "could not read compose_hash for CVM $CVM_ID"
  log "compose_hash=0x$hash"
  send_seq "addComposeHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$hash"
  send_seq "addAllowedAppId-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
}

# 3. Upgrade the stock proxy to ClusterMember + bind the cluster (atomic upgradeToAndCall).
upgrade_member() {
  _load; [ -n "${X:-}" ] || die "no app_id state; run 'deploy' first"
  local reinit; reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  send_seq "upgrade-member-${NODE}" "$X" "upgradeToAndCall(address,bytes)" "$MEMBER_IMPL" "$reinit"
  # Read-back can briefly hit a lagging RPC node right after the tx; retry (no sleep —
  # each cast call is a network round-trip, so a few iterations span a couple seconds).
  local c=""
  for _ in 1 2 3 4 5 6 7 8; do
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    [ "${c,,}" = "${CLUSTER,,}" ] && break
  done
  log "X.cluster()=$c (expect $CLUSTER)"
}

# 4. Poll the chain for the in-CVM sidecar's self-registration.
verify() {
  _load; [ -n "${X:-}" ] || die "no app_id state; run 'deploy' first"
  local i id
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ node $NODE registered: memberId=$id owner=$(cast call $X 'owner()(address)' --rpc-url "$RPC_URL")"
      return 0
    fi
    log "… not registered yet (attempt $i/45, memberCount=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL"))"
    sleep 20
  done
  die "node $NODE not registered after timeout — check: phala cvms logs $CVM_ID"
}

log "=== Path A node bring-up: $NODE ==="
case "${2:-all}" in
  env-file) _build_env_file ;;                         # build the sealed env (idempotent)
  deploy)  deploy_cvm ;;
  prime)   prime_gate ;;
  upgrade) upgrade_member ;;
  setup)   deploy_cvm; prime_gate; upgrade_member ;;  # fast on-chain path, no register wait
  verify)  verify ;;                                   # long poll; run separately/background
  all)     deploy_cvm; prime_gate; upgrade_member; verify ;;
  *) die "usage: node-pathA.sh <node-name> [all|setup|deploy|prime|upgrade|verify|env-file]" ;;
esac
