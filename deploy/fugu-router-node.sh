#!/usr/bin/env bash
# Fugu-router AttestMesh node on the self-hosted on-chain dstack box.
#
# Deploys the LiteLLM Sakana-Fugu subscription-pooling proxy as a strictly
# MESH-ONLY on-chain-anchored AttestMesh node via the canonical Path-A flow
# (telegram-sync template). Data services are EXTERNAL cluster nodes: pg-ha
# (role litellm created by the CVM's own pg-provision) + redis-ha. Langfuse
# observability lives on its OWN CVM (langfuse-node), reached through the
# CVM's :18420 sidecar-netns forwarder — no tailnet on this node. Mesh
# endpoint :18410 (LiteLLM). See deploy/fugu-router-runbook.md for the full
# runbook + risks.
#
#   setup   (ONCE, BEFORE deploy: generate ~/.attestmesh/fugu-router.env — all
#     secrets except provider keys, which Dan pastes at the gate or sources from
#     ~/.attestmesh/redpill-key and ~/.attestmesh/xai-key; prints the Sakana
#     training-opt-out reminder and exits nonzero while keys are missing)
#   -> deploy (stock DstackApp + sealed env + bridge CreateVm, gateway OFF)
#   -> prime (allowlist compose_hash + app_id on the cluster)
#   -> bind  (hash pre-check — C3 membership is permanent — then upgradeToAndCall)
#   -> verify (sidecar self-registers -> memberIdOf(X) != 0)
#   -> verify-health (box-side :9090; the port only binds POST-bind)
#   -> verify-db / verify-redis (data planes)
#   -> verify-proxy (real fugu-ultra + direct provider calls through :18410)
#   -> verify-langfuse-trace (the completion's trace, with orchestration-token
#      metadata, visible via the langfuse-node public API — callback->langfuse-node)
#   -> verify-isolation
#
# Secrets live in ~/.attestmesh/fugu-router.env (generated ONCE) and ride to the
# box over ssh STDIN (printf %q; webhost pattern) — never on argv.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: fugu-router-node.sh <node-name> [setup|deploy|prime|bind|start|register-direct|verify|verify-health|verify-db|verify-redis|verify-proxy|verify-langfuse-trace|verify-isolation|update|stop|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/fugu-router-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"

# The mesh jump host: the ssh-node's sshd-mesh (port 1023, sidecar netns — it sits
# ON the wg mesh). See deploy/compose/ssh-node.yaml + ~/.ssh/config.
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"

SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/fugu-router.env}"
FUGU_RPC_FILE="${FUGU_RPC_FILE:-$HOME/.attestmesh/fugu-router-rpc.env}"
REDPILL_KEY_FILE="${REDPILL_KEY_FILE:-$HOME/.attestmesh/redpill-key}"
XAI_KEY_FILE="${XAI_KEY_FILE:-$HOME/.attestmesh/xai-key}"

# Sibling cluster/node state (for the redis/langfuse mesh IPs + verify credentials).
REDISHA_CSTATE="${REDISHA_CSTATE:-$LOGDIR/redis-ha-redis-ha.state}"
LANGFUSE_STATE="${LANGFUSE_STATE:-$LOGDIR/langfuse-node-langfuse-node.state}"

# pg-ha peer mesh IPs (informational — the compose hardcodes them for its socat
# forwarders; verify-db probes pg1 directly).
PG1_MESH_IP="${PG1_MESH_IP:-10.18.147.86}"

# CVM sizing: all heavy data services are external; 30 GB holds images only.
export BOX_VCPU="${BOX_VCPU:-2}" BOX_MEM="${BOX_MEM:-4096}" BOX_DISK="${BOX_DISK:-30}"
# No host port-forwards; bridge mode; gateway OFF; app-bound disk key.
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-false}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
export BOX_NO_INSTANCE_ID="${BOX_NO_INSTANCE_ID:-false}"

STATE="$LOGDIR/fugu-router-node-${NODE}.state"
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
EOF
}

_load() { [ -f "$STATE" ] && source "$STATE" || true; }
ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }
ssh_mesh() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$MESH_SSH_HOST" "$@"; }

