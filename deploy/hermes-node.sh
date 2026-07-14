#!/usr/bin/env bash
# Hermes agent AttestMesh node on the self-hosted dstack box — "deploy more agents".
#
# Each node is one Hermes Agent with its own identity set:
#   Matrix account   — auto-provisioned via the matrix-admin-agent LLM bot
#                      (provision-matrix; deterministic on-chain upgrade path is
#                      MATRIX_ADMIN_SENDERS, see docs/specs/matrix-admin-agent.md §8.1)
#   Hindsight bank   — bank_id = agent name on the Hindsight node (mesh-only)
#   email mailbox    — Fastmail (operator creates the account; creds in the env file)
#   GitHub identity  — machine-account PAT (operator creates; PAT in the env file)
#   model endpoint   — fugu-router/fugu-ultra by default
#
# Per-agent config lives in ~/.attestmesh/agents/<node>.env (+ optional
# <node>.soul.md persona); `hermes-node.sh <node> init` writes a template.
# Secrets are sealed into the CVM (never committed); the box helper receives
# them via a 0600 tmpfs env file, not argv.
#
# The node joins the existing Matrix cluster by default:
#   deploy/logs/matrix-node-matrix-node.state -> CLUSTER + MEMBER_IMPL (+ IAPW for provisioning)
# NOTE: the cluster has no removeMember — every agent node is a PERMANENT member.
#
# Usage:
#   bash deploy/hermes-node.sh <node> init
#   <fill ~/.attestmesh/agents/<node>.env, create Fastmail + GitHub accounts>
#   bash deploy/hermes-node.sh <node> all
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: hermes-node.sh <node-name> [init|provision-matrix|deploy|prime|bind|register-direct|verify|verify-ssh|verify-hermes|update|all|setup]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/hermes-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
HINDSIGHT_STATE="${HINDSIGHT_STATE:-$LOGDIR/hindsight-node-hindsight-node.state}"
FUGU_ROUTER_STATE="${FUGU_ROUTER_STATE:-$LOGDIR/fugu-router-node-fugu-router.state}"
FUGU_ROUTER_SECRETS="${FUGU_ROUTER_SECRETS:-$HOME/.attestmesh/fugu-router.env}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
MESH_JUMP="${MESH_JUMP:-attestmesh-mesh-node}"   # ssh alias with a foot on the wg mesh
# Prefer the operator-maintained union file so updates never drop sealed keys.
if [ -z "${AUTHORIZED_KEYS_FILE:-}" ] && [ -s "$HOME/.attestmesh/ssh-node-authorized-keys" ]; then
  AUTHORIZED_KEYS_FILE="$HOME/.attestmesh/ssh-node-authorized-keys"
fi
AUTHORIZED_KEYS_FILE="${AUTHORIZED_KEYS_FILE:-$HOME/.ssh/authorized_keys}"
export BOX_VCPU="${BOX_VCPU:-4}" BOX_MEM="${BOX_MEM:-16384}" BOX_DISK="${BOX_DISK:-60}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

AGENT_DIR="$HOME/.attestmesh/agents"
AGENT_ENV="$AGENT_DIR/${NODE}.env"
AGENT_SOUL="$AGENT_DIR/${NODE}.soul.md"
STATE="$LOGDIR/hermes-node-${NODE}.state"
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

_matrix_server() {
  local mx
  mx=$(grep '^X=' "$MATRIX_STATE" | cut -d= -f2-)
  [ -n "$mx" ] || die "no Matrix node app_id in $MATRIX_STATE"
  printf '%s.%s' "$(printf '%s' "${mx#0x}" | tr 'A-Z' 'a-z')" "$GATEWAY_DOMAIN"
}

_provider_slug() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr '_' '-'
}

_is_fugu_router_provider() {
  case "$(_provider_slug "$1")" in
    fugu-router|fugu|sakana-fugu-router) return 0 ;;
    *) return 1 ;;
  esac
}

_fugu_router_base_url() {
  local ip
  [ -s "$FUGU_ROUTER_STATE" ] || return 0
  ip=$(grep '^MESH_IP=' "$FUGU_ROUTER_STATE" | cut -d= -f2- | head -1)
  [ -n "$ip" ] || return 0
  printf 'http://%s:18410/v1' "$ip"
}

