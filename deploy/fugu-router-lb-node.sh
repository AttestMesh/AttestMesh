#!/usr/bin/env bash
# Mesh-only HAProxy front door for fugu-router blue/green cutovers.
#
# Deploy once, then switch the active backend to a verified fugu-router CVM:
#
#   deploy/fugu-router-lb-node.sh fugu-router-lb all
#   deploy/fugu-router-lb-node.sh fugu-router-lb switch fugu-router-green
#   deploy/fugu-router-node.sh fugu-router stop
#
# The LB binds <lb-mesh-ip>:18410 for clients and <lb-mesh-ip>:18411 for its
# authenticated switch API. The dstack gateway carries only the sidecar's
# WireGuard-over-TLS ingress; the HTTP/API ports are not gateway-published.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: fugu-router-lb-node.sh <node-name> [deploy|prime|bind|start|verify|verify-health|debug-log|verify-lb|verify-proxy|verify-isolation|switch|update|stop|all] [router-node-or-ip]}"
ACTION="${2:-all}"
TARGET="${3:-}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/fugu-router-lb-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/fugu-router.env}"
FUGU_RPC_FILE="${FUGU_RPC_FILE:-$HOME/.attestmesh/fugu-router-rpc.env}"

export BOX_VCPU="${BOX_VCPU:-1}" BOX_MEM="${BOX_MEM:-1024}" BOX_DISK="${BOX_DISK:-10}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
export BOX_NO_INSTANCE_ID="${BOX_NO_INSTANCE_ID:-false}"

STATE="$LOGDIR/fugu-router-lb-node-${NODE}.state"
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
MESH_IP=${MESH_IP:-}
ACTIVE_BACKEND=${ACTIVE_BACKEND:-}
EOF
}

_load() { [ -f "$STATE" ] && source "$STATE" || true; }
ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }
ssh_mesh() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$MESH_SSH_HOST" "$@"; }