# fugu-router joins the existing Matrix cluster (C3) by default: reuses the
# already-deployed Path-A ClusterMember impl.
_default_cluster_env() {
  if [ -z "${CLUSTER:-}" ] || [ -z "${MEMBER_IMPL:-}" ]; then
    [ -f "$MATRIX_STATE" ] || die "missing cluster state: $MATRIX_STATE (set CLUSTER + MEMBER_IMPL to override)"
    CLUSTER="${CLUSTER:-$(grep '^CLUSTER=' "$MATRIX_STATE" | cut -d= -f2-)}"
    MEMBER_IMPL="${MEMBER_IMPL:-$(grep '^MEMBER_IMPL=' "$MATRIX_STATE" | cut -d= -f2-)}"
  fi
  [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "could not resolve CLUSTER/MEMBER_IMPL"
}

# redis-ha mesh IPs from their node state files (compute-peers output); REDIS_HA_IP_N
# env overrides win. Sealed into the CVM (values, not hash).
_ha_ips() {
  local i f
  for i in 1 2 3; do
    if [ -z "$(eval echo "\${REDIS_HA_IP_$i:-}")" ]; then
      f="$LOGDIR/redis-ha-node-redis-ha-r$i.state"
      [ -f "$f" ] || die "missing $f (deploy redis-ha through compute-peers first, or export REDIS_HA_IP_$i)"
      eval "REDIS_HA_IP_$i=\$(grep '^MESH_IP=' '$f' | cut -d= -f2-)"
    fi
    [ -n "$(eval echo "\${REDIS_HA_IP_$i}")" ] || die "empty redis mesh IP for index $i"
  done
}

# langfuse-node mesh IP (the :18420 forwarder target — litellm's trace callback);
# LANGFUSE_NODE_IP env override wins. Sealed into the CVM (value, not hash).
_langfuse_ip() {
  if [ -z "${LANGFUSE_NODE_IP:-}" ]; then
    [ -f "$LANGFUSE_STATE" ] || die "missing $LANGFUSE_STATE (deploy langfuse-node through mesh discovery first, or export LANGFUSE_NODE_IP)"
    LANGFUSE_NODE_IP=$(grep '^MESH_IP=' "$LANGFUSE_STATE" | cut -d= -f2-)
  fi
  [ -n "${LANGFUSE_NODE_IP:-}" ] || die "empty langfuse-node mesh IP (no MESH_IP= in $LANGFUSE_STATE — export LANGFUSE_NODE_IP to override)"
}

# Generated ONCE into the 0600 secrets file and re-read every run. Existing values
# always win (regenerating would desync the DB role + Langfuse project credentials).
# Provider keys are left blank for the operator; RedPill and xAI can also be sourced
# from ~/.attestmesh/*-key files at deploy time.
_ensure_secrets() {
  umask 077
  mkdir -p "$(dirname "$SECRETS_FILE")"
  if [ ! -s "$SECRETS_FILE" ]; then
    cat > "$SECRETS_FILE" <<EOF
# fugu-router node secrets — generated $(date -u +%FT%TZ) by fugu-router-node.sh setup.
# Sakana keys are pasted by the operator (deploy gate). Do the training opt-out FIRST.
# RedPill API key may be pasted here or sourced from ~/.attestmesh/redpill-key.
# xAI API key may be pasted here or sourced from ~/.attestmesh/xai-key.
# LANGFUSE_INIT_PROJECT_* are the callback credentials litellm sends to langfuse-node —
# they MUST match the langfuse-node deployment's LANGFUSE_INIT project keys.
SAKANA_API_BASE=
SAKANA_SUB_1_KEY=
SAKANA_SUB_2_KEY=
SAKANA_SUB_3_KEY=
SAKANA_CIDRS=
REDPILL_API_BASE=https://api.redpill.ai/v1
REDPILL_API_KEY=
REDPILL_CIDRS=66.220.6.0/24
XAI_API_BASE=https://api.x.ai/v1
XAI_API_KEY=
XAI_CIDRS=
FUGU_SUB_1_LABEL=Subscription 1
FUGU_SUB_2_LABEL=Subscription 2
FUGU_SUB_3_LABEL=Subscription 3
FUGU_SUB_1_BILLING_PLAN=
FUGU_SUB_2_BILLING_PLAN=
FUGU_SUB_3_BILLING_PLAN=
# ISO timestamps. These accounts can have different billing/reset anchors.
FUGU_SUB_1_5H_RESET_ANCHOR=
FUGU_SUB_2_5H_RESET_ANCHOR=
FUGU_SUB_3_5H_RESET_ANCHOR=
FUGU_SUB_1_WEEKLY_RESET_ANCHOR=
FUGU_SUB_2_WEEKLY_RESET_ANCHOR=
FUGU_SUB_3_WEEKLY_RESET_ANCHOR=
FUGU_SUB_1_MONTHLY_RESET_ANCHOR=
FUGU_SUB_2_MONTHLY_RESET_ANCHOR=
FUGU_SUB_3_MONTHLY_RESET_ANCHOR=
# Optional calibrated allowances in Fugu input-token-equivalent usage units.
FUGU_SUB_1_5H_ALLOWANCE_USAGE_UNITS=
FUGU_SUB_2_5H_ALLOWANCE_USAGE_UNITS=
FUGU_SUB_3_5H_ALLOWANCE_USAGE_UNITS=
FUGU_SUB_1_WEEKLY_ALLOWANCE_USAGE_UNITS=
FUGU_SUB_2_WEEKLY_ALLOWANCE_USAGE_UNITS=
FUGU_SUB_3_WEEKLY_ALLOWANCE_USAGE_UNITS=
FUGU_SUB_1_MONTHLY_ALLOWANCE_USAGE_UNITS=
FUGU_SUB_2_MONTHLY_ALLOWANCE_USAGE_UNITS=
FUGU_SUB_3_MONTHLY_ALLOWANCE_USAGE_UNITS=
LITELLM_MASTER_KEY=sk-$(openssl rand -hex 24)
LITELLM_SALT_KEY=sk-$(openssl rand -hex 24)
LITELLM_DB_PASSWORD=$(openssl rand -hex 24)
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-$(openssl rand -hex 16)
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-$(openssl rand -hex 16)
EOF
    log "generated fresh fugu-router secrets -> $SECRETS_FILE (sync LANGFUSE_INIT_PROJECT_* with langfuse-node)"
  fi
  grep -q '^XAI_API_BASE=' "$SECRETS_FILE" || printf '\nXAI_API_BASE=https://api.x.ai/v1\n' >> "$SECRETS_FILE"
  grep -q '^XAI_API_KEY=' "$SECRETS_FILE" || printf 'XAI_API_KEY=\n' >> "$SECRETS_FILE"
  grep -q '^XAI_CIDRS=' "$SECRETS_FILE" || printf 'XAI_CIDRS=\n' >> "$SECRETS_FILE"
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  if [ -f "$FUGU_RPC_FILE" ]; then
    # shellcheck disable=SC1090
    source "$FUGU_RPC_FILE"
  fi
  REDPILL_API_BASE="${REDPILL_API_BASE:-https://api.redpill.ai/v1}"
  REDPILL_CIDRS="${REDPILL_CIDRS:-66.220.6.0/24}"
  XAI_API_BASE="${XAI_API_BASE:-https://api.x.ai/v1}"
  if [ -z "${REDPILL_API_KEY:-}" ] && [ -s "$REDPILL_KEY_FILE" ]; then
    REDPILL_API_KEY="$(tr -d '[:space:]' < "$REDPILL_KEY_FILE")"
  fi
  if [ -z "${XAI_API_KEY:-}" ] && [ -s "$XAI_KEY_FILE" ]; then
    XAI_API_KEY="$(tr -d '[:space:]' < "$XAI_KEY_FILE")"
  fi
  [ -n "${LITELLM_MASTER_KEY:-}" ] && [ -n "${LANGFUSE_INIT_PROJECT_SECRET_KEY:-}" ] || die "incomplete secrets in $SECRETS_FILE"
}

# Hard gate on the way to deploy: Sakana keys must be present (Dan pastes them after
# completing the training opt-out).
_require_sakana() {
  if [ -z "${SAKANA_SUB_1_KEY:-}" ] || [ -z "${SAKANA_SUB_2_KEY:-}" ] || [ -z "${SAKANA_API_BASE:-}" ]; then
    log "──────────────────────────────────────────────────────────────────────"
    log "GATE: Sakana keys missing in $SECRETS_FILE"
    log "  1. Complete the Sakana TRAINING OPT-OUT for every subscription FIRST."
    log "  2. Paste SAKANA_API_BASE, SAKANA_SUB_1_KEY, SAKANA_SUB_2_KEY [, SAKANA_SUB_3_KEY],"
    log "     the narrowest observed SAKANA_CIDRS, and optional FUGU_SUB_* reset anchors."
    log "  3. Re-run this action."
    log "──────────────────────────────────────────────────────────────────────"
    die "Sakana keys not provided — refusing to continue toward deploy"
  fi
  [ -n "${SAKANA_CIDRS:-}" ] || die "SAKANA_CIDRS empty in $SECRETS_FILE (litellm-egress-fw allowlist — Wave-1 agent D resolves these)"
}

_require_redpill() {
  [ -n "${REDPILL_API_BASE:-}" ] || die "REDPILL_API_BASE empty in $SECRETS_FILE"
  [ -n "${REDPILL_API_KEY:-}" ] || die "REDPILL_API_KEY empty (set it in $SECRETS_FILE or create $REDPILL_KEY_FILE)"
  [ -n "${REDPILL_CIDRS:-}" ] || die "REDPILL_CIDRS empty in $SECRETS_FILE (litellm-egress-fw RedPill allowlist)"
}

_require_xai() {
  [ -n "${XAI_API_BASE:-}" ] || die "XAI_API_BASE empty in $SECRETS_FILE"
  [ -n "${XAI_API_KEY:-}" ] || die "XAI_API_KEY empty (set it in $SECRETS_FILE or create $XAI_KEY_FILE)"
  [ -n "${XAI_CIDRS:-}" ] || die "XAI_CIDRS empty in $SECRETS_FILE (litellm-egress-fw xAI allowlist)"
}

_require_env() {
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  _ensure_secrets
  _require_sakana
  _require_redpill
  _require_xai
  _ha_ips
  _langfuse_ip
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

# Forward compose + helper to the box and run a box-side mode. Secrets ride ssh
# STDIN as printf-%q'd assignments sourced by the remote shell (webhost pattern).
_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/fugu-router-node-box.py" "$BOX_HOST:/tmp/fugu-router-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n' "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n' "${CVM_RPC_URL:-${FUGU_RPC_URL:-$RPC_URL}}"
    printf 'E_BUNDLER_URL=%q\n' "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
    printf 'E_GAS_POLICY_ID=%q\n' "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "$INDEXER_REGISTRY_ADDR"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_CLUSTER=%q\n' "${CLUSTER:-}"
    printf 'E_REDIS_HA_IP_1=%q\n' "${REDIS_HA_IP_1:-}"
    printf 'E_REDIS_HA_IP_2=%q\n' "${REDIS_HA_IP_2:-}"
    printf 'E_REDIS_HA_IP_3=%q\n' "${REDIS_HA_IP_3:-}"
    printf 'E_LANGFUSE_NODE_IP=%q\n' "${LANGFUSE_NODE_IP:-}"
    printf 'E_SAKANA_API_BASE=%q\n' "${SAKANA_API_BASE:-}"
    printf 'E_SAKANA_SUB_1_KEY=%q\n' "${SAKANA_SUB_1_KEY:-}"
    printf 'E_SAKANA_SUB_2_KEY=%q\n' "${SAKANA_SUB_2_KEY:-}"
    printf 'E_SAKANA_SUB_3_KEY=%q\n' "${SAKANA_SUB_3_KEY:-}"
    printf 'E_SAKANA_CIDRS=%q\n' "${SAKANA_CIDRS:-}"
    printf 'E_REDPILL_API_BASE=%q\n' "${REDPILL_API_BASE:-}"
    printf 'E_REDPILL_API_KEY=%q\n' "${REDPILL_API_KEY:-}"
    printf 'E_REDPILL_CIDRS=%q\n' "${REDPILL_CIDRS:-}"
    printf 'E_XAI_API_BASE=%q\n' "${XAI_API_BASE:-}"
    printf 'E_XAI_API_KEY=%q\n' "${XAI_API_KEY:-}"
    printf 'E_XAI_CIDRS=%q\n' "${XAI_CIDRS:-}"
    printf 'E_FUGU_SUB_1_ENABLED=%q\n' "true"
    printf 'E_FUGU_SUB_2_ENABLED=%q\n' "true"
    printf 'E_FUGU_SUB_3_ENABLED=%q\n' "$([ -n "${SAKANA_SUB_3_KEY:-}" ] && echo true || echo false)"
    printf 'E_FUGU_SUB_1_LABEL=%q\n' "${FUGU_SUB_1_LABEL:-Subscription 1}"
    printf 'E_FUGU_SUB_2_LABEL=%q\n' "${FUGU_SUB_2_LABEL:-Subscription 2}"
    printf 'E_FUGU_SUB_3_LABEL=%q\n' "${FUGU_SUB_3_LABEL:-Subscription 3}"
    printf 'E_FUGU_SUB_1_BILLING_PLAN=%q\n' "${FUGU_SUB_1_BILLING_PLAN:-}"
    printf 'E_FUGU_SUB_2_BILLING_PLAN=%q\n' "${FUGU_SUB_2_BILLING_PLAN:-}"
    printf 'E_FUGU_SUB_3_BILLING_PLAN=%q\n' "${FUGU_SUB_3_BILLING_PLAN:-}"
    printf 'E_FUGU_SUB_1_5H_RESET_ANCHOR=%q\n' "${FUGU_SUB_1_5H_RESET_ANCHOR:-}"
    printf 'E_FUGU_SUB_2_5H_RESET_ANCHOR=%q\n' "${FUGU_SUB_2_5H_RESET_ANCHOR:-}"
    printf 'E_FUGU_SUB_3_5H_RESET_ANCHOR=%q\n' "${FUGU_SUB_3_5H_RESET_ANCHOR:-}"
    printf 'E_FUGU_SUB_1_WEEKLY_RESET_ANCHOR=%q\n' "${FUGU_SUB_1_WEEKLY_RESET_ANCHOR:-}"
    printf 'E_FUGU_SUB_2_WEEKLY_RESET_ANCHOR=%q\n' "${FUGU_SUB_2_WEEKLY_RESET_ANCHOR:-}"
    printf 'E_FUGU_SUB_3_WEEKLY_RESET_ANCHOR=%q\n' "${FUGU_SUB_3_WEEKLY_RESET_ANCHOR:-}"
    printf 'E_FUGU_SUB_1_MONTHLY_RESET_ANCHOR=%q\n' "${FUGU_SUB_1_MONTHLY_RESET_ANCHOR:-}"
    printf 'E_FUGU_SUB_2_MONTHLY_RESET_ANCHOR=%q\n' "${FUGU_SUB_2_MONTHLY_RESET_ANCHOR:-}"
    printf 'E_FUGU_SUB_3_MONTHLY_RESET_ANCHOR=%q\n' "${FUGU_SUB_3_MONTHLY_RESET_ANCHOR:-}"
    printf 'E_FUGU_SUB_1_5H_ALLOWANCE_USAGE_UNITS=%q\n' "${FUGU_SUB_1_5H_ALLOWANCE_USAGE_UNITS:-}"
    printf 'E_FUGU_SUB_2_5H_ALLOWANCE_USAGE_UNITS=%q\n' "${FUGU_SUB_2_5H_ALLOWANCE_USAGE_UNITS:-}"
    printf 'E_FUGU_SUB_3_5H_ALLOWANCE_USAGE_UNITS=%q\n' "${FUGU_SUB_3_5H_ALLOWANCE_USAGE_UNITS:-}"
    printf 'E_FUGU_SUB_1_WEEKLY_ALLOWANCE_USAGE_UNITS=%q\n' "${FUGU_SUB_1_WEEKLY_ALLOWANCE_USAGE_UNITS:-}"
    printf 'E_FUGU_SUB_2_WEEKLY_ALLOWANCE_USAGE_UNITS=%q\n' "${FUGU_SUB_2_WEEKLY_ALLOWANCE_USAGE_UNITS:-}"
    printf 'E_FUGU_SUB_3_WEEKLY_ALLOWANCE_USAGE_UNITS=%q\n' "${FUGU_SUB_3_WEEKLY_ALLOWANCE_USAGE_UNITS:-}"
    printf 'E_FUGU_SUB_1_MONTHLY_ALLOWANCE_USAGE_UNITS=%q\n' "${FUGU_SUB_1_MONTHLY_ALLOWANCE_USAGE_UNITS:-}"
    printf 'E_FUGU_SUB_2_MONTHLY_ALLOWANCE_USAGE_UNITS=%q\n' "${FUGU_SUB_2_MONTHLY_ALLOWANCE_USAGE_UNITS:-}"
    printf 'E_FUGU_SUB_3_MONTHLY_ALLOWANCE_USAGE_UNITS=%q\n' "${FUGU_SUB_3_MONTHLY_ALLOWANCE_USAGE_UNITS:-}"
    printf 'E_LITELLM_MASTER_KEY=%q\n' "${LITELLM_MASTER_KEY:-}"
    printf 'E_LITELLM_SALT_KEY=%q\n' "${LITELLM_SALT_KEY:-}"
    printf 'E_LITELLM_DB_PASSWORD=%q\n' "${LITELLM_DB_PASSWORD:-}"
    printf 'E_LANGFUSE_INIT_PROJECT_PUBLIC_KEY=%q\n' "${LANGFUSE_INIT_PROJECT_PUBLIC_KEY:-}"
    printf 'E_LANGFUSE_INIT_PROJECT_SECRET_KEY=%q\n' "${LANGFUSE_INIT_PROJECT_SECRET_KEY:-}"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_NO_INSTANCE_ID='$BOX_NO_INSTANCE_ID' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/fugu-router-node-box.py $mode $app_id $vm_id'"
}

_box_stop_vm() {
  [ -n "${VM_ID:-}" ] || die "need VM_ID (run deploy first)"
  scp -o BatchMode=yes -q "$HERE/fugu-router-node-box.py" "$BOX_HOST:/tmp/fugu-router-node-box.py"
  ssh_box "sudo BOX_NAME='$NODE' $BOX_PY /tmp/fugu-router-node-box.py stop '$VM_ID'"
}

_box_start_vm() {
  [ -n "${VM_ID:-}" ] || die "need VM_ID (run deploy first)"
  scp -o BatchMode=yes -q "$HERE/fugu-router-node-box.py" "$BOX_HOST:/tmp/fugu-router-node-box.py"
  ssh_box "sudo BOX_NAME='$NODE' $BOX_PY /tmp/fugu-router-node-box.py start '$VM_ID'"
}

# ONCE, BEFORE deploy: generate the secrets file. Exits nonzero while the Sakana keys
# (the deploy gate) are missing so orchestration can block on it visibly.
setup() {
  _ensure_secrets
  if [ -z "${SAKANA_SUB_1_KEY:-}" ] || [ -z "${SAKANA_SUB_2_KEY:-}" ] || [ -z "${SAKANA_API_BASE:-}" ]; then
    log "setup: $SECRETS_FILE generated/present."
    log "STILL MISSING (fill in before deploy):"
    [ -z "${SAKANA_API_BASE:-}" ]  && log "  - SAKANA_API_BASE"
    [ -z "${SAKANA_SUB_1_KEY:-}" ] && log "  - SAKANA_SUB_1_KEY"
    [ -z "${SAKANA_SUB_2_KEY:-}" ] && log "  - SAKANA_SUB_2_KEY (+ optional SUB_3)"
    [ -z "${SAKANA_CIDRS:-}" ]     && log "  - SAKANA_CIDRS (narrowest observed egress CIDRs)"
    log "REMINDER: complete the Sakana TRAINING OPT-OUT before pasting any key."
    exit 2
  fi
  _require_redpill
  if [ -z "${XAI_API_KEY:-}" ] || [ -z "${XAI_CIDRS:-}" ]; then
    log "setup: xAI direct models still need:"
    [ -z "${XAI_API_KEY:-}" ] && log "  - XAI_API_KEY (or create $XAI_KEY_FILE)"
    [ -z "${XAI_CIDRS:-}" ]    && log "  - XAI_CIDRS (narrowest observed api.x.ai egress CIDRs)"
  fi
  _require_xai
  log "✔ setup complete — all secrets present in $SECRETS_FILE"
}

deploy_cvm() {
  _load; _default_cluster_env; _require_env
  _save
  log "▶ box deploy_app fugu-router node=$NODE compose=$COMPOSE cluster=$CLUSTER"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed fugu-router node app_id=$X compose_hash=$H vm=$VM_ID"
  log "mesh-only: LiteLLM <mesh-ip>:18410 (Langfuse dashboard lives on langfuse-node)"
}

prime_gate() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  local allowed_hash allowed_app
  allowed_hash=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_hash" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "fugu-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  fi
  allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_app" = true ]; then
    log "app id already allowlisted"
  else
    send_seq "fugu-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  fi
}