_fugu_router_master_key() {
  local value
  [ -s "$FUGU_ROUTER_SECRETS" ] || return 0
  value=$(grep '^LITELLM_MASTER_KEY=' "$FUGU_ROUTER_SECRETS" | tail -1 | cut -d= -f2-)
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

_default_cluster_env() {
  if [ -z "${CLUSTER:-}" ] || [ -z "${MEMBER_IMPL:-}" ]; then
    [ -f "$MATRIX_STATE" ] || die "missing Matrix cluster state: $MATRIX_STATE"
    CLUSTER="${CLUSTER:-$(grep '^CLUSTER=' "$MATRIX_STATE" | cut -d= -f2-)}"
    MEMBER_IMPL="${MEMBER_IMPL:-$(grep '^MEMBER_IMPL=' "$MATRIX_STATE" | cut -d= -f2-)}"
  fi
  [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "could not resolve CLUSTER/MEMBER_IMPL"
}

_load_agent_env() {
  [ -s "$AGENT_ENV" ] || die "missing agent env file: $AGENT_ENV (run: hermes-node.sh $NODE init)"
  set -a; source "$AGENT_ENV"; set +a
  local server; server=$(_matrix_server)
  AGENT_NAME="${AGENT_NAME:-$NODE}"
  MODEL_PROVIDER_NAME="${MODEL_PROVIDER_NAME:-fugu-router}"
  if _is_fugu_router_provider "$MODEL_PROVIDER_NAME"; then
    MODEL_BASE_URL="${MODEL_BASE_URL:-$(_fugu_router_base_url)}"
    MODEL_PROVIDER_NAME="fugu-router"
    if [ -z "${MODEL_API_KEY:-}" ]; then
      MODEL_API_KEY="$(_fugu_router_master_key)"
    fi
  else
    MODEL_BASE_URL="${MODEL_BASE_URL:-https://api.sakana.ai/v1}"
  fi
  MODEL_NAME="${MODEL_NAME:-fugu-ultra}"
  MATRIX_HOMESERVER="${MATRIX_HOMESERVER:-http://10.18.196.231:18080}"
  MATRIX_ALLOWED_USERS="${MATRIX_ALLOWED_USERS:-@lsdan:${server},@fran:${server}}"
  MATRIX_ENCRYPTION="${MATRIX_ENCRYPTION:-true}"
  HINDSIGHT_API_URL="${HINDSIGHT_API_URL:-http://10.18.78.76:18888}"
  if [ -z "${HINDSIGHT_API_KEY:-}" ] && [ -s "$HINDSIGHT_STATE" ]; then
    HINDSIGHT_API_KEY=$(grep '^TAK=' "$HINDSIGHT_STATE" | cut -d= -f2-)
  fi
  HINDSIGHT_BANK_ID="${HINDSIGHT_BANK_ID:-$NODE}"
  HINDSIGHT_RETAIN_TAGS="${HINDSIGHT_RETAIN_TAGS:-attestmesh}"
  EMAIL_IMAP_HOST="${EMAIL_IMAP_HOST:-imap.fastmail.com}"
  EMAIL_SMTP_HOST="${EMAIL_SMTP_HOST:-smtp.fastmail.com}"
  GIT_USER_NAME="${GIT_USER_NAME:-$AGENT_NAME}"
  GIT_USER_EMAIL="${GIT_USER_EMAIL:-${EMAIL_ADDRESS:-}}"
  HERMES_SOUL_B64="${HERMES_SOUL_B64:-}"
  if [ -z "$HERMES_SOUL_B64" ] && [ -s "$AGENT_SOUL" ]; then
    HERMES_SOUL_B64=$(base64 -w0 "$AGENT_SOUL")
  fi
}

_require_env() {
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  [ -s "$AUTHORIZED_KEYS_FILE" ] || die "missing/non-empty authorized_keys file: $AUTHORIZED_KEYS_FILE"
  SSH_AUTHORIZED_KEYS_B64="${SSH_AUTHORIZED_KEYS_B64:-$(base64 -w0 "$AUTHORIZED_KEYS_FILE")}"
  [ -n "$SSH_AUTHORIZED_KEYS_B64" ] || die "could not encode $AUTHORIZED_KEYS_FILE"
  [ -n "${MODEL_API_KEY:-}" ] || die "MODEL_API_KEY missing in $AGENT_ENV"
  [ -n "${MODEL_BASE_URL:-}" ] || die "MODEL_BASE_URL unresolved (deploy fugu-router first or set MODEL_BASE_URL in $AGENT_ENV)"
  [ -n "${HINDSIGHT_API_KEY:-}" ] || die "HINDSIGHT_API_KEY unresolved (no $HINDSIGHT_STATE?)"
  [ -n "${MATRIX_ACCESS_TOKEN:-}" ] || die "MATRIX_ACCESS_TOKEN missing — run: hermes-node.sh $NODE provision-matrix"
  [ -n "${MATRIX_USER_ID:-}" ] || die "MATRIX_USER_ID missing — run: hermes-node.sh $NODE provision-matrix"
}

send_seq() {
  local label="$1"; shift
  send_with_nonce_retry "$label" "$@"
}

# Ship the sealed env to the box as a 0600 tmpfs file (not argv — see
# deploy-scripts review 2026-07: secrets on sudo argv are box-`ps` visible).
_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok envtmp rf
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/hermes-node-box.py" "$BOX_HOST:/tmp/hermes-node-box.py"
  envtmp=$(mktemp)
  chmod 600 "$envtmp"
  {
    printf "BOX_NAME=%q\nBOX_COMPOSE=%q\n" "$NODE" "/tmp/${NODE}.yaml"
    printf "BOX_VCPU=%q\nBOX_MEM=%q\nBOX_DISK=%q\nBOX_PORTS=%q\n" "$BOX_VCPU" "$BOX_MEM" "$BOX_DISK" "$BOX_PORTS"
    printf "BOX_GATEWAY_ENABLED=%q\nBOX_NET_MODE=%q\nBOX_FRESH_DISK=%q\n" "$BOX_GATEWAY_ENABLED" "$BOX_NET_MODE" "${BOX_FRESH_DISK:-}"
    # CVM_* variants: what the sidecar sees (Alchemy dead → publicnode; a non-bundler
    # endpoint makes the sidecar fail fast and emit ATTESTMESH_DIRECT_REGISTER,
    # which register-direct picks up — same pattern as db-ha/pocket-mcp).
    printf "E_CHAIN_ID=%q\nE_RPC_URL=%q\nE_BUNDLER_URL=%q\nE_GAS_POLICY_ID=%q\n" "$CHAIN_ID" "${CVM_RPC_URL:-$RPC_URL}" "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}" "${GAS_POLICY_ID:-}"
    printf "E_INDEXER_REGISTRY_ADDR=%q\nE_GATEWAY_DOMAIN=%q\nE_CLUSTER=%q\n" "$INDEXER_REGISTRY_ADDR" "$GATEWAY_DOMAIN" "${CLUSTER:-}"
    printf "E_SSH_AUTHORIZED_KEYS_B64=%q\n" "$SSH_AUTHORIZED_KEYS_B64"
    printf "E_AGENT_NAME=%q\n" "$AGENT_NAME"
    printf "E_MODEL_PROVIDER_NAME=%q\nE_MODEL_BASE_URL=%q\nE_MODEL_NAME=%q\nE_MODEL_API_KEY=%q\n" \
      "$MODEL_PROVIDER_NAME" "$MODEL_BASE_URL" "$MODEL_NAME" "${MODEL_API_KEY:-}"
    printf "E_MATRIX_HOMESERVER=%q\nE_MATRIX_USER_ID=%q\nE_MATRIX_ACCESS_TOKEN=%q\n" \
      "$MATRIX_HOMESERVER" "${MATRIX_USER_ID:-}" "${MATRIX_ACCESS_TOKEN:-}"
    printf "E_MATRIX_ALLOWED_USERS=%q\nE_MATRIX_ENCRYPTION=%q\n" "$MATRIX_ALLOWED_USERS" "$MATRIX_ENCRYPTION"
    printf "E_HINDSIGHT_API_URL=%q\nE_HINDSIGHT_API_KEY=%q\nE_HINDSIGHT_BANK_ID=%q\nE_HINDSIGHT_RETAIN_TAGS=%q\n" \
      "$HINDSIGHT_API_URL" "${HINDSIGHT_API_KEY:-}" "$HINDSIGHT_BANK_ID" "$HINDSIGHT_RETAIN_TAGS"
    printf "E_EMAIL_ADDRESS=%q\nE_EMAIL_PASSWORD=%q\nE_EMAIL_IMAP_HOST=%q\nE_EMAIL_SMTP_HOST=%q\nE_EMAIL_HOME_ADDRESS=%q\n" \
      "${EMAIL_ADDRESS:-}" "${EMAIL_PASSWORD:-}" "$EMAIL_IMAP_HOST" "$EMAIL_SMTP_HOST" "${EMAIL_HOME_ADDRESS:-}"
    printf "E_GITHUB_TOKEN=%q\nE_GIT_USER_NAME=%q\nE_GIT_USER_EMAIL=%q\n" \
      "${GITHUB_TOKEN:-}" "$GIT_USER_NAME" "${GIT_USER_EMAIL:-}"
    printf "E_HERMES_SOUL_B64=%q\nE_WORKSPACE_GIT_URL=%q\n" "$HERMES_SOUL_B64" "${WORKSPACE_GIT_URL:-}"
    printf "E_DSTACK_DOCKER_USERNAME=%q\nE_DSTACK_DOCKER_PASSWORD=%q\nE_DSTACK_DOCKER_REGISTRY=%q\n" \
      "${guser:-dmvt}" "$gtok" "ghcr.io"
  } > "$envtmp"
  rf="/dev/shm/hermes-node-${NODE}-env.$$"
  scp -o BatchMode=yes -q "$envtmp" "$BOX_HOST:$rf"
  rm -f "$envtmp"
  ssh_box "chmod 600 $rf; sudo bash -c 'set -a; source $rf; rm -f $rf; set +a; exec $BOX_PY /tmp/hermes-node-box.py $mode $app_id $vm_id'"
}

_gw_host() {
  local xb="${X#0x}" port="${1:-1022}"
  printf '%s-%s.%s' "$(printf '%s' "$xb" | tr 'A-Z' 'a-z')" "$port" "$GATEWAY_DOMAIN"
}

_node_ssh() {
  local host; host="$(_gw_host 1022)"
  ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="$LOGDIR/hermes-node-${NODE}.known_hosts" \
    -o ProxyCommand="openssl s_client -quiet -connect ${host}:443 -servername ${host}" \
    "root@${host}" "$@"
}

init_env() {
  install -d -m 700 "$AGENT_DIR"
  [ -s "$AGENT_ENV" ] && die "already exists: $AGENT_ENV"
  umask 077
  cat > "$AGENT_ENV" <<EOF
# Hermes agent identity for node "$NODE" — sourced by deploy/hermes-node.sh.
# Fill the REQUIRED lines; create the Fastmail mailbox + GitHub machine
# account by hand first. provision-matrix fills the MATRIX_* lines.

# Default: fugu-router via deploy/logs/fugu-router-node-fugu-router.state and
# ~/.attestmesh/fugu-router.env. Uncomment to override or to use direct Sakana.
#MODEL_PROVIDER_NAME=fugu-router
#MODEL_API_KEY=             # defaults to fugu-router LITELLM_MASTER_KEY when available
#MODEL_BASE_URL=            # defaults to http://<fugu-router-mesh-ip>:18410/v1
#MODEL_NAME=fugu-ultra
#
# Direct Sakana fallback:
#MODEL_PROVIDER_NAME=custom
#MODEL_API_KEY=             # Sakana key
#MODEL_BASE_URL=https://api.sakana.ai/v1

EMAIL_ADDRESS=              # Fastmail mailbox for this agent
EMAIL_PASSWORD=             # Fastmail app password (IMAP/SMTP)
#EMAIL_HOME_ADDRESS=        # operator "home" address the agent reports to

GITHUB_TOKEN=               # machine-account PAT
#GIT_USER_NAME=$NODE
#GIT_USER_EMAIL=            # defaults to EMAIL_ADDRESS

#MATRIX_ALLOWED_USERS=      # defaults to @lsdan + @fran on the cluster homeserver
#WORKSPACE_GIT_URL=         # e.g. https://github.com/AttestMesh/dstack-deep-dive.git
#HINDSIGHT_BANK_ID=$NODE
#HINDSIGHT_RETAIN_TAGS=attestmesh

# Filled by: bash deploy/hermes-node.sh $NODE provision-matrix
MATRIX_USER_ID=
MATRIX_ACCESS_TOKEN=
EOF
  log "✔ wrote $AGENT_ENV — fill it in (persona goes in $AGENT_SOUL)"
}

provision_matrix() {
  _load_agent_env
  if [ -n "${MATRIX_ACCESS_TOKEN:-}" ]; then
    log "MATRIX_ACCESS_TOKEN already present in $AGENT_ENV — skipping"
    return 0
  fi
  [ -s "$MATRIX_STATE" ] || die "missing $MATRIX_STATE"
  local iapw server hs_host hs_port lport out uid tok
  iapw=$(grep '^IAPW=' "$MATRIX_STATE" | cut -d= -f2-)
  [ -n "$iapw" ] || die "no IAPW (admin password) in $MATRIX_STATE"
  server=$(_matrix_server)
  hs_host=$(printf '%s' "$MATRIX_HOMESERVER" | sed -E 's|^https?://||; s|/.*$||; s|:.*$||')
  hs_port=$(printf '%s' "$MATRIX_HOMESERVER" | sed -nE 's|^https?://[^:/]+:([0-9]+).*|\1|p')
  lport=$(( (RANDOM % 2000) + 28080 ))
  log "▶ provisioning @${NODE}:${server} via ${MESH_JUMP} tunnel (localhost:$lport)"
  ssh -f -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -L "127.0.0.1:${lport}:${hs_host}:${hs_port:-80}" "$MESH_JUMP" sleep 600 \
    || die "could not open mesh tunnel via $MESH_JUMP"
  out=$(HS_URL="http://127.0.0.1:${lport}" MATRIX_SERVER="$server" AGENT_USERNAME="$NODE" \
        ADMIN_USER="${MATRIX_ADMIN_LOCALPART:-lsdan}" ADMIN_PASSWORD="$iapw" \
        python3 "$HERE/hermes-matrix-provision.py") || die "matrix provisioning failed"
  uid=$(echo "$out" | jq -r .user_id)
  tok=$(echo "$out" | jq -r .access_token)
  [ -n "$tok" ] && [ "$tok" != null ] || die "no token in provisioner output"
  sed -i "s|^MATRIX_USER_ID=.*|MATRIX_USER_ID=$uid|; s|^MATRIX_ACCESS_TOKEN=.*|MATRIX_ACCESS_TOKEN=$tok|" "$AGENT_ENV"
  grep -q '^MATRIX_ACCESS_TOKEN=.\+' "$AGENT_ENV" || { umask 077; printf 'MATRIX_USER_ID=%s\nMATRIX_ACCESS_TOKEN=%s\n' "$uid" "$tok" >> "$AGENT_ENV"; }
  log "✔ provisioned $uid (token stored in $AGENT_ENV)"
}

deploy_cvm() {
  _load; _default_cluster_env; _load_agent_env; _require_env
  _save
  log "▶ box deploy_app hermes node=$NODE compose=$COMPOSE cluster=$CLUSTER"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed hermes node app_id=$X compose_hash=$H vm=$VM_ID"
  log "ssh gateway hosts: $(_gw_host 1022) / $(_gw_host 1023)"
}

prime_gate() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  local allowed_hash allowed_app
  allowed_hash=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_hash" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "hermes-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  fi
  allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_app" = true ]; then
    log "app id already allowlisted"
  else
    send_seq "hermes-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  fi
}

