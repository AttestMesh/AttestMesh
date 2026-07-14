#!/usr/bin/env bash
# Langfuse AttestMesh node on the self-hosted on-chain dstack box.
#
# Deploys Langfuse v3 observability (web + worker) as its OWN full on-chain-anchored
# AttestMesh node via the canonical Path-A flow — extracted OUT of the fugu-router
# node (clean-slate fix for its open 401 api-key mismatch; see
# deploy/fugu-router-runbook.md "Known issues" #1). All data services are EXTERNAL
# cluster nodes: pg-ha (role+db langfuse ensured by the CVM's own pg-provision),
# redis-ha, clickhouse-ha, r2-host. Mesh endpoint :18420 (Langfuse UI/API); tailnet
# exposes ONLY Langfuse (ts-firewall).
#
# GATEWAY IS ON for this node ("HA clusters need gateway on" lesson): the wg
# transport is dial-out-only via <app_id>-51900s, and the GATEWAY-OFF fugu-router
# must dial IN here to flush its litellm->langfuse callback. Two gateway-off nodes
# can never link. Hence BOX_GATEWAY_ENABLED=true + BOX_NO_INSTANCE_ID=false (the
# no_instance_id+gateway pair boot-loops on dstack 0.5.11).
#
#   setup   (ONCE, BEFORE deploy: write ~/.attestmesh/langfuse-node.env by REUSING
#     the langfuse credentials from ~/.attestmesh/fugu-router.env — NEVER regenerate:
#     litellm on fugu-router keeps sending the same pk/sk, and the pg role password
#     must keep matching)
#   -> deploy (stock DstackApp + sealed env + bridge CreateVm, gateway ON)
#   -> prime (allowlist compose_hash + app_id on the cluster)
#   -> bind  (hash pre-check — C3 membership is permanent — then upgradeToAndCall)
#   -> verify (sidecar self-registers -> memberIdOf(X) != 0)
#   -> verify-health (box-side :9090; the port only binds POST-bind)
#   -> verify-db / verify-clickhouse / verify-redis (data planes)
#   -> verify-langfuse (health 200 + pk/sk auth 200 — the fugu 401 regression check)
#   -> verify-isolation / verify-tailnet
#
# Secrets live in ~/.attestmesh/langfuse-node.env (written ONCE from fugu-router.env)
# and ride to the box over ssh STDIN (printf %q; webhost pattern) — never on argv.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: langfuse-node.sh <node-name> [setup|deploy|prime|bind|verify|verify-health|verify-db|verify-clickhouse|verify-redis|verify-langfuse|verify-isolation|verify-tailnet|update|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/langfuse-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
TS_SUFFIX="${TS_SUFFIX:-tail39cb2e.ts.net}"

# The mesh jump host: the ssh-node's sshd-mesh (port 1023, sidecar netns — it sits
# ON the wg mesh). See deploy/compose/ssh-node.yaml + ~/.ssh/config.
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"

SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/langfuse-node.env}"
# The EXISTING fugu-router secrets: the single source for the langfuse credentials
# (pk/sk, DB password, init identities) + S3 creds. Setup COPIES, never regenerates.
FUGU_SECRETS="${FUGU_SECRETS:-$HOME/.attestmesh/fugu-router.env}"
TS_AUTHKEY_FILE="${TS_AUTHKEY_FILE:-$HOME/.attestmesh/fugu-router-ts.authkey}"

# Sibling cluster state (for the redis/ch mesh IPs + verify credentials).
REDISHA_CSTATE="${REDISHA_CSTATE:-$LOGDIR/redis-ha-redis-ha.state}"
CHHA_CSTATE="${CHHA_CSTATE:-$LOGDIR/clickhouse-ha-clickhouse-ha.state}"

# pg-ha peer mesh IPs (informational — the compose hardcodes them for its socat
# forwarders; verify-db probes pg1 directly).
PG1_MESH_IP="${PG1_MESH_IP:-10.18.147.86}"
R2_HOST_MESH_IP="${R2_HOST_MESH_IP:-10.18.163.210}"