bind_member() {
  _load; _default_cluster_env; _require_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  # C3 membership is PERMANENT — pre-check that the live compose/env still measures to
  # the deployed hash before binding (plan risk 4).
  local nh
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "hash pre-check failed: could not compute compose_hash"
  [ "0x$nh" = "0x${H#0x}" ] || die "hash pre-check MISMATCH: live compose measures 0x$nh but deployed hash is $H — do NOT bind; redeploy or reconcile first"
  log "✔ hash pre-check OK (0x$nh)"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind fugu-router X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/fugu-bind-${NODE}.$(ts).log"
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
  log "✔ bound fugu-router node X -> $CLUSTER"
}

register_direct() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER"
  local id payload member calldata i
  id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null || true)
  if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
    log "fugu-router node already registered: memberId=$id"
    return 0
  fi
  for i in $(seq 1 60); do
    payload=$(ssh_box "sudo bash -s" <<SCRIPT 2>/dev/null
set -u
VMID="$VM_ID"
$(_bridge_ip_snippet)
p=\$(curl -sfm 8 "http://\$IP:9092/registration-calldata" 2>/dev/null || true)
if [ -n "\$p" ]; then
  printf '%s\n' "\$p"
elif [ -f "/srv/data/dstack/vm/$VM_ID/serial.log" ] || [ -f "/srv/data/dstack/vm/$VM_ID/serial.history.log" ]; then
  sed 's/\\x1b\\[[0-9;]*m//g' "/srv/data/dstack/vm/$VM_ID/serial.log" "/srv/data/dstack/vm/$VM_ID/serial.history.log" 2>/dev/null \
    | grep 'ATTESTMESH_DIRECT_REGISTER ' | sed 's/^.*ATTESTMESH_DIRECT_REGISTER //' | tail -1
fi
SCRIPT
)
    if [ -n "$payload" ] && echo "$payload" | jq -e '.calldata and .member' >/dev/null 2>&1; then
      member=$(echo "$payload" | jq -r .member)
      calldata=$(echo "$payload" | jq -r .calldata)
      [ "${member,,}" = "${X,,}" ] || die "registration helper emitted member=$member, expected X=$X"
      log "▶ direct dstack_register for $NODE member=$member via operator tx"
      send_seq "direct-dstack-register-${NODE}" "$CLUSTER" --data "$calldata"
      return 0
    fi
    log "… waiting for registration helper calldata ($i/60)"
    sleep 5
  done
  die "registration helper calldata not found on bridge helper or CVM serial logs"
}