bind_member() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind hermes X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/hermes-bind-${NODE}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --async --rpc-url $BOX_RPC --private-key "\$KEY"
SCRIPT
  confirm_latest_transaction "hermes-bind-${NODE}" "$RPC_URL" "$LOGDIR/hermes-bind-${NODE}.*.log" || die "bind transaction not confirmed"
  local c=""
  for _ in 1 2 3 4 5 6 7 8; do
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    [ "${c,,}" = "${CLUSTER,,}" ] && break
    sleep 2
  done
  log "X.cluster()=$c (expect $CLUSTER)"
  [ "${c,,}" = "${CLUSTER,,}" ] || die "bind did not stick (X.cluster()=$c)"
  log "✔ bound hermes node X -> $CLUSTER"
}

# Interim registration path while there is no working EIP-4337 bundler
# (Alchemy dead): the sidecar, on a failing bundler, emits its registration
# calldata to the console as `ATTESTMESH_DIRECT_REGISTER {member,calldata}`;
# the deployer sends it directly, paying gas. Same pattern as
# indexer-member-node.sh / pocket-mcp / db-ha.
register_direct() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] || die "need X/VM_ID in $STATE"
  local zero id payload member calldata i
  zero=$ZERO32
  id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null || true)
  if [ -n "$id" ] && [ "$id" != "$zero" ]; then
    log "hermes node already registered: memberId=$id"
    return 0
  fi
  local helper_url
  helper_url="https://$(printf '%s' "${X#0x}" | tr 'A-Z' 'a-z')-9092.${GATEWAY_DOMAIN}/registration-calldata"
  for i in $(seq 1 60); do
    # HTTP first (the helper serves its payload on :9092 via the dstack
    # gateway; container stdout does NOT reliably reach the box serial log —
    # proven on the tessera maiden deploy). Serial scrape kept as fallback.
    payload=$(curl -sfm 8 "$helper_url" 2>/dev/null)
    [ -n "$payload" ] || payload=$(ssh_box "sudo sed 's/\\x1b\\[[0-9;]*m//g' /srv/data/dstack/vm/$VM_ID/serial.log /srv/data/dstack/vm/$VM_ID/serial.history.log 2>/dev/null" \
      | grep 'ATTESTMESH_DIRECT_REGISTER ' | sed 's/^.*ATTESTMESH_DIRECT_REGISTER //' | tail -1)
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
  die "registration helper calldata not found in CVM serial logs"
}