_is_ipv4() {
  [[ "${1:-}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
}

_default_cluster_env() {
  if [ -z "${CLUSTER:-}" ] || [ -z "${MEMBER_IMPL:-}" ]; then
    [ -f "$MATRIX_STATE" ] || die "missing cluster state: $MATRIX_STATE (set CLUSTER + MEMBER_IMPL to override)"
    CLUSTER="${CLUSTER:-$(grep '^CLUSTER=' "$MATRIX_STATE" | cut -d= -f2-)}"
    MEMBER_IMPL="${MEMBER_IMPL:-$(grep '^MEMBER_IMPL=' "$MATRIX_STATE" | cut -d= -f2-)}"
  fi
  [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "could not resolve CLUSTER/MEMBER_IMPL"
}

_member_mesh_ip() {
  local app="${1:-}" cluster="${2:-}" member_id raw mesh_int
  [ -n "$app" ] && [ -n "$cluster" ] || return 1
  member_id=$(cast call "$cluster" "memberIdOf(address)(bytes32)" "$app" --rpc-url "$RPC_URL" 2>/dev/null) || return 1
  [ -n "$member_id" ] && [ "$member_id" != "$ZERO32" ] || return 1
  raw=$(cast call "$cluster" "meshIpOf(bytes32)(uint32)" "$member_id" --rpc-url "$RPC_URL" 2>/dev/null) || return 1
  mesh_int=$(echo "$raw" | grep -oE '^[0-9]+' | head -1)
  [ -n "$mesh_int" ] || return 1
  python3 - "$mesh_int" <<'PY'
import ipaddress
import sys

print(ipaddress.IPv4Address(int(sys.argv[1])))
PY
}

_router_target_ip() {
  local target="$1" f app cluster ip
  [ -n "$target" ] || die "missing router node name or mesh IP"
  if _is_ipv4 "$target"; then
    echo "$target"
    return 0
  fi
  f="$LOGDIR/fugu-router-node-${target}.state"
  [ -f "$f" ] || die "missing router state for target '$target': $f"
  if [ "${FUGU_LB_BACKEND_MODE:-bridge}" = bridge ]; then
    ip=$(_router_target_bridge_ip "$f" 2>/dev/null || true)
    if [ -n "$ip" ]; then
      echo "$ip"
      return 0
    fi
  fi
  ip=$(grep '^MESH_IP=' "$f" | cut -d= -f2-)
  if [ -n "$ip" ]; then
    echo "$ip"
    return 0
  fi
  app=$(grep '^X=' "$f" | cut -d= -f2-)
  cluster=$(grep '^CLUSTER=' "$f" | cut -d= -f2-)
  cluster="${cluster:-${CLUSTER:-}}"
  ip=$(_member_mesh_ip "$app" "$cluster") || die "could not resolve mesh IP for router target '$target'"
  echo "$ip"
}

_router_target_bridge_ip() {
  local state_file="$1" vm_id
  vm_id=$(grep '^VM_ID=' "$state_file" | cut -d= -f2-)
  [ -n "$vm_id" ] || return 1
  ssh_box "sudo bash -s" <<SCRIPT
set -u
VMID="$vm_id"
MAC=\$(ps axww -o args | grep -F "/srv/data/dstack/vm/\$VMID/" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2 || true)
[ -n "\$MAC" ] || exit 1
IP=\$(ip neigh show dev dstack-br0 | grep -i "\$MAC" | grep -oE '^10\.0\.[0-9]+\.[0-9]+' | head -1 || true)
[ -n "\$IP" ] || exit 1
echo "\$IP"
SCRIPT
}

_ensure_lb_secrets() {
  if [ -f "$SECRETS_FILE" ]; then
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
  fi
  if [ -f "$FUGU_RPC_FILE" ]; then
    # shellcheck disable=SC1090
    source "$FUGU_RPC_FILE"
  fi
  FUGU_LB_ADMIN_KEY="${FUGU_LB_ADMIN_KEY:-${LITELLM_MASTER_KEY:-}}"
  [ -n "${FUGU_LB_ADMIN_KEY:-}" ] || die "missing FUGU_LB_ADMIN_KEY (or LITELLM_MASTER_KEY in $SECRETS_FILE)"
}

_require_litellm_key() {
  _ensure_lb_secrets
  [ -n "${LITELLM_MASTER_KEY:-}" ] || die "missing LITELLM_MASTER_KEY in $SECRETS_FILE"
}

_require_env() {
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  _ensure_lb_secrets
  if [ -n "${FUGU_LB_INITIAL_BACKEND:-}" ] && ! _is_ipv4 "$FUGU_LB_INITIAL_BACKEND"; then
    _default_cluster_env
    FUGU_LB_INITIAL_BACKEND="$(_router_target_ip "$FUGU_LB_INITIAL_BACKEND")"
  fi
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

_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/fugu-router-lb-node-box.py" "$BOX_HOST:/tmp/fugu-router-lb-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n' "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n' "${CVM_RPC_URL:-${FUGU_RPC_URL:-$RPC_URL}}"
    printf 'E_BUNDLER_URL=%q\n' "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
    printf 'E_GAS_POLICY_ID=%q\n' "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "$INDEXER_REGISTRY_ADDR"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_FUGU_LB_ADMIN_KEY=%q\n' "${FUGU_LB_ADMIN_KEY:-}"
    printf 'E_FUGU_LB_INITIAL_BACKEND=%q\n' "${FUGU_LB_INITIAL_BACKEND:-}"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_NO_INSTANCE_ID='$BOX_NO_INSTANCE_ID' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/fugu-router-lb-node-box.py $mode $app_id $vm_id'"
}

_box_stop_vm() {
  [ -n "${VM_ID:-}" ] || die "need VM_ID (run deploy first)"
  scp -o BatchMode=yes -q "$HERE/fugu-router-lb-node-box.py" "$BOX_HOST:/tmp/fugu-router-lb-node-box.py"
  ssh_box "sudo BOX_NAME='$NODE' $BOX_PY /tmp/fugu-router-lb-node-box.py stop '$VM_ID'"
}

_box_start_vm() {
  [ -n "${VM_ID:-}" ] || die "need VM_ID (run deploy first)"
  scp -o BatchMode=yes -q "$HERE/fugu-router-lb-node-box.py" "$BOX_HOST:/tmp/fugu-router-lb-node-box.py"
  ssh_box "sudo BOX_NAME='$NODE' $BOX_PY /tmp/fugu-router-lb-node-box.py start '$VM_ID'"
}

deploy_cvm() {
  _load; _default_cluster_env; _require_env
  _save
  log "▶ box deploy_app fugu-router-lb node=$NODE compose=$COMPOSE cluster=$CLUSTER"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed fugu-router-lb app_id=$X compose_hash=$H vm=$VM_ID"
  log "mesh-only: LB <mesh-ip>:18410, control <mesh-ip>:18411"
}

prime_gate() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  local allowed_hash allowed_app
  allowed_hash=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_hash" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "fugu-lb-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  fi
  allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_app" = true ]; then
    log "app id already allowlisted"
  else
    send_seq "fugu-lb-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  fi
}

bind_member() {
  _load; _default_cluster_env; _require_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local nh
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "hash pre-check failed: could not compute compose_hash"
  [ "0x$nh" = "0x${H#0x}" ] || die "hash pre-check MISMATCH: live compose measures 0x$nh but deployed hash is $H"
  log "✔ hash pre-check OK (0x$nh)"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind fugu-router-lb X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/fugu-lb-bind-${NODE}.$(ts).log"
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
  log "✔ bound fugu-router-lb node X -> $CLUSTER"
}

verify() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ fugu-router-lb registered: memberId=$id memberCount=$count"
      return 0
    fi
    log "… fugu-router-lb not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  die "fugu-router-lb did not register"
}

_bridge_ip_snippet() {
  cat <<'SNIP'
MAC=$(ps axww -o args | grep -F "$VMID" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2)
[ -n "$MAC" ] || { echo "NO_QEMU"; exit 3; }
IP=$(ip neigh show dev dstack-br0 | grep -i "$MAC" | grep -oE '^10\.0\.[0-9]+\.[0-9]+' | head -1)
[ -n "$IP" ] || { echo "NO_IP"; exit 4; }
SNIP
}

verify_health() {
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

debug_log() {
  _load
  [ -n "${VM_ID:-}" ] || die "need VM_ID (run deploy first)"
  local out rc=0
  out=$(ssh_box "sudo bash -s" <<SCRIPT 2>&1
set -euo pipefail
VMID="$VM_ID"
$(_bridge_ip_snippet)
echo "DEBUG_LOG: vm=\$VMID bridge_ip=\$IP"
curl -fsS --max-time 10 "http://\$IP:19090/sidecar.log"
SCRIPT
  ) || rc=$?
  echo "$out" | tee "$LOGDIR/fugu-lb-sidecar-${NODE}.$(ts).log" >&2
  return "$rc"
}

_discover_mesh_ip() {
  [ -n "${MESH_IP:-}" ] && return 0
  _default_cluster_env
  local resolved
  resolved=$(_member_mesh_ip "${X:-}" "${CLUSTER:-}") || return 1
  MESH_IP="$resolved"
  _save
}

verify_lb() {
  _load; _ensure_lb_secrets
  _discover_mesh_ip || die "LB node not registered with a mesh IP"
  local out
  out=$(ssh_mesh "curl -fsS --max-time 10 'http://$MESH_IP:18411/healthz'" 2>&1) || die "LB control health failed: $out"
  echo "$out"
  echo "$out" | grep -q '"haproxy_socket": true' || die "LB control health missing haproxy socket"
  log "✔ fugu-router-lb control API live on $MESH_IP:18411"
}

switch_backend() {
  _load; _ensure_lb_secrets
  _discover_mesh_ip || die "LB node not registered with a mesh IP"
  local target="${1:-${TARGET:-}}" backend out
  [ -n "$target" ] || die "usage: fugu-router-lb-node.sh $NODE switch <router-node-or-ip>"
  _default_cluster_env
  backend="$(_router_target_ip "$target")"
  log "▶ switching fugu-router-lb $NODE to backend $target ($backend)"
  out=$(ssh_mesh "FUGU_LB_ADMIN_KEY=$(printf '%q' "$FUGU_LB_ADMIN_KEY") BACKEND=$(printf '%q' "$backend") MESH_IP=$(printf '%q' "$MESH_IP") bash -s" <<'SCRIPT' 2>&1
payload="{\"backend\":\"$BACKEND\"}"
curl -fsS --max-time 30 \
  -H "Authorization: Bearer $FUGU_LB_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  --data-binary "$payload" \
  "http://$MESH_IP:18411/switch"
SCRIPT
) || die "switch failed: $out"
  ACTIVE_BACKEND="$backend"; _save
  echo "$out" | tee "$LOGDIR/fugu-lb-switch-${NODE}.$(ts).log" >&2
  log "✔ fugu-router-lb active backend is now $backend"
}

verify_proxy() {
  _load; _require_litellm_key
  _discover_mesh_ip || die "LB node not registered with a mesh IP"
  local out
  out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
models=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "http://$MESH_IP:18410/v1/models" 2>&1)
echo "\$models" | grep -q '"fugu-ultra"' || { echo "LB_PROXY: FAIL - /v1/models missing fugu-ultra: \$models"; exit 2; }
echo "\$models" | grep -q '"glm-5.2"' || { echo "LB_PROXY: FAIL - /v1/models missing glm-5.2: \$models"; exit 2; }
echo "\$models" | grep -q '"qwen/qwen3-embedding-8b"' || { echo "LB_PROXY: FAIL - /v1/models missing qwen/qwen3-embedding-8b: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-4.5"' || { echo "LB_PROXY: FAIL - /v1/models missing grok-4.5: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-4.3"' || { echo "LB_PROXY: FAIL - /v1/models missing grok-4.3: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-imagine-image-quality"' || { echo "LB_PROXY: FAIL - /v1/models missing grok-imagine-image-quality: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-imagine-image"' || { echo "LB_PROXY: FAIL - /v1/models missing grok-imagine-image: \$models"; exit 2; }
curl -fsS --max-time 10 "http://$MESH_IP:18410/health/liveliness" >/dev/null 2>&1 || { echo "LB_PROXY: FAIL - liveliness"; exit 3; }
fugu_comp=\$(curl -fsS --max-time 120 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "http://$MESH_IP:18410/v1/chat/completions" \
  -d '{"model":"fugu-ultra","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-lb-proxy"]}}' 2>&1)
echo "\$fugu_comp" | grep -q '"choices"' || { echo "LB_PROXY: FAIL - fugu-ultra completion: \$fugu_comp"; exit 4; }
glm_comp=\$(curl -fsS --max-time 120 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "http://$MESH_IP:18410/v1/chat/completions" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-lb-proxy","redpill-glm"]}}' 2>&1)
echo "\$glm_comp" | grep -q '"choices"' || { echo "LB_PROXY: FAIL - glm-5.2 completion: \$glm_comp"; exit 5; }
emb=\$(curl -fsS --max-time 120 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "http://$MESH_IP:18410/v1/embeddings" \
  -d '{"model":"qwen/qwen3-embedding-8b","input":"fugu-router lb verify embedding smoke"}' 2>&1)
echo "\$emb" | grep -q '"embedding"' || { echo "LB_PROXY: FAIL - qwen embedding: \$emb"; exit 6; }
grok45_comp=\$(curl -fsS --max-time 180 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "http://$MESH_IP:18410/v1/chat/completions" \
  -d '{"model":"grok-4.5","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-lb-proxy","xai-grok-4.5"]}}' 2>&1)
echo "\$grok45_comp" | grep -q '"choices"' || { echo "LB_PROXY: FAIL - grok-4.5 completion: \$grok45_comp"; exit 7; }
grok43_comp=\$(curl -fsS --max-time 180 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "http://$MESH_IP:18410/v1/chat/completions" \
  -d '{"model":"grok-4.3","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-lb-proxy","xai-grok-4.3"]}}' 2>&1)
echo "\$grok43_comp" | grep -q '"choices"' || { echo "LB_PROXY: FAIL - grok-4.3 completion: \$grok43_comp"; exit 8; }
img_quality=\$(curl -fsS --max-time 240 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "http://$MESH_IP:18410/v1/images/generations" \
  -d '{"model":"grok-imagine-image-quality","prompt":"A tiny plain red square icon on a white background.","n":1,"response_format":"url","metadata":{"tags":["verify-lb-proxy","xai-grok-imagine-image-quality"]}}' 2>&1)
echo "\$img_quality" | grep -q '"data"' || { echo "LB_PROXY: FAIL - grok-imagine-image-quality image generation: \$img_quality"; exit 9; }
dash45=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "http://$MESH_IP:18410/fugu/api/summary?window=5h&model=grok-4.5" 2>&1)
echo "\$dash45" | grep -q '"account_id":"direct:grok-4.5"' || { echo "LB_PROXY: FAIL - dashboard filter missing direct grok-4.5 route: \$dash45"; exit 10; }
dash43=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "http://$MESH_IP:18410/fugu/api/summary?window=5h&model=grok-4.3" 2>&1)
echo "\$dash43" | grep -q '"account_id":"direct:grok-4.3"' || { echo "LB_PROXY: FAIL - dashboard filter missing direct grok-4.3 route: \$dash43"; exit 11; }
dash_img_quality=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "http://$MESH_IP:18410/fugu/api/summary?window=5h&model=grok-imagine-image-quality" 2>&1)
echo "\$dash_img_quality" | grep -q '"account_id":"direct:grok-imagine-image-quality"' || { echo "LB_PROXY: FAIL - dashboard filter missing direct grok-imagine-image-quality route: \$dash_img_quality"; exit 12; }
echo "LB_PROXY: PASS"
SCRIPT
)
  echo "$out" | tee "$LOGDIR/fugu-lb-proxy-${NODE}.$(ts).log" >&2
  echo "$out" | grep -q '^LB_PROXY: PASS' || die "verify-proxy through LB failed"
  log "✔ LB proxy live on $MESH_IP:18410 — Grok chat/image and dashboard filters verified"
}