verify() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ fugu-router node registered: memberId=$id memberCount=$count"
      return 0
    fi
    log "… fugu-router node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  die "fugu-router node did not register"
}

# Box-side bridge-IP resolver for this VM (qemu args -> TAP MAC -> ip neigh).
_bridge_ip_snippet() {
  cat <<'SNIP'
MAC=$(ps axww -o args | grep -F "$VMID" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2)
[ -n "$MAC" ] || { echo "NO_QEMU"; exit 3; }
IP=$(ip neigh show dev dstack-br0 | grep -i "$MAC" | grep -oE '^10\.0\.[0-9]+\.[0-9]+' | head -1)
[ -n "$IP" ] || { echo "NO_IP"; exit 4; }
SNIP
}

_member_mesh_ip() {
  local app="${1:-${X:-}}" cluster="${2:-${CLUSTER:-}}" member_id raw mesh_int
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

# POST-BIND observability: the sidecar binds :9090 only AFTER member.cluster()
# resolves — pre-bind the port is closed (hindsight lesson, 2026-07-01).
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

# Discover this node's mesh IP from the jump host: enumerate wg peer allowed-ips and
# probe the mesh-only LiteLLM port :18410 (any HTTP answer on /health/liveliness — the
# stable "this is the fugu-router node" fingerprint; :18420 would ALSO match the
# langfuse-node CVM, so it cannot disambiguate).
_mesh_discover_snippet() {
  cat <<'SNIP'
IF=$(wg show interfaces 2>/dev/null | awk '{print $1; exit}')
CAND=""
[ -n "$IF" ] && CAND=$(wg show "$IF" allowed-ips 2>/dev/null | awk '{print $2}' | cut -d/ -f1)
[ -n "$CAND" ] || CAND=$(ip -o route show 2>/dev/null | awk '/dev wg/ {print $1}' | cut -d/ -f1)
for ip in $CAND; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://$ip:18410/health/liveliness" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    echo "MESH_IP=$ip"
    exit 0
  fi
done
echo "MESH_IP="
SNIP
}

_discover_mesh_ip() {
  [ -n "${MESH_IP:-}" ] && return 0
  _default_cluster_env
  local resolved
  if resolved=$(_member_mesh_ip "${X:-}" "${CLUSTER:-}"); then
    MESH_IP="$resolved"; _save
    return 0
  fi
  local i out ip
  for i in $(seq 1 45); do
    out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
$(_mesh_discover_snippet)
SCRIPT
)
    ip=$(echo "$out" | grep -oE '^MESH_IP=.*' | cut -d= -f2)
    if [ -n "$ip" ]; then
      MESH_IP="$ip"; _save
      return 0
    fi
    log "… node not discoverable on the mesh yet ($i/45)"
    sleep 20
  done
  return 1
}

