#!/usr/bin/env bash
# Fugu-router AttestMesh node on the self-hosted on-chain dstack box.
#
# Deploys the LiteLLM Sakana-Fugu subscription-pooling proxy + Langfuse v3
# observability as a full on-chain-anchored AttestMesh node via the canonical
# Path-A flow (telegram-sync template). All data services are EXTERNAL cluster
# nodes: pg-ha (roles litellm+langfuse created by the CVM's own pg-provision),
# redis-ha, clickhouse-ha, r2-host. Mesh endpoints :18410 (LiteLLM) / :18420
# (Langfuse UI); tailnet exposes ONLY Langfuse (ts-firewall). See
# deploy/fugu-router-runbook.md for the full runbook + risks.
#
#   setup   (ONCE, BEFORE deploy: generate ~/.attestmesh/fugu-router.env — all
#     secrets except the Sakana keys, which Dan pastes at the gate; prints the
#     Sakana training-opt-out reminder and exits nonzero while keys are missing)
#   -> deploy (stock DstackApp + sealed env + bridge CreateVm, gateway OFF)
#   -> prime (allowlist compose_hash + app_id on the cluster)
#   -> bind  (hash pre-check — C3 membership is permanent — then upgradeToAndCall)
#   -> verify (sidecar self-registers -> memberIdOf(X) != 0)
#   -> verify-health (box-side :9090; the port only binds POST-bind)
#   -> verify-db / verify-clickhouse / verify-s3 / verify-redis (data planes)
#   -> verify-proxy (real fugu-ultra completion through :18410)
#   -> verify-langfuse-trace (the completion's trace, with orchestration-token
#      metadata, visible via the Langfuse public API — callback->worker->CH->S3)
#   -> verify-isolation / verify-tailnet
#
# Secrets live in ~/.attestmesh/fugu-router.env (generated ONCE) and ride to the
# box over ssh STDIN (printf %q; webhost pattern) — never on argv.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: fugu-router-node.sh <node-name> [setup|deploy|prime|bind|verify|verify-health|verify-db|verify-clickhouse|verify-s3|verify-redis|verify-proxy|verify-langfuse-trace|verify-isolation|verify-tailnet|update|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/fugu-router-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
TS_SUFFIX="${TS_SUFFIX:-tail39cb2e.ts.net}"

# The mesh jump host: the ssh-node's sshd-mesh (port 1023, sidecar netns — it sits
# ON the wg mesh). See deploy/compose/ssh-node.yaml + ~/.ssh/config.
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"

SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/fugu-router.env}"
R2HOST_ENV="${R2HOST_ENV:-$HOME/.attestmesh/r2-host.env}"

# Sibling cluster state (for the redis/ch mesh IPs + verify credentials).
REDISHA_CSTATE="${REDISHA_CSTATE:-$LOGDIR/redis-ha-redis-ha.state}"
CHHA_CSTATE="${CHHA_CSTATE:-$LOGDIR/clickhouse-ha-clickhouse-ha.state}"

# pg-ha peer mesh IPs (informational — the compose hardcodes them for its socat
# forwarders; verify-db probes pg1 directly).
PG1_MESH_IP="${PG1_MESH_IP:-10.18.147.86}"
R2_HOST_MESH_IP="${R2_HOST_MESH_IP:-10.18.163.210}"

# CVM sizing: all heavy data services are external; 30 GB holds images only.
export BOX_VCPU="${BOX_VCPU:-2}" BOX_MEM="${BOX_MEM:-4096}" BOX_DISK="${BOX_DISK:-30}"
# No host port-forwards; bridge mode; gateway OFF; app-bound disk key.
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-false}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
export BOX_NO_INSTANCE_ID="${BOX_NO_INSTANCE_ID:-true}"

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