# CVM sizing: all heavy data services are external; 30 GB holds images only.
export BOX_VCPU="${BOX_VCPU:-2}" BOX_MEM="${BOX_MEM:-4096}" BOX_DISK="${BOX_DISK:-30}"
# No host port-forwards; bridge mode; gateway ON (wg dial-in target for the
# gateway-off fugu-router) + no_instance_id=false (dstack 0.5.11 boot-loop gotcha).
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
export BOX_NO_INSTANCE_ID="${BOX_NO_INSTANCE_ID:-false}"

STATE="$LOGDIR/langfuse-node-${NODE}.state"
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

# langfuse-node joins the existing Matrix cluster (C3) by default: reuses the
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

# fugu-router mesh IP (the :18410 forwarder target — for the dashboard's LLM-connection /
# playground features). FUGU_ROUTER_IP env override wins. Sealed as a value, not measured.
_fugu_ip() {
  if [ -z "${FUGU_ROUTER_IP:-}" ]; then
    local f="$LOGDIR/fugu-router-node-fugu-router.state"
    [ -f "$f" ] && FUGU_ROUTER_IP=$(grep '^MESH_IP=' "$f" | cut -d= -f2-) || true
  fi
  [ -n "${FUGU_ROUTER_IP:-}" ] || die "empty fugu-router mesh IP (no MESH_IP= in fugu-router-node-fugu-router.state — export FUGU_ROUTER_IP)"
}

# Written ONCE into the 0600 secrets file and re-read every run. The langfuse values
# are COPIED from the existing fugu-router secrets — NEVER regenerated: litellm on
# fugu-router keeps sending the SAME pk/sk, and the pg-ha 'langfuse' role password
# must keep matching. TS_AUTHKEY is reused from the fugu-router tailnet key file.
_ensure_secrets() {
  umask 077
  mkdir -p "$(dirname "$SECRETS_FILE")"
  if [ ! -s "$SECRETS_FILE" ]; then
    [ -s "$FUGU_SECRETS" ] || die "missing $FUGU_SECRETS — the langfuse credentials MUST be reused from the fugu-router node, not regenerated"
    # shellcheck disable=SC1090
    source "$FUGU_SECRETS"
    local k
    for k in LANGFUSE_DB_PASSWORD LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT LANGFUSE_ENCRYPTION_KEY \
             LANGFUSE_INIT_ORG_ID LANGFUSE_INIT_PROJECT_ID LANGFUSE_INIT_PROJECT_PUBLIC_KEY \
             LANGFUSE_INIT_PROJECT_SECRET_KEY LANGFUSE_INIT_USER_EMAIL LANGFUSE_INIT_USER_PASSWORD \
             S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY S3_BUCKET; do
      [ -n "${!k:-}" ] || die "empty $k in $FUGU_SECRETS — cannot seed $SECRETS_FILE"
    done
    local tsk="${TS_AUTHKEY:-}"
    [ -s "$TS_AUTHKEY_FILE" ] && tsk="$(tr -d '[:space:]' < "$TS_AUTHKEY_FILE")"
    [ -n "$tsk" ] || die "no TS_AUTHKEY ($TS_AUTHKEY_FILE empty and none in $FUGU_SECRETS)"
    cat > "$SECRETS_FILE" <<EOF
# langfuse-node secrets — written $(date -u +%FT%TZ) by langfuse-node.sh setup.
# Values COPIED from fugu-router.env — never regenerate: litellm on fugu-router
# keeps sending the same pk/sk, and the pg-ha 'langfuse' role password must not
# change. TS_AUTHKEY reused from $TS_AUTHKEY_FILE.
LANGFUSE_DB_PASSWORD=${LANGFUSE_DB_PASSWORD}
LANGFUSE_NEXTAUTH_SECRET=${LANGFUSE_NEXTAUTH_SECRET}
LANGFUSE_SALT=${LANGFUSE_SALT}
LANGFUSE_ENCRYPTION_KEY=${LANGFUSE_ENCRYPTION_KEY}
LANGFUSE_INIT_ORG_ID=${LANGFUSE_INIT_ORG_ID}
LANGFUSE_INIT_PROJECT_ID=${LANGFUSE_INIT_PROJECT_ID}
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=${LANGFUSE_INIT_PROJECT_PUBLIC_KEY}
LANGFUSE_INIT_PROJECT_SECRET_KEY=${LANGFUSE_INIT_PROJECT_SECRET_KEY}
LANGFUSE_INIT_USER_EMAIL=${LANGFUSE_INIT_USER_EMAIL}
LANGFUSE_INIT_USER_PASSWORD=${LANGFUSE_INIT_USER_PASSWORD}
S3_ACCESS_KEY_ID=${S3_ACCESS_KEY_ID}
S3_SECRET_ACCESS_KEY=${S3_SECRET_ACCESS_KEY}
S3_BUCKET=${S3_BUCKET}
S3_REGION=${S3_REGION:-us-east-1}
TS_AUTHKEY=${tsk}
EOF
    log "wrote langfuse-node secrets -> $SECRETS_FILE (langfuse creds REUSED from $FUGU_SECRETS)"
  fi
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  [ -n "${LANGFUSE_DB_PASSWORD:-}" ] && [ -n "${LANGFUSE_INIT_PROJECT_SECRET_KEY:-}" ] || die "incomplete secrets in $SECRETS_FILE"
}