# The CVM's pg-provision created the litellm role+db at boot; prove it from the mesh
# shell by logging in as litellm and checking its migrated tables (the langfuse role/db
# is langfuse-node's own pg-provision's job).
verify_db() {
  _load; _ensure_secrets
  local out
  out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
export LC_ALL=C
psql "postgresql://litellm:${LITELLM_DB_PASSWORD}@${PG1_MESH_IP}:5432/litellm?connect_timeout=5" -tAc \
  "SELECT current_user || ':' || count(*) FROM information_schema.tables WHERE table_name = 'LiteLLM_VerificationToken'" 2>&1
SCRIPT
)
  echo "$out"
  echo "$out" | grep -q '^litellm:1$' || die "verify-db failed: LiteLLM schema not migrated (got: $out)"
  log "✔ pg-ha role+db live: litellm schema migrated"
}

# SET/GET through the CVM's :16379 forwarder path (bound in the sidecar netns, so it is
# reachable at the node's mesh IP) AND directly against a redis-ha node's :6379.
verify_redis() {
  _load
  _discover_mesh_ip || die "node not discoverable on the mesh"
  local vpw
  vpw=$(grep '^REDISHA_VERIFY_PASSWORD=' "$REDISHA_CSTATE" | cut -d= -f2-)
  [ -n "$vpw" ] || die "no REDISHA_VERIFY_PASSWORD in $REDISHA_CSTATE"
  _ha_ips
  local out
  out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
command -v redis-cli >/dev/null 2>&1 || { apt-get update -qq >/dev/null 2>&1; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq redis-tools >/dev/null 2>&1; }
stamp="fugu-\$(date -u +%s)-\$RANDOM"
redis-cli --no-auth-warning -u "redis://meshverify:${vpw}@${MESH_IP}:16379" SET verify:fugu "\$stamp" >/dev/null 2>&1 \
  || { echo "REDIS: FAIL - SET via the :16379 forwarder"; exit 2; }
got=\$(redis-cli --no-auth-warning -u "redis://meshverify:${vpw}@${REDIS_HA_IP_1}:6379" GET verify:fugu 2>/dev/null)
[ "\$got" = "\$stamp" ] || { echo "REDIS: FAIL - readback via redis-ha :6379 (got: \$got)"; exit 3; }
echo "REDIS: PASS"
SCRIPT
)
  echo "$out"
  echo "$out" | grep -q '^REDIS: PASS' || die "verify-redis failed"
  log "✔ redis path live: write via the CVM forwarder, readback via redis-ha directly"
}