# redis-ha / clickhouse-ha mesh IPs from their node state files (compute-peers output);
# REDIS_HA_IP_N / CH_HA_IP_N env overrides win. Sealed into the CVM (values, not hash).
_ha_ips() {
  local i f
  for i in 1 2 3; do
    if [ -z "$(eval echo "\${REDIS_HA_IP_$i:-}")" ]; then
      f="$LOGDIR/redis-ha-node-redis-ha-r$i.state"
      [ -f "$f" ] || die "missing $f (deploy redis-ha through compute-peers first, or export REDIS_HA_IP_$i)"
      eval "REDIS_HA_IP_$i=\$(grep '^MESH_IP=' '$f' | cut -d= -f2-)"
    fi
    if [ -z "$(eval echo "\${CH_HA_IP_$i:-}")" ]; then
      f="$LOGDIR/clickhouse-ha-node-clickhouse-ha-ch$i.state"
      [ -f "$f" ] || die "missing $f (deploy clickhouse-ha through compute-peers first, or export CH_HA_IP_$i)"
      eval "CH_HA_IP_$i=\$(grep '^MESH_IP=' '$f' | cut -d= -f2-)"
    fi
    [ -n "$(eval echo "\${REDIS_HA_IP_$i}")" ] && [ -n "$(eval echo "\${CH_HA_IP_$i}")" ] \
      || die "empty redis/ch mesh IP for index $i"
  done
}

# Generated ONCE into the 0600 secrets file and re-read every run. Existing values
# always win (regenerating would desync the DB roles + Langfuse init credentials).
# The Sakana keys are the ONLY fields left blank for Dan (deploy gate).
_ensure_secrets() {
  umask 077
  mkdir -p "$(dirname "$SECRETS_FILE")"
  if [ ! -s "$SECRETS_FILE" ]; then
    # S3 creds come from the r2-host deployment env (dedicated gateway creds).
    local s3ak="" s3sk="" s3bucket="" s3region="us-east-1"
    if [ -f "$R2HOST_ENV" ]; then
      s3ak=$(grep -E '^(S3_ACCESS_KEY_ID|AWS_ACCESS_KEY_ID|S3GW_ACCESS_KEY)=' "$R2HOST_ENV" | head -1 | cut -d= -f2-)
      s3sk=$(grep -E '^(S3_SECRET_ACCESS_KEY|AWS_SECRET_ACCESS_KEY|S3GW_SECRET_KEY)=' "$R2HOST_ENV" | head -1 | cut -d= -f2-)
      s3bucket=$(grep -E '^(S3_BUCKET|R2_BUCKET|BUCKET)=' "$R2HOST_ENV" | head -1 | cut -d= -f2-)
    fi
    cat > "$SECRETS_FILE" <<EOF
# fugu-router node secrets — generated $(date -u +%FT%TZ) by fugu-router-node.sh setup.
# Sakana keys are pasted by the operator (deploy gate). Do the training opt-out FIRST.
SAKANA_API_BASE=
SAKANA_SUB_1_KEY=
SAKANA_SUB_2_KEY=
SAKANA_SUB_3_KEY=
SAKANA_PAYG_KEY=
SAKANA_CIDRS=
LITELLM_MASTER_KEY=sk-$(openssl rand -hex 24)
LITELLM_SALT_KEY=sk-$(openssl rand -hex 24)
LITELLM_DB_PASSWORD=$(openssl rand -hex 24)
LANGFUSE_DB_PASSWORD=$(openssl rand -hex 24)
LANGFUSE_NEXTAUTH_SECRET=$(openssl rand -hex 32)
LANGFUSE_SALT=$(openssl rand -hex 32)
LANGFUSE_ENCRYPTION_KEY=$(openssl rand -hex 32)
LANGFUSE_INIT_ORG_ID=attestmesh
LANGFUSE_INIT_PROJECT_ID=fugu-router
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-$(openssl rand -hex 16)
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-$(openssl rand -hex 16)
LANGFUSE_INIT_USER_EMAIL=lsdan@flashbots.net
LANGFUSE_INIT_USER_PASSWORD=$(openssl rand -hex 16)
S3_ACCESS_KEY_ID=${s3ak}
S3_SECRET_ACCESS_KEY=${s3sk}
S3_BUCKET=${s3bucket}
S3_REGION=${s3region}
TS_AUTHKEY=
EOF
    log "generated fresh fugu-router secrets -> $SECRETS_FILE (S3 creds seeded from r2-host.env: ${s3ak:+yes}${s3ak:-NO — fill in manually})"
  fi
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  [ -n "${LITELLM_MASTER_KEY:-}" ] && [ -n "${LANGFUSE_INIT_PROJECT_SECRET_KEY:-}" ] || die "incomplete secrets in $SECRETS_FILE"
}