verify_isolation() {
  _load
  [ -n "${VM_ID:-}" ] || die "need VM_ID"
  log "▶ host-isolation check vm=$VM_ID"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/fugu-lb-isolation-${NODE}.$(ts).log"
set -u
VMID="$VM_ID"
$(_bridge_ip_snippet)
echo "ISOLATION: vm=\$VMID bridge_ip=\$IP"
bad=0
for p in 18410 18411; do
  if timeout 3 bash -c "</dev/tcp/\$IP/\$p" 2>/dev/null; then
    echo "  !! \$IP:\$p REACHABLE from host — INVARIANT VIOLATION"; bad=1
  else echo "  \$IP:\$p refused from host (good)"; fi
done
timeout 3 bash -c "</dev/tcp/\$IP/9090" 2>/dev/null && echo "  \$IP:9090 answers (expected)" || { echo "  !! 9090 not answering"; bad=1; }
[ \$bad -eq 0 ] && echo "ISOLATION: PASS" || { echo "ISOLATION: FAIL"; exit 5; }
SCRIPT
  local rc=${PIPESTATUS[0]}
  [ "$rc" = 0 ] || die "host-isolation check failed (rc=$rc)"
  log "✔ host-isolation invariant holds"
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
    send_seq "fugu-lb-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh" \
      || die "failed to allowlist compose hash 0x$nh"
  fi
  settle_compose_hash_for_kms "$CLUSTER" "$nh"
  out=$(_box_run update "$X" "$VM_ID") || die "LB in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r '.vm_id // empty'); [ -n "$VM_ID" ] || VM_ID="$(_load; echo "${VM_ID:-}")"
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ fugu-router-lb update complete mode=$mode vm=$VM_ID"
}