# LiteLLM: /v1/models + /health/liveliness with the master key, then tiny real
# fugu-ultra + RedPill GLM completions and one RedPill Qwen embedding call.
verify_proxy() {
  _load; _ensure_secrets
  local mode="${FUGU_ROUTER_VERIFY_BACKEND:-bridge}" target_url out bridge_ip
  if [ "$mode" = mesh ]; then
    _discover_mesh_ip || die "node not discoverable on the mesh"
    target_url="http://$MESH_IP:18410"
    out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
BASE_URL="$target_url"
models=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "\$BASE_URL/v1/models" 2>&1)
echo "\$models" | grep -q '"fugu-ultra"' || { echo "PROXY: FAIL - /v1/models missing fugu-ultra: \$models"; exit 2; }
echo "\$models" | grep -q '"glm-5.2"' || { echo "PROXY: FAIL - /v1/models missing glm-5.2: \$models"; exit 2; }
echo "\$models" | grep -q '"qwen/qwen3-embedding-8b"' || { echo "PROXY: FAIL - /v1/models missing qwen/qwen3-embedding-8b: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-4.5"' || { echo "PROXY: FAIL - /v1/models missing grok-4.5: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-4.3"' || { echo "PROXY: FAIL - /v1/models missing grok-4.3: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-imagine-image-quality"' || { echo "PROXY: FAIL - /v1/models missing grok-imagine-image-quality: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-imagine-image"' || { echo "PROXY: FAIL - /v1/models missing grok-imagine-image: \$models"; exit 2; }
curl -fsS --max-time 10 "\$BASE_URL/health/liveliness" >/dev/null 2>&1 || { echo "PROXY: FAIL - liveliness"; exit 3; }
fugu_comp=\$(curl -fsS --max-time 120 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/chat/completions" \
  -d '{"model":"fugu-ultra","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-proxy"]}}' 2>&1)
echo "\$fugu_comp" | grep -q '"choices"' || { echo "PROXY: FAIL - fugu-ultra completion: \$fugu_comp"; exit 4; }
glm_comp=\$(curl -fsS --max-time 120 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/chat/completions" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-proxy","redpill-glm"]}}' 2>&1)
echo "\$glm_comp" | grep -q '"choices"' || { echo "PROXY: FAIL - glm-5.2 completion: \$glm_comp"; exit 5; }
emb=\$(curl -fsS --max-time 120 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/embeddings" \
  -d '{"model":"qwen/qwen3-embedding-8b","input":"fugu-router verify-proxy embedding smoke"}' 2>&1)
echo "\$emb" | grep -q '"embedding"' || { echo "PROXY: FAIL - qwen embedding: \$emb"; exit 6; }
grok45_comp=\$(curl -fsS --max-time 180 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/chat/completions" \
  -d '{"model":"grok-4.5","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-proxy","xai-grok-4.5"]}}' 2>&1)
echo "\$grok45_comp" | grep -q '"choices"' || { echo "PROXY: FAIL - grok-4.5 completion: \$grok45_comp"; exit 7; }
grok43_comp=\$(curl -fsS --max-time 180 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/chat/completions" \
  -d '{"model":"grok-4.3","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-proxy","xai-grok-4.3"]}}' 2>&1)
echo "\$grok43_comp" | grep -q '"choices"' || { echo "PROXY: FAIL - grok-4.3 completion: \$grok43_comp"; exit 8; }
img_quality=\$(curl -fsS --max-time 240 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/images/generations" \
  -d '{"model":"grok-imagine-image-quality","prompt":"A tiny plain red square icon on a white background.","n":1,"response_format":"url","metadata":{"tags":["verify-proxy","xai-grok-imagine-image-quality"]}}' 2>&1)
echo "\$img_quality" | grep -q '"data"' || { echo "PROXY: FAIL - grok-imagine-image-quality image generation: \$img_quality"; exit 9; }
dash45=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "\$BASE_URL/fugu/api/summary?window=5h&model=grok-4.5" 2>&1)
echo "\$dash45" | grep -q '"account_id":"direct:grok-4.5"' || { echo "PROXY: FAIL - dashboard filter missing direct grok-4.5 route: \$dash45"; exit 10; }
dash43=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "\$BASE_URL/fugu/api/summary?window=5h&model=grok-4.3" 2>&1)
echo "\$dash43" | grep -q '"account_id":"direct:grok-4.3"' || { echo "PROXY: FAIL - dashboard filter missing direct grok-4.3 route: \$dash43"; exit 11; }
dash_img_quality=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "\$BASE_URL/fugu/api/summary?window=5h&model=grok-imagine-image-quality" 2>&1)
echo "\$dash_img_quality" | grep -q '"account_id":"direct:grok-imagine-image-quality"' || { echo "PROXY: FAIL - dashboard filter missing direct grok-imagine-image-quality route: \$dash_img_quality"; exit 12; }
echo "PROXY: PASS"
SCRIPT
)
  else
    [ -n "${VM_ID:-}" ] || die "need VM_ID for bridge verify-proxy"
    bridge_ip=$(ssh_box "sudo bash -s" <<SCRIPT 2>/dev/null
VMID="$VM_ID"
$(_bridge_ip_snippet)
printf '%s\n' "\$IP"
SCRIPT
)
    echo "$bridge_ip" | grep -Eq '^10\.0\.[0-9]+\.[0-9]+$' || die "could not resolve bridge IP for $NODE (got: $bridge_ip)"
    target_url="http://$bridge_ip:18410"
    out=$(ssh_box "bash -s" <<SCRIPT 2>/dev/null
BASE_URL="$target_url"
models=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "\$BASE_URL/v1/models" 2>&1)
echo "\$models" | grep -q '"fugu-ultra"' || { echo "PROXY: FAIL - /v1/models missing fugu-ultra: \$models"; exit 2; }
echo "\$models" | grep -q '"glm-5.2"' || { echo "PROXY: FAIL - /v1/models missing glm-5.2: \$models"; exit 2; }
echo "\$models" | grep -q '"qwen/qwen3-embedding-8b"' || { echo "PROXY: FAIL - /v1/models missing qwen/qwen3-embedding-8b: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-4.5"' || { echo "PROXY: FAIL - /v1/models missing grok-4.5: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-4.3"' || { echo "PROXY: FAIL - /v1/models missing grok-4.3: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-imagine-image-quality"' || { echo "PROXY: FAIL - /v1/models missing grok-imagine-image-quality: \$models"; exit 2; }
echo "\$models" | grep -q '"grok-imagine-image"' || { echo "PROXY: FAIL - /v1/models missing grok-imagine-image: \$models"; exit 2; }
curl -fsS --max-time 10 "\$BASE_URL/health/liveliness" >/dev/null 2>&1 || { echo "PROXY: FAIL - liveliness"; exit 3; }
fugu_comp=\$(curl -fsS --max-time 120 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/chat/completions" \
  -d '{"model":"fugu-ultra","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-proxy"]}}' 2>&1)
echo "\$fugu_comp" | grep -q '"choices"' || { echo "PROXY: FAIL - fugu-ultra completion: \$fugu_comp"; exit 4; }
glm_comp=\$(curl -fsS --max-time 120 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/chat/completions" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-proxy","redpill-glm"]}}' 2>&1)
echo "\$glm_comp" | grep -q '"choices"' || { echo "PROXY: FAIL - glm-5.2 completion: \$glm_comp"; exit 5; }
emb=\$(curl -fsS --max-time 120 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/embeddings" \
  -d '{"model":"qwen/qwen3-embedding-8b","input":"fugu-router verify-proxy embedding smoke"}' 2>&1)
echo "\$emb" | grep -q '"embedding"' || { echo "PROXY: FAIL - qwen embedding: \$emb"; exit 6; }
grok45_comp=\$(curl -fsS --max-time 180 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/chat/completions" \
  -d '{"model":"grok-4.5","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-proxy","xai-grok-4.5"]}}' 2>&1)
echo "\$grok45_comp" | grep -q '"choices"' || { echo "PROXY: FAIL - grok-4.5 completion: \$grok45_comp"; exit 7; }
grok43_comp=\$(curl -fsS --max-time 180 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/chat/completions" \
  -d '{"model":"grok-4.3","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-proxy","xai-grok-4.3"]}}' 2>&1)
echo "\$grok43_comp" | grep -q '"choices"' || { echo "PROXY: FAIL - grok-4.3 completion: \$grok43_comp"; exit 8; }
img_quality=\$(curl -fsS --max-time 240 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "\$BASE_URL/v1/images/generations" \
  -d '{"model":"grok-imagine-image-quality","prompt":"A tiny plain red square icon on a white background.","n":1,"response_format":"url","metadata":{"tags":["verify-proxy","xai-grok-imagine-image-quality"]}}' 2>&1)
echo "\$img_quality" | grep -q '"data"' || { echo "PROXY: FAIL - grok-imagine-image-quality image generation: \$img_quality"; exit 9; }
dash45=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "\$BASE_URL/fugu/api/summary?window=5h&model=grok-4.5" 2>&1)
echo "\$dash45" | grep -q '"account_id":"direct:grok-4.5"' || { echo "PROXY: FAIL - dashboard filter missing direct grok-4.5 route: \$dash45"; exit 10; }
dash43=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "\$BASE_URL/fugu/api/summary?window=5h&model=grok-4.3" 2>&1)
echo "\$dash43" | grep -q '"account_id":"direct:grok-4.3"' || { echo "PROXY: FAIL - dashboard filter missing direct grok-4.3 route: \$dash43"; exit 11; }
dash_img_quality=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "\$BASE_URL/fugu/api/summary?window=5h&model=grok-imagine-image-quality" 2>&1)
echo "\$dash_img_quality" | grep -q '"account_id":"direct:grok-imagine-image-quality"' || { echo "PROXY: FAIL - dashboard filter missing direct grok-imagine-image-quality route: \$dash_img_quality"; exit 12; }
echo "PROXY: PASS"
SCRIPT
)
  fi
  echo "$out" | tee "$LOGDIR/fugu-proxy-${NODE}.$(ts).log" >&2
  echo "$out" | grep -q '^PROXY: PASS' || die "verify-proxy failed"
  log "✔ LiteLLM live at $target_url — fugu-ultra, glm-5.2, qwen embeddings, Grok chat/image, and dashboard filters verified"
}