# Hard gate on the way to deploy: Sakana keys must be present (Dan pastes them after
# completing the training opt-out).
_require_sakana() {
  if [ -z "${SAKANA_SUB_1_KEY:-}" ] || [ -z "${SAKANA_PAYG_KEY:-}" ] || [ -z "${SAKANA_API_BASE:-}" ]; then
    log "──────────────────────────────────────────────────────────────────────"
    log "GATE: Sakana keys missing in $SECRETS_FILE"
    log "  1. Complete the Sakana TRAINING OPT-OUT for every subscription FIRST."
    log "  2. Paste SAKANA_API_BASE, SAKANA_SUB_1_KEY [, SAKANA_SUB_2/3_KEY],"
    log "     SAKANA_PAYG_KEY and the narrowest observed SAKANA_CIDRS."
    log "  3. Re-run this action."
    log "──────────────────────────────────────────────────────────────────────"
    die "Sakana keys not provided — refusing to continue toward deploy"
  fi
  [ -n "${SAKANA_CIDRS:-}" ] || die "SAKANA_CIDRS empty in $SECRETS_FILE (litellm-egress-fw allowlist — Wave-1 agent D resolves these)"
}

_require_env() {
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  _ensure_secrets
  _require_sakana
  _ha_ips
  [ -n "${S3_ACCESS_KEY_ID:-}" ] && [ -n "${S3_SECRET_ACCESS_KEY:-}" ] && [ -n "${S3_BUCKET:-}" ] \
    || die "S3 creds/bucket missing in $SECRETS_FILE (from ~/.attestmesh/r2-host.env; bucket must be PRE-CREATED)"
  [ -n "${TS_AUTHKEY:-}" ] || die "TS_AUTHKEY empty in $SECRETS_FILE (mint via the Tailscale OAuth client — Wave-1 agent D)"
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
    printf 'E_RPC_URL=%q\n' "$RPC_URL"
    printf 'E_BUNDLER_URL=%q\n' "${BUNDLER_URL:-$RPC_URL}"
    printf 'E_GAS_POLICY_ID=%q\n' "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "$INDEXER_REGISTRY_ADDR"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_REDIS_HA_IP_1=%q\n' "${REDIS_HA_IP_1:-}"
    printf 'E_REDIS_HA_IP_2=%q\n' "${REDIS_HA_IP_2:-}"
    printf 'E_REDIS_HA_IP_3=%q\n' "${REDIS_HA_IP_3:-}"
    printf 'E_CH_HA_IP_1=%q\n' "${CH_HA_IP_1:-}"
    printf 'E_CH_HA_IP_2=%q\n' "${CH_HA_IP_2:-}"
    printf 'E_CH_HA_IP_3=%q\n' "${CH_HA_IP_3:-}"
    printf 'E_SAKANA_API_BASE=%q\n' "${SAKANA_API_BASE:-}"
    printf 'E_SAKANA_SUB_1_KEY=%q\n' "${SAKANA_SUB_1_KEY:-}"
    printf 'E_SAKANA_SUB_2_KEY=%q\n' "${SAKANA_SUB_2_KEY:-}"
    printf 'E_SAKANA_SUB_3_KEY=%q\n' "${SAKANA_SUB_3_KEY:-}"
    printf 'E_SAKANA_PAYG_KEY=%q\n' "${SAKANA_PAYG_KEY:-}"
    printf 'E_SAKANA_CIDRS=%q\n' "${SAKANA_CIDRS:-}"
    printf 'E_LITELLM_MASTER_KEY=%q\n' "${LITELLM_MASTER_KEY:-}"
    printf 'E_LITELLM_SALT_KEY=%q\n' "${LITELLM_SALT_KEY:-}"
    printf 'E_LITELLM_DB_PASSWORD=%q\n' "${LITELLM_DB_PASSWORD:-}"
    printf 'E_LANGFUSE_DB_PASSWORD=%q\n' "${LANGFUSE_DB_PASSWORD:-}"
    printf 'E_LANGFUSE_NEXTAUTH_SECRET=%q\n' "${LANGFUSE_NEXTAUTH_SECRET:-}"
    printf 'E_LANGFUSE_SALT=%q\n' "${LANGFUSE_SALT:-}"
    printf 'E_LANGFUSE_ENCRYPTION_KEY=%q\n' "${LANGFUSE_ENCRYPTION_KEY:-}"
    printf 'E_LANGFUSE_INIT_ORG_ID=%q\n' "${LANGFUSE_INIT_ORG_ID:-}"
    printf 'E_LANGFUSE_INIT_PROJECT_ID=%q\n' "${LANGFUSE_INIT_PROJECT_ID:-}"
    printf 'E_LANGFUSE_INIT_PROJECT_PUBLIC_KEY=%q\n' "${LANGFUSE_INIT_PROJECT_PUBLIC_KEY:-}"
    printf 'E_LANGFUSE_INIT_PROJECT_SECRET_KEY=%q\n' "${LANGFUSE_INIT_PROJECT_SECRET_KEY:-}"
    printf 'E_LANGFUSE_INIT_USER_EMAIL=%q\n' "${LANGFUSE_INIT_USER_EMAIL:-}"
    printf 'E_LANGFUSE_INIT_USER_PASSWORD=%q\n' "${LANGFUSE_INIT_USER_PASSWORD:-}"
    printf 'E_S3_ACCESS_KEY_ID=%q\n' "${S3_ACCESS_KEY_ID:-}"
    printf 'E_S3_SECRET_ACCESS_KEY=%q\n' "${S3_SECRET_ACCESS_KEY:-}"
    printf 'E_S3_BUCKET=%q\n' "${S3_BUCKET:-}"
    printf 'E_S3_REGION=%q\n' "${S3_REGION:-us-east-1}"
    printf 'E_TS_AUTHKEY=%q\n' "${TS_AUTHKEY:-}"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_NO_INSTANCE_ID='$BOX_NO_INSTANCE_ID' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/fugu-router-node-box.py $mode $app_id $vm_id'"
}

