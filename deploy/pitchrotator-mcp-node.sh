#!/usr/bin/env bash
# Dedicated, mesh-only PitchRotator MCP network using dstack Path-A admission.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: pitchrotator-mcp-node.sh <node-name> <action>}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
BOX_KMS_ROOT_SIGNER="${BOX_KMS_ROOT_SIGNER:-0x7fa63d99495be2129cf28eee54e2ef2724e3aa2e}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/pitchrotator-mcp-node.yaml}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"
MESH_PROBE_URL="${MESH_PROBE_URL:-}"
MESH_CIDR_IP="${MESH_CIDR_IP:-170065920}" # 10.35.0.0, dedicated v1 network
MESH_CIDR_PREFIX="${MESH_CIDR_PREFIX:-16}"
RECEIPT="$ROOT/contracts/script/deployments/${CHAIN_ID}.json"

export BOX_VCPU="${BOX_VCPU:-2}" BOX_MEM="${BOX_MEM:-4096}" BOX_DISK="${BOX_DISK:-40}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/pitchrotator-mcp.env}"
REDPILL_KEY_FILE="${REDPILL_KEY_FILE:-$HOME/.attestmesh/pitchrotator-redpill.key}"
PITCHROTATOR_SOURCE_COMMIT=66b5495b0ea0695ef6d2a35969d444da4f680a52
PITCHROTATOR_SOURCE_TREE=30ef21a38034bf1d1f7001445a6feea89a424cb3
PITCHROTATOR_SOURCE_SHA256=57aa6a29108cdaa5a46cd6d12b962c7c01c8ca824882b77f16767ea395843e1d
PITCHROTATOR_OVERLAY_SHA256=b13e1cb243f3978ccaee8a1eb1a82c987503066c550b1703ae4b65b37dd93b7a

STATE="$LOGDIR/pitchrotator-mcp-node-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000
RENDERED_COMPOSE=""
cleanup_rendered() { [ -z "$RENDERED_COMPOSE" ] || rm -f "$RENDERED_COMPOSE"; }
trap cleanup_rendered EXIT

_save() {
  umask 077
  cat >"$STATE" <<EOF
UPDATED_AT=$(ts)
X=${X:-}
H=${H:-}
VM_ID=${VM_ID:-}
CLUSTER=${CLUSTER:-}
MEMBER_IMPL=${MEMBER_IMPL:-}
MESH_IP=${MESH_IP:-}
EOF
}
_load() { [ -f "$STATE" ] && source "$STATE" || true; }
ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }
ssh_mesh() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$MESH_SSH_HOST" "$@"; }
send_seq() { local label="$1"; shift; send_with_nonce_retry "$label" "$@"; }