_require_env() {
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  _ensure_secrets
  _ha_ips
  _fugu_ip
  [ -n "${S3_ACCESS_KEY_ID:-}" ] && [ -n "${S3_SECRET_ACCESS_KEY:-}" ] && [ -n "${S3_BUCKET:-}" ] \
    || die "S3 creds/bucket missing in $SECRETS_FILE (seeded from $FUGU_SECRETS; bucket must be PRE-CREATED)"
  [ -n "${TS_AUTHKEY:-}" ] || die "TS_AUTHKEY empty in $SECRETS_FILE (reuse $TS_AUTHKEY_FILE or mint via the Tailscale OAuth client)"
}

send_seq() {
  local label="$1"; shift
  send_with_nonce_retry "$label" "$@"
}

# Forward compose + helper to the box and run a box-side mode. Secrets ride ssh
# STDIN as printf-%q'd assignments sourced by the remote shell (webhost pattern).
_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/langfuse-node-box.py" "$BOX_HOST:/tmp/langfuse-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n' "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n' "${CVM_RPC_URL:-$RPC_URL}"
    printf 'E_BUNDLER_URL=%q\n' "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
    printf 'E_GAS_POLICY_ID=%q\n' "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "$INDEXER_REGISTRY_ADDR"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_REDIS_HA_IP_1=%q\n' "${REDIS_HA_IP_1:-}"
    printf 'E_REDIS_HA_IP_2=%q\n' "${REDIS_HA_IP_2:-}"
    printf 'E_REDIS_HA_IP_3=%q\n' "${REDIS_HA_IP_3:-}"
    printf 'E_CH_HA_IP_1=%q\n' "${CH_HA_IP_1:-}"
    printf 'E_CH_HA_IP_2=%q\n' "${CH_HA_IP_2:-}"
    printf 'E_CH_HA_IP_3=%q\n' "${CH_HA_IP_3:-}"
    printf 'E_FUGU_ROUTER_IP=%q\n' "${FUGU_ROUTER_IP:-}"
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
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/langfuse-node-box.py $mode $app_id $vm_id'"
}

# ONCE, BEFORE deploy: write the secrets file by copying the langfuse credentials
# out of fugu-router.env (they must stay IDENTICAL — the litellm callback keeps
# sending the same pk/sk). Dies loudly if the source values are missing.
setup() {
  _ensure_secrets
  log "✔ setup complete — langfuse credentials reused from $FUGU_SECRETS (pk/sk unchanged for the litellm callback)"
}

deploy_cvm() {
  _load; _default_cluster_env; _require_env
  _save
  log "▶ box deploy_app langfuse node=$NODE compose=$COMPOSE cluster=$CLUSTER"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed langfuse node app_id=$X compose_hash=$H vm=$VM_ID"
  log "gateway ON (wg dial-in via ${X#0x}-51900s): the gateway-off fugu-router dials this node"
  log "mesh-only service: Langfuse <mesh-ip>:18420 (+ tailnet https, langfuse-only)"
}

prime_gate() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  local allowed_hash allowed_app
  allowed_hash=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_hash" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "langfuse-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  fi
  allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_app" = true ]; then
    log "app id already allowlisted"
  else
    send_seq "langfuse-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  fi
}

