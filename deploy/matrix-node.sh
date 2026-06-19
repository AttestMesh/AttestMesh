#!/usr/bin/env bash
# Matrix node (single-node app) bring-up on the SELF-HOSTED on-chain dstack box (attestmesh.xyz).
# Mirrors node-pathA.sh but for the BOX's MCP deploy_app flow (not Phala). Order-sensitive, logged,
# re-entrant via a state file. Encodes every fix from deploy/matrix-node-steps-log.md.
#
#   deploy  → register a stock DstackApp + seal env + CreateVm on the box (app_id X + compose_hash H),
#             then WAIT for synapse /_matrix to be live. (The first member of a fresh cluster is the
#             IMMUTABLE CSK originator and the contracts have no removeMember, so the node must be
#             fully working BEFORE it registers.)
#   cluster → DeployCluster diamond (this machine; kmsRootSigner = THIS box's; initialComposeHashes=[H]).
#   patha   → onchain.sh patha-upgrade (Path-A DstackFacet + ClusterMember impl, cut in). [needed: the
#             8453.json clusterMemberImpl lacks reinitializeFromDstackApp]
#   prime   → addAllowedAppId(cluster, X)   (hash already seeded at `cluster`).
#   bind    → on the box (box deployer key): upgradeToAndCall(X, impl, reinitializeFromDstackApp(cluster)).
#   verify  → poll memberCount/memberIdOf(X) + sidecar /healthz + Matrix well-known.
#   update  → IN-PLACE roll (new compose/env) REUSING X: addComposeHash(cluster, H') → stop old CVM →
#             CreateVm(app_id=X). Preserves membership + CSK originator. No new cluster.
#
#   source deploy/env.sh \
#     && TS_AUTHKEY=tskey-… [BOX_KMS_ROOT_SIGNER=0x7fa6… MESH_CIDR_IP=…] \
#        deploy/matrix-node.sh <node-name> [all|deploy|cluster|patha|prime|bind|verify|update]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: matrix-node.sh <node-name> [all|deploy|cluster|patha|prime|bind|verify|update]}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
BOX_KMS_ROOT_SIGNER="${BOX_KMS_ROOT_SIGNER:-0x7fa63d99495be2129cf28eee54e2ef2724e3aa2e}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/matrix-node.yaml}"
MESH_CIDR_IP="${MESH_CIDR_IP:-168951808}"          # 10.18.0.0/16 — pick a UNIQUE /16 per cluster
MESH_CIDR_PREFIX="${MESH_CIDR_PREFIX:-16}"
export BOX_VCPU="${BOX_VCPU:-4}" BOX_MEM="${BOX_MEM:-8192}" BOX_DISK="${BOX_DISK:-60}"
export BOX_PORTS="${BOX_PORTS:-[\"tcp:127.0.0.1:8080:80\",\"tcp:127.0.0.1:9091:9090\",\"tcp:127.0.0.1:9102:9100\"]}"  # no host port >20000
# Health-check host ports derived from BOX_PORTS, so multiple nodes can coexist on one box.
_hostport() { echo "$BOX_PORTS" | tr ',[]' ' ' | tr -d '"' | tr ' ' '\n' | awk -F: -v vm="$1" '$4==vm{print $3; exit}'; }
NGINX_PORT="$(_hostport 80)";     NGINX_PORT="${NGINX_PORT:-8080}"
SIDECAR_PORT="$(_hostport 9090)"; SIDECAR_PORT="${SIDECAR_PORT:-9091}"
AGENT_PORT="$(_hostport 9100)";   AGENT_PORT="${AGENT_PORT:-9102}"
GW_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
RECEIPT="$ROOT/contracts/script/deployments/${CHAIN_ID}.json"
STATE="$LOGDIR/matrix-node-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000

_save() { printf 'X=%s\nH=%s\nVM_ID=%s\nCLUSTER=%s\nMEMBER_IMPL=%s\nPGPW=%s\nIAPW=%s\n' \
  "${X:-}" "${H:-}" "${VM_ID:-}" "${CLUSTER:-}" "${MEMBER_IMPL:-}" "${PGPW:-}" "${IAPW:-}" > "$STATE"; }
_load() { [ -f "$STATE" ] && source "$STATE" || true; }

ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }
_vmm() { ssh_box "curl -s --max-time 30 http://127.0.0.1:9080/prpc/$1?json -H 'content-type: application/json' -d '$2'"; }