# Poll the langfuse-node public API until verify-proxy's completion appears as a trace
# WITH the orchestration-token metadata — proves litellm's callback -> :18420 forwarder
# -> langfuse-node ingest end-to-end.
verify_langfuse_trace() {
  _load; _ensure_secrets
  _langfuse_ip
  local i out
  for i in $(seq 1 40); do
    out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
curl -fsS --max-time 10 -u "${LANGFUSE_INIT_PROJECT_PUBLIC_KEY}:${LANGFUSE_INIT_PROJECT_SECRET_KEY}" "http://$LANGFUSE_NODE_IP:18420/api/public/traces?limit=10"
SCRIPT
)
    if echo "$out" | grep -q '"orchestration'; then
      log "✔ trace with orchestration-token metadata visible on langfuse-node"
      echo "$out" | head -c 600
      return 0
    fi
    if echo "$out" | grep -q '"data"'; then
      log "… traces API up, orchestration metadata not there yet ($i/40)"
    else
      log "… traces API not answering yet ($i/40)"
    fi
    sleep 15
  done
  die "no trace with orchestration-token metadata appeared within 10m — check fugu_telemetry callback + the langfuse-node ingest path"
}

# From the box: every service port must refuse on the bridge IP; only 9090/51900 answer.
verify_isolation() {
  _load
  [ -n "${VM_ID:-}" ] || die "need VM_ID"
  log "▶ host-isolation check vm=$VM_ID"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/fugu-isolation-${NODE}.$(ts).log"
set -u
VMID="$VM_ID"
$(_bridge_ip_snippet)
echo "ISOLATION: vm=\$VMID bridge_ip=\$IP"
bad=0
for p in 4000 15431 15432 15433 16379 16380 16381 18410 18420; do
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
    send_seq "fugu-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh" \
      || die "failed to allowlist compose hash 0x$nh"
  fi
  settle_compose_hash_for_kms "$CLUSTER" "$nh"
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r '.vm_id // empty'); [ -n "$VM_ID" ] || VM_ID="$(_load; echo "${VM_ID:-}")"
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ fugu-router node update complete mode=$mode vm=$VM_ID"
}