stop_member() {
  _load
  [ -n "${VM_ID:-}" ] || die "need VM_ID in $STATE"
  local out
  out=$(_box_stop_vm) || die "failed to stop VM $VM_ID"
  echo "$out"
  log "✔ fugu-router-lb VM stop requested vm=$VM_ID"
}

start_member() {
  _load
  [ -n "${VM_ID:-}" ] || die "need VM_ID in $STATE"
  local out
  out=$(_box_start_vm) || die "failed to start VM $VM_ID"
  echo "$out"
  log "✔ fugu-router-lb VM start requested vm=$VM_ID"
}

log "=== Fugu-router LB AttestMesh node: $NODE ==="
case "$ACTION" in
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  start) start_member ;;
  verify) verify ;;
  verify-health) verify_health ;;
  debug-log) debug_log ;;
  verify-lb) verify_lb ;;
  verify-proxy) verify_proxy ;;
  verify-isolation) verify_isolation ;;
  switch) switch_backend "$TARGET" ;;
  update) update_member ;;
  stop) stop_member ;;
  all)
    deploy_cvm
    prime_gate
    bind_member
    start_member
    verify
    verify_health
    initial_backend="${FUGU_LB_INITIAL_BACKEND:-}"
    verify_lb
    if [ -z "$initial_backend" ]; then
      switch_backend "${FUGU_LB_INITIAL_ROUTER:-fugu-router}"
    fi
    verify_proxy
    ;;
  *) die "usage: fugu-router-lb-node.sh <node-name> [deploy|prime|bind|start|verify|verify-health|debug-log|verify-lb|verify-proxy|verify-isolation|switch|update|stop|all] [router-node-or-ip]" ;;
esac