NEXT_NONCE=""
send_seq() {  # nonce-safe back-to-back cast send (AttestMesh deployer, this machine)
  local label="$1"; shift
  [ -n "$NEXT_NONCE" ] || NEXT_NONCE=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL")
  run_step "$label" cast send "$@" --nonce "$NEXT_NONCE" --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY" \
    && NEXT_NONCE=$((NEXT_NONCE + 1))
}

# Run the box-side helper (matrix-node-box.py) with the sealed secret env passed over SSH (in-memory,
# never written to disk). Usage: _box_run <deploy|hash|update> [app_id]. Echoes the helper's stdout.
_box_run() {
  local mode="$1" app_id="${2:-}" guser gtok
  : "${TS_AUTHKEY:?set TS_AUTHKEY (operator tailscale key)}"
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/matrix-node-box.py" "$BOX_HOST:/tmp/matrix-node-box.py"
  ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' \
    E_RPC_URL='$RPC_URL' E_BUNDLER_URL='${BUNDLER_URL:-$RPC_URL}' E_GAS_POLICY_ID='${GAS_POLICY_ID:-}' \
    E_POSTGRES_PASSWORD='$PGPW' E_TS_AUTHKEY='$TS_AUTHKEY' E_DSTACK_DOCKER_USERNAME='${guser:-dmvt}' E_DSTACK_DOCKER_PASSWORD='$gtok' \
    E_BOT_USERNAME='${BOT_USERNAME:-admin-agent}' E_BOT_PASSWORD='${BOT_PASSWORD:-}' \
    E_MATRIX_ADMIN_MXIDS='${MATRIX_ADMIN_MXIDS:-}' E_MATRIX_ADMIN_SENDERS='${MATRIX_ADMIN_SENDERS:-}' \
    E_INITIAL_ADMIN='${INITIAL_ADMIN:-}' E_INITIAL_ADMIN_PASSWORD='${INITIAL_ADMIN_PASSWORD:-}' \
    E_LLM_BASE_URL='${LLM_BASE_URL:-}' E_LLM_MODEL='${LLM_MODEL:-}' E_LLM_API_KEY='${LLM_API_KEY:-}' \
    $BOX_PY /tmp/matrix-node-box.py $mode $app_id"
}

# Required matrix-admin-agent env (secrets in-memory, like TS_AUTHKEY). MATRIX_ADMIN_SENDERS +
# INITIAL_ADMIN are optional (empty → on-chain channel off / no declared admin).
_require_agent_env() {
  local v missing=""
  for v in BOT_PASSWORD MATRIX_ADMIN_MXIDS LLM_BASE_URL LLM_MODEL LLM_API_KEY; do
    [ -n "${!v:-}" ] || missing="$missing $v"
  done
  [ -z "$missing" ] || die "missing required matrix-admin-agent env:$missing  (pass them in the invocation, e.g. BOT_PASSWORD=… LLM_API_KEY=…)"
}

# Stable initial-admin password: generated ONCE (if INITIAL_ADMIN is set and none was provided),
# persisted in the state file, and reused across rolls so the operator's login doesn't change.
_ensure_iapw() {
  if [ -n "${INITIAL_ADMIN:-}" ] && [ -z "${INITIAL_ADMIN_PASSWORD:-}" ]; then
    INITIAL_ADMIN_PASSWORD="${IAPW:-$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 28)}"
  fi
  IAPW="${INITIAL_ADMIN_PASSWORD:-}"; export INITIAL_ADMIN_PASSWORD
}

# Poll the box loopback (8080 → nginx → Matrix) for synapse readiness.
_wait_synapse() {
  local i body
  for i in $(seq 1 60); do
    body=$(ssh_box "curl -s --max-time 6 http://127.0.0.1:${NGINX_PORT}/_matrix/client/versions" 2>/dev/null)
    echo "$body" | grep -q '"versions"' && { log "✔ synapse live (/_matrix/client/versions)"; return 0; }
    log "… synapse not ready ($i/60)"; sleep 12
  done
  die "synapse never came up — inspect the CVM (vm_logs $VM_ID) / box compose"
}