stop_member() {
  _load
  [ -n "${VM_ID:-}" ] || die "need VM_ID in $STATE"
  local out
  out=$(_box_stop_vm) || die "failed to stop VM $VM_ID"
  echo "$out"
  log "✔ fugu-router node VM stop requested vm=$VM_ID"
}

start_member() {
  _load
  [ -n "${VM_ID:-}" ] || die "need VM_ID in $STATE"
  local out
  out=$(_box_start_vm) || die "failed to start VM $VM_ID"
  echo "$out"
  log "✔ fugu-router node VM start requested vm=$VM_ID"
}

log "=== Fugu-router AttestMesh node: $NODE ==="
case "$ACTION" in
  setup) setup ;;
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  start) start_member ;;
  register-direct) register_direct ;;
  verify) verify ;;
  verify-health) verify_health ;;
  verify-db) verify_db ;;
  verify-redis) verify_redis ;;
  verify-proxy) verify_proxy ;;
  verify-langfuse-trace) verify_langfuse_trace ;;
  verify-isolation) verify_isolation ;;
  update) update_member ;;
  stop) stop_member ;;
  all) setup; deploy_cvm; prime_gate; bind_member; start_member; register_direct; verify; verify_health; verify_db; verify_redis; verify_isolation; verify_proxy; verify_langfuse_trace ;;
  *) die "usage: fugu-router-node.sh <node-name> [setup|deploy|prime|bind|start|register-direct|verify|verify-health|verify-db|verify-redis|verify-proxy|verify-langfuse-trace|verify-isolation|update|stop|all]" ;;
esac
