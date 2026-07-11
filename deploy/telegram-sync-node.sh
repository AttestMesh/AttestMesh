#!/usr/bin/env bash
# Telegram-sync AttestMesh node on the self-hosted on-chain dstack box.
#
# Deploys the telegram-sync daemon (AttestMesh/telegram-sync) + its Matrix admin
# agent (deploy/telegram-admin-agent) as a full on-chain-anchored AttestMesh node
# via the canonical Path-A flow (r2-host template):
#   provision-matrix (ONCE, BEFORE deploy: ensure the @telegram-admin Matrix account
#     via the matrix-admin-agent bot + create the operator DM room; writes
#     MATRIX_ROOM_ID into the secrets file — the room id is sealed into the CVM)
#   -> deploy (stock DstackApp + sealed env + bridge CreateVm, gateway OFF)
#   -> prime (allowlist compose_hash + app_id on the cluster)
#   -> bind  (upgradeToAndCall the proxy to ClusterMember, box deployer key)
#   -> verify (sidecar self-registers -> memberIdOf(X) != 0)
#   -> verify-health (box-side :9090; the port only binds POST-bind)
#   -> verify-sync (mesh :18082 sync health via the ssh-node mesh jump)
#   -> verify-agent-health (mesh :18100 agent /healthz: matrix+egress-lock+llm)
#   -> verify-db (psql as telegram_sync against pg-ha over the mesh)
#   -> verify-agent (real Matrix roundtrip: lsdan sends `!tg status` to the bot)
#
# The database role/db (telegram_sync) is created by the CVM itself at boot
# (pg-provision one-shot derives the pg-ha superuser password from the CSK), so
# there is NO driver-side DB step — verify-db just proves it worked.
#
# Day-2 rolls: `update` recomputes the compose_hash, allowlists it FIRST, then does
# an in-place UpgradeApp (session + control volumes survive). BOX_FRESH_DISK=1
# wipes the Telethon session — plan to re-auth via the agent afterwards.
#
# Secrets live in ~/.attestmesh/telegram-sync.env (generated ONCE) and ride to the
# box over ssh STDIN (printf %q; webhost pattern) — never on argv.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: telegram-sync-node.sh <node-name> [provision-matrix|deploy|prime|bind|verify|verify-health|verify-sync|verify-agent-health|verify-db|verify-mcp|verify-agent|update|setup|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/telegram-sync-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
TS_SUFFIX="${TS_SUFFIX:-tail39cb2e.ts.net}"

# The mesh jump host: the ssh-node's sshd-mesh (port 1023, sidecar netns — it sits
# ON the wg mesh). See deploy/compose/ssh-node.yaml + ~/.ssh/config.
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"

# App secrets: generated ONCE (the DB password is what the CVM's pg-provision
# converges the role to; the Matrix bot password is what provision-matrix set).
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/telegram-sync.env}"
BOT_LOCALPART="${BOT_LOCALPART:-telegram-admin}"

# pg-ha peer mesh IPs (informational — the compose hardcodes them for its socat
# forwarders; verify-db probes pg1 directly).
PG1_MESH_IP="${PG1_MESH_IP:-10.18.147.86}"

# CVM sizing: telethon + asyncpg + the admin agent are light; disk holds images +
# the (tiny) session/control volumes.
export BOX_VCPU="${BOX_VCPU:-2}" BOX_MEM="${BOX_MEM:-4096}" BOX_DISK="${BOX_DISK:-40}"
# No host port-forwards; bridge mode; gateway OFF (mesh-only node — runyard pairing
# with no_instance_id, which keeps the disk key app-bound).
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-false}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
export BOX_NO_INSTANCE_ID="${BOX_NO_INSTANCE_ID:-true}"

STATE="$LOGDIR/telegram-sync-node-${NODE}.state"
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