# 1. Register stock DstackApp + CreateVm via the box, then wait for Matrix to be live.
deploy_cvm() {
  _load; _require_agent_env; _ensure_iapw
  PGPW="${PGPW:-$(openssl rand -hex 24)}"; _save
  log "▶ box deploy_app node=$NODE compose=$COMPOSE"
  local out; out=$(_box_run deploy) || die "box deploy failed"
  local j; j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id); H=$(echo "$j" | jq -r .compose_hash); VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  local xb="${X#0x}"
  log "✔ deployed app_id(X)=$X compose_hash(H)=$H vm=$VM_ID  →  https://${xb,,}.${GW_DOMAIN}"
  _wait_synapse
}

# 2. DeployCluster diamond — kmsRootSigner = THIS box's; compose hash H seeded at init.
deploy_cluster() {
  _load; [ -n "${H:-}" ] || die "no compose_hash; run 'deploy' first"
  local salt cfg lf; salt=$(cast keccak "attestmesh-cluster-${NODE}")
  cfg="$ROOT/contracts/script/clusters/${NODE}.json"; lf="$LOGDIR/matrix-cluster-${NODE}.$(ts).log"
  cat > "$cfg" <<JSON
{ "clusterOwner": "$DEPLOYER_ADDR", "kmsRootSigner": "$BOX_KMS_ROOT_SIGNER",
  "initialComposeHashes": ["0x${H#0x}"], "initialDeviceIds": [],
  "allowAnyDevice": true, "requireTcbUpToDate": false,
  "meshCidrIp": $MESH_CIDR_IP, "meshCidrPrefix": $MESH_CIDR_PREFIX, "salt": "$salt" }
JSON
  log "▶ DeployCluster ($cfg, mesh ${MESH_CIDR_IP}/${MESH_CIDR_PREFIX})"
  CLUSTER_FACTORY=$(jq -r .clusterDiamondFactory "$RECEIPT") \
  MEMBER_FACTORY=$(jq -r .clusterMemberFactory "$RECEIPT") \
  CLUSTER_CONFIG="script/clusters/${NODE}.json" \
    bash -c "cd '$ROOT/contracts' && forge script script/DeployCluster.s.sol:DeployCluster --rpc-url '$RPC_URL' --broadcast" 2>&1 | tee "$lf"
  CLUSTER=$(grep -iE 'Cluster deployed:' "$lf" | grep -oE '0x[0-9a-fA-F]{40}' | head -1)
  [ -n "$CLUSTER" ] || die "could not parse cluster address (see $lf)"
  _save; log "✔ cluster=$CLUSTER"
}

# 3. Path-A facet + a fresh ClusterMember impl (the 8453.json impl lacks reinitializeFromDstackApp).
patha_upgrade() {
  _load; [ -n "${CLUSTER:-}" ] || die "no cluster; run 'cluster' first"
  bash -c "cd '$ROOT' && source deploy/env.sh >/dev/null && deploy/onchain.sh patha-upgrade $CLUSTER" || die "patha-upgrade failed"
  local b="$ROOT/contracts/broadcast/UpgradeDstackFacetPathA.s.sol/${CHAIN_ID}/run-latest.json"
  MEMBER_IMPL=$(jq -r '[.transactions[]|select(.transactionType=="CREATE" and .contractName=="ClusterMember")][-1].contractAddress' "$b" 2>/dev/null)
  [ -n "$MEMBER_IMPL" ] && [ "$MEMBER_IMPL" != null ] || die "could not parse ClusterMember impl from $b"
  _save; log "✔ patha-upgrade done; MEMBER_IMPL=$MEMBER_IMPL"
}

# 4. Prime the cluster gate (app_id; hash already seeded at `cluster`).
prime_gate() {
  _load; [ -n "${CLUSTER:-}" ] && [ -n "${X:-}" ] || die "need cluster+app (run deploy+cluster first)"
  send_seq "prime-allowAppId-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  log "allowedAppIds($X)=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL")"
}

