#!/usr/bin/env bash
# Generic workload AttestMesh node on the self-hosted dstack box.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: generic-node.sh <node-name> [deploy|prime|bind|verify|update|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/agent-session-mcp-node.yaml}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"

export BOX_VCPU="${BOX_VCPU:-2}" BOX_MEM="${BOX_MEM:-4096}" BOX_DISK="${BOX_DISK:-40}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

# --- App-tier secrets/config (sealed per-var as ${VAR} in the compose) --------------
# Auto-create strong app secrets on first run; INGEST_TOKEN must stay stable across rolls.
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/agent-session-mcp.env}"
REDPILL_KEY_FILE="${REDPILL_KEY_FILE:-$HOME/.attestmesh/redpill-key}"
HINDSIGHT_STATE="${HINDSIGHT_STATE:-$LOGDIR/hindsight-node-hindsight-node.state}"
if [ ! -s "$SECRETS_FILE" ]; then
  install -d -m 700 "$(dirname "$SECRETS_FILE")"
  ( umask 077; printf 'APP_DB_PASSWORD=%s\nINGEST_TOKEN=%s\n' "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" > "$SECRETS_FILE" )
  log "generated app secrets at $SECRETS_FILE"
fi
set -a; source "$SECRETS_FILE"; set +a
: "${APP_DB_PASSWORD:?set APP_DB_PASSWORD in $SECRETS_FILE}"
: "${INGEST_TOKEN:?set INGEST_TOKEN in $SECRETS_FILE}"
RUNTIME_ENV_FILE="${RUNTIME_ENV_FILE:-$HOME/.attestmesh/agent-session-hindsight.env}"
if [ -s "$RUNTIME_ENV_FILE" ]; then
  # The file supplies persistent defaults, but an explicit one-shot deployment
  # override must win.  Sourcing the file directly used to replace values such
  # as HINDSIGHT_OUTBOX_RUN_LIMIT passed on the command line, silently turning a
  # bounded canary back into an unbounded worker.
  _runtime_override_names=()
  _runtime_override_values=()
  while IFS= read -r _runtime_line; do
    _runtime_name="${_runtime_line%%=*}"
    [[ "$_runtime_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ -v $_runtime_name ]]; then
      _runtime_override_names+=("$_runtime_name")
      _runtime_override_values+=("${!_runtime_name}")
    fi
  done < "$RUNTIME_ENV_FILE"
  set -a; source "$RUNTIME_ENV_FILE"; set +a
  for _runtime_i in "${!_runtime_override_names[@]}"; do
    printf -v "${_runtime_override_names[$_runtime_i]}" '%s' \
      "${_runtime_override_values[$_runtime_i]}"
    export "${_runtime_override_names[$_runtime_i]}"
  done
  unset _runtime_line _runtime_name _runtime_i
  unset _runtime_override_names _runtime_override_values
fi
HINDSIGHT_MESH_IP="${HINDSIGHT_MESH_IP:-$(grep -E '^MESH_IP=' "$HINDSIGHT_STATE" 2>/dev/null | head -1 | cut -d= -f2-)}"
HINDSIGHT_TOKEN="${HINDSIGHT_TOKEN:-$(grep -E '^TAK=' "$HINDSIGHT_STATE" 2>/dev/null | head -1 | cut -d= -f2-)}"
HINDSIGHT_PROXY_TOKEN="${HINDSIGHT_PROXY_TOKEN:-$(grep -E '^BPT=' "$HINDSIGHT_STATE" 2>/dev/null | head -1 | cut -d= -f2-)}"
[ -n "$HINDSIGHT_MESH_IP" ] || die "could not resolve MESH_IP from $HINDSIGHT_STATE"
[ -n "$HINDSIGHT_TOKEN" ] || die "could not resolve HINDSIGHT_TOKEN (TAK) from $HINDSIGHT_STATE"

EMBEDDING_VIA_HINDSIGHT_PROXY="${EMBEDDING_VIA_HINDSIGHT_PROXY:-true}"
EMBEDDING_MODEL="${EMBEDDING_MODEL:-qwen/qwen3-embedding-8b}"
EMBEDDING_DIM="${EMBEDDING_DIM:-1024}"
if [ "$EMBEDDING_VIA_HINDSIGHT_PROXY" = true ]; then
  [ -n "$HINDSIGHT_PROXY_TOKEN" ] || die "could not resolve BPT from $HINDSIGHT_STATE"
  EMBEDDING_URL="http://sidecar:18889/v1/embeddings"
  EMBEDDING_API_KEY="$HINDSIGHT_PROXY_TOKEN"
  ALLOW_BASE_URL="http://sidecar:18889"
else
  EMBEDDING_URL="${EMBEDDING_URL:-https://api.redpill.ai/v1/embeddings}"
  if [ -z "${EMBEDDING_API_KEY:-}" ]; then
    [ -s "$REDPILL_KEY_FILE" ] || die "no EMBEDDING_API_KEY and no RedPill key at $REDPILL_KEY_FILE"
    EMBEDDING_API_KEY="$(tr -d '[:space:]' < "$REDPILL_KEY_FILE")"
  fi
  ALLOW_BASE_URL="$EMBEDDING_URL"
fi
HINDSIGHT_URL="${HINDSIGHT_URL:-http://sidecar:18888}"
HINDSIGHT_BANK="${HINDSIGHT_BANK:-agent-sessions}"
HINDSIGHT_SYNC_ENABLED="${HINDSIGHT_SYNC_ENABLED:-false}"
HINDSIGHT_OUTBOX_MAX_IN_FLIGHT="${HINDSIGHT_OUTBOX_MAX_IN_FLIGHT:-6}"
HINDSIGHT_OUTBOX_TICK_SECONDS="${HINDSIGHT_OUTBOX_TICK_SECONDS:-30}"
HINDSIGHT_OUTBOX_RUN_LIMIT="${HINDSIGHT_OUTBOX_RUN_LIMIT:-0}"
RECALL_BACKEND="${RECALL_BACKEND:-postgres}"
DATABASE_URL="${DATABASE_URL:-postgresql://agent_sessions:${APP_DB_PASSWORD}@sidecar:15431,sidecar:15432,sidecar:15433/agent_sessions?target_session_attrs=read-write&connect_timeout=5}"

# Seed cluster identity from a live C3 state file if not already exported.
_PGHA_STATE="$LOGDIR/pg-ha-pg-ha.state"
CLUSTER="${CLUSTER:-$(grep -E '^CLUSTER=' "$_PGHA_STATE" 2>/dev/null | head -1 | cut -d= -f2-)}"
MEMBER_IMPL="${MEMBER_IMPL:-$(grep -E '^MEMBER_IMPL=' "$_PGHA_STATE" 2>/dev/null | head -1 | cut -d= -f2-)}"

STATE="$LOGDIR/agent-session-mcp-node-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000

_save() {
  umask 077
  cat > "$STATE" <<EOF
UPDATED_AT=$(ts)
STATE_PHASE=${STATE_PHASE:-unknown}
X=${X:-}
H=${H:-}
VM_ID=${VM_ID:-}
CLUSTER=${CLUSTER:-}
MEMBER_IMPL=${MEMBER_IMPL:-}
KMS_ROOT=${KMS_ROOT:-}
GATEWAY_DOMAIN=${GATEWAY_DOMAIN:-}
EOF
}

_load() { [ -f "$STATE" ] && source "$STATE" || true; }
ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }
ssh_mesh() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$MESH_SSH_HOST" "$@"; }

_require_tools() {
  local tool
  for tool in jq cast ssh scp curl; do
    command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
  done
}

_require_env() {
  _require_tools
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  [ -n "${CLUSTER:-}" ] || die "missing CLUSTER"
  [ -n "${MEMBER_IMPL:-}" ] || die "missing MEMBER_IMPL"
  [ -n "${KMS_ROOT:-}" ] || die "missing KMS_ROOT"
  [ -n "${BUNDLER_URL:-}" ] || die "missing BUNDLER_URL (cluster members need an EIP-4337 bundler/paymaster endpoint)"
  [ -n "${GAS_POLICY_ID:-}" ] || die "missing GAS_POLICY_ID (cluster members need paymaster sponsorship)"
  [ "${BUNDLER_URL:-}" != "${RPC_URL:-}" ] || log "BUNDLER_URL equals RPC_URL; continuing because some providers multiplex bundler + node RPC"
  [ -s "$COMPOSE" ] || die "missing compose file: $COMPOSE"
  APP_ENV_B64="${APP_ENV_B64:-}"
}

embedding_preflight() {
  local response body code dim
  if [ "$EMBEDDING_VIA_HINDSIGHT_PROXY" = true ]; then
    response=$(ssh_mesh "bash -s" <<SCRIPT
curl -sS --max-time 60 -w '\n%{http_code}' \
  -X POST 'http://$HINDSIGHT_MESH_IP:18889/v1/embeddings' \
  -H 'Authorization: Bearer $EMBEDDING_API_KEY' \
  -H 'Content-Type: application/json' \
  --data '{"model":"$EMBEDDING_MODEL","input":["attestmesh deployment embedding preflight"]}'
SCRIPT
    ) || die "metered Qwen embedding preflight transport failure"
  else
    response=$(curl -sS --max-time 60 -w $'\n%{http_code}' \
      -X POST "$EMBEDDING_URL" \
      -H "Authorization: Bearer $EMBEDDING_API_KEY" \
      -H 'Content-Type: application/json' \
      --data "$(jq -nc --arg model "$EMBEDDING_MODEL" \
        '{model:$model,input:["attestmesh deployment embedding preflight"]}')" \
      || die "Qwen embedding preflight transport failure")
  fi
  code=${response##*$'\n'}
  body=${response%$'\n'*}
  [ "$code" = 200 ] || die "Qwen embedding preflight returned HTTP $code"
  dim=$(jq -er '.data[0].embedding | length' <<<"$body" 2>/dev/null) \
    || die "Qwen embedding preflight response omitted a vector"
  [ "$dim" -ge "$EMBEDDING_DIM" ] \
    || die "Qwen embedding preflight returned dimension $dim (< $EMBEDDING_DIM)"
  log "✔ Qwen embedding preflight passed model=$EMBEDDING_MODEL native_dim=$dim configured_dim=$EMBEDDING_DIM"
}

preflight() {
  _load; _require_env
  log "▶ preflight generic node=$NODE compose=$COMPOSE cluster=$CLUSTER"
  cast chain-id --rpc-url "$RPC_URL" >/dev/null || die "RPC_URL is not reachable"
  cast code "$CLUSTER" --rpc-url "$RPC_URL" | grep -Eq '^0x[0-9a-fA-F]{4,}$' || die "CLUSTER has no code: $CLUSTER"
  cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" >/dev/null || die "CLUSTER does not expose memberCount(): $CLUSTER"
  cast code "$MEMBER_IMPL" --rpc-url "$RPC_URL" | grep -Eq '^0x[0-9a-fA-F]{4,}$' || die "MEMBER_IMPL has no code: $MEMBER_IMPL"
  ssh_box "sudo test -x '$BOX_PY' && sudo test -r '$BOX_DEPLOYER_KEY'" >/dev/null || die "box prerequisites missing on $BOX_HOST"
  embedding_preflight
  _box_run hash >/dev/null || die "box cannot render/hash compose $COMPOSE"
  STATE_PHASE="preflighted"; _save
  log "✔ preflight passed for generic node=$NODE"
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
  scp -o BatchMode=yes -q "$HERE/agent-session-mcp-node-box.py" "$BOX_HOST:/tmp/agent-session-mcp-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n' "${CHAIN_ID:-}"
    printf 'E_RPC_URL=%q\n' "${CVM_RPC_URL:-${RPC_URL:-}}"
    printf 'E_BUNDLER_URL=%q\n' "${CVM_BUNDLER_URL:-${BUNDLER_URL:-${RPC_URL:-}}}"
    printf 'E_GAS_POLICY_ID=%q\n' "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "${INDEXER_REGISTRY_ADDR:-}"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_CLUSTER=%q\n' "${CLUSTER:-}"
    printf 'E_MEMBER_IMPL=%q\n' "${MEMBER_IMPL:-}"
    printf 'E_APP_DB_PASSWORD=%q\n' "${APP_DB_PASSWORD:-}"
    printf 'E_DATABASE_URL=%q\n' "${DATABASE_URL:-}"
    printf 'E_HINDSIGHT_URL=%q\n' "${HINDSIGHT_URL:-}"
    printf 'E_HINDSIGHT_MESH_IP=%q\n' "${HINDSIGHT_MESH_IP:-}"
    printf 'E_HINDSIGHT_TOKEN=%q\n' "${HINDSIGHT_TOKEN:-}"
    printf 'E_HINDSIGHT_BANK=%q\n' "${HINDSIGHT_BANK:-agent-sessions}"
    printf 'E_HINDSIGHT_SYNC_ENABLED=%q\n' "${HINDSIGHT_SYNC_ENABLED:-false}"
    printf 'E_HINDSIGHT_OUTBOX_MAX_IN_FLIGHT=%q\n' "${HINDSIGHT_OUTBOX_MAX_IN_FLIGHT:-6}"
    printf 'E_HINDSIGHT_OUTBOX_TICK_SECONDS=%q\n' "${HINDSIGHT_OUTBOX_TICK_SECONDS:-30}"
    printf 'E_HINDSIGHT_OUTBOX_RUN_LIMIT=%q\n' "${HINDSIGHT_OUTBOX_RUN_LIMIT:-0}"
    printf 'E_RECALL_BACKEND=%q\n' "${RECALL_BACKEND:-postgres}"
    printf 'E_INGEST_TOKEN=%q\n' "${INGEST_TOKEN:-}"
    printf 'E_EMBEDDING_URL=%q\n' "${EMBEDDING_URL:-}"
    printf 'E_EMBEDDING_API_KEY=%q\n' "${EMBEDDING_API_KEY:-}"
    printf 'E_EMBEDDING_MODEL=%q\n' "${EMBEDDING_MODEL:-}"
    printf 'E_EMBEDDING_DIM=%q\n' "${EMBEDDING_DIM:-}"
    printf 'E_ALLOW_BASE_URL=%q\n' "${ALLOW_BASE_URL:-${EMBEDDING_URL:-}}"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/agent-session-mcp-node-box.py $mode $app_id $vm_id'"
}

_box_stop_vm() {
  local vm_id="${1:?vm_id required}"
  scp -o BatchMode=yes -q "$HERE/agent-session-mcp-node-box.py" "$BOX_HOST:/tmp/agent-session-mcp-node-box.py"
  ssh_box "sudo BOX_NAME='$NODE' $BOX_PY /tmp/agent-session-mcp-node-box.py stop '$vm_id'"
}

_box_vm_json() {
  [ -n "${VM_ID:-}" ] || return 0
  ssh_box "sudo VM_ID='$VM_ID' $BOX_PY - <<'PY'
import json, os, sys
sys.path.insert(0, '/opt/dstack-mcp')
import mcp_dstack as m
try:
    resp = m.vmm('GetInfo', {'id': os.environ['VM_ID']})
    info = resp.get('info') or {}
    print(json.dumps({
        'found': bool(resp.get('found')),
        'status': info.get('status'),
        'boot_progress': info.get('boot_progress'),
        'boot_error': info.get('boot_error'),
    }))
except Exception as exc:
    print(json.dumps({'found': None, 'error': str(exc)}))
PY"
}

_box_boot_detail() {
  [ -n "${VM_ID:-}" ] || return 0
  ssh_box "sudo VM_ID='$VM_ID' $BOX_PY - <<'PY'
import os, sys
sys.path.insert(0, '/opt/dstack-mcp')
import mcp_dstack as m
try:
    log = m.vm_logs(vm_id=os.environ['VM_ID'], lines=700, channel='serial')
except Exception as exc:
    print(f'unable to fetch serial log: {exc}')
    raise SystemExit(0)
needles = (
    'Error response from daemon:',
    'OCI runtime create failed:',
    'dependency failed',
    'unhealthy',
    'failed to start containers',
    'Failed to start App Compose Service',
)
matches = [line.strip() for line in log.splitlines() if any(n in line for n in needles)]
print(' | '.join(matches[-4:]))
PY"
}

deploy_cvm() {
  _load; _require_env
  if [ -n "${VM_ID:-}" ]; then
    log "existing VM_ID in $STATE; deploy is not destructive. Run cleanup first or use update."
    die "refusing to create a second VM for node=$NODE"
  fi
  _save
  log "▶ box deploy_app generic node=$NODE compose=$COMPOSE cluster=$CLUSTER (create stopped)"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  STATE_PHASE="deployed-stopped"
  _save
  log "✔ deployed generic node app_id=$X compose_hash=$H vm=$VM_ID"
}

start_cvm() {
  _load; _require_env
  [ -n "${VM_ID:-}" ] || die "need VM_ID (run deploy first)"
  log "▶ start generic VM vm=$VM_ID after allowlist/bind"
  ssh_box "sudo VM_ID='$VM_ID' $BOX_PY - <<'PY'
import os, sys
sys.path.insert(0, '/opt/dstack-mcp')
import mcp_dstack as m
m.vmm('StartVm', {'id': os.environ['VM_ID']})
print('started ' + os.environ['VM_ID'])
PY"
  STATE_PHASE="started"; _save
  log "✔ start requested for generic VM vm=$VM_ID"
}

prime_gate() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  if [ "$(cast call "$CLUSTER" 'allowedKmsRoots(address)(bool)' "$KMS_ROOT" --rpc-url "$RPC_URL" 2>/dev/null)" != true ]; then
    send_seq "generic-addKmsRoot-${NODE}" "$CLUSTER" "addAllowedKmsRoot(address)" "$KMS_ROOT"
  else
    log "KMS root already allowlisted"
  fi
  if [ "$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)" != true ]; then
    send_seq "generic-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  else
    log "compose hash already allowlisted"
  fi
  if [ "$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)" != true ]; then
    send_seq "generic-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  else
    log "app id already allowlisted"
  fi
  STATE_PHASE="primed"; _save
}

bind_member() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind generic X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/generic-bind-${NODE}.$(ts).log"
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
  STATE_PHASE="bound"; _save
  log "✔ bound generic node X -> $CLUSTER"
}