# telegram-sync joins the existing Matrix cluster (C3) by default: reuses the
# already-deployed Path-A ClusterMember impl. Override CLUSTER + MEMBER_IMPL for
# another cluster. MATRIX_X anchors the Matrix server_name for the bot identity.
_default_cluster_env() {
  # Read from the Matrix node state only for values not already provided via env,
  # so a full CLUSTER+MEMBER_IMPL+MATRIX_X override works without the state file.
  if [ -z "${CLUSTER:-}" ] || [ -z "${MEMBER_IMPL:-}" ] || [ -z "${MATRIX_X:-}" ]; then
    [ -f "$MATRIX_STATE" ] || die "missing cluster state: $MATRIX_STATE (set CLUSTER + MEMBER_IMPL + MATRIX_X to override)"
    CLUSTER="${CLUSTER:-$(grep '^CLUSTER=' "$MATRIX_STATE" | cut -d= -f2-)}"
    MEMBER_IMPL="${MEMBER_IMPL:-$(grep '^MEMBER_IMPL=' "$MATRIX_STATE" | cut -d= -f2-)}"
    MATRIX_X="${MATRIX_X:-$(grep '^X=' "$MATRIX_STATE" | cut -d= -f2-)}"
  fi
  [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] && [ -n "${MATRIX_X:-}" ] || die "could not resolve CLUSTER/MEMBER_IMPL/MATRIX_X"
}