verify() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ hermes node registered: memberId=$id memberCount=$count"
      return 0
    fi
    log "… hermes node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  die "hermes node did not register"
}

verify_ssh_gateway() {
  _load
  [ -n "${X:-}" ] || die "need X (run deploy first)"
  local host line i
  host="$(_gw_host 1022)"
  for i in $(seq 1 30); do
    line=$(timeout 10 openssl s_client -quiet -servername "$host" -connect "$host:443" </dev/null 2>/dev/null | head -1 || true)
    if printf '%s' "$line" | grep -q '^SSH-'; then
      log "✔ ssh gateway is reachable: $host:443 -> $line"
      return 0
    fi
    log "… ssh gateway not ready ($i/30): ${line:-<no banner>}"
    sleep 10
  done
  die "ssh gateway never returned an SSH banner at $host:443"
}

# The gateway self-reports into /root/.hermes/gateway_state.json on the shared
# workspace volume — readable from the bridge shell, so verify over ssh.
verify_hermes() {
  _load
  [ -n "${X:-}" ] || die "need X (run deploy first)"
  local i out
  for i in $(seq 1 30); do
    out=$(_node_ssh 'cat /root/.hermes/gateway_state.json 2>/dev/null' 2>/dev/null || true)
    if printf '%s' "$out" | grep -Eq '"matrix": *\{"state": *"connected"'; then
      log "✔ hermes gateway up, matrix connected"
      printf '%s\n' "$out" | head -c 600; echo
      return 0
    fi
    log "… hermes gateway not connected yet ($i/30)"
    sleep 20
  done
  die "hermes gateway never reported matrix connected (check: ssh to $(_gw_host 1022))"
}

update_member() {
  _load; _default_cluster_env; _load_agent_env; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  local nh allowed out j mode
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "hermes-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ hermes node update complete mode=$mode vm=$VM_ID"
  verify
  verify_ssh_gateway
}

log "=== Hermes agent AttestMesh node: $NODE ==="
case "$ACTION" in
  init) init_env ;;
  provision-matrix) provision_matrix ;;
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  verify-ssh) verify_ssh_gateway ;;
  verify-hermes) verify_hermes ;;
  update) update_member ;;
  register-direct) register_direct ;;
  setup) provision_matrix; deploy_cvm; prime_gate; bind_member; register_direct ;;
  all) provision_matrix; deploy_cvm; prime_gate; bind_member; register_direct; verify; verify_ssh_gateway; verify_hermes ;;
  *) die "usage: hermes-node.sh <node-name> [init|provision-matrix|deploy|prime|bind|register-direct|verify|verify-ssh|verify-hermes|update|all|setup]" ;;
esac