verify() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count vm found boot_error boot_progress owner root_allowed hash_allowed app_allowed
  for i in $(seq 1 45); do
    if [ -n "${VM_ID:-}" ]; then
      vm=$(_box_vm_json || true)
      found=$(echo "$vm" | jq -r '.found // empty' 2>/dev/null)
      boot_error=$(echo "$vm" | jq -r '.boot_error // empty' 2>/dev/null)
      boot_progress=$(echo "$vm" | jq -r '.boot_progress // empty' 2>/dev/null)
      [ "$found" != false ] || die "enclave VM disappeared from dstack while waiting for mesh registration (vm=$VM_ID)"
      if [ "$i" -gt 6 ] && [ -n "$boot_error" ] && [ "$boot_error" != null ]; then
        local detail
        detail=$(_box_boot_detail || true)
        if [ -n "$detail" ]; then
          die "enclave VM boot failed before mesh registration: $boot_error (progress: ${boot_progress:-unknown}, vm=$VM_ID; serial: $detail)"
        fi
        die "enclave VM boot failed before mesh registration: $boot_error (progress: ${boot_progress:-unknown}, vm=$VM_ID)"
      fi
    fi
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ generic node registered: memberId=$id memberCount=$count"
      STATE_PHASE="registered"; _save
      return 0
    fi
    log "… generic node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  owner=$(cast call "$X" 'owner()(address)' --rpc-url "$RPC_URL" 2>/dev/null || true)
  root_allowed=$(cast call "$CLUSTER" 'allowedKmsRoots(address)(bool)' "$KMS_ROOT" --rpc-url "$RPC_URL" 2>/dev/null || true)
  hash_allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null || true)
  app_allowed=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null || true)
  die "generic node did not register (owner=${owner:-?}, allowedKmsRoots[$KMS_ROOT]=${root_allowed:-?}, allowedComposeHash=${hash_allowed:-?}, allowedAppId=${app_allowed:-?})"
}