_require_env() {
  local tool indexer
  for tool in jq cast ssh scp curl sed; do
    command -v "$tool" >/dev/null || die "missing required tool: $tool"
  done
  [ -s "$COMPOSE" ] || die "missing compose: $COMPOSE"
  if [ -s "$SECRETS_FILE" ]; then
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
  fi
  MODEL_API_KEY="${MODEL_API_KEY:-${REDPILL_API_KEY:-}}"
  if [ -z "$MODEL_API_KEY" ]; then
    [ -s "$REDPILL_KEY_FILE" ] || die "missing MODEL_API_KEY and RedPill key file: $REDPILL_KEY_FILE"
    MODEL_API_KEY="$(tr -d '\r\n' <"$REDPILL_KEY_FILE")"
  fi
  [ -n "$MODEL_API_KEY" ] || die "RedPill model API key is empty"
  : "${PITCHROTATOR_IMAGE:?set immutable PITCHROTATOR_IMAGE (repository@sha256:64hex)}"
  [[ "$PITCHROTATOR_IMAGE" =~ ^[a-z0-9.-]+([:/][a-z0-9._/-]+)+@sha256:[0-9a-f]{64}$ ]] \
    || die "PITCHROTATOR_IMAGE must be an immutable repository@sha256:64hex reference"
  [ "$BOX_GATEWAY_ENABLED" = true ] || die "PitchRotator requires the gateway-TCP mesh transport; the workload port remains unpublished"
  [ "$BOX_PORTS" = '[]' ] || die "PitchRotator v1 publishes no host ports; BOX_PORTS must remain []"
  indexer=$(jq -r .indexerRegistry "$RECEIPT" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  CVM_RUNTIME_RPC_URL="${CVM_RUNTIME_RPC_URL:-$(box_local_rpc_url "$BOX_HOST" pitchrotator)}"
  [ -n "${BUNDLER_URL:-}" ] || die "missing BUNDLER_URL"
  [ -n "${GAS_POLICY_ID:-}" ] || die "missing GAS_POLICY_ID"
  ssh_box "sudo test -x '$BOX_PY' && sudo test -r '$BOX_DEPLOYER_KEY'" >/dev/null \
    || die "box prerequisites missing on $BOX_HOST"
}

_render_compose() {
  [ -n "$RENDERED_COMPOSE" ] && return 0
  RENDERED_COMPOSE=$(mktemp "${TMPDIR:-/tmp}/pitchrotator-compose.XXXXXX.yaml")
  chmod 600 "$RENDERED_COMPOSE"
  # Image references cannot contain the sed delimiter used here.
  sed "s|\${PITCHROTATOR_IMAGE}|$PITCHROTATOR_IMAGE|g" "$COMPOSE" >"$RENDERED_COMPOSE"
  grep -Fq "image: $PITCHROTATOR_IMAGE" "$RENDERED_COMPOSE" || die "image render failed"
  grep -Fq '${PITCHROTATOR_IMAGE}' "$RENDERED_COMPOSE" && die "unresolved image placeholder"
}

_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok
  _render_compose
  guser=$(sed -nE 's/^[[:space:]]*username[[:space:]]*=[[:space:]]*"?([^" ]+)"?.*/\1/p' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1)
  gtok=$(sed -nE 's/^[[:space:]]*token[[:space:]]*=[[:space:]]*"?([^" ]+)"?.*/\1/p' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1)
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$RENDERED_COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/pitchrotator-mcp-node-box.py" "$BOX_HOST:/tmp/pitchrotator-mcp-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n' "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n' "$CVM_RUNTIME_RPC_URL"
    printf 'E_BUNDLER_URL=%q\n' "$BUNDLER_URL"
    printf 'E_GAS_POLICY_ID=%q\n' "$GAS_POLICY_ID"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "$INDEXER_REGISTRY_ADDR"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_MODEL_API_KEY=%q\n' "$MODEL_API_KEY"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' ghcr.io
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU='$BOX_VCPU' BOX_MEM='$BOX_MEM' BOX_DISK='$BOX_DISK' BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED=true BOX_NET_MODE='$BOX_NET_MODE' bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/pitchrotator-mcp-node-box.py $mode $app_id $vm_id'"
}

preflight() {
  _load; _require_env
  cast chain-id --rpc-url "$RPC_URL" >/dev/null || die "RPC_URL unreachable"
  local hash
  hash=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$hash" ] || die "box failed to hash resolved compose"
  log "✔ preflight: source=$PITCHROTATOR_SOURCE_COMMIT tree=$PITCHROTATOR_SOURCE_TREE source_sha256=$PITCHROTATOR_SOURCE_SHA256 overlay_sha256=$PITCHROTATOR_OVERLAY_SHA256 image=$PITCHROTATOR_IMAGE compose_hash=0x$hash"
}

deploy_cvm() {
  _load; _require_env
  [ -z "${VM_ID:-}" ] || die "existing VM_ID; use update"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(printf '%s\n' "$out" | grep '"app_id"' | tail -1)
  X=$(jq -r .app_id <<<"$j"); H=$(jq -r .compose_hash <<<"$j"); VM_ID=$(jq -r .vm_id <<<"$j")
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse deploy result"
  _save; log "✔ deployed stopped app=$X compose_hash=$H vm=$VM_ID"
}