bind_member() {
  _load; _default_cluster_env; _require_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  # C3 membership is PERMANENT — pre-check that the live compose/env still measures to
  # the deployed hash before binding.
  local nh
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "hash pre-check failed: could not compute compose_hash"
  [ "0x$nh" = "0x${H#0x}" ] || die "hash pre-check MISMATCH: live compose measures 0x$nh but deployed hash is $H — do NOT bind; redeploy or reconcile first"
  log "✔ hash pre-check OK (0x$nh)"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind langfuse node X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/langfuse-bind-${NODE}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --async --rpc-url $BOX_RPC --private-key "\$KEY"
SCRIPT
  confirm_latest_transaction "langfuse-bind-${NODE}" "$RPC_URL" "$LOGDIR/langfuse-bind-${NODE}.*.log" || die "bind transaction not confirmed"
  local c=""
  for _ in 1 2 3 4 5 6 7 8; do
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    [ "${c,,}" = "${CLUSTER,,}" ] && break
    sleep 2
  done
  log "X.cluster()=$c (expect $CLUSTER)"
  [ "${c,,}" = "${CLUSTER,,}" ] || die "bind did not stick (X.cluster()=$c)"
  log "✔ bound langfuse node X -> $CLUSTER"
}

verify() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ langfuse node registered: memberId=$id memberCount=$count"
      return 0
    fi
    log "… langfuse node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  die "langfuse node did not register"
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
# probe the mesh-only Langfuse port :18420. NOTE: the fugu-router node ALSO serves
# :18420 until its langfuse stack is removed — the langfuse node is the candidate
# WITHOUT LiteLLM on :18410, so a :18410 answer disqualifies the IP.
_mesh_discover_snippet() {
  cat <<'SNIP'
IF=$(wg show interfaces 2>/dev/null | awk '{print $1; exit}')
CAND=""
[ -n "$IF" ] && CAND=$(wg show "$IF" allowed-ips 2>/dev/null | awk '{print $2}' | cut -d/ -f1)
[ -n "$CAND" ] || CAND=$(ip -o route show 2>/dev/null | awk '/dev wg/ {print $1}' | cut -d/ -f1)
for ip in $CAND; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://$ip:18420/api/public/health" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    lcode=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://$ip:18410/health/liveliness" 2>/dev/null)
    [ "$lcode" = "000" ] || continue
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

# The CVM's pg-provision ensured the langfuse role+db at boot; prove it from the mesh
# shell by logging in as langfuse and checking its migrated tables.
verify_db() {
  _load; _ensure_secrets
  local out
  out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
export LC_ALL=C
psql "postgresql://langfuse:${LANGFUSE_DB_PASSWORD}@${PG1_MESH_IP}:5432/langfuse?connect_timeout=5" -tAc \
  "SELECT current_user || ':' || count(*) FROM information_schema.tables WHERE table_name IN ('projects','api_keys')" 2>&1
SCRIPT
)
  echo "$out"
  echo "$out" | grep -q '^langfuse:2$' || die "verify-db failed: Langfuse migrations missing (got: $out)"
  log "✔ pg-ha role+db live: langfuse schema migrated"
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
stamp="langfuse-\$(date -u +%s)-\$RANDOM"
redis-cli --no-auth-warning -u "redis://meshverify:${vpw}@${MESH_IP}:16379" SET verify:langfuse "\$stamp" >/dev/null 2>&1 \
  || { echo "REDIS: FAIL - SET via the :16379 forwarder"; exit 2; }
got=\$(redis-cli --no-auth-warning -u "redis://meshverify:${vpw}@${REDIS_HA_IP_1}:6379" GET verify:langfuse 2>/dev/null)
[ "\$got" = "\$stamp" ] || { echo "REDIS: FAIL - readback via redis-ha :6379 (got: \$got)"; exit 3; }
echo "REDIS: PASS"
SCRIPT
)
  echo "$out"
  echo "$out" | grep -q '^REDIS: PASS' || die "verify-redis failed"
  log "✔ redis path live: write via the CVM forwarder, readback via redis-ha directly"
}