_matrix_server_name() {
  printf '%s.gateway.attestmesh.xyz' "$(printf '%s' "${MATRIX_X#0x}" | tr A-Z a-z)"
}

_matrix_iapw() {
  grep '^IAPW=' "$MATRIX_STATE" | cut -d= -f2-
}

# Live tailnet FQDN of the Matrix node (MagicDNS name bumps on fresh-disk rolls).
_matrix_fqdn() {
  if [ -n "${MATRIX_TAILNET_FQDN:-}" ]; then
    printf '%s\n' "$MATRIX_TAILNET_FQDN"
    return 0
  fi
  local n
  for n in $(tailscale status 2>/dev/null | awk 'tolower($2) ~ /^matrix-attestmesh/ {print $2}'); do
    curl -sS --max-time 6 "https://$n.$TS_SUFFIX/_matrix/client/versions" 2>/dev/null | grep -q '"versions"' && {
      printf '%s\n' "$n.$TS_SUFFIX"
      return 0
    }
  done
  return 1
}

# Generated ONCE into the 0600 secrets file and re-read every run. Regenerating
# would desync the DB role password and the bot's Matrix account, so existing
# values always win. TELEGRAM_API_ID/HASH are auto-seeded from the local
# telegram-mcp runner when present (same Telegram app).
_ensure_secrets() {
  umask 077
  mkdir -p "$(dirname "$SECRETS_FILE")"
  if [ ! -s "$SECRETS_FILE" ]; then
    local api_id="" api_hash="" runner="$HOME/telegram-mcp/run_telegram_server.sh"
    if [ -f "$runner" ]; then
      api_id=$(sed -nE 's/^export TELEGRAM_API_ID=([0-9]+).*/\1/p' "$runner" | head -1)
      api_hash=$(sed -nE 's/^export TELEGRAM_API_HASH=([0-9a-f]+).*/\1/p' "$runner" | head -1)
    fi
    cat > "$SECRETS_FILE" <<EOF
TELEGRAM_API_ID=${api_id}
TELEGRAM_API_HASH=${api_hash}
TG_PHONE=
TG_DB_PASSWORD=$(openssl rand -hex 24)
TG_MATRIX_PASSWORD=$(openssl rand -hex 16)
MATRIX_ROOM_ID=
EOF
    log "generated fresh telegram-sync secrets -> $SECRETS_FILE (api creds seeded: ${api_id:+yes}${api_id:-NO — fill in manually})"
  fi
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  [ -n "${TELEGRAM_API_ID:-}" ] && [ -n "${TELEGRAM_API_HASH:-}" ] || die "TELEGRAM_API_ID/HASH missing in $SECRETS_FILE (from https://my.telegram.org/auth)"
  [ -n "${TG_DB_PASSWORD:-}" ] && [ -n "${TG_MATRIX_PASSWORD:-}" ] || die "incomplete secrets in $SECRETS_FILE"
  # Backfill secrets added after the file was first generated (idempotent, never overwrites).
  if [ -z "${SEARCH_DB_PASSWORD:-}" ]; then
    SEARCH_DB_PASSWORD="$(openssl rand -hex 24)"
    printf 'SEARCH_DB_PASSWORD=%s\n' "$SEARCH_DB_PASSWORD" >> "$SECRETS_FILE"
    log "added read-only FTS role password (SEARCH_DB_PASSWORD) -> $SECRETS_FILE"
  fi
}

_require_env() {
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  _ensure_secrets
  [ -n "${LLM_API_KEY:-}" ] || LLM_API_KEY="$(cat "$HOME/.attestmesh/redpill-key" 2>/dev/null || true)"
  [ -n "${LLM_API_KEY:-}" ] || die "no LLM key (put it in ~/.attestmesh/redpill-key or export LLM_API_KEY)"
  LLM_BASE_URL="${LLM_BASE_URL:-https://api.redpill.ai/v1}"
  LLM_MODEL="${LLM_MODEL:-z-ai/glm-5.2}"
  local server_name; server_name="$(_matrix_server_name)"
  MATRIX_USER_ID="${MATRIX_USER_ID:-@${BOT_LOCALPART}:${server_name}}"
  MATRIX_ADMIN_MXIDS="${MATRIX_ADMIN_MXIDS:-@lsdan:${server_name}}"
  [ -n "${MATRIX_ROOM_ID:-}" ] || die "MATRIX_ROOM_ID is empty — run '$0 $NODE provision-matrix' first (the room id is sealed into the CVM)"
  # asyncpg does not implement libpq's connect_timeout DSN keyword; it treats
  # unknown query keys as PostgreSQL server settings.  Connection setup is
  # bounded with asyncpg's timeout= argument in the two application images.
  DATABASE_URL="postgresql://telegram_sync:${TG_DB_PASSWORD}@sidecar:15431,sidecar:15432,sidecar:15433/telegram_sync?target_session_attrs=read-write"
  # Image understanding: route xAI's OpenAI-compatible vision API through redpill
  # (reuses the redpill key + tight 66.220.6.0/24 egress; avoids Cloudflare-fronted api.x.ai).
  XAI_API_KEY="${XAI_API_KEY:-$LLM_API_KEY}"
  XAI_BASE_URL="${XAI_BASE_URL:-https://api.redpill.ai/v1}"
  XAI_MODEL="${XAI_MODEL:-x-ai/grok-4.1-fast}"
  # Read-only DSN for the FTS MCP server (telegram_search role, created at boot by pg-provision).
  SEARCH_DATABASE_URL="postgresql://telegram_search:${SEARCH_DB_PASSWORD}@sidecar:15431,sidecar:15432,sidecar:15433/telegram_sync?target_session_attrs=read-write"
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
# STDIN as printf-%q'd assignments sourced by the remote shell (webhost pattern) —
# they never appear on the remote argv/ps/sudo log. Only BOX_* knobs ride argv.
_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/telegram-sync-node-box.py" "$BOX_HOST:/tmp/telegram-sync-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n' "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n' "${CVM_RPC_URL:-$RPC_URL}"
    printf 'E_BUNDLER_URL=%q\n' "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
    printf 'E_GAS_POLICY_ID=%q\n' "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "$INDEXER_REGISTRY_ADDR"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_TELEGRAM_API_ID=%q\n' "$TELEGRAM_API_ID"
    printf 'E_TELEGRAM_API_HASH=%q\n' "$TELEGRAM_API_HASH"
    printf 'E_TG_PHONE=%q\n' "${TG_PHONE:-}"
    printf 'E_TG_DB_PASSWORD=%q\n' "$TG_DB_PASSWORD"
    printf 'E_DATABASE_URL=%q\n' "$DATABASE_URL"
    printf 'E_XAI_API_KEY=%q\n' "${XAI_API_KEY:-}"
    printf 'E_XAI_MODEL=%q\n' "${XAI_MODEL:-}"
    printf 'E_XAI_BASE_URL=%q\n' "${XAI_BASE_URL:-}"
    printf 'E_SEARCH_DB_PASSWORD=%q\n' "${SEARCH_DB_PASSWORD:-}"
    printf 'E_SEARCH_DATABASE_URL=%q\n' "${SEARCH_DATABASE_URL:-}"
    printf 'E_MATRIX_USER_ID=%q\n' "$MATRIX_USER_ID"
    printf 'E_MATRIX_PASSWORD=%q\n' "$TG_MATRIX_PASSWORD"
    printf 'E_MATRIX_ROOM_ID=%q\n' "$MATRIX_ROOM_ID"
    printf 'E_MATRIX_ADMIN_MXIDS=%q\n' "$MATRIX_ADMIN_MXIDS"
    printf 'E_LLM_BASE_URL=%q\n' "$LLM_BASE_URL"
    printf 'E_LLM_API_KEY=%q\n' "$LLM_API_KEY"
    printf 'E_LLM_MODEL=%q\n' "$LLM_MODEL"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_NO_INSTANCE_ID='$BOX_NO_INSTANCE_ID' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/telegram-sync-node-box.py $mode $app_id $vm_id'"
}

# ONCE, BEFORE deploy: ensure the bot's Matrix account (via the matrix-admin-agent
# LLM bot, as lsdan) + create the operator<->bot DM room, then persist the room id
# into the secrets file. Runs against the TAILNET FQDN (mesh-tunnel logins get
# per-source rate-limited — pg-ha lesson 7).
provision_matrix() {
  _default_cluster_env
  _ensure_secrets
  if [ -n "${MATRIX_ROOM_ID:-}" ]; then
    log "MATRIX_ROOM_ID already set ($MATRIX_ROOM_ID) — skipping provision (blank it in $SECRETS_FILE to re-run)"
    return 0
  fi
  local fqdn iapw server_name out room_id
  fqdn=$(_matrix_fqdn) || die "could not discover the Matrix node's tailnet FQDN (tailscale status)"
  iapw=$(_matrix_iapw); [ -n "$iapw" ] || die "no IAPW in $MATRIX_STATE"
  server_name="$(_matrix_server_name)"
  log "▶ provisioning @${BOT_LOCALPART}:${server_name} + DM room via https://$fqdn"
  # NOTE: capture NEW_USER into a plain var FIRST. Bash evaluates command-prefix
  # assignments left-to-right and later ones see earlier ones, so writing
  # `BOT_LOCALPART=matrix-admin-agent NEW_USER="$BOT_LOCALPART"` would make NEW_USER
  # expand to "matrix-admin-agent" (the admin bot) instead of our new account.
  local new_user="$BOT_LOCALPART"
  out=$(HS_URL="https://$fqdn" MATRIX_SERVER="$server_name" ADMIN_USER="${ADMIN_USER:-lsdan}" \
        ADMIN_PASSWORD="$iapw" BOT_LOCALPART="matrix-admin-agent" \
        NEW_USER="$new_user" NEW_PASSWORD="$TG_MATRIX_PASSWORD" \
        NEW_DISPLAYNAME="Telegram admin" \
        python3 "$HERE/telegram-provision-bot.py") || die "provisioning failed: $out"
  echo "$out" | tail -3
  room_id=$(echo "$out" | grep -oE '"room_id": *"[^"]+"' | tail -1 | sed -E 's/.*: *"([^"]+)"/\1/')
  [ -n "$room_id" ] || die "could not parse room_id from provisioner output"
  sed -i "s|^MATRIX_ROOM_ID=.*|MATRIX_ROOM_ID=${room_id}|" "$SECRETS_FILE"
  log "✔ Matrix bot provisioned; DM room $room_id persisted to $SECRETS_FILE"
}

deploy_cvm() {
  _load; _default_cluster_env; _require_env
  _save
  log "▶ box deploy_app telegram-sync node=$NODE compose=$COMPOSE cluster=$CLUSTER"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed telegram-sync node app_id=$X compose_hash=$H vm=$VM_ID"
  log "mesh-only: sync health <mesh-ip>:18082, agent health <mesh-ip>:18100"
}

prime_gate() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  local allowed_hash allowed_app
  allowed_hash=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_hash" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "tgsync-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  fi
  allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_app" = true ]; then
    log "app id already allowlisted"
  else
    send_seq "tgsync-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  fi
}