# ONCE, BEFORE deploy: generate the secrets file. Exits nonzero while the Sakana keys
# (the deploy gate) are missing so orchestration can block on it visibly.
setup() {
  _ensure_secrets
  if [ -z "${SAKANA_SUB_1_KEY:-}" ] || [ -z "${SAKANA_PAYG_KEY:-}" ] || [ -z "${SAKANA_API_BASE:-}" ] || [ -z "${TS_AUTHKEY:-}" ]; then
    log "setup: $SECRETS_FILE generated/present."
    log "STILL MISSING (fill in before deploy):"
    [ -z "${SAKANA_API_BASE:-}" ]  && log "  - SAKANA_API_BASE"
    [ -z "${SAKANA_SUB_1_KEY:-}" ] && log "  - SAKANA_SUB_1_KEY (+ optional SUB_2/SUB_3)"
    [ -z "${SAKANA_PAYG_KEY:-}" ]  && log "  - SAKANA_PAYG_KEY"
    [ -z "${SAKANA_CIDRS:-}" ]     && log "  - SAKANA_CIDRS (narrowest observed egress CIDRs)"
    [ -z "${TS_AUTHKEY:-}" ]       && log "  - TS_AUTHKEY (tailnet key for the Langfuse UI)"
    log "REMINDER: complete the Sakana TRAINING OPT-OUT before pasting any key."
    exit 2
  fi
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
  log "mesh-only: LiteLLM <mesh-ip>:18410, Langfuse <mesh-ip>:18420 (+ tailnet https)"
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
MAC=$(ps -eo args | grep -F "$VMID" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2)
[ -n "$MAC" ] || { echo "NO_QEMU"; exit 3; }
IP=$(ip neigh show dev dstack-br0 | grep -i "$MAC" | grep -oE '^10\.0\.[0-9]+\.[0-9]+' | head -1)
[ -n "$IP" ] || { echo "NO_IP"; exit 4; }
SNIP
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
# probe the mesh-only Langfuse port :18420 (langfuse-web answers /api/public/health as
# soon as it's up — the stable "this is the fugu-router node" fingerprint).
_mesh_discover_snippet() {
  cat <<'SNIP'
IF=$(wg show interfaces 2>/dev/null | awk '{print $1; exit}')
CAND=""
[ -n "$IF" ] && CAND=$(wg show "$IF" allowed-ips 2>/dev/null | awk '{print $2}' | cut -d/ -f1)
[ -n "$CAND" ] || CAND=$(ip -o route show 2>/dev/null | awk '/dev wg/ {print $1}' | cut -d/ -f1)
for ip in $CAND; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://$ip:18420/api/public/health" 2>/dev/null)
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

# The CVM's pg-provision created roles+dbs at boot; prove it from the mesh shell by
# logging in as litellm AND langfuse and checking their migrated tables.
verify_db() {
  _load; _ensure_secrets
  local out
  out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
export LC_ALL=C
psql "postgresql://litellm:${LITELLM_DB_PASSWORD}@${PG1_MESH_IP}:5432/litellm?connect_timeout=5" -tAc \
  "SELECT current_user || ':' || count(*) FROM information_schema.tables WHERE table_name = 'LiteLLM_VerificationToken'" 2>&1
psql "postgresql://langfuse:${LANGFUSE_DB_PASSWORD}@${PG1_MESH_IP}:5432/langfuse?connect_timeout=5" -tAc \
  "SELECT current_user || ':' || count(*) FROM information_schema.tables WHERE table_name IN ('projects','api_keys')" 2>&1
SCRIPT
)
  echo "$out"
  echo "$out" | grep -q '^litellm:1$'  || die "verify-db failed: LiteLLM schema not migrated (got: $out)"
  echo "$out" | grep -q '^langfuse:2$' || die "verify-db failed: Langfuse migrations missing (got: $out)"
  log "✔ pg-ha roles+dbs live: litellm + langfuse schemas migrated"
}

# Langfuse's /api/public/ready covers its ClickHouse connection; additionally prove the
# Langfuse schema exists as REPLICATED tables on all three ch nodes (system.replicas).
verify_clickhouse() {
  _load; _ensure_secrets
  _discover_mesh_ip || die "node not discoverable on the mesh"
  local vpw
  vpw=$(grep '^CHHA_VERIFY_PASSWORD=' "$CHHA_CSTATE" | cut -d= -f2-)
  [ -n "$vpw" ] || die "no CHHA_VERIFY_PASSWORD in $CHHA_CSTATE"
  _ha_ips
  local out
  out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://$MESH_IP:18420/api/public/ready")
echo "READY=\$code"
for ip in $CH_HA_IP_1 $CH_HA_IP_2 $CH_HA_IP_3; do
  n=\$(curl -fsS --max-time 8 "http://\$ip:8123/" -u "meshverify:$vpw" \
    --data-binary "SELECT count() FROM system.replicas WHERE database = 'langfuse'" 2>/dev/null)
  echo "REPLICAS \$ip=\${n:-?}"
done
SCRIPT
)
  echo "$out"
  echo "$out" | grep -q '^READY=200' || die "Langfuse /api/public/ready not 200"
  local bad
  bad=$(echo "$out" | grep '^REPLICAS' | awk -F= '$2 == "?" || $2 == 0 || $2 == "" {print}')
  [ -z "$bad" ] || die "Langfuse tables not replicated on all ch nodes: $bad"
  log "✔ Langfuse ready + schema present as replicated tables on all three ch nodes"
}

