#!/usr/bin/env bash
# Hindsight (agent memory) AttestMesh node on the self-hosted on-chain dstack box.
#
# Deploys https://github.com/vectorize-io/hindsight as a full, on-chain-anchored
# AttestMesh node via the canonical Path-A flow:
#   deploy (stock DstackApp + sealed env + bridge CreateVm, gateway ON for wg ingress)
#     -> prime (allowlist compose_hash + app_id on the cluster)
#     -> bind  (upgradeToAndCall the proxy to ClusterMember, box deployer key)
#     -> verify (sidecar self-registers -> memberIdOf(X) != 0)
#     -> verify-sidecar (box-side :9090 health; the port only binds POST-bind)
#     -> verify-app / verify-e2e (over the WIREGUARD MESH via the ssh-node's
#        sshd-mesh jump — the node has NO tailscale and NO public HTTP)
#     -> verify-isolation (box-side: private ports refuse at the bridge IP)
#
# Day-2 rolls: `update` recomputes the compose_hash, allowlists it FIRST, then does
# an in-place, DISK-PRESERVING UpgradeApp (BOX_FRESH_DISK=1 forces a wipe of the
# pg0 memory store).
#
# Secrets are read at runtime from ~/.attestmesh/ and passed in-memory over SSH as
# E_* vars; nothing secret is written to disk except the state file (0600), which
# persists the generated Hindsight tenant/CP keys across rolls.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: hindsight-node.sh <node-name> [deploy|verify-sidecar|prime|bind|verify|verify-app|verify-e2e|verify-isolation|update|setup|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/hindsight-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"

# The mesh jump host: the ssh-node's sshd-mesh (port 1023, sidecar netns — it sits
# ON the wg mesh). See deploy/compose/ssh-node.yaml + ~/.ssh/config.
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"

# LLM: OpenAI-compatible endpoint, pinned + egress-enforced in the compose.
# gpt-oss is the family Hindsight recommends for structured-output extraction.
HINDSIGHT_LLM_BASE_URL="${HINDSIGHT_LLM_BASE_URL:-https://api.redpill.ai/v1}"
HINDSIGHT_LLM_MODEL="${HINDSIGHT_LLM_MODEL:-openai/gpt-oss-120b}"
LLM_KEY_FILE="${LLM_KEY_FILE:-$HOME/.attestmesh/redpill-key}"

# CVM sizing (torch CPU inference for local embeddings/reranker + pg0 + Next.js UI).
export BOX_VCPU="${BOX_VCPU:-4}" BOX_MEM="${BOX_MEM:-6144}" BOX_DISK="${BOX_DISK:-40}"
# No host port-forwards; bridge mode; gateway ON only for wg-over-gateway-TCP (51900).
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

STATE="$LOGDIR/hindsight-node-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000

_save() {
  umask 077
  cat > "$STATE" <<EOF
X=${X:-}
H=${H:-}
VM_ID=${VM_ID:-}
CLUSTER=${CLUSTER:-}
MEMBER_IMPL=${MEMBER_IMPL:-}
GATEWAY_DOMAIN=${GATEWAY_DOMAIN:-}
TAK=${TAK:-}
CPK=${CPK:-}
MESH_IP=${MESH_IP:-}
EOF
}

_load() { [ -f "$STATE" ] && source "$STATE" || true; }
ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }
ssh_mesh() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$MESH_SSH_HOST" "$@"; }