bind_member() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind telegram-sync X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/tgsync-bind-${NODE}.$(ts).log"
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
  log "✔ bound telegram-sync node X -> $CLUSTER"
}

verify() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ telegram-sync node registered: memberId=$id memberCount=$count"
      return 0
    fi
    log "… telegram-sync node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  die "telegram-sync node did not register"
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

# Discover this node's mesh IP from the jump host: enumerate wg peer allowed-ips
# and probe the AGENT health port :18100 — it answers (200 or 503) as soon as the
# agent boots, independent of Telegram auth. The sync port :18082 is NOT usable for
# discovery: pre-auth the sync daemon exits before binding :8082, so :18100 is the
# reliable "this is the telegram-sync node" fingerprint on the mesh.
_mesh_discover_snippet() {
  cat <<'SNIP'
IF=$(wg show interfaces 2>/dev/null | awk '{print $1; exit}')
CAND=""
[ -n "$IF" ] && CAND=$(wg show "$IF" allowed-ips 2>/dev/null | awk '{print $2}' | cut -d/ -f1)
[ -n "$CAND" ] || CAND=$(ip -o route show 2>/dev/null | awk '/dev wg/ {print $1}' | cut -d/ -f1)
for ip in $CAND; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://$ip:18100/healthz" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    echo "MESH_IP=$ip"
    exit 0
  fi
done
echo "MESH_IP="
SNIP
}