deploy_cluster() {
  _load; [ -n "${H:-}" ] || die "run deploy first"
  local salt cfg lf
  salt=$(cast keccak "attestmesh-pitchrotator-${NODE}")
  cfg="$ROOT/contracts/script/clusters/${NODE}.json"
  lf="$LOGDIR/pitchrotator-cluster-${NODE}.$(ts).log"
  install -d -m 700 "$(dirname "$cfg")"
  cat >"$cfg" <<JSON
{"clusterOwner":"$DEPLOYER_ADDR","kmsRootSigner":"$BOX_KMS_ROOT_SIGNER","initialComposeHashes":["0x${H#0x}"],"initialDeviceIds":[],"allowAnyDevice":true,"requireTcbUpToDate":true,"meshCidrIp":$MESH_CIDR_IP,"meshCidrPrefix":$MESH_CIDR_PREFIX,"salt":"$salt"}
JSON
  CLUSTER_FACTORY=$(jq -r .clusterDiamondFactory "$RECEIPT") \
  MEMBER_FACTORY=$(jq -r .clusterMemberFactory "$RECEIPT") \
  CLUSTER_CONFIG="script/clusters/${NODE}.json" \
    bash -c "cd '$ROOT/contracts' && forge script script/DeployCluster.s.sol:DeployCluster --rpc-url '$RPC_URL' --broadcast" 2>&1 | tee "$lf"
  CLUSTER=$(grep -iE 'Cluster deployed:' "$lf" | grep -oE '0x[0-9a-fA-F]{40}' | head -1)
  [ -n "$CLUSTER" ] || die "could not parse dedicated cluster address"
  _save; log "✔ dedicated PitchRotator cluster=$CLUSTER"
}

patha_upgrade() {
  _load; [ -n "${CLUSTER:-}" ] || die "run cluster first"
  bash -c "cd '$ROOT' && source deploy/env.sh >/dev/null && deploy/onchain.sh patha-upgrade '$CLUSTER'" || die "patha-upgrade failed"
  local b="$ROOT/contracts/broadcast/UpgradeDstackFacetPathA.s.sol/${CHAIN_ID}/run-latest.json"
  MEMBER_IMPL=$(jq -r '[.transactions[]|select(.transactionType=="CREATE" and .contractName=="ClusterMember")][-1].contractAddress' "$b")
  [ -n "$MEMBER_IMPL" ] && [ "$MEMBER_IMPL" != null ] || die "could not parse ClusterMember impl"
  _save; log "✔ Path-A installed member_impl=$MEMBER_IMPL"
}