# Hindsight joins the existing Matrix cluster by default (reuses the already-deployed
# Path-A ClusterMember impl). Override CLUSTER + MEMBER_IMPL to target a fresh cluster.
_default_cluster_env() {
  if [ -z "${CLUSTER:-}" ] || [ -z "${MEMBER_IMPL:-}" ]; then
    [ -f "$MATRIX_STATE" ] || die "missing cluster state: $MATRIX_STATE (set CLUSTER + MEMBER_IMPL to override)"
    CLUSTER="${CLUSTER:-$(grep '^CLUSTER=' "$MATRIX_STATE" | cut -d= -f2-)}"
    MEMBER_IMPL="${MEMBER_IMPL:-$(grep '^MEMBER_IMPL=' "$MATRIX_STATE" | cut -d= -f2-)}"
  fi
  [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "could not resolve CLUSTER/MEMBER_IMPL"
}

_require_env() {
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"

  if [ -z "${HINDSIGHT_LLM_API_KEY:-}" ]; then
    [ -s "$LLM_KEY_FILE" ] || die "missing LLM key file: $LLM_KEY_FILE (or set HINDSIGHT_LLM_API_KEY)"
    HINDSIGHT_LLM_API_KEY="$(tr -d '[:space:]' < "$LLM_KEY_FILE")"
  fi
  # Generated ONCE, persisted in the state file, reused across rolls so API
  # consumers' credentials don't churn.
  TAK="${HINDSIGHT_TENANT_API_KEY:-${TAK:-$(openssl rand -hex 32)}}"
  CPK="${HINDSIGHT_CP_ACCESS_KEY:-${CPK:-$(openssl rand -hex 24)}}"
}

send_seq() {
  local label="$1"; shift
  local nonce
  nonce=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL")
  run_step "$label" cast send "$@" --nonce "$nonce" --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY" && return 0
  log "↻ $label: refetching nonce + retrying"
  sleep 4
  nonce=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL")
  run_step "${label}-retry" cast send "$@" --nonce "$nonce" --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY"
}

# Forward compose + helper to the box and run a box-side mode with sealed E_* env.
_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/hindsight-node-box.py" "$BOX_HOST:/tmp/hindsight-node-box.py"
  ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    E_CHAIN_ID='$CHAIN_ID' E_RPC_URL='$RPC_URL' E_BUNDLER_URL='${BUNDLER_URL:-$RPC_URL}' E_GAS_POLICY_ID='${GAS_POLICY_ID:-}' E_INDEXER_REGISTRY_ADDR='$INDEXER_REGISTRY_ADDR' E_GATEWAY_DOMAIN='$GATEWAY_DOMAIN' \
    E_HINDSIGHT_API_LLM_BASE_URL='$HINDSIGHT_LLM_BASE_URL' E_HINDSIGHT_API_LLM_MODEL='$HINDSIGHT_LLM_MODEL' E_HINDSIGHT_API_LLM_API_KEY='$HINDSIGHT_LLM_API_KEY' \
    E_HINDSIGHT_API_TENANT_API_KEY='$TAK' E_HINDSIGHT_CP_ACCESS_KEY='$CPK' \
    E_DSTACK_DOCKER_USERNAME='${guser:-dmvt}' E_DSTACK_DOCKER_PASSWORD='$gtok' E_DSTACK_DOCKER_REGISTRY='ghcr.io' \
    $BOX_PY /tmp/hindsight-node-box.py $mode $app_id $vm_id"
}

deploy_cvm() {
  _load; _default_cluster_env; _require_env
  _save
  log "▶ box deploy_app hindsight node=$NODE compose=$COMPOSE cluster=$CLUSTER"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed hindsight node app_id=$X compose_hash=$H vm=$VM_ID"
  log "mesh-only: API/UI reachable ONLY at <mesh-ip>:18888/:18999 by cluster members"
}

# Box-side bridge-IP resolver for this VM (qemu args -> TAP MAC -> ip neigh).
_bridge_ip_snippet() {
  cat <<'SNIP'
MAC=$(ps -eo args | grep -F "$VMID" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2)
[ -n "$MAC" ] || { echo "NO_QEMU"; exit 3; }
IP=$(ip neigh show dev dstack-br0 | grep -i "$MAC" | grep -oE '^10\.0\.[0-9]+\.[0-9]+' | head -1)
[ -n "$IP" ] || { echo "NO_IP"; exit 4; }
SNIP
}

# POST-BIND observability: the sidecar's :9090 healthz is compose-published, so
# the box can poll it at the CVM's bridge IP (the TEE blocks container logs).
# NOTE: this agent build binds the health server only AFTER member.cluster()
# resolves — pre-bind the port is closed while the sidecar loops on
# "cluster not resolvable yet (awaiting ClusterMember upgrade?)". So this check
# is meaningful only after `bind` (learned the hard way, 2026-07-01).
verify_sidecar() {
  _load
  [ -n "${VM_ID:-}" ] || die "need VM_ID (run deploy first)"
  local i out
  for i in $(seq 1 40); do
    out=$(ssh_box "sudo bash -s" <<SCRIPT 2>/dev/null
set -u
VMID="$VM_ID"
$(_bridge_ip_snippet)
curl -sS --max-time 5 "http://\$IP:9090/healthz" 2>/dev/null
SCRIPT
)
    if echo "$out" | grep -q '"phase"'; then
      log "✔ sidecar healthz: $out"
      return 0
    fi
    log "… sidecar not answering yet ($i/40): ${out:-<no response>}"
    sleep 15
  done
  die "sidecar :9090 never answered at the bridge IP — check vm_logs $VM_ID"
}