cleanup() {
  _load
  _require_tools
  local id="" force="${FORCE_CLEANUP:-0}" allowed_app=""
  if [ -n "${CLUSTER:-}" ] && [ -n "${X:-}" ] && [ -n "${RPC_URL:-}" ]; then
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null || true)
  fi
  if [ -n "$id" ] && [ "$id" != "$ZERO32" ] && [ "$force" != "1" ]; then
    log "cleanup skipped: node is registered (memberId=$id). Set FORCE_CLEANUP=1 to stop the VM anyway."
    STATE_PHASE="registered"; _save
    return 0
  fi

  if [ -n "${VM_ID:-}" ]; then
    log "▶ cleanup stop VM_ID=$VM_ID force=$force"
    if _box_stop_vm "$VM_ID"; then
      log "✔ cleanup stop completed for vm=$VM_ID"
    else
      log "cleanup stop reported an error for vm=$VM_ID; keeping state for manual follow-up"
    fi
  else
    log "cleanup no-op: no VM_ID in $STATE"
  fi

  if [ -n "${CLUSTER:-}" ] && [ -n "${X:-}" ] && [ -n "${RPC_URL:-}" ] && [ -n "${PRIVATE_KEY:-}" ]; then
    allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null || true)
    if [ "$allowed_app" = true ] && { [ -z "$id" ] || [ "$id" = "$ZERO32" ]; }; then
      send_seq "generic-removeApp-${NODE}" "$CLUSTER" "removeAllowedAppId(address)" "$X" || log "removeAllowedAppId failed; app remains allowlisted"
    fi
  fi

  STATE_PHASE="cleaned"; _save
}

update_member() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  local nh allowed out j mode
  embedding_preflight
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" != true ]; then
    send_seq "generic-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  else
    log "compose hash already allowlisted"
  fi
  settle_compose_hash_for_kms "$CLUSTER" "$nh"
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r '.vm_id // empty'); [ -n "$VM_ID" ] || { _load; : "${VM_ID:=}"; }
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ generic node update complete mode=$mode vm=$VM_ID"
}

register_direct() {
  "$HERE/indexer-member-node.sh" "$NODE" register-member-direct
}

log "=== generic AttestMesh node: $NODE ==="
case "$ACTION" in
  preflight) preflight ;;
  deploy) deploy_cvm ;;
  start) start_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  register-direct) register_direct ;;
  update) update_member ;;
  cleanup|stop) cleanup ;;
  all) preflight; deploy_cvm; prime_gate; bind_member; start_cvm; register_direct; verify ;;
  *) die "usage: generic-node.sh <node-name> [preflight|deploy|start|prime|bind|verify|register-direct|update|cleanup|stop|all]" ;;
esac