# Resolve + persist MESH_IP off the agent health port. Shared by verify-sync and
# verify-agent-health so either can run first.
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

# Sync-daemon reachability over the wireguard mesh. NON-FATAL when the session is
# unauthorized: the daemon exits before binding :8082, so :18082 legitimately
# refuses until the operator completes a login via the agent. Reports honestly.
verify_sync() {
  _load
  _discover_mesh_ip || die "node never became discoverable on the mesh — check sshd-mesh jump ($MESH_SSH_HOST), wg peering, and verify-agent-health"
  log "✔ node discovered on the mesh at $MESH_IP"
  local out
  out=$(ssh_mesh "curl -sS --max-time 6 http://$MESH_IP:18082/health" 2>/dev/null || true)
  if echo "$out" | grep -q '"status"'; then
    log "✔ sync health at $MESH_IP:18082: $out"
  else
    log "ℹ sync daemon not serving health yet at $MESH_IP:18082 — expected until the Telegram session is authorized (use the agent to log in)"
  fi
}

# Agent /healthz over the mesh: 200 requires matrix login + room join + egress
# canary locked. This is the primary "node is operable" signal (works pre-auth).
verify_agent_health() {
  _load
  _discover_mesh_ip || die "node never became discoverable on the mesh"
  local i out
  for i in $(seq 1 40); do
    out=$(ssh_mesh "curl -sS --max-time 6 http://$MESH_IP:18100/healthz" 2>/dev/null)
    if echo "$out" | grep -q '"status": *"ok"'; then
      log "✔ agent healthz: $out"
      return 0
    fi
    log "… agent not green yet ($i/40): ${out:-<no response>}"
    sleep 15
  done
  die "agent /healthz never went green at $MESH_IP:18100 — inspect the JSON above (matrix_logged_in / egress_locked / last_error)"
}

# The CVM's pg-provision created role+db at boot; prove it from the mesh shell by
# logging in as telegram_sync. Credentials travel inside the ssh-encrypted script
# text, never argv. (LC_ALL=C: the jump host's psql wrapper spams locale warnings.)
verify_db() {
  _load; _ensure_secrets
  local out
  out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
export LC_ALL=C
psql "postgresql://telegram_sync:${TG_DB_PASSWORD}@${PG1_MESH_IP}:5432/telegram_sync?connect_timeout=5" -tAc "SELECT current_user || ':' || current_database()" 2>&1
SCRIPT
)
  echo "$out" | grep -q '^telegram_sync:telegram_sync$' || die "verify-db failed: $out"
  log "✔ pg-ha role+db live: telegram_sync can log in to its database via HAProxy"
}