prime_gate() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  local allowed_hash allowed_app
  allowed_hash=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_hash" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "hindsight-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  fi
  allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_app" = true ]; then
    log "app id already allowlisted"
  else
    send_seq "hindsight-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  fi
}

bind_member() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind hindsight X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/hindsight-bind-${NODE}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --rpc-url $BOX_RPC --private-key "\$KEY" 2>&1 | grep -iE "^status|^transactionHash|error|FailedCall" | head -3
SCRIPT
  local c=""
  for _ in 1 2 3 4 5 6 7 8; do
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    [ "${c,,}" = "${CLUSTER,,}" ] && break
    sleep 2
  done
  log "X.cluster()=$c (expect $CLUSTER)"
  [ "${c,,}" = "${CLUSTER,,}" ] || die "bind did not stick (X.cluster()=$c)"
  log "✔ bound hindsight node X -> $CLUSTER"
}

verify() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ hindsight node registered: memberId=$id memberCount=$count"
      return 0
    fi
    log "… hindsight node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  die "hindsight node did not register"
}

# Discover the hindsight node's mesh IP from the jump host: enumerate wg peer
# allowed-ips and probe :18888/health (only the hindsight node serves it).
# Runs REMOTELY on sshd-mesh; echoes "MESH_IP=<ip>" on success.
_mesh_discover_snippet() {
  cat <<'SNIP'
IF=$(wg show interfaces 2>/dev/null | awk '{print $1; exit}')
CAND=""
[ -n "$IF" ] && CAND=$(wg show "$IF" allowed-ips 2>/dev/null | awk '{print $2}' | cut -d/ -f1)
# fallback: per-peer /32 routes on the wg iface
[ -n "$CAND" ] || CAND=$(ip -o route show 2>/dev/null | awk '/dev wg/ {print $1}' | cut -d/ -f1)
for ip in $CAND; do
  if curl -sf --max-time 4 "http://$ip:18888/health" >/dev/null 2>&1; then
    echo "MESH_IP=$ip"
    exit 0
  fi
done
echo "MESH_IP="
SNIP
}

# App reachability over the mesh: /health must return 200 {"status":"healthy"}.
# 503 = engine still initializing (model load + pg0 init + migrations, <=300s)
# or the boot LLM-verification call failed (egress/model problem).
verify_app() {
  _load
  local i out ip
  for i in $(seq 1 45); do
    out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
$(_mesh_discover_snippet)
SCRIPT
)
    ip=$(echo "$out" | grep -oE '^MESH_IP=.*' | cut -d= -f2)
    if [ -n "$ip" ]; then
      MESH_IP="$ip"; _save
      local body
      body=$(ssh_mesh "curl -sS --max-time 6 http://$ip:18888/health" 2>/dev/null)
      log "✔ hindsight healthy over the mesh at $ip:18888 — $body"
      return 0
    fi
    log "… hindsight not reachable over the mesh yet ($i/45)"
    sleep 20
  done
  die "hindsight /health never answered over the mesh — check sshd-mesh jump ($MESH_SSH_HOST), wg peering, and the CVM"
}

