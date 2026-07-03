#!/usr/bin/env bash
# RunYard AttestMesh node on the self-hosted on-chain dstack box.
#
# Deploys RunYard (a self-hosted control plane for AI agent runs: `hub` web app +
# `runner`) as a full, on-chain-anchored AttestMesh node via the canonical Path-A
# flow:
#   deploy (stock DstackApp + sealed env + bridge CreateVm, gateway OFF, no_instance_id)
#     -> prime (allowlist compose_hash + app_id on the cluster)
#     -> bind  (upgradeToAndCall the proxy to ClusterMember, box deployer key)
#     -> verify (sidecar self-registers -> memberIdOf(X) != 0)
#     -> verify-health (sidecar :9090 reachable at the CVM dstack-br0 IP)
#     -> verify-hub    (hub :80 reachable at the CVM dstack-br0 IP — the admin path)
#
# ACCESS MODEL (b): the hub is expose-only (not gateway-published) — private,
# box-reachable at the CVM dstack-br0 IP for admin. Gateway is OFF (see the
# BOX_GATEWAY_ENABLED note below); the sidecar registers on-chain via RPC/bundler,
# not the gateway, so membership is unaffected.
#
# Day-2 rolls: `update` recomputes the compose_hash, allowlists it FIRST, then does
# an in-place, DISK-PRESERVING UpgradeApp (BOX_FRESH_DISK=1 forces a wipe; with
# no_instance_id the app-bound disk key means volumes persist even across a fresh
# CreateVm unless explicitly wiped).
#
# Secrets: generated ONCE and persisted to $SECRETS_FILE (git-ignored, umask 077),
# then read on every run. Regenerating would rotate SECRETS_ENC_KEY / the session
# secret on the next `update` and break stored state — so we NEVER regenerate if
# the file exists. Values are passed in-memory over SSH as E_* vars; nothing
# secret is written to the repo.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: runyard-node.sh <node-name> [deploy|prime|bind|verify|verify-health|verify-hub|update|setup|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/runyard-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"

# Secrets file (git-ignored, never committed). Generated on first run; read after.
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/runyard.env}"

# CVM sizing (operator-requested: RunYard runner builds per-run containers).
export BOX_VCPU="${BOX_VCPU:-16}" BOX_MEM="${BOX_MEM:-65536}" BOX_DISK="${BOX_DISK:-500}"
# Bridge mode, gateway OFF, no host port-forwards, app-bound disk key.
# GATEWAY OFF is REQUIRED with no_instance_id:true — on dstack 0.5.11 the gateway
# registration rejects an empty instance_id (400 "instance id is empty") and the
# CVM boot-loops. The sidecar still self-registers on-chain (RPC/bundler, no
# gateway) and the hub is reached box-side via the dstack-br0 bridge IP, so gateway
# off costs only wg-over-gateway mesh transport — which has no gateway-on C3 peer
# today (matrix-node is gateway-off). Revisit only if a gateway-on peer appears AND
# a dstack version fixes the no_instance_id/gateway conflict.
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-false}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
export BOX_NO_INSTANCE_ID="${BOX_NO_INSTANCE_ID:-true}"

STATE="$LOGDIR/runyard-node-${NODE}.state"
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
EOF
}

_load() { [ -f "$STATE" ] && source "$STATE" || true; }
ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }

# RunYard joins the existing Matrix cluster (C3) by default — reuses the already-
# deployed Path-A ClusterMember impl. Override CLUSTER + MEMBER_IMPL to retarget.
_default_cluster_env() {
  if [ -z "${CLUSTER:-}" ] || [ -z "${MEMBER_IMPL:-}" ]; then
    [ -f "$MATRIX_STATE" ] || die "missing cluster state: $MATRIX_STATE (set CLUSTER + MEMBER_IMPL to override)"
    CLUSTER="${CLUSTER:-$(grep '^CLUSTER=' "$MATRIX_STATE" | cut -d= -f2-)}"
    MEMBER_IMPL="${MEMBER_IMPL:-$(grep '^MEMBER_IMPL=' "$MATRIX_STATE" | cut -d= -f2-)}"
  fi
  [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "could not resolve CLUSTER/MEMBER_IMPL"
}

# Generate the three RunYard secrets ONCE, persist, then load. RUNYARD_HUB_TOKEN
# is mapped to the same value as RUNYARD_HUB_BOOTSTRAP_TOKEN (single admin cred).
_ensure_secrets() {
  if [ ! -f "$SECRETS_FILE" ]; then
    log "generating RunYard secrets -> $SECRETS_FILE (first run)"
    mkdir -p "$(dirname "$SECRETS_FILE")"
    umask 077
    local sess enc boot
    sess=$(openssl rand -hex 32)
    enc=$(openssl rand -hex 32)
    boot=$(openssl rand -hex 32)
    cat > "$SECRETS_FILE" <<EOF
RUNYARD_HUB_SESSION_SECRET=$sess
SECRETS_ENC_KEY=$enc
RUNYARD_HUB_BOOTSTRAP_TOKEN=$boot
EOF
  fi
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  local k
  # TS_AUTHKEY (tailnet ingress) is minted out-of-band and appended to the secrets
  # file; required now that the compose runs a tailscale sidecar.
  for k in RUNYARD_HUB_SESSION_SECRET SECRETS_ENC_KEY RUNYARD_HUB_BOOTSTRAP_TOKEN TS_AUTHKEY; do
    [ -n "${!k:-}" ] || die "secret $k not set in $SECRETS_FILE"
  done
  # Co-located runner reuses the bootstrap token as its hub auth token.
  RUNYARD_HUB_TOKEN="$RUNYARD_HUB_BOOTSTRAP_TOKEN"
}

_require_env() {
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  _ensure_secrets
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
  scp -o BatchMode=yes -q "$HERE/runyard-node-box.py" "$BOX_HOST:/tmp/runyard-node-box.py"
  ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_NO_INSTANCE_ID='$BOX_NO_INSTANCE_ID' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    E_CHAIN_ID='$CHAIN_ID' E_RPC_URL='$RPC_URL' E_BUNDLER_URL='${BUNDLER_URL:-$RPC_URL}' E_GAS_POLICY_ID='${GAS_POLICY_ID:-}' E_INDEXER_REGISTRY_ADDR='$INDEXER_REGISTRY_ADDR' E_GATEWAY_DOMAIN='$GATEWAY_DOMAIN' \
    E_RUNYARD_HUB_SESSION_SECRET='$RUNYARD_HUB_SESSION_SECRET' E_SECRETS_ENC_KEY='$SECRETS_ENC_KEY' E_RUNYARD_HUB_BOOTSTRAP_TOKEN='$RUNYARD_HUB_BOOTSTRAP_TOKEN' E_RUNYARD_HUB_TOKEN='$RUNYARD_HUB_TOKEN' E_TS_AUTHKEY='$TS_AUTHKEY' \
    E_DSTACK_DOCKER_USERNAME='${guser:-dmvt}' E_DSTACK_DOCKER_PASSWORD='$gtok' E_DSTACK_DOCKER_REGISTRY='ghcr.io' \
    $BOX_PY /tmp/runyard-node-box.py $mode $app_id $vm_id"
}

deploy_cvm() {
  _load; _default_cluster_env; _require_env
  _save
  log "▶ box deploy_app runyard node=$NODE compose=$COMPOSE cluster=$CLUSTER"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed runyard node app_id=$X compose_hash=$H vm=$VM_ID"
  log "hub is expose-only (private); reach it + sidecar :9090 at the CVM dstack-br0 IP from the box"
}

prime_gate() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  local allowed_hash allowed_app
  allowed_hash=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_hash" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "runyard-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  fi
  allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_app" = true ]; then
    log "app id already allowlisted"
  else
    send_seq "runyard-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  fi
}

bind_member() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind runyard X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/runyard-bind-${NODE}.$(ts).log"
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
  log "✔ bound runyard node X -> $CLUSTER"
}

verify() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ runyard node registered: memberId=$id memberCount=$count"
      return 0
    fi
    log "… runyard node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  die "runyard node did not register"
}

# Sidecar health from the box, against the CVM's dstack-br0 IP. Set RUNYARD_CVM_IP
# (find it on the box: `ip neigh show dev dstack-br0`, or the MCP vm_info). Pass =
# /healthz returns HTTP 200 from the box.
verify_health() {
  _load
  local ip="${RUNYARD_CVM_IP:-}" i code
  [ -n "$ip" ] || die "set RUNYARD_CVM_IP to the CVM's dstack-br0 IP (ip neigh show dev dstack-br0 on the box)"
  for i in $(seq 1 30); do
    code=$(ssh_box "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://${ip}:9090/healthz" 2>/dev/null || true)
    if [ "$code" = 200 ]; then
      log "✔ sidecar healthy: http://${ip}:9090/healthz -> 200"
      ssh_box "curl -s --max-time 8 http://${ip}:9090/healthz" 2>/dev/null || true
      return 0
    fi
    log "… sidecar health not ready ($i/30, /healthz -> ${code:-000})"
    sleep 10
  done
  die "sidecar health never returned 200 at http://${ip}:9090/healthz"
}

# Hub reachability from the box, against the CVM's dstack-br0 IP:80 — the admin
# access path (model b). expose-only means it's NOT gateway-published; the box
# reaches it directly on the bridge. Pass = an HTTP response on :80 (any status;
# a 200/302/401 all prove the hub answered). Set RUNYARD_CVM_IP.
verify_hub() {
  _load
  local ip="${RUNYARD_CVM_IP:-}" i code
  [ -n "$ip" ] || die "set RUNYARD_CVM_IP to the CVM's dstack-br0 IP (ip neigh show dev dstack-br0 on the box)"
  for i in $(seq 1 30); do
    code=$(ssh_box "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://${ip}:80/" 2>/dev/null || true)
    if [ -n "$code" ] && [ "$code" != 000 ]; then
      log "✔ hub reachable from box: http://${ip}:80/ -> HTTP $code (box → CVM:80 over dstack-br0 works)"
      return 0
    fi
    log "… hub not reachable yet ($i/30, :80 -> ${code:-000})"
    sleep 10
  done
  die "hub never answered at http://${ip}:80/ from the box (bridge host→CVM:80 may be blocked — flag for admin-path rethink)"
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
    send_seq "runyard-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r '.vm_id // empty'); [ -n "$VM_ID" ] || { _load; : "${VM_ID:=}"; }
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ runyard node update complete mode=$mode vm=$VM_ID"
}

log "=== RunYard AttestMesh node: $NODE ==="
case "$ACTION" in
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  verify-health) verify_health ;;
  verify-hub) verify_hub ;;
  update) update_member ;;
  setup) deploy_cvm; prime_gate; bind_member ;;
  all) deploy_cvm; prime_gate; bind_member; verify ;;
  *) die "usage: runyard-node.sh <node-name> [deploy|prime|bind|verify|verify-health|verify-hub|update|setup|all]" ;;
esac