# 5. Upgrade stock proxy X → ClusterMember + bind cluster (on the box, box deployer key).
bind_member() {
  _load; [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit; reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind X=$X → impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/matrix-bind-${NODE}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --rpc-url $BOX_RPC --private-key "\$KEY" 2>&1 | grep -iE "^status|^transactionHash|error|FailedCall" | head -3
SCRIPT
  local c=""
  for _ in 1 2 3 4 5 6 7 8; do
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    [ "${c,,}" = "${CLUSTER,,}" ] && break
  done
  log "X.cluster()=$c (expect $CLUSTER)"
  [ "${c,,}" = "${CLUSTER,,}" ] || die "bind did not stick (X.cluster()=$c)"
  log "✔ bound X → $CLUSTER"
}

# 6. Poll the chain for the sidecar's self-registration; show sidecar + Matrix health.
verify() {
  _load; [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ $NODE registered: memberId=$id memberCount=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL")"
      log "  sidecar: $(ssh_box "curl -s --max-time 6 http://127.0.0.1:${SIDECAR_PORT}/healthz" 2>/dev/null)"
      log "  matrix:  $(ssh_box "curl -s --max-time 6 http://127.0.0.1:${NGINX_PORT}/.well-known/matrix/server" 2>/dev/null)"
      local xb="${X#0x}"; log "  url:     https://${xb,,}.${GW_DOMAIN}/_matrix/client/versions"
      return 0
    fi
    log "… not registered yet ($i/45, memberCount=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null))"
    sleep 20
  done
  die "$NODE not registered after timeout"
}

# 6b. Verify the matrix-admin-agent. The CVM is a sealed TEE (no exec; the box can't fetch
# per-container logs — steps-log §6), so the agent SELF-checks its egress and folds the result into
# /healthz (exposed on the box loopback like the sidecar). A `"status":"ok"` means: admin token
# acquired + Matrix synced + EGRESS CONFIRMED LOCKED. If egress isn't locked the agent fails closed
# (exits, no /healthz) — so this poll catches an un-firewalled agent.
verify_agent() {
  _load; [ -n "${X:-}" ] || die "need X (run deploy first)"
  local i body
  for i in $(seq 1 45); do
    body=$(ssh_box "curl -s --max-time 6 http://127.0.0.1:${AGENT_PORT}/healthz" 2>/dev/null)
    if echo "$body" | grep -q '"status":"ok"'; then
      log "✔ matrix-admin-agent ready (admin token + matrix sync + egress LOCKED)"
      return 0
    fi
    log "… agent not ready ($i/45): ${body:-<no response>}"
    sleep 12
  done
  die "matrix-admin-agent never reported ready — vm_logs $VM_ID and check the agent bootstrap + agent-egress-fw (the agent fails CLOSED if egress is not locked)"
}

# Day-2: roll a new compose/env onto the LIVE node, REUSING its app_id (keeps membership + CSK
# originator). Allowlists the new hash FIRST, stops the old CVM, then CreateVm(app_id=X).
update_member() {
  _load; [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster (do a full deploy first)"
  _require_agent_env; _ensure_iapw
  PGPW="${PGPW:-$(openssl rand -hex 24)}"
  local nh; nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  if [ "$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL")" = "true" ]; then
    log "hash already allowlisted"
  else
    send_seq "update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  [ -n "${VM_ID:-}" ] && { log "stopping old CVM $VM_ID"; _vmm StopVm "{\"id\":\"$VM_ID\"}" >/dev/null 2>&1 || true; }
  local out; out=$(_box_run update "$X") || die "reuse CreateVm failed"
  log "$out"
  VM_ID=$(echo "$out" | grep -oE '"vm_id":"[^"]*"' | head -1 | sed -E 's/.*:"([^"]*)".*/\1/')
  H="$nh"; _save
  log "✔ in-place update: reused X=$X new vm=$VM_ID (membership/CSK preserved). Waiting for synapse…"
  _wait_synapse
  verify_agent
}

log "=== matrix-node bring-up: $NODE ==="
case "${2:-all}" in
  deploy)  deploy_cvm ;;
  cluster) deploy_cluster ;;
  patha)   patha_upgrade ;;
  prime)   prime_gate ;;
  bind)    bind_member ;;
  verify)  verify ;;
  verify-agent) verify_agent ;;
  update)  update_member ;;
  setup)   deploy_cvm; deploy_cluster; patha_upgrade; prime_gate; bind_member ;;  # on-chain path, no register wait
  all)     deploy_cvm; deploy_cluster; patha_upgrade; prime_gate; bind_member; verify; verify_agent ;;
  *) die "usage: matrix-node.sh <node-name> [all|deploy|cluster|patha|prime|bind|verify|verify-agent|update|setup]" ;;
esac