# Full retain -> recall roundtrip THROUGH the LLM (proves redpill egress +
# structured-output extraction + embedded pg0 + auth, end to end). The tenant
# key travels inside the ssh-encrypted script text (bash -s stdin), never argv.
verify_e2e() {
  _load
  [ -n "${TAK:-}" ] || die "no tenant API key in state (run deploy first)"
  [ -n "${MESH_IP:-}" ] || die "no MESH_IP in state (run verify-app first)"
  local out
  out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
B="http://$MESH_IP:18888"
H="Authorization: Bearer $TAK"
R=\$(curl -sS --max-time 180 -X POST "\$B/v1/default/banks/attestmesh-smoke/memories" \
  -H 'Content-Type: application/json' -H "\$H" \
  -d '{"items":[{"content":"Alice works at Google as a software engineer"}],"async":false}')
echo "RETAIN: \$R"
sleep 2
Q=\$(curl -sS --max-time 60 -X POST "\$B/v1/default/banks/attestmesh-smoke/memories/recall" \
  -H 'Content-Type: application/json' -H "\$H" \
  -d '{"query":"What does Alice do?","max_tokens":2048}')
echo "RECALL: \$Q"
A=\$(curl -sS --max-time 6 -o /dev/null -w '%{http_code}' -X POST "\$B/v1/default/banks/attestmesh-smoke/memories/recall" \
  -H 'Content-Type: application/json' -d '{"query":"x"}')
echo "NOAUTH: \$A"
SCRIPT
)
  echo "$out" | tee "$LOGDIR/hindsight-e2e-${NODE}.$(ts).log" >&2
  echo "$out" | grep -q '"success": *true'      || die "retain did not report success"
  echo "$out" | grep -qiE 'RECALL:.*(google|engineer|alice)' || die "recall returned nothing about the retained fact"
  echo "$out" | grep -qE '^NOAUTH: *401'         || die "API answered WITHOUT the tenant key — auth gate not active"
  log "✔ E2E OK: retain -> LLM extraction -> recall roundtrip + auth gate (401 without key)"
}

# Host-isolation: from the BOX, hindsight's private ports must be UNREACHABLE at
# the CVM's bridge IP. 9090 (sidecar health) + 51900 (wg ingress) are the ONLY
# published ports — 9090 must answer, the app/mesh ports must not.
verify_isolation() {
  _load; [ -n "${VM_ID:-}" ] || die "no VM_ID in state (run deploy first)"
  log "▶ host-isolation check for vm=$VM_ID"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/hindsight-isolation-${NODE}.$(ts).log"
set -u
VMID="$VM_ID"
$(_bridge_ip_snippet)
echo "ISOLATION: vm=\$VMID bridge_ip=\$IP"
bad=0
for p in 8888 9999 18888 18999; do
  if curl -sS --max-time 3 -o /dev/null "http://\$IP:\$p/" 2>/dev/null; then
    echo "  !! \$IP:\$p REACHABLE from host — INVARIANT VIOLATION"; bad=1
  else echo "  \$IP:\$p refused from host (good)"; fi
done
curl -sS --max-time 3 "http://\$IP:9090/healthz" >/dev/null 2>&1 \
  && echo "  \$IP:9090 sidecar health answers (expected, published)" \
  || { echo "  !! \$IP:9090 sidecar health NOT answering"; bad=1; }
[ \$bad -eq 0 ] && echo "ISOLATION: PASS" || { echo "ISOLATION: FAIL"; exit 5; }
SCRIPT
  local rc=${PIPESTATUS[0]}
  [ "$rc" = 0 ] || die "host-isolation check failed (rc=$rc) — see the log above"
  log "✔ isolation holds: app/mesh ports refuse from the host; only sidecar health + wg are published"
}

update_member() {
  _load; _default_cluster_env; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  local nh allowed out j mode
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "hindsight-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r '.vm_id // empty'); [ -n "$VM_ID" ] || VM_ID="$(_load; echo "${VM_ID:-}")"
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ hindsight node update complete mode=$mode vm=$VM_ID"
  # NOTE: service checks (verify / verify-app / verify-e2e) are separate steps.
}

log "=== Hindsight AttestMesh node: $NODE ==="
case "$ACTION" in
  deploy) deploy_cvm ;;
  verify-sidecar) verify_sidecar ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  verify-app) verify_app ;;
  verify-e2e) verify_e2e ;;
  verify-isolation) verify_isolation ;;
  update) update_member ;;
  setup) deploy_cvm; prime_gate; bind_member ;;
  all) deploy_cvm; prime_gate; bind_member; verify; verify_sidecar; verify_app; verify_e2e; verify_isolation ;;
  *) die "usage: hindsight-node.sh <node-name> [deploy|verify-sidecar|prime|bind|verify|verify-app|verify-e2e|verify-isolation|update|setup|all]" ;;
esac