# Real Matrix roundtrip: lsdan posts `!tg status` in the DM room and expects the
# agent's deterministic status reply.
verify_agent() {
  _load; _default_cluster_env; _ensure_secrets
  local fqdn iapw server_name
  fqdn=$(_matrix_fqdn) || die "could not discover the Matrix node's tailnet FQDN"
  iapw=$(_matrix_iapw); [ -n "$iapw" ] || die "no IAPW in $MATRIX_STATE"
  server_name="$(_matrix_server_name)"
  [ -n "${MATRIX_ROOM_ID:-}" ] || die "no MATRIX_ROOM_ID in $SECRETS_FILE"
  run_step "tgsync-verify-agent-${NODE}" env \
    MATRIX_PROBE_FQDN="$fqdn" \
    MATRIX_PROBE_ROOM_ID="$MATRIX_ROOM_ID" \
    MATRIX_PROBE_USER="${ADMIN_USER:-lsdan}" \
    MATRIX_PROBE_PASSWORD="$iapw" \
    MATRIX_PROBE_BOT="@${BOT_LOCALPART}:${server_name}" \
    MATRIX_PROBE_COMMAND="!tg status" \
    MATRIX_PROBE_EXPECT_RE="Telegram session: (AUTHORIZED|NOT authorized|unknown)" \
    python3 "$HERE/matrix-probe.py"
  log "✔ agent answered !tg status in the DM room"
}

# Exercise the mesh-only FTS MCP endpoint: tunnel through the mesh jump to
# <mesh-ip>:18085 and run a real MCP client (initialize + list tools + archive_stats).
verify_mcp() {
  _load
  _discover_mesh_ip || die "node not discoverable on the mesh"
  local lport="${MCP_LPORT:-18099}"
  ssh -f -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -L "127.0.0.1:${lport}:${MESH_IP}:18085" "$MESH_SSH_HOST" sleep 90 \
    || die "could not open mesh tunnel to $MESH_IP:18085 via $MESH_SSH_HOST"
  local out
  out=$(docker run --rm --network host python:3.12-slim bash -lc "
    pip install --quiet 'mcp>=1.2' >/dev/null 2>&1
    python3 - <<PY
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def main():
    async with streamablehttp_client('http://127.0.0.1:${lport}/mcp') as (r,w,_):
        async with ClientSession(r,w) as s:
            await s.initialize()
            t=await s.list_tools()
            print('TOOLS:', [x.name for x in t.tools])
            res=await s.call_tool('archive_stats', {})
            print('STATS:', ''.join(c.text for c in res.content if hasattr(c,'text'))[:300])
asyncio.run(main())
PY" 2>&1) || true
  echo "$out" | tee "$LOGDIR/tgsync-mcp-${NODE}.$(ts).log" >&2
  echo "$out" | grep -q "'search_messages'" || die "MCP tools/list did not return search_messages over the mesh"
  log "✔ FTS MCP reachable on the mesh at $MESH_IP:18085/mcp (tools listed + archive_stats OK)"
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
    send_seq "tgsync-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
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
  log "✔ telegram-sync node update complete mode=$mode vm=$VM_ID"
  # NOTE: service checks (verify-sync / verify-agent-health / verify-agent) are separate steps.
}

log "=== Telegram-sync AttestMesh node: $NODE ==="
case "$ACTION" in
  provision-matrix) provision_matrix ;;
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  verify-health) verify_health ;;
  verify-sync) verify_sync ;;
  verify-agent-health) verify_agent_health ;;
  verify-db) verify_db ;;
  verify-mcp) verify_mcp ;;
  verify-agent) verify_agent ;;
  update) update_member ;;
  setup) deploy_cvm; prime_gate; bind_member ;;
  all) deploy_cvm; prime_gate; bind_member; verify; verify_health; verify_sync; verify_agent_health; verify_db; verify_mcp; verify_agent ;;
  *) die "usage: telegram-sync-node.sh <node-name> [provision-matrix|deploy|prime|bind|verify|verify-health|verify-sync|verify-agent-health|verify-db|verify-mcp|verify-agent|update|setup|all]" ;;
esac