# SigV4 PutObject + a FULL multipart round-trip against r2-host (risk 1: rclone-serve-s3
# multipart unproven). Pure-stdlib python on the mesh jump — no boto needed.
verify_s3() {
  _load; _ensure_secrets
  local out
  # Creds ride stdin inside the ssh-encrypted script text — never the remote argv.
  out=$({
    printf 'export S3_EP=%q S3_BUCKET=%q S3_REGION=%q AK=%q SK=%q\n' \
      "http://${R2_HOST_MESH_IP}:19000" "$S3_BUCKET" "${S3_REGION:-us-east-1}" \
      "$S3_ACCESS_KEY_ID" "$S3_SECRET_ACCESS_KEY"
    echo "python3 - <<'PY'"
    cat <<'PY_BODY'
import datetime, hashlib, hmac, os, sys, urllib.request, urllib.parse
EP, BUCKET, REGION = os.environ["S3_EP"], os.environ["S3_BUCKET"], os.environ["S3_REGION"]
AK, SK = os.environ["AK"], os.environ["SK"]
HOST = urllib.parse.urlparse(EP).netloc

def sign(method, path, query="", body=b""):
    t = datetime.datetime.utcnow()
    amz, ds = t.strftime("%Y%m%dT%H%M%SZ"), t.strftime("%Y%m%d")
    ph = hashlib.sha256(body).hexdigest()
    headers = {"host": HOST, "x-amz-content-sha256": ph, "x-amz-date": amz}
    ch = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    sh = ";".join(sorted(headers))
    creq = f"{method}\n{path}\n{query}\n{ch}\n{sh}\n{ph}"
    scope = f"{ds}/{REGION}/s3/aws4_request"
    sts = f"AWS4-HMAC-SHA256\n{amz}\n{scope}\n{hashlib.sha256(creq.encode()).hexdigest()}"
    k = hmac.new(("AWS4" + SK).encode(), ds.encode(), hashlib.sha256).digest()
    for part in (REGION, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={AK}/{scope}, SignedHeaders={sh}, Signature={sig}"
    del headers["host"]
    return headers

def req(method, path, query="", body=b""):
    url = f"{EP}{path}" + (f"?{query}" if query else "")
    r = urllib.request.Request(url, data=body if body else None, method=method,
                               headers=sign(method, path, query, body))
    with urllib.request.urlopen(r, timeout=20) as resp:
        return resp.status, resp.read()

# r2gw (rclone serve s3 over crypt) has measured read-after-write lag (~7s) — poll GETs.
def get_poll(path, deadline=45):
    import time
    last = None
    for _ in range(deadline // 3):
        try:
            return req("GET", path)
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 404:
                raise
            time.sleep(3)
    raise last

stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
key = f"langfuse-events/verify/put-{stamp}.txt"
st, _ = req("PUT", f"/{BUCKET}/{key}", body=b"attestmesh verify-s3 " + stamp.encode())
assert st in (200, 201), f"PutObject failed: {st}"
st, got = get_poll(f"/{BUCKET}/{key}")
assert st == 200 and b"verify-s3" in got, "GET readback failed"
print("PUT: OK")

mkey = f"langfuse-events/verify/multipart-{stamp}.bin"
st, xml = req("POST", f"/{BUCKET}/{mkey}", query="uploads=")
assert st == 200, f"CreateMultipartUpload failed: {st}"
uid = xml.decode().split("<UploadId>")[1].split("</UploadId>")[0]
etags = []
part = os.urandom(5 * 1024 * 1024)  # 5 MiB min part size
for n in (1, 2):
    q = f"partNumber={n}&uploadId={urllib.parse.quote(uid)}"
    url = f"{EP}/{BUCKET}/{mkey}?{q}"
    r = urllib.request.Request(url, data=part, method="PUT",
                               headers=sign("PUT", f"/{BUCKET}/{mkey}", q, part))
    with urllib.request.urlopen(r, timeout=60) as resp:
        assert resp.status == 200, f"UploadPart {n} failed"
        etags.append(resp.headers["ETag"])
body = ("<CompleteMultipartUpload>" + "".join(
    f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>" for n, e in zip((1, 2), etags)
) + "</CompleteMultipartUpload>").encode()
q = f"uploadId={urllib.parse.quote(uid)}"
st, _ = req("POST", f"/{BUCKET}/{mkey}", query=q, body=body)
assert st == 200, f"CompleteMultipartUpload failed: {st}"
st, got = get_poll(f"/{BUCKET}/{mkey}")
assert st == 200 and len(got) == 2 * len(part), f"multipart readback size mismatch: {len(got)}"
print("MULTIPART: OK (2x5MiB round-trip)")
print("S3: PASS")
PY_BODY
    echo "PY"
  } | ssh_mesh "bash -s" 2>&1)
  echo "$out" | tee "$LOGDIR/fugu-s3-${NODE}.$(ts).log" >&2
  echo "$out" | grep -q '^S3: PASS' || die "verify-s3 failed (multipart against r2-host — see risk 1)"
  log "✔ r2-host S3: PutObject + full multipart round-trip OK under langfuse-events/verify/"
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

# LiteLLM: /v1/models + /health/liveliness with the master key, then ONE real
# fugu-ultra completion (max_tokens 16 — this is a paid call, deliberately tiny).
verify_proxy() {
  _load; _ensure_secrets
  _discover_mesh_ip || die "node not discoverable on the mesh"
  local out
  out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
models=\$(curl -fsS --max-time 10 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" "http://$MESH_IP:18410/v1/models" 2>&1)
echo "\$models" | grep -q '"fugu-ultra"' || { echo "PROXY: FAIL - /v1/models missing fugu-ultra: \$models"; exit 2; }
curl -fsS --max-time 10 "http://$MESH_IP:18410/health/liveliness" >/dev/null 2>&1 || { echo "PROXY: FAIL - liveliness"; exit 3; }
comp=\$(curl -fsS --max-time 120 -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" -H 'Content-Type: application/json' \
  -X POST "http://$MESH_IP:18410/v1/chat/completions" \
  -d '{"model":"fugu-ultra","messages":[{"role":"user","content":"Say OK."}],"max_tokens":16,"metadata":{"tags":["verify-proxy"]}}' 2>&1)
echo "\$comp" | grep -q '"choices"' || { echo "PROXY: FAIL - completion: \$comp"; exit 4; }
echo "PROXY: PASS"
SCRIPT
)
  echo "$out" | tee "$LOGDIR/fugu-proxy-${NODE}.$(ts).log" >&2
  echo "$out" | grep -q '^PROXY: PASS' || die "verify-proxy failed"
  log "✔ LiteLLM live on :18410 — models listed + one real fugu-ultra completion returned"
}

# Poll the Langfuse public API until verify-proxy's completion appears as a trace WITH
# the orchestration-token metadata — proves callback -> worker -> ClickHouse -> S3.
verify_langfuse_trace() {
  _load; _ensure_secrets
  _discover_mesh_ip || die "node not discoverable on the mesh"
  local i out
  for i in $(seq 1 40); do
    out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
curl -fsS --max-time 10 -u "${LANGFUSE_INIT_PROJECT_PUBLIC_KEY}:${LANGFUSE_INIT_PROJECT_SECRET_KEY}" "http://$MESH_IP:18420/api/public/traces?limit=10"
SCRIPT
)
    if echo "$out" | grep -q '"orchestration'; then
      log "✔ trace with orchestration-token metadata visible in Langfuse"
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
  die "no trace with orchestration-token metadata appeared within 10m — check fugu_telemetry callback + langfuse-worker"
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
for p in 3000 3030 4000 15431 15432 15433 16379 16380 16381 18123 18124 18125 19000 19001 19002 19003 18410 18420; do
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

# Tailnet exposes ONLY Langfuse: https health 200 on the MagicDNS name; LiteLLM
# (:18410/:4000) and the forwarder ports must NOT answer on the tailnet IP.
verify_tailnet() {
  _load
  local fqdn="" n tsip
  if [ -n "${FUGU_TAILNET_FQDN:-}" ]; then
    fqdn="$FUGU_TAILNET_FQDN"
  else
    for n in $(tailscale status 2>/dev/null | awk 'tolower($2) ~ /^fugu-router/ {print $2}'); do
      if curl -sS --max-time 8 "https://$n.$TS_SUFFIX/api/public/health" 2>/dev/null | grep -qi 'ok\|"status"'; then
        fqdn="$n.$TS_SUFFIX"; break
      fi
    done
  fi
  [ -n "$fqdn" ] || die "could not find a live fugu-router tailnet FQDN (tailscale status; MagicDNS name bumps each fresh-disk roll)"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "https://$fqdn/api/public/health")
  [ "$code" = 200 ] || die "tailnet Langfuse health returned $code (want 200)"
  log "✔ Langfuse healthy over the tailnet at https://$fqdn"
  tsip=$(tailscale status 2>/dev/null | awk -v h="${fqdn%%.*}" '$2 == h {print $1}' | head -1)
  [ -n "$tsip" ] || die "could not resolve the node's tailnet IP"
  local bad=0 p
  for p in 4000 18410 3000 18420 16379 15431 18123 19000 9090; do
    if timeout 3 bash -c "</dev/tcp/$tsip/$p" 2>/dev/null; then
      log "  !! $tsip:$p REACHABLE over the tailnet — ts-firewall scope violation"; bad=1
    fi
  done
  [ "$bad" = 0 ] || die "verify-tailnet FAILED: non-Langfuse ports reachable over the tailnet"
  log "✔ ts-firewall scope proven: only :443 (Langfuse serve) answers on the tailnet"
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
    send_seq "fugu-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r '.vm_id // empty'); [ -n "$VM_ID" ] || VM_ID="$(_load; echo "${VM_ID:-}")"
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ fugu-router node update complete mode=$mode vm=$VM_ID"
  log "NOTE: the tailnet MagicDNS name may bump on a fresh-disk roll — check tailscale status."
}

log "=== Fugu-router AttestMesh node: $NODE ==="
case "$ACTION" in
  setup) setup ;;
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  verify-health) verify_health ;;
  verify-db) verify_db ;;
  verify-clickhouse) verify_clickhouse ;;
  verify-s3) verify_s3 ;;
  verify-redis) verify_redis ;;
  verify-proxy) verify_proxy ;;
  verify-langfuse-trace) verify_langfuse_trace ;;
  verify-isolation) verify_isolation ;;
  verify-tailnet) verify_tailnet ;;
  update) update_member ;;
  all) setup; deploy_cvm; prime_gate; bind_member; verify; verify_health; verify_db; verify_clickhouse; verify_s3; verify_redis; verify_isolation; verify_tailnet; verify_proxy; verify_langfuse_trace ;;
  *) die "usage: fugu-router-node.sh <node-name> [setup|deploy|prime|bind|verify|verify-health|verify-db|verify-clickhouse|verify-s3|verify-redis|verify-proxy|verify-langfuse-trace|verify-isolation|verify-tailnet|update|all]" ;;
esac