# Langfuse itself: /api/public/health must be 200, AND the pk/sk pair (the SAME one
# litellm on fugu-router sends) must authenticate with 200 — this is the direct
# regression check for the fugu deploy's open 401 (api_keys hash mismatch).
verify_langfuse() {
  _load; _ensure_secrets
  _discover_mesh_ip || die "node not discoverable on the mesh"
  local out
  out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
h=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://$MESH_IP:18420/api/public/health")
echo "HEALTH=\$h"
a=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -u "${LANGFUSE_INIT_PROJECT_PUBLIC_KEY}:${LANGFUSE_INIT_PROJECT_SECRET_KEY}" "http://$MESH_IP:18420/api/public/projects")
echo "AUTH=\$a"
SCRIPT
)
  echo "$out"
  echo "$out" | grep -q '^HEALTH=200' || die "verify-langfuse failed: /api/public/health not 200 (got: $out)"
  echo "$out" | grep -q '^AUTH=200' || die "verify-langfuse FAILED: pk/sk auth not 200 (got: $out) — the fugu 401 (api_keys fast_hashed_secret_key mismatch) has resurfaced; see deploy/fugu-router-runbook.md Known issues #1"
  log "✔ Langfuse healthy on :18420 + pk/sk auth accepted (fugu 401 cleared on this node)"
}

# From the box: every service port must refuse on the bridge IP; only 9090/51900 answer.
verify_isolation() {
  _load
  [ -n "${VM_ID:-}" ] || die "need VM_ID"
  log "▶ host-isolation check vm=$VM_ID"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/langfuse-isolation-${NODE}.$(ts).log"
set -u
VMID="$VM_ID"
$(_bridge_ip_snippet)
echo "ISOLATION: vm=\$VMID bridge_ip=\$IP"
bad=0
for p in 3000 3030 15431 15432 15433 16379 16380 16381 18123 18124 18125 19000 19001 19002 19003 18420; do
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

# Tailnet exposes ONLY Langfuse: https health 200 on the MagicDNS name; the forwarder
# and service ports must NOT answer on the tailnet IP.
verify_tailnet() {
  _load
  local fqdn="" n tsip
  if [ -n "${LANGFUSE_TAILNET_FQDN:-}" ]; then
    fqdn="$LANGFUSE_TAILNET_FQDN"
  else
    for n in $(tailscale status 2>/dev/null | awk 'tolower($2) ~ /^langfuse/ {print $2}'); do
      if curl -sS --max-time 8 "https://$n.$TS_SUFFIX/api/public/health" 2>/dev/null | grep -qi 'ok\|"status"'; then
        fqdn="$n.$TS_SUFFIX"; break
      fi
    done
  fi
  [ -n "$fqdn" ] || die "could not find a live langfuse tailnet FQDN (tailscale status; MagicDNS name bumps each fresh-disk roll)"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "https://$fqdn/api/public/health")
  [ "$code" = 200 ] || die "tailnet Langfuse health returned $code (want 200)"
  log "✔ Langfuse healthy over the tailnet at https://$fqdn"
  tsip=$(tailscale status 2>/dev/null | awk -v h="${fqdn%%.*}" '$2 == h {print $1}' | head -1)
  [ -n "$tsip" ] || die "could not resolve the node's tailnet IP"
  local bad=0 p
  for p in 3000 3030 18420 16379 15431 18123 19000 9090; do
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
    send_seq "langfuse-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r '.vm_id // empty'); [ -n "$VM_ID" ] || VM_ID="$(_load; echo "${VM_ID:-}")"
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ langfuse node update complete mode=$mode vm=$VM_ID"
  log "NOTE: the tailnet MagicDNS name may bump on a fresh-disk roll — check tailscale status."
}

log "=== Langfuse AttestMesh node: $NODE ==="
case "$ACTION" in
  setup) setup ;;
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  verify-health) verify_health ;;
  verify-db) verify_db ;;
  verify-clickhouse) verify_clickhouse ;;
  verify-redis) verify_redis ;;
  verify-langfuse) verify_langfuse ;;
  verify-isolation) verify_isolation ;;
  verify-tailnet) verify_tailnet ;;
  update) update_member ;;
  all) setup; deploy_cvm; prime_gate; bind_member; verify; verify_health; verify_db; verify_clickhouse; verify_redis; verify_isolation; verify_tailnet; verify_langfuse ;;
  *) die "usage: langfuse-node.sh <node-name> [setup|deploy|prime|bind|verify|verify-health|verify-db|verify-clickhouse|verify-redis|verify-langfuse|verify-isolation|verify-tailnet|update|all]" ;;
esac