prime_gate() {
  _load; [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need app+cluster"
  [ "$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL")" = true ] || die "cluster was not seeded with compose hash"
  if [ "$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL")" != true ]; then
    send_seq "pitchrotator-addApp-${NODE}" "$CLUSTER" 'addAllowedAppId(address)' "$X"
  fi
  [ "$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL")" = true ] || die "app_id admission failed"
}

bind_member() {
  _load; [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need app+cluster+member impl"
  local reinit
  reinit=$(cast calldata 'reinitializeFromDstackApp(address)' "$CLUSTER")
  # The box deployer key remains on the box. This follows the fleet Path-A bind
  # operation; no workload/API secret is placed in argv or logs.
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/pitchrotator-bind-${NODE}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' '$BOX_DEPLOYER_KEY')
cast send '$X' 'upgradeToAndCall(address,bytes)' '$MEMBER_IMPL' '$reinit' --async --rpc-url '$BOX_RPC' --private-key "\$KEY"
SCRIPT
  confirm_latest_transaction "pitchrotator-bind-${NODE}" "$RPC_URL" "$LOGDIR/pitchrotator-bind-${NODE}.*.log" || die "bind not confirmed"
  [ "$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" | tr A-F a-f)" = "$(tr A-F a-f <<<"$CLUSTER")" ] || die "bind did not stick"
  _save
}

start_cvm() {
  _load; [ -n "${VM_ID:-}" ] || die "run deploy first"
  ssh_box "sudo VM_ID='$VM_ID' '$BOX_PY' -" <<'PY'
import os, sys
sys.path.insert(0, '/opt/dstack-mcp')
import mcp_dstack as m
m.vmm('StartVm', {'id': os.environ['VM_ID']})
PY
}

verify() {
  _load; [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need app+cluster"
  local i id
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" 'memberIdOf(address)(bytes32)' "$X" --rpc-url "$RPC_URL" 2>/dev/null || true)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      local raw
      raw=$(cast call "$CLUSTER" 'meshIpOf(bytes32)(uint32)' "$id" --json --rpc-url "$RPC_URL" | jq -er '.[0]') \
        || die "registered member has no mesh IP"
      MESH_IP=$(python3 - "$raw" <<'PY'
import ipaddress, sys
print(ipaddress.IPv4Address(int(sys.argv[1], 0)))
PY
)
      _save; log "✔ registered member_id=$id mesh_ip=$MESH_IP"; return 0
    fi
    log "… waiting for PitchRotator membership ($i/45)"; sleep 20
  done
  die "PitchRotator did not register"
}

verify_mcp() {
  _load; [ -n "${MESH_IP:-}" ] || verify
  local health attest
  if [ -n "$MESH_PROBE_URL" ]; then
    health=$(curl -fsS --max-time 20 "$MESH_PROBE_URL") || die "confidential mesh peer verifier failed"
    jq -e '.ok == true and .mode == "tdx" and .trusted == true' <<<"$health" >/dev/null \
      || die "confidential mesh peer did not verify PitchRotator"
    log "✔ confidential peer verified MCP /health and self-reported TDX-mode /attestation over the dedicated mesh"
    return 0
  fi
  health=$(ssh_mesh "curl -fsS --max-time 10 'http://$MESH_IP:8787/health'") || die "mesh-only health probe failed"
  jq -e '.ok == true' <<<"$health" >/dev/null || die "invalid /health response"
  attest=$(ssh_mesh "curl -fsS --max-time 20 'http://$MESH_IP:8787/attestation?challenge=attestmesh-deploy-verification'") || die "attestation probe failed"
  jq -e '.mode == "tdx" and .trusted == true and (.quote | type == "string" and length > 0)' <<<"$attest" >/dev/null \
    || die "PitchRotator did not return trusted TDX attestation"
  log "✔ MCP /health and self-reported TDX-mode /attestation smoke passed over mesh (not cryptographic quote verification)"
}

update_member() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need deployed state"
  local nh out j
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1); [ -n "$nh" ] || die "hash failed"
  if [ "$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL")" != true ]; then
    send_seq "pitchrotator-update-hash-${NODE}" "$CLUSTER" 'addComposeHash(bytes32)' "0x$nh"
  fi
  [ "$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL")" = true ] || die "new hash not admitted"
  out=$(_box_run update "$X" "$VM_ID") || die "update failed"
  j=$(printf '%s\n' "$out" | grep '"app_id"' | tail -1)
  H=$(jq -r .compose_hash <<<"$j"); _save
  log "✔ update complete compose_hash=$H"
}

log "=== dedicated PitchRotator MCP node: $NODE ==="
case "$ACTION" in
  preflight) preflight ;;
  deploy) deploy_cvm ;;
  cluster) deploy_cluster ;;
  patha) patha_upgrade ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  start) start_cvm ;;
  verify) verify ;;
  verify-mcp) verify_mcp ;;
  update) update_member ;;
  all) preflight; deploy_cvm; deploy_cluster; patha_upgrade; prime_gate; bind_member; start_cvm; verify; verify_mcp ;;
  *) die "usage: $0 <node> [preflight|deploy|cluster|patha|prime|bind|start|verify|verify-mcp|update|all]" ;;
esac
