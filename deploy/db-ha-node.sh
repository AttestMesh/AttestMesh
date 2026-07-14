#!/usr/bin/env bash
# Co-tenanted database HA cluster deploy: N CVMs (default 3), each running one
# member of Postgres HA, Redis HA, and ClickHouse HA behind one AttestMesh sidecar.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: db-ha-node.sh <name> [register-all|compute-peers|create-all|deploy-all|prime-all|bind-all|verify-all|verify-status|verify-isolation-all|update <dbN>|update-all|all]}"
ACTION="${2:-all}"
ARG3="${3:-}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/db-ha-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
SSH_STATE="${SSH_STATE:-$LOGDIR/ssh-node-ssh-node.state}"
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/db-ha.env}"
[ -f "$SECRETS_FILE" ] && source "$SECRETS_FILE"

DBHA_COUNT="${DBHA_COUNT:-3}"
export BOX_VCPU="${BOX_VCPU:-16}" BOX_MEM="${BOX_MEM:-32768}" BOX_DISK="${BOX_DISK:-160}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
BACKUP_ENABLED="${BACKUP_ENABLED:-true}"
BACKUP_PREFIX="${BACKUP_PREFIX:-db-ha/pg}"
BACKUP_RESTORE="${BACKUP_RESTORE:-}"

CSTATE="$LOGDIR/db-ha-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000
[ $((DBHA_COUNT % 2)) -eq 0 ] && log "WARNING: DBHA_COUNT=$DBHA_COUNT is even; 3 or 5 is recommended for quorum services"

_nodes() { local i; for i in $(seq 1 "$DBHA_COUNT"); do echo "db$i"; done; }
_idx() { printf '%s\n' "${1#db}"; }
_pg_name() { printf 'pg%s\n' "$(_idx "$1")"; }
_redis_name() { printf 'r%s\n' "$(_idx "$1")"; }
_ch_name() { printf 'ch%s\n' "$(_idx "$1")"; }
_node_state() { echo "$LOGDIR/db-ha-node-${NODE}-$1.state"; }

_save_cluster() {
  umask 077
  cat > "$CSTATE" <<EOF
CLUSTER=${CLUSTER:-}
MEMBER_IMPL=${MEMBER_IMPL:-}
MATRIX_X=${MATRIX_X:-}
MATRIX_MESH_IP=${MATRIX_MESH_IP:-}
MATRIX_ROOM_ID=${MATRIX_ROOM_ID:-}
MATRIX_ADMIN_MXIDS=${MATRIX_ADMIN_MXIDS:-}
CIDR_IP=${CIDR_IP:-}
CIDR_PREFIX=${CIDR_PREFIX:-}
MESH_CIDR_STR=${MESH_CIDR_STR:-}
DBHA_PEERS=${DBHA_PEERS:-}
PGHA_PEERS=${PGHA_PEERS:-}
REDIS_PEERS=${REDIS_PEERS:-}
CH_PEERS=${CH_PEERS:-}
PGHA_VERIFY_PASSWORD=${PGHA_VERIFY_PASSWORD:-}
REDISHA_VERIFY_PASSWORD=${REDISHA_VERIFY_PASSWORD:-}
CHHA_VERIFY_PASSWORD=${CHHA_VERIFY_PASSWORD:-}
BOTPASSWORD=${BOTPASSWORD:-}
DBHA_INITIALIZED=${DBHA_INITIALIZED:-}
EOF
}
_load_cluster() { [ -f "$CSTATE" ] && source "$CSTATE" || true; }

_nload() {
  X=""; H=""; VM_ID=""; MESH_IP=""
  local f; f="$(_node_state "$1")"
  [ -f "$f" ] && source "$f" || true
}
_nsave() {
  local f; f="$(_node_state "$1")"
  umask 077
  cat > "$f" <<EOF
X=${X:-}
H=${H:-}
VM_ID=${VM_ID:-}
MESH_IP=${MESH_IP:-}
EOF
}

ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }

_ipv4_from_u32() {
  local n="$1"
  printf '%d.%d.%d.%d' "$(( (n >> 24) & 255 ))" "$(( (n >> 16) & 255 ))" "$(( (n >> 8) & 255 ))" "$(( n & 255 ))"
}

_matrix_server_name() {
  printf '%s.gateway.attestmesh.xyz' "$(printf '%s' "${MATRIX_X#0x}" | tr A-Z a-z)"
}

_bot_user_id() { printf '@dbha-%s:%s' "$1" "$(_matrix_server_name)"; }

_default_matrix_env() {
  [ -f "$MATRIX_STATE" ] || die "missing Matrix state: $MATRIX_STATE"
  local m_x m_cluster m_impl
  m_x=$(grep '^X=' "$MATRIX_STATE" | cut -d= -f2-)
  m_cluster=$(grep '^CLUSTER=' "$MATRIX_STATE" | cut -d= -f2-)
  m_impl=$(grep '^MEMBER_IMPL=' "$MATRIX_STATE" | cut -d= -f2-)
  [ -n "$m_x" ] && [ -n "$m_cluster" ] && [ -n "$m_impl" ] || die "Matrix state lacks X/CLUSTER/MEMBER_IMPL"

  MATRIX_X="${MATRIX_X:-$m_x}"
  CLUSTER="${CLUSTER:-$m_cluster}"
  MEMBER_IMPL="${MEMBER_IMPL:-$m_impl}"

  local matrix_member_id mesh_u32
  matrix_member_id=$(cast call "$CLUSTER" 'memberIdOf(address)(bytes32)' "$MATRIX_X" --rpc-url "$RPC_URL" 2>/dev/null)
  [ -n "$matrix_member_id" ] && [ "$matrix_member_id" != "$ZERO32" ] || die "Matrix app is not registered in cluster $CLUSTER"
  mesh_u32=$(cast call "$CLUSTER" 'meshIpOf(bytes32)(uint32)' "$matrix_member_id" --rpc-url "$RPC_URL" 2>/dev/null | awk '{print int($1)}')
  [ -n "$mesh_u32" ] && [ "$mesh_u32" != 0 ] || die "could not resolve Matrix mesh IP"
  MATRIX_MESH_IP="${MATRIX_MESH_IP:-$(_ipv4_from_u32 "$mesh_u32")}"

  local server_name; server_name="$(_matrix_server_name)"
  MATRIX_ROOM_ID="${MATRIX_ROOM_ID:-!QlbJvhWoxMNcJvVwCr:${server_name}}"
  MATRIX_ADMIN_MXIDS="${MATRIX_ADMIN_MXIDS:-@lsdan:${server_name}}"
}

_require_env() {
  local missing="" indexer
  [ -n "${LLM_API_KEY:-}" ] || LLM_API_KEY="$(cat "$HOME/.attestmesh/redpill-key" 2>/dev/null || true)"
  LLM_BASE_URL="${LLM_BASE_URL:-https://api.redpill.ai/v1}"
  LLM_MODEL="${LLM_MODEL:-z-ai/glm-5.2}"
  BUNDLER_URL="${BUNDLER_URL:-$RPC_URL}"
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  for v in LLM_BASE_URL LLM_MODEL LLM_API_KEY BUNDLER_URL INDEXER_REGISTRY_ADDR; do
    [ -n "${!v:-}" ] && [ "${!v:-}" != null ] || missing="$missing $v"
  done
  if [ "$BACKUP_ENABLED" = true ]; then
    for v in R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_ENDPOINT R2_BUCKET; do
      [ -n "${!v:-}" ] || missing="$missing $v"
    done
  fi
  [ -z "$missing" ] || die "missing required env:$missing (put R2_*/BOTPASSWORD in $SECRETS_FILE)"
}

send_seq() {
  local label="$1"; shift
  send_with_nonce_retry "$label" "$@"
}

_mesh_math_init() {
  [ -n "${CIDR_IP:-}" ] && [ -n "${CIDR_PREFIX:-}" ] && return 0
  ATTESTOR_ID="${ATTESTOR_ID:-$(cast keccak "attestmesh.attestor.dstack")}"
  local out
  mapfile -t out < <(cast call "$CLUSTER" 'meshCidr()(uint32,uint8)' --rpc-url "$RPC_URL")
  CIDR_IP=$(awk '{print int($1)}' <<<"${out[0]:-}")
  CIDR_PREFIX=$(awk '{print int($1)}' <<<"${out[1]:-}")
  [ -n "$CIDR_IP" ] && [ "$CIDR_IP" != 0 ] && [ -n "$CIDR_PREFIX" ] && [ "$CIDR_PREFIX" != 0 ] \
    || die "could not read meshCidr() from $CLUSTER"
  MESH_CIDR_STR="$(_ipv4_from_u32 "$CIDR_IP")/$CIDR_PREFIX"
}

_member_id_for_app() {
  ATTESTOR_ID="${ATTESTOR_ID:-$(cast keccak "attestmesh.attestor.dstack")}"
  cast keccak "$(cast abi-encode 'f(address,address,bytes32)' "$CLUSTER" "$1" "$ATTESTOR_ID")"
}

_mesh_ip_for_member() {
  local low_hash low32 host_count offset
  low_hash=$(cast keccak "$1")
  low32=$(( 16#${low_hash: -8} ))
  host_count=$(( 1 << (32 - CIDR_PREFIX) ))
  offset=$(( low32 % (host_count - 2) + 1 ))
  _ipv4_from_u32 $(( CIDR_IP | offset ))
}

_box_run() {
  local mode="$1" node="${2:-db1}" app_id="${3:-}" vm_id="${4:-}" guser gtok bootstrap idx aliases
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  bootstrap="${NODE_BOOTSTRAP:-new}"
  idx="$(_idx "$node")"
  aliases="dbha-${node},pgha-pg${idx}"
  [ "$node" = db1 ] && aliases="${aliases},db-ha,pg-ha"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/db-ha-node-box.py" "$BOX_HOST:/tmp/db-ha-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n'              "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n'               "${CVM_RPC_URL:-$RPC_URL}"
    printf 'E_BUNDLER_URL=%q\n'           "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
    printf 'E_GAS_POLICY_ID=%q\n'         "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "${INDEXER_REGISTRY_ADDR:-}"
    printf 'E_GATEWAY_DOMAIN=%q\n'        "$GATEWAY_DOMAIN"
    printf 'E_PGHA_NODE_NAME=%q\n'        "pg${idx}"
    printf 'E_PGHA_PEERS=%q\n'            "${PGHA_PEERS:-}"
    printf 'E_PGHA_BOOTSTRAP=%q\n'        "$bootstrap"
    printf 'E_PGHA_MESH_CIDR=%q\n'        "${MESH_CIDR_STR:-}"
    printf 'E_PGHA_VERIFY_PASSWORD=%q\n'  "${PGHA_VERIFY_PASSWORD:-}"
    printf 'E_BACKUP_ENABLED=%q\n'        "$BACKUP_ENABLED"
    printf 'E_BACKUP_PREFIX=%q\n'         "$BACKUP_PREFIX"
    printf 'E_BACKUP_RESTORE=%q\n'        "$BACKUP_RESTORE"
    printf 'E_R2_ACCESS_KEY_ID=%q\n'      "${R2_ACCESS_KEY_ID:-}"
    printf 'E_R2_SECRET_ACCESS_KEY=%q\n'  "${R2_SECRET_ACCESS_KEY:-}"
    printf 'E_R2_ENDPOINT=%q\n'           "${R2_ENDPOINT:-}"
    printf 'E_R2_BUCKET=%q\n'             "${R2_BUCKET:-}"
    printf 'E_R2_REGION=%q\n'             "${R2_REGION:-us-east-1}"
    printf 'E_REDISHA_NODE_NAME=%q\n'     "r${idx}"
    printf 'E_REDIS_PEERS=%q\n'           "${REDIS_PEERS:-}"
    printf 'E_REDISHA_BOOTSTRAP=%q\n'     "$bootstrap"
    printf 'E_REDISHA_VERIFY_PASSWORD=%q\n' "${REDISHA_VERIFY_PASSWORD:-}"
    printf 'E_CHHA_NODE_NAME=%q\n'        "ch${idx}"
    printf 'E_CH_PEERS=%q\n'              "${CH_PEERS:-}"
    printf 'E_CHHA_BOOTSTRAP=%q\n'        "$bootstrap"
    printf 'E_CHHA_VERIFY_PASSWORD=%q\n'  "${CHHA_VERIFY_PASSWORD:-}"
    printf 'E_MATRIX_MESH_IP=%q\n'        "${MATRIX_MESH_IP:-}"
    printf 'E_MATRIX_USER_ID=%q\n'        "$(_bot_user_id "$node")"
    printf 'E_MATRIX_PASSWORD=%q\n'       "${BOTPASSWORD:-}"
    printf 'E_MATRIX_ROOM_ID=%q\n'        "${MATRIX_ROOM_ID:-}"
    printf 'E_MATRIX_ADMIN_MXIDS=%q\n'    "${MATRIX_ADMIN_MXIDS:-}"
    printf 'E_MATRIX_MENTION_ALIASES=%q\n' "$aliases"
    printf 'E_LLM_BASE_URL=%q\n'          "${LLM_BASE_URL:-}"
    printf 'E_LLM_MODEL=%q\n'             "${LLM_MODEL:-}"
    printf 'E_LLM_API_KEY=%q\n'           "${LLM_API_KEY:-}"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "sudo BOX_APP_NAME='${NODE}' BOX_NAME='${NODE}-${node}' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/db-ha-node-box.py $mode $app_id $vm_id'"
}

register_all() {
  _load_cluster; _default_matrix_env; _require_env
  _save_cluster
  local n out j
  for n in $(_nodes); do
    _nload "$n"
    if [ -n "$X" ]; then log "$n already registered: $X"; continue; fi
    log "register DstackApp for $n"
    out=$(_box_run register "$n") || die "box register failed for $n"
    j=$(echo "$out" | grep '"app_id"' | tail -1)
    X=$(echo "$j" | jq -r .app_id)
    H=$(echo "$j" | jq -r .compose_hash)
    [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from register: $out"
    _nsave "$n"
    log "$n app_id=$X compose_hash=$H"
  done
}

compute_peers() {
  _load_cluster; _default_matrix_env; _mesh_math_init
  local used=" " n mid ip attempts out j existing id idx
  declare -A OURS
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$X" ] && OURS["$(_member_id_for_app "$X" | tr 'A-Z' 'a-z')"]=1
  done
  existing=$(cast call "$CLUSTER" 'listMembers()(bytes32[])' --rpc-url "$RPC_URL" | tr -d '[] ' | tr ',' '\n')
  for id in $existing; do
    [ -n "$id" ] || continue
    [ -n "${OURS[${id,,}]:-}" ] && continue
    used="$used$(_mesh_ip_for_member "$id") "
  done
  log "reserved non-dbha member IPs:$used"
  DBHA_PEERS=""; PGHA_PEERS=""; REDIS_PEERS=""; CH_PEERS=""
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$X" ] || die "$n has no app_id; run register-all first"
    attempts=0
    while :; do
      mid=$(_member_id_for_app "$X")
      ip=$(_mesh_ip_for_member "$mid")
      case "$used" in
        *" $ip "*)
          attempts=$((attempts + 1))
          [ "$attempts" -le 3 ] || die "$n: 3 mesh-IP collisions in a row; inspect manually"
          log "$n app $X collides on $ip; registering replacement app_id ($attempts/3)"
          out=$(_box_run register "$n") || die "replacement register failed"
          j=$(echo "$out" | grep '"app_id"' | tail -1)
          X=$(echo "$j" | jq -r .app_id); H=$(echo "$j" | jq -r .compose_hash)
          ;;
        *) break ;;
      esac
    done
    used="$used$ip "
    MESH_IP="$ip"
    idx="$(_idx "$n")"
    _nsave "$n"
    DBHA_PEERS="${DBHA_PEERS:+$DBHA_PEERS,}$n=$ip"
    PGHA_PEERS="${PGHA_PEERS:+$PGHA_PEERS,}pg$idx=$ip"
    REDIS_PEERS="${REDIS_PEERS:+$REDIS_PEERS,}r$idx=$ip"
    CH_PEERS="${CH_PEERS:+$CH_PEERS,}ch$idx=$ip"
    log "$n -> memberId=$mid meshIp=$ip (pg$idx/r$idx/ch$idx)"
  done
  _save_cluster
  log "DBHA_PEERS=$DBHA_PEERS"
  log "PGHA_PEERS=$PGHA_PEERS"
  log "REDIS_PEERS=$REDIS_PEERS"
  log "CH_PEERS=$CH_PEERS"
}

create_all() {
  _load_cluster; _default_matrix_env; _require_env; _mesh_math_init
  [ -n "${PGHA_PEERS:-}" ] && [ -n "${REDIS_PEERS:-}" ] && [ -n "${CH_PEERS:-}" ] || die "run compute-peers first"
  [ -n "${BOTPASSWORD:-}" ] || die "missing BOTPASSWORD for @dbha-dbN bot users; put it in $SECRETS_FILE"
  PGHA_VERIFY_PASSWORD="${PGHA_VERIFY_PASSWORD:-$(openssl rand -hex 24)}"
  REDISHA_VERIFY_PASSWORD="${REDISHA_VERIFY_PASSWORD:-$(openssl rand -hex 24)}"
  CHHA_VERIFY_PASSWORD="${CHHA_VERIFY_PASSWORD:-$(openssl rand -hex 24)}"
  _save_cluster
  local n out j bootstrap
  bootstrap=new
  [ -n "${DBHA_INITIALIZED:-}" ] && bootstrap=join
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$X" ] || die "$n has no app_id; run register-all first"
    if [ -n "$VM_ID" ]; then log "$n already has vm=$VM_ID"; continue; fi
    log "CreateVm $n (app $X, bootstrap=$bootstrap, ${BOX_VCPU}vcpu/${BOX_MEM}MB)"
    out=$(NODE_BOOTSTRAP="$bootstrap" _box_run create "$n" "$X") || die "box create failed for $n"
    j=$(echo "$out" | grep '"app_id"' | tail -1)
    VM_ID=$(echo "$j" | jq -r .vm_id)
    H=$(echo "$j" | jq -r .compose_hash)
    [ -n "$VM_ID" ] && [ "$VM_ID" != null ] || die "could not parse vm_id from create: $out"
    _nsave "$n"
    log "$n vm=$VM_ID"
  done
}

deploy_all() { register_all; compute_peers; create_all; }

prime_all() {
  _load_cluster; _default_matrix_env
  local n h="" allowed
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$H" ] || die "$n has no compose hash; run register-all first"
    [ -z "$h" ] && h="$H"
    [ "$h" = "$H" ] || die "compose hashes differ between nodes ($h vs $H)"
  done
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${h#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "dbha-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${h#0x}"
  fi
  for n in $(_nodes); do
    _nload "$n"
    allowed=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    if [ "$allowed" = true ]; then
      log "$n app id already allowlisted"
    else
      send_seq "dbha-addApp-${NODE}-${n}" "$CLUSTER" "addAllowedAppId(address)" "$X"
    fi
  done
}

bind_all() {
  _load_cluster; _default_matrix_env
  [ -n "${MEMBER_IMPL:-}" ] || die "need MEMBER_IMPL"
  local n reinit c
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$X" ] || die "$n has no app_id"
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ "${c,,}" = "${CLUSTER,,}" ]; then log "$n already bound"; continue; fi
    log "bind $n X=$X -> impl $MEMBER_IMPL"
    ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/dbha-bind-${NODE}-${n}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --async --rpc-url $BOX_RPC --private-key "\$KEY"
SCRIPT
    confirm_latest_transaction "dbha-bind-${NODE}-${n}" "$RPC_URL" "$LOGDIR/dbha-bind-${NODE}-${n}.*.log" || die "bind transaction not confirmed for $n"
    c=""
    for _ in 1 2 3 4 5 6 7 8; do
      c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
      [ "${c,,}" = "${CLUSTER,,}" ] && break
      sleep 2
    done
    [ "${c,,}" = "${CLUSTER,,}" ] || die "bind did not stick for $n (X.cluster()=$c)"
    log "bound $n"
  done
}

verify_all() {
  _load_cluster; _default_matrix_env; _mesh_math_init
  local n i id count onchain_u32 onchain_ip
  for n in $(_nodes); do
    _nload "$n"
    id=""
    for i in $(seq 1 45); do
      id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
      count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
      [ -n "$id" ] && [ "$id" != "$ZERO32" ] && break
      log "$n not registered yet ($i/45, memberCount=${count:-?})"
      sleep 20
      id=""
    done
    [ -n "$id" ] || die "$n did not register"
    onchain_u32=$(cast call "$CLUSTER" 'meshIpOf(bytes32)(uint32)' "$id" --rpc-url "$RPC_URL" | awk '{print int($1)}')
    onchain_ip=$(_ipv4_from_u32 "$onchain_u32")
    [ "$onchain_ip" = "$MESH_IP" ] || die "$n mesh-IP drift: precomputed $MESH_IP but chain says $onchain_ip"
    log "$n registered: memberId=$id meshIp=$onchain_ip"
  done
}

_mesh_ssh() {
  [ -f "$SSH_STATE" ] || die "missing ssh-node state: $SSH_STATE"
  local sx host
  sx=$(grep '^X=' "$SSH_STATE" | cut -d= -f2-)
  host="$(printf '%s' "${sx#0x}" | tr 'A-Z' 'a-z')-1023.${GATEWAY_DOMAIN}"
  ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new \
      -o UserKnownHostsFile="$LOGDIR/dbha-mesh-shell.known_hosts" \
      -o ProxyCommand="openssl s_client -quiet -connect ${host}:443 -servername ${host} 2>/dev/null" \
      "root@$host" "$@"
}

verify_status() {
  _load_cluster
  [ -n "${DBHA_PEERS:-}" ] || die "need DBHA_PEERS in $CSTATE"
  local first_ip
  first_ip="${DBHA_PEERS#*=}"; first_ip="${first_ip%%,*}"
  log "checking merged status server at $first_ip:8009"
  {
    printf 'IP=%q\n' "$first_ip"
    cat <<'RSCRIPT'
set -u
command -v curl >/dev/null 2>&1 || { apt-get update -qq >/dev/null 2>&1; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl >/dev/null 2>&1; }
for i in $(seq 1 40); do
  if curl -fsS --max-time 5 "http://$IP:8009/" >/tmp/dbha-status-index 2>/dev/null \
     && curl -fsS --max-time 5 "http://$IP:8009/state" >/dev/null 2>&1 \
     && curl -fsS --max-time 5 "http://$IP:8009/redis/state" >/dev/null 2>&1 \
     && curl -fsS --max-time 5 "http://$IP:8009/clickhouse/state" >/dev/null 2>&1; then
    echo "STATUS: PASS - merged status server exposes postgres root, redis/state, clickhouse/state"
    exit 0
  fi
  echo "  waiting for merged status files ($i/40)"
  sleep 15
done
echo "STATUS: FAIL - merged status server did not expose all expected status files"
exit 2
RSCRIPT
  } | _mesh_ssh "bash -s" 2>&1 | tee "$LOGDIR/dbha-status-${NODE}.$(ts).log"
  local rc=${PIPESTATUS[1]}
  [ "$rc" = 0 ] || die "merged status verification failed (rc=$rc)"
  DBHA_INITIALIZED="${DBHA_INITIALIZED:-1}"
  _save_cluster
  log "merged status verification passed"
}

verify_isolation_all() {
  _load_cluster
  local n rc
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$VM_ID" ] || die "no VM_ID for $n"
    log "host-isolation check for $n vm=$VM_ID"
    ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/dbha-isolation-${NODE}-${n}.$(ts).log"
set -u
VMID="$VM_ID"
MAC=\$(ps -eo args | grep -F "\$VMID" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2)
[ -n "\$MAC" ] || { echo "ISOLATION: could not find qemu for \$VMID"; exit 3; }
IP=\$(ip neigh show dev dstack-br0 | grep -i "\$MAC" | grep -oE '^10\.0\.[0-9]+\.[0-9]+' | head -1)
[ -n "\$IP" ] || { echo "ISOLATION: no bridge IP for MAC \$MAC yet"; exit 4; }
echo "ISOLATION: vm=\$VMID mac=\$MAC bridge_ip=\$IP"
bad=0
for p in 2379 2380 5432 5433 5434 6379 6380 6381 8123 8008 8009 9000 9009 9181 9234 18123 19001 26379; do
  if timeout 3 bash -c "</dev/tcp/\$IP/\$p" 2>/dev/null; then
    echo "  !! \$IP:\$p REACHABLE from host"; bad=1
  else echo "  \$IP:\$p refused from host"; fi
done
[ \$bad -eq 0 ] && echo "ISOLATION: PASS" || { echo "ISOLATION: FAIL"; exit 5; }
SCRIPT
    rc=${PIPESTATUS[0]}
    [ "$rc" = 0 ] || die "host-isolation check failed for $n (rc=$rc)"
  done
  log "host-isolation invariant holds on all $DBHA_COUNT nodes"
}

update_member() {
  local n="${1:?usage: db-ha-node.sh <name> update <dbN>}"
  _load_cluster; _default_matrix_env; _require_env; _mesh_math_init
  [ -n "${PGHA_PEERS:-}" ] && [ -n "${REDIS_PEERS:-}" ] && [ -n "${CH_PEERS:-}" ] || die "need peer maps in $CSTATE"
  _nload "$n"
  [ -n "$X" ] && [ -n "$VM_ID" ] || die "need X/VM_ID for $n"
  local nh allowed out j mode
  nh=$(NODE_BOOTSTRAP=join _box_run hash "$n" | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" != true ]; then
    send_seq "dbha-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  out=$(NODE_BOOTSTRAP=join _box_run update "$n" "$X" "$VM_ID") || die "update failed for $n"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _nsave "$n"
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "$n update complete: mode=$mode vm=$VM_ID"
}

update_all() {
  local n
  for n in $(_nodes); do
    update_member "$n"
    verify_status
  done
  log "rolled all $DBHA_COUNT db-ha nodes"
}

case "$ACTION" in
  register-all) register_all ;;
  compute-peers) compute_peers ;;
  create-all) create_all ;;
  deploy-all) deploy_all ;;
  prime|prime-all) prime_all ;;
  bind|bind-all) bind_all ;;
  verify|verify-all) verify_all ;;
  verify-status) verify_status ;;
  verify-isolation|verify-isolation-all) verify_isolation_all ;;
  update) update_member "$ARG3"; verify_status ;;
  update-only) update_member "$ARG3" ;;
  update-all) update_all ;;
  all) deploy_all; prime_all; bind_all; verify_all; verify_status; verify_isolation_all ;;
  *) die "unknown action: $ACTION" ;;
esac
