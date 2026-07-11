#!/usr/bin/env bash
# Postgres HA cluster deploy on the self-hosted dstack box: N nodes (default 3) of
# Patroni + etcd + HAProxy, joined to the Matrix node's AttestMesh cluster.
# See docs/specs/pg-ha.md. Order-sensitive, logged, re-entrant via state files; the
# Smithers workflow in deploy/workflows/pg-ha.tsx shells out to these subcommands.
#
# The multi-node twist vs postgres-node.sh: CVM deployment is split register -> compute-peers
# -> create so every node's mesh IP is derived off-chain (AttestFacet.meshIpOf math) BEFORE
# any CVM boots, giving etcd a static bootstrap list with zero off-chain coordination.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: pg-ha-node.sh <name> [deploy-all|prime-all|bind-all|verify-all|verify-ha|verify-failover|verify-isolation-all|verify-agent|switchover <pgN>|cycle-replica <pgN>|resize <pgN>|resize-all|update <pgN>|update-all|all|register-all|compute-peers|create-all|deploy|prime|bind|verify <pgN>]}"
ACTION="${2:-all}"
ARG3="${3:-}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/pg-ha-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
SSH_STATE="${SSH_STATE:-$LOGDIR/ssh-node-ssh-node.state}"
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/pg-ha.env}"
[ -f "$SECRETS_FILE" ] && source "$SECRETS_FILE"
export BOX_VCPU="${BOX_VCPU:-8}" BOX_MEM="${BOX_MEM:-65536}" BOX_DISK="${BOX_DISK:-256}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
TS_SUFFIX="${TS_SUFFIX:-tail39cb2e.ts.net}"
PGHA_COUNT="${PGHA_COUNT:-3}"
BACKUP_ENABLED="${BACKUP_ENABLED:-true}"
BACKUP_PREFIX="${BACKUP_PREFIX:-pg-ha}"
BACKUP_RESTORE="${BACKUP_RESTORE:-}"

CSTATE="$LOGDIR/pg-ha-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000
[ $((PGHA_COUNT % 2)) -eq 0 ] && log "⚠ PGHA_COUNT=$PGHA_COUNT is EVEN — no quorum majority benefit; 3 or 5 recommended"

_nodes() { local i; for i in $(seq 1 "$PGHA_COUNT"); do echo "pg$i"; done; }
_node_state() { echo "$LOGDIR/pg-ha-node-${NODE}-$1.state"; }

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
PGHA_PEERS=${PGHA_PEERS:-}
PGHA_VERIFY_PASSWORD=${PGHA_VERIFY_PASSWORD:-}
BOTPASSWORD=${BOTPASSWORD:-}
PGHA_INITIALIZED=${PGHA_INITIALIZED:-}
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

_bot_user_id() { printf '@pgha-%s:%s' "$1" "$(_matrix_server_name)"; }

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
  local nonce
  nonce=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL")
  run_step "$label" cast send "$@" --nonce "$nonce" --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY" && return 0
  log "↻ $label: refetching nonce + retrying"
  sleep 4
  nonce=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL")
  run_step "${label}-retry" cast send "$@" --nonce "$nonce" --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY"
}

# ── mesh-IP precompute (must match AttestFacet.meshIpOf + sidecar wg/cidr.rs) ────────────

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
  # ip = cidr | ((uint32(keccak256(memberId)) % (hostCount - 2)) + 1); uint32 = low 4 bytes.
  local low_hash low32 host_count offset
  low_hash=$(cast keccak "$1")
  low32=$(( 16#${low_hash: -8} ))
  host_count=$(( 1 << (32 - CIDR_PREFIX) ))
  offset=$(( low32 % (host_count - 2) + 1 ))
  _ipv4_from_u32 $(( CIDR_IP | offset ))
}

# ── box helper (secrets over ssh stdin as a %q payload — never remote argv) ─────────────

_box_run() {
  local mode="$1" node="${2:-pg1}" app_id="${3:-}" vm_id="${4:-}" guser gtok bootstrap
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  bootstrap="${NODE_BOOTSTRAP:-new}"
  # Per-node mention alias so `@pgha-pgN` addresses exactly one bot. Only pg1 also answers to
  # the friendly shared "pg-ha" alias — giving it to every node would make one `pg-ha: …`
  # message activate all N agents in the shared room (N staged switchovers, N LLM runs).
  local aliases="pgha-${node}"
  [ "$node" = pg1 ] && aliases="${aliases},pg-ha"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/pg-ha-node-box.py" "$BOX_HOST:/tmp/pg-ha-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n'              "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n'               "${CVM_RPC_URL:-$RPC_URL}"
    printf 'E_BUNDLER_URL=%q\n'           "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
    printf 'E_GAS_POLICY_ID=%q\n'         "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "${INDEXER_REGISTRY_ADDR:-}"
    printf 'E_GATEWAY_DOMAIN=%q\n'        "$GATEWAY_DOMAIN"
    printf 'E_PGHA_NODE_NAME=%q\n'        "$node"
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
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/pg-ha-node-box.py $mode $app_id $vm_id'"
}

# ── pipeline: register-all -> compute-peers -> create-all ───────────────────────────────

register_all() {
  _load_cluster; _default_matrix_env; _require_env
  _save_cluster
  local n out j
  for n in $(_nodes); do
    _nload "$n"
    if [ -n "$X" ]; then log "· $n already registered: $X"; continue; fi
    log "▶ register DstackApp for $n"
    out=$(_box_run register "$n") || die "box register failed for $n"
    j=$(echo "$out" | grep '"app_id"' | tail -1)
    X=$(echo "$j" | jq -r .app_id)
    H=$(echo "$j" | jq -r .compose_hash)
    [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from register: $out"
    _nsave "$n"
    log "✔ $n app_id=$X compose_hash=$H"
  done
}

compute_peers() {
  _load_cluster; _default_matrix_env; _mesh_math_init
  local used=" " n mid ip attempts out j existing id
  # Our own nodes' member IDs — so a RE-RUN (resume) doesn't treat a node we already
  # registered as an external collision and needlessly register a throwaway app_id.
  declare -A OURS
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$X" ] && OURS["$(_member_id_for_app "$X" | tr 'A-Z' 'a-z')"]=1
  done
  # Existing members' IPs are reserved (collision would break BOTH nodes' meshes). Derive
  # each IP locally from its memberId with the same math (verify_all cross-checks on-chain),
  # so this costs ONE listMembers() call instead of one meshIpOf() RPC per member.
  existing=$(cast call "$CLUSTER" 'listMembers()(bytes32[])' --rpc-url "$RPC_URL" | tr -d '[] ' | tr ',' '\n')
  for id in $existing; do
    [ -n "$id" ] || continue
    [ -n "${OURS[${id,,}]:-}" ] && continue
    used="$used$(_mesh_ip_for_member "$id") "
  done
  log "reserved (non-pgha) member IPs:$used"
  PGHA_PEERS=""
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$X" ] || die "$n has no app_id — run register-all first"
    attempts=0
    while :; do
      mid=$(_member_id_for_app "$X")
      ip=$(_mesh_ip_for_member "$mid")
      case "$used" in
        *" $ip "*)
          attempts=$((attempts + 1))
          [ "$attempts" -le 3 ] || die "$n: 3 mesh-IP collisions in a row — inspect manually"
          log "⚠ $n app $X collides on $ip — registering a replacement app_id ($attempts/3)"
          out=$(_box_run register "$n") || die "replacement register failed"
          j=$(echo "$out" | grep '"app_id"' | tail -1)
          X=$(echo "$j" | jq -r .app_id); H=$(echo "$j" | jq -r .compose_hash)
          ;;
        *)
          break
          ;;
      esac
    done
    used="$used$ip "
    MESH_IP="$ip"
    _nsave "$n"
    PGHA_PEERS="${PGHA_PEERS:+$PGHA_PEERS,}$n=$ip"
    log "✔ $n → memberId=$mid meshIp=$ip"
  done
  _save_cluster
  log "✔ PGHA_PEERS=$PGHA_PEERS (mesh $MESH_CIDR_STR)"
}

create_all() {
  _load_cluster; _default_matrix_env; _require_env; _mesh_math_init
  [ -n "${PGHA_PEERS:-}" ] || die "no PGHA_PEERS — run compute-peers first"
  [ -n "${BOTPASSWORD:-}" ] || die "missing BOTPASSWORD (shared password of the @pgha-pgN bot users; put it in $SECRETS_FILE)"
  PGHA_VERIFY_PASSWORD="${PGHA_VERIFY_PASSWORD:-$(openssl rand -hex 24)}"
  _save_cluster
  local n out j bootstrap
  bootstrap=new
  [ -n "${PGHA_INITIALIZED:-}" ] && bootstrap=join   # scale-out onto a live cluster
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$X" ] || die "$n has no app_id — run register-all first"
    if [ -n "$VM_ID" ]; then log "· $n already has vm=$VM_ID"; continue; fi
    log "▶ CreateVm $n (app $X, bootstrap=$bootstrap)"
    out=$(NODE_BOOTSTRAP="$bootstrap" _box_run create "$n" "$X") || die "box create failed for $n"
    j=$(echo "$out" | grep '"app_id"' | tail -1)
    VM_ID=$(echo "$j" | jq -r .vm_id)
    H=$(echo "$j" | jq -r .compose_hash)
    [ -n "$VM_ID" ] && [ "$VM_ID" != null ] || die "could not parse vm_id from create: $out"
    _nsave "$n"
    log "✔ $n vm=$VM_ID"
  done
}

deploy_all() { register_all; compute_peers; create_all; }

prime_all() {
  _load_cluster; _default_matrix_env
  local n h="" allowed
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$H" ] || die "$n has no compose hash — run register-all first"
    [ -z "$h" ] && h="$H"
    [ "$h" = "$H" ] || die "compose hashes differ between nodes ($h vs $H) — re-run register-all from one tree"
  done
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${h#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "pgha-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${h#0x}"
  fi
  for n in $(_nodes); do
    _nload "$n"
    allowed=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    if [ "$allowed" = true ]; then
      log "· $n app id already allowlisted"
    else
      send_seq "pgha-addApp-${NODE}-${n}" "$CLUSTER" "addAllowedAppId(address)" "$X"
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
    if [ "${c,,}" = "${CLUSTER,,}" ]; then log "· $n already bound"; continue; fi
    log "▶ bind $n X=$X → impl $MEMBER_IMPL (box deployer)"
    ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/pgha-bind-${NODE}-${n}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --rpc-url $BOX_RPC --private-key "\$KEY" 2>&1 | grep -iE "^status|^transactionHash|error|FailedCall" | head -3
SCRIPT
    c=""
    for _ in 1 2 3 4 5 6 7 8; do
      c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
      [ "${c,,}" = "${CLUSTER,,}" ] && break
      sleep 2
    done
    [ "${c,,}" = "${CLUSTER,,}" ] || die "bind did not stick for $n (X.cluster()=$c)"
    log "✔ bound $n"
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
      log "… $n not registered yet ($i/45, memberCount=${count:-?})"
      sleep 20
      id=""
    done
    [ -n "$id" ] || die "$n did not register"
    # The etcd bootstrap ran with the PRECOMPUTED IP — a mismatch here means the off-chain
    # math drifted from AttestFacet.meshIpOf and the mesh addressing is wrong. Hard fail.
    onchain_u32=$(cast call "$CLUSTER" 'meshIpOf(bytes32)(uint32)' "$id" --rpc-url "$RPC_URL" | awk '{print int($1)}')
    onchain_ip=$(_ipv4_from_u32 "$onchain_u32")
    [ "$onchain_ip" = "$MESH_IP" ] || die "$n mesh-IP drift: precomputed $MESH_IP but chain says $onchain_ip"
    log "✔ $n registered: memberId=$id meshIp=$onchain_ip (matches precompute)"
  done
}

# ── verification from the mesh (vantage = ssh-node mesh shell on :1023) ─────────────────

_mesh_ssh() {
  [ -f "$SSH_STATE" ] || die "missing ssh-node state: $SSH_STATE (the mesh shell is the verify vantage)"
  local sx host
  sx=$(grep '^X=' "$SSH_STATE" | cut -d= -f2-)
  host="$(printf '%s' "${sx#0x}" | tr 'A-Z' 'a-z')-1023.${GATEWAY_DOMAIN}"
  ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new \
      -o UserKnownHostsFile="$LOGDIR/pgha-mesh-shell.known_hosts" \
      -o ProxyCommand="openssl s_client -quiet -connect ${host}:443 -servername ${host} 2>/dev/null" \
      "root@$host" "$@"
}

verify_ha() {
  _load_cluster
  [ -n "${PGHA_PEERS:-}" ] && [ -n "${PGHA_VERIFY_PASSWORD:-}" ] || die "need PGHA_PEERS + PGHA_VERIFY_PASSWORD in $CSTATE"
  log "▶ HA verification from the mesh shell (peers: $PGHA_PEERS)"
  {
    printf 'PEERS=%q\nVPW=%q\nEXPECT=%q\n' "$PGHA_PEERS" "$PGHA_VERIFY_PASSWORD" "$PGHA_COUNT"
    cat <<'RSCRIPT'
set -u
export LC_ALL=C   # the Ubuntu psql wrapper is perl; missing locales spam stderr otherwise
need=""
for t in psql jq curl; do command -v "$t" >/dev/null 2>&1 || need=1; done
if [ -n "$need" ]; then
  apt-get update -qq >/dev/null 2>&1
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql-client jq curl >/dev/null 2>&1
fi
for t in psql jq curl; do
  command -v "$t" >/dev/null 2>&1 || { echo "HA: FAIL - $t unavailable on the mesh shell"; exit 9; }
done
declare -A IP; NAMES=()
IFS=',' read -ra PAIRS <<< "$PEERS"
for p in "${PAIRS[@]}"; do n="${p%%=*}"; IP[$n]="${p#*=}"; NAMES+=("$n"); done
FIRST="${IP[${NAMES[0]}]}"

echo "HA: waiting for one leader + $((EXPECT - 1)) streaming replicas"
ok=""
for i in $(seq 1 60); do
  c="$(curl -fsS --max-time 5 "http://$FIRST:8008/cluster" 2>/dev/null || true)"
  leaders=$(jq -r '[.members[]? | select(.role == "leader")] | length' <<<"$c" 2>/dev/null || echo 0)
  streaming=$(jq -r '[.members[]? | select(.state == "streaming")] | length' <<<"$c" 2>/dev/null || echo 0)
  zero_lag=$(jq -r '[.members[]? | select(.role == "replica" and .state == "streaming" and ((.lag // 0) == 0))] | length' <<<"$c" 2>/dev/null || echo 0)
  if [ "${leaders:-0}" = 1 ] \
     && [ "${streaming:-0}" -ge $((EXPECT - 1)) ] \
     && [ "${zero_lag:-0}" -ge $((EXPECT - 1)) ]; then ok=1; break; fi
  echo "  … ($i/60) leaders=${leaders:-?} streaming=${streaming:-?} zero_lag=${zero_lag:-?}"
  sleep 15
done
[ -n "$ok" ] || { echo "HA: FAIL - cluster never converged"; exit 2; }
echo "$c" | jq -c '.members[] | {name, role, state, timeline, lag}'

# Per-node checks poll: a just-rolled CVM takes minutes to come back, and update-all
# runs this gate immediately after StartVm.
for n in "${NAMES[@]}"; do
  ok=""
  for i in $(seq 1 40); do
    if curl -fsS --max-time 5 "http://${IP[$n]}:2379/health" 2>/dev/null | grep -q '"true"' \
       && curl -fsS --max-time 5 "http://${IP[$n]}:8008/cluster" >/dev/null 2>&1; then
      ok=1; break
    fi
    [ $((i % 4)) -eq 0 ] && echo "  … waiting for $n etcd/REST ($i/40)"
    sleep 12
  done
  [ -n "$ok" ] || { echo "HA: FAIL - etcd/Patroni REST unhealthy on $n after 8m"; exit 3; }
done
echo "HA: etcd + Patroni REST healthy on all ${#NAMES[@]} nodes"

for n in "${NAMES[@]}"; do
  r=$(psql "postgresql://meshverify:$VPW@${IP[$n]}:5432/postgres" -tAc 'select pg_is_in_recovery()' 2>/dev/null | tr -d '[:space:]')
  [ "$r" = f ] || { echo "HA: FAIL - $n :5432 did not route to the primary (got: $r)"; exit 5; }
  r=$(psql "postgresql://meshverify:$VPW@${IP[$n]}:5433/postgres" -tAc 'select pg_is_in_recovery()' 2>/dev/null | tr -d '[:space:]')
  [ "$r" = t ] || { echo "HA: FAIL - $n :5433 did not route to a replica (got: $r)"; exit 6; }
done
echo "HA: HAProxy routing correct on every node (5432→primary, 5433→replica)"

stamp="smoke-$(date -u +%s)-$RANDOM"
psql "postgresql://meshverify:$VPW@$FIRST:5432/postgres" -v ON_ERROR_STOP=1 -q \
  -c 'create table if not exists verify.smoke(stamp text primary key, at timestamptz default now())' \
  -c "insert into verify.smoke(stamp) values ('$stamp')" \
  || { echo "HA: FAIL - write via :5432 failed"; exit 7; }
for n in "${NAMES[@]}"; do
  seen=""
  for i in $(seq 1 20); do
    got=$(psql "postgresql://meshverify:$VPW@${IP[$n]}:5433/postgres" -tAc \
      "select count(*) from verify.smoke where stamp = '$stamp'" 2>/dev/null | tr -d '[:space:]')
    [ "$got" = 1 ] && { seen=1; break; }
    sleep 3
  done
  [ -n "$seen" ] || { echo "HA: FAIL - write did not replicate to $n within 60s"; exit 8; }
done
echo "HA: PASS - write on :5432 visible on every node's :5433 replica path"
RSCRIPT
  } | _mesh_ssh "bash -s" 2>&1 | tee "$LOGDIR/pgha-ha-${NODE}.$(ts).log"
  local rc=${PIPESTATUS[1]}
  [ "$rc" = 0 ] || die "HA verification failed (rc=$rc)"
  _load_cluster
  if [ -z "${PGHA_INITIALIZED:-}" ]; then
    PGHA_INITIALIZED=1
    _save_cluster
    log "cluster marked initialized (future create-all runs will join, not bootstrap)"
  fi
  log "✔ HA verification passed"
}

verify_failover() {
  _load_cluster
  [ -n "${PGHA_PEERS:-}" ] && [ -n "${PGHA_VERIFY_PASSWORD:-}" ] || die "need PGHA_PEERS + PGHA_VERIFY_PASSWORD"
  local first_ip leader
  first_ip="${PGHA_PEERS#*=}"; first_ip="${first_ip%%,*}"
  leader=$(_mesh_ssh "curl -fsS --max-time 5 http://${first_ip}:8008/cluster" \
    | jq -r '.members[] | select(.role == "leader") | .name')
  [ -n "$leader" ] && [ "$leader" != null ] || die "no current leader visible via $first_ip"
  _nload "$leader"
  [ -n "$VM_ID" ] || die "no VM_ID recorded for leader $leader"
  log "▶ failover drill: current leader=$leader (vm $VM_ID) — stopping it"
  _box_run stop "$leader" "$VM_ID" >/dev/null || die "StopVm failed"
  {
    printf 'PEERS=%q\nVPW=%q\nOLD=%q\n' "$PGHA_PEERS" "$PGHA_VERIFY_PASSWORD" "$leader"
    cat <<'RSCRIPT'
set -u
declare -A IP; NAMES=()
IFS=',' read -ra PAIRS <<< "$PEERS"
for p in "${PAIRS[@]}"; do n="${p%%=*}"; IP[$n]="${p#*=}"; NAMES+=("$n"); done
new=""
for i in $(seq 1 30); do
  for n in "${NAMES[@]}"; do
    [ "$n" = "$OLD" ] && continue
    c="$(curl -fsS --max-time 4 "http://${IP[$n]}:8008/cluster" 2>/dev/null || true)"
    cand=$(jq -r '.members[]? | select(.role == "leader") | .name' <<<"$c" 2>/dev/null)
    if [ -n "$cand" ] && [ "$cand" != "$OLD" ] && [ "$cand" != null ]; then new="$cand"; break 2; fi
  done
  echo "  … ($i/30) waiting for a new leader"
  sleep 5
done
[ -n "$new" ] || { echo "FAILOVER: FAIL - no new leader within 150s"; exit 2; }
echo "FAILOVER: new leader = $new (was $OLD)"
for n in "${NAMES[@]}"; do
  [ "$n" = "$OLD" ] && continue
  ok=""
  for i in $(seq 1 12); do
    if psql "postgresql://meshverify:$VPW@${IP[$n]}:5432/postgres" -q -v ON_ERROR_STOP=1 \
      -c "insert into verify.smoke(stamp) values ('failover-$(date -u +%s)-$RANDOM')" 2>/dev/null; then
      ok=1; break
    fi
    sleep 5
  done
  [ -n "$ok" ] || { echo "FAILOVER: FAIL - writes via $n :5432 did not recover"; exit 3; }
done
echo "FAILOVER: PASS - writes recovered through the surviving nodes"
RSCRIPT
  } | _mesh_ssh "bash -s" 2>&1 | tee "$LOGDIR/pgha-failover-${NODE}.$(ts).log"
  local rc=${PIPESTATUS[1]}
  log "▶ restarting old leader vm $VM_ID"
  _box_run start "$leader" "$VM_ID" >/dev/null || die "StartVm failed (cluster is running degraded on $((PGHA_COUNT - 1)) nodes!)"
  [ "$rc" = 0 ] || die "failover drill failed (rc=$rc) — old leader restarted"
  # Confirm the old leader rejoins as a replica.
  {
    printf 'PEERS=%q\nOLD=%q\n' "$PGHA_PEERS" "$leader"
    cat <<'RSCRIPT'
set -u
declare -A IP
IFS=',' read -ra PAIRS <<< "$PEERS"
for p in "${PAIRS[@]}"; do IP[${p%%=*}]="${p#*=}"; done
for n in "${!IP[@]}"; do FIRST="${IP[$n]}"; break; done
for i in $(seq 1 60); do
  c="$(curl -fsS --max-time 4 "http://$FIRST:8008/cluster" 2>/dev/null || true)"
  state=$(jq -r --arg n "$OLD" '.members[]? | select(.name == $n) | .state' <<<"$c" 2>/dev/null)
  role=$(jq -r --arg n "$OLD" '.members[]? | select(.name == $n) | .role' <<<"$c" 2>/dev/null)
  if [ "$state" = streaming ] || { [ "$state" = running ] && [ "$role" = replica ]; }; then
    echo "REJOIN: PASS - $OLD is back as $role/$state"
    exit 0
  fi
  echo "  … ($i/60) $OLD state=${state:-?} role=${role:-?}"
  sleep 10
done
echo "REJOIN: FAIL - $OLD did not rejoin within 600s"
exit 4
RSCRIPT
  } | _mesh_ssh "bash -s" 2>&1 | tee -a "$LOGDIR/pgha-failover-${NODE}.$(ts).log"
  rc=${PIPESTATUS[1]}
  [ "$rc" = 0 ] || die "old leader did not rejoin (rc=$rc)"
  log "✔ failover drill passed: promote + write-recovery + rejoin"
}

verify_isolation_all() {
  _load_cluster
  local n rc
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$VM_ID" ] || die "no VM_ID for $n"
    log "▶ host-isolation check for $n vm=$VM_ID"
    ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/pgha-isolation-${NODE}-${n}.$(ts).log"
set -u
VMID="$VM_ID"
MAC=\$(ps -eo args | grep -F "\$VMID" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2)
[ -n "\$MAC" ] || { echo "ISOLATION: could not find qemu for \$VMID"; exit 3; }
IP=\$(ip neigh show dev dstack-br0 | grep -i "\$MAC" | grep -oE '^10\.0\.[0-9]+\.[0-9]+' | head -1)
[ -n "\$IP" ] || { echo "ISOLATION: no bridge IP for MAC \$MAC yet (CVM mid-boot?)"; exit 4; }
echo "ISOLATION: vm=\$VMID mac=\$MAC bridge_ip=\$IP"
bad=0
for p in 2379 2380 5432 5433 5434 8008 8009 9090 9100 18080; do
  if timeout 3 bash -c "</dev/tcp/\$IP/\$p" 2>/dev/null; then
    echo "  !! \$IP:\$p REACHABLE from host — INVARIANT VIOLATION"; bad=1
  else echo "  \$IP:\$p refused from host (good)"; fi
done
[ \$bad -eq 0 ] && echo "ISOLATION: PASS" || { echo "ISOLATION: FAIL"; exit 5; }
SCRIPT
    rc=${PIPESTATUS[0]}
    [ "$rc" = 0 ] || die "host-isolation check failed for $n (rc=$rc)"
  done
  log "✔ host-isolation invariant holds on all $PGHA_COUNT nodes"
}

# ── Matrix agent verification (copied from postgres-node.sh) ─────────────────────────────

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

_matrix_verify_credentials() {
  _load_cluster
  _default_matrix_env
  MATRIX_VERIFY_LOCALPART="${MATRIX_VERIFY_USER:-${INITIAL_ADMIN:-}}"
  case "$MATRIX_VERIFY_LOCALPART" in
    @*:*) MATRIX_VERIFY_LOCALPART="$(printf '%s' "$MATRIX_VERIFY_LOCALPART" | sed -nE 's/^@([^:]+):.*/\1/p')" ;;
  esac
  if [ -z "$MATRIX_VERIFY_LOCALPART" ]; then
    MATRIX_VERIFY_LOCALPART="$(printf '%s' "$MATRIX_ADMIN_MXIDS" | cut -d, -f1 | sed -nE 's/^@([^:]+):.*/\1/p')"
  fi
  MATRIX_VERIFY_PASSWORD_RESOLVED="${MATRIX_VERIFY_PASSWORD:-${INITIAL_ADMIN_PASSWORD:-}}"
  if [ -z "$MATRIX_VERIFY_PASSWORD_RESOLVED" ] && [ -f "$MATRIX_STATE" ]; then
    MATRIX_VERIFY_PASSWORD_RESOLVED="$(grep '^IAPW=' "$MATRIX_STATE" | cut -d= -f2-)"
  fi
  [ -n "$MATRIX_VERIFY_LOCALPART" ] && [ -n "$MATRIX_VERIFY_PASSWORD_RESOLVED" ] || \
    die "need Matrix verifier credentials: set MATRIX_VERIFY_USER/MATRIX_VERIFY_PASSWORD or keep IAPW in $MATRIX_STATE"
}

_matrix_expect_reply() {
  local label="$1" bot="$2" command="$3" expect_re="$4" fqdn
  _matrix_verify_credentials
  fqdn="$(_matrix_fqdn)" || die "could not find live Matrix tailnet FQDN (set MATRIX_TAILNET_FQDN=...)"
  log "▶ Matrix room check: $label via https://$fqdn"
  if ! MATRIX_PROBE_FQDN="$fqdn" \
    MATRIX_PROBE_ROOM_ID="$MATRIX_ROOM_ID" \
    MATRIX_PROBE_USER="$MATRIX_VERIFY_LOCALPART" \
    MATRIX_PROBE_PASSWORD="$MATRIX_VERIFY_PASSWORD_RESOLVED" \
    MATRIX_PROBE_BOT="$bot" \
    MATRIX_PROBE_COMMAND="$command" \
    MATRIX_PROBE_EXPECT_RE="$expect_re" \
    python3 "$HERE/matrix-probe.py"; then
    die "Matrix room check failed: $label"
  fi
  log "✔ Matrix room check passed: $label"
}

verify_agent() {
  _load_cluster; _default_matrix_env
  local bot
  bot="$(_bot_user_id pg1)"
  _matrix_expect_reply "pgha-admin-agent status" "$bot" "$bot !pgha status" "HA cluster"
}

switchover() {
  local candidate="${1:?usage: pg-ha-node.sh <name> switchover <pgN>}" first_ip control_ip topology leader eligible response pair request_ok
  local -a pairs
  _load_cluster
  case " $(_nodes | tr '\n' ' ') " in
    *" $candidate "*) ;;
    *) die "unknown switchover candidate: $candidate" ;;
  esac
  first_ip="${PGHA_PEERS#*=}"; first_ip="${first_ip%%,*}"
  topology=$(_mesh_ssh "curl -fsS --max-time 5 http://${first_ip}:8008/cluster")
  leader=$(jq -r '.members[]? | select(.role == "leader") | .name' <<<"$topology")
  eligible=$(jq -r --arg n "$candidate" '
    .members[]?
    | select(.name == $n and .role == "replica")
    | select(.state == "streaming" or .state == "running")
    | select((.lag // 0) == 0)
    | .name' <<<"$topology")
  [ -n "$leader" ] && [ "$eligible" = "$candidate" ] \
    || die "candidate $candidate is not a zero-lag streaming replica"
  control_ip=""
  IFS=',' read -ra pairs <<<"$PGHA_PEERS"
  for pair in "${pairs[@]}"; do
    [ "${pair%%=*}" = "$candidate" ] && control_ip="${pair#*=}"
  done
  [ -n "$control_ip" ] || die "could not resolve mesh IP for candidate $candidate"

  log "▶ controlled Patroni switchover: leader=$leader candidate=$candidate"
  request_ok=1
  response=$(
    {
      printf 'TOKEN=%q\nCANDIDATE=%q\nCONTROL=%q\n' "$PGHA_VERIFY_PASSWORD" "$candidate" "$control_ip"
      cat <<'RSCRIPT'
payload=$(printf '{"candidate":"%s"}' "$CANDIDATE")
curl -fsS --max-time 30 -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary "$payload" \
  "http://$CONTROL:8010/switchover"
RSCRIPT
    } | _mesh_ssh "bash -s"
  ) || request_ok=0
  if [ "$request_ok" = 1 ]; then
    jq -e --arg candidate "$candidate" '.candidate == $candidate' <<<"$response" >/dev/null \
      || die "Patroni control response did not confirm candidate $candidate: $response"
    log "Patroni switchover accepted by the mesh control plane"
  else
    # Patroni may finish a safe demotion/promotion after the controller's HTTP
    # deadline. Never retry the mutation blindly: prove the requested topology
    # below and accept only the exact requested candidate as leader.
    log "Patroni control request was not acknowledged; proving topology before continuing"
  fi

  for i in $(seq 1 30); do
    leader=$(_mesh_ssh "curl -fsS --max-time 5 http://${first_ip}:8008/cluster" 2>/dev/null \
      | jq -r '.members[]? | select(.role == "leader") | .name' || true)
    if [ "$leader" = "$candidate" ]; then
      verify_ha
      log "✔ controlled switchover complete: leader=$candidate"
      return 0
    fi
    log "… waiting for leader=$candidate (current=${leader:-none}, $i/30)"
    sleep 2
  done
  die "Patroni did not make $candidate leader within 60s"
}

cycle_replica() {
  local target="${1:?usage: pg-ha-node.sh <name> cycle-replica <pgN>}" first_ip leader state role
  _load_cluster
  first_ip="${PGHA_PEERS#*=}"; first_ip="${first_ip%%,*}"
  leader=$(_mesh_ssh "curl -fsS --max-time 5 http://${first_ip}:8008/cluster" \
    | jq -r '.members[]? | select(.role == "leader") | .name')
  [ -n "$leader" ] && [ "$leader" != null ] || die "no current Patroni leader"
  [ "$target" != "$leader" ] || die "refusing to cycle leader $target; choose a replica"
  _nload "$target"
  [ -n "$VM_ID" ] || die "no VM_ID recorded for $target"

  log "▶ stopping replica $target (vm=$VM_ID) for the client ingress-node gate"
  _box_run stop "$target" "$VM_ID" >/dev/null || die "StopVm failed for replica $target"
  sleep 15
  log "▶ starting replica $target (vm=$VM_ID)"
  _box_run start "$target" "$VM_ID" >/dev/null || die "StartVm failed for replica $target"

  for i in $(seq 1 60); do
    read -r role state < <(_mesh_ssh "curl -fsS --max-time 5 http://${first_ip}:8008/cluster" 2>/dev/null \
      | jq -r --arg n "$target" '.members[]? | select(.name == $n) | [.role,.state] | @tsv' || true)
    if [ "$role" = replica ] && { [ "$state" = streaming ] || [ "$state" = running ]; }; then
      verify_ha
      log "✔ replica cycle complete: $target is $role/$state"
      return 0
    fi
    log "… waiting for $target to rejoin (role=${role:-?} state=${state:-?}, $i/60)"
    sleep 5
  done
  die "$target did not rejoin as a streaming replica within 300s"
}

# ── day-2 ────────────────────────────────────────────────────────────────────────────────

update_member() {
  local n="${1:?usage: pg-ha-node.sh <name> update <pgN>}"
  _load_cluster; _default_matrix_env; _require_env; _mesh_math_init
  [ -n "${PGHA_PEERS:-}" ] && [ -n "${BOTPASSWORD:-}" ] || die "need PGHA_PEERS/BOTPASSWORD in $CSTATE / $SECRETS_FILE"
  _nload "$n"
  [ -n "$X" ] && [ -n "$VM_ID" ] || die "need X/VM_ID for $n"
  local nh allowed out j
  nh=$(NODE_BOOTSTRAP=join _box_run hash "$n" | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "pgha-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  settle_compose_hash_for_kms "$CLUSTER" "$nh"
  # BOOTSTRAP=join is safe on every roll: a preserved data dir short-circuits it, and a
  # fresh disk (BOX_FRESH_DISK=1) must re-join the established quorum anyway.
  out=$(NODE_BOOTSTRAP=join _box_run update "$n" "$X" "$VM_ID") || die "in-place update failed for $n"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _nsave "$n"
  local mode
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  if [ "$mode" = createvm ]; then
    log "✔ $n fresh-disk CreateVm: vm=$VM_ID — membership + mesh IP kept, local data reset (etcd/Patroni will re-join)"
  else
    log "✔ $n in-place UpgradeApp: vm=$VM_ID — disk/data preserved"
  fi
}

update_all() {
  local first_ip topology leader candidate
  local -a replicas
  _load_cluster
  verify_ha
  first_ip="${PGHA_PEERS#*=}"; first_ip="${first_ip%%,*}"
  topology=$(_mesh_ssh "curl -fsS --max-time 5 http://${first_ip}:8008/cluster")
  leader=$(jq -r '.members[] | select(.role == "leader") | .name' <<<"$topology")
  mapfile -t replicas < <(jq -r '.members[] | select(.role == "replica") | .name' <<<"$topology" | sort)
  [ -n "$leader" ] && [ "${#replicas[@]}" -eq $((PGHA_COUNT - 1)) ] \
    || die "unexpected Patroni topology before update"

  for candidate in "${replicas[@]}"; do
    update_member "$candidate"
    verify_ha
    if [ "${SKIP_CLIENT_PROBES:-0}" != 1 ]; then
      "$HERE/pg-ha-client-failover.sh" probe-once
    fi
  done
  candidate="${replicas[0]}"
  switchover "$candidate"
  update_member "$leader"
  verify_ha
  if [ "${SKIP_CLIENT_PROBES:-0}" != 1 ]; then
    "$HERE/pg-ha-client-failover.sh" probe-once
  fi
  log "✔ rolled all $PGHA_COUNT nodes"
}

host_storage_guard() {
  local used
  used=$(ssh_box "df -P /srv/data/dstack | awk 'NR==2 {gsub(/%/,\"\",\$5); print \$5}'")
  [ -n "$used" ] || die "could not read dstack host filesystem usage"
  [ "$used" -lt 85 ] || die "host storage is ${used}% used; refusing resize at the 15% free-space guard"
  [ "$used" -lt 80 ] || log "⚠ host storage is ${used}% used (20% free-space alert threshold crossed)"
}

vm_info_json() {
  local node="$1" vm_id="$2"
  _box_run info "$node" "$vm_id" | grep -E '^\{.*"vm_id"' | tail -1
}

resize_member() {
  local target="${1:?usage: pg-ha-node.sh <name> resize <pgN>}" first_ip role before status out after evidence
  local before_identity after_identity
  _load_cluster
  verify_ha
  first_ip="${PGHA_PEERS#*=}"; first_ip="${first_ip%%,*}"
  role=$(_mesh_ssh "curl -fsS --max-time 5 http://${first_ip}:8008/cluster" \
    | jq -r --arg n "$target" '.members[]? | select(.name == $n) | .role')
  [ "$role" = replica ] || die "refusing to resize $target while role=${role:-unknown}; switch it to a replica first"
  _nload "$target"
  [ -n "$VM_ID" ] || die "no VM_ID recorded for $target"
  host_storage_guard

  before="$(vm_info_json "$target" "$VM_ID")"
  [ -n "$before" ] || die "could not read VMM resources for $target"
  before_identity=$(jq -c '[.vm_id,.app_id,.compose_hash,.image]' <<<"$before")
  if [ "$(jq -r .vcpu <<<"$before")" = "$BOX_VCPU" ] \
    && [ "$(jq -r .memory <<<"$before")" = "$BOX_MEM" ] \
    && [ "$(jq -r .disk_size <<<"$before")" = "$BOX_DISK" ]; then
    log "· $target already at ${BOX_VCPU} vCPU / ${BOX_MEM} MB / ${BOX_DISK} GB"
    return 0
  fi

  log "▶ stopping replica $target for resource-only resize to ${BOX_VCPU} vCPU / ${BOX_MEM} MB / ${BOX_DISK} GB"
  _box_run stop "$target" "$VM_ID" >/dev/null || die "StopVm failed for $target"
  status=""
  for i in $(seq 1 60); do
    status=$(vm_info_json "$target" "$VM_ID" | jq -r '.status // ""')
    if [ "$status" = stopped ] || [ "$status" = exited ]; then break; fi
    sleep 2
  done
  if [ "$status" != stopped ] && [ "$status" != exited ]; then
    _box_run start "$target" "$VM_ID" >/dev/null 2>&1 || true
    die "$target did not stop cleanly; resize was not attempted"
  fi

  out=$(_box_run resize "$target" "$VM_ID") || {
    _box_run start "$target" "$VM_ID" >/dev/null 2>&1 || true
    die "resource-only ResizeVm failed for $target"
  }
  echo "$out"
  _box_run start "$target" "$VM_ID" >/dev/null || die "StartVm failed after resizing $target"

  after=""
  for i in $(seq 1 120); do
    after="$(vm_info_json "$target" "$VM_ID")"
    status=$(jq -r '.status // ""' <<<"$after")
    if [ -n "$(jq -r '.boot_error // empty' <<<"$after")" ]; then
      die "$target boot failed after resize: $(jq -r .boot_error <<<"$after")"
    fi
    evidence=$(jq -r '.disk_boot_evidence[]?' <<<"$after")
    if [ "$status" = running ] \
      && [ "$(jq -r .vcpu <<<"$after")" = "$BOX_VCPU" ] \
      && [ "$(jq -r .memory <<<"$after")" = "$BOX_MEM" ] \
      && [ "$(jq -r .disk_size <<<"$after")" = "$BOX_DISK" ] \
      && grep -Eq '[[:space:]]2[0-9]{2}(\.[0-9]+)?G[[:space:]]' <<<"$evidence"; then
      break
    fi
    [ $((i % 6)) -ne 0 ] || log "… waiting for $target boot/filesystem expansion ($i/120)"
    sleep 5
  done
  evidence=$(jq -r '.disk_boot_evidence[]?' <<<"$after")
  [ "$status" = running ] || die "$target did not return to running after resize"
  [ "$(jq -r .vcpu <<<"$after")" = "$BOX_VCPU" ] || die "$target vCPU readback mismatch"
  [ "$(jq -r .memory <<<"$after")" = "$BOX_MEM" ] || die "$target memory readback mismatch"
  [ "$(jq -r .disk_size <<<"$after")" = "$BOX_DISK" ] || die "$target disk readback mismatch"
  after_identity=$(jq -c '[.vm_id,.app_id,.compose_hash,.image]' <<<"$after")
  [ "$after_identity" = "$before_identity" ] \
    || die "$target identity changed across resource-only resize: before=$before_identity after=$after_identity"
  grep -Eq '[[:space:]]2[0-9]{2}(\.[0-9]+)?G[[:space:]]' <<<"$evidence" \
    || die "$target guest filesystem did not report expanded 2xx GB capacity"

  verify_ha
  if [ "${SKIP_CLIENT_PROBES:-0}" != 1 ]; then
    "$HERE/pg-ha-client-failover.sh" probe-once
  fi
  host_storage_guard
  log "✔ $target resized and verified at ${BOX_VCPU} vCPU / ${BOX_MEM} MB / ${BOX_DISK} GB"
}

resize_all() {
  local first_ip topology leader candidate
  local -a replicas
  _load_cluster
  verify_ha
  first_ip="${PGHA_PEERS#*=}"; first_ip="${first_ip%%,*}"
  topology=$(_mesh_ssh "curl -fsS --max-time 5 http://${first_ip}:8008/cluster")
  leader=$(jq -r '.members[] | select(.role == "leader") | .name' <<<"$topology")
  mapfile -t replicas < <(jq -r '.members[] | select(.role == "replica") | .name' <<<"$topology" | sort)
  [ -n "$leader" ] && [ "${#replicas[@]}" -eq $((PGHA_COUNT - 1)) ] \
    || die "unexpected Patroni topology before resize"

  for candidate in "${replicas[@]}"; do
    resize_member "$candidate"
  done
  candidate="${replicas[0]}"
  switchover "$candidate"
  resize_member "$leader"
  verify_ha
  if [ "${SKIP_CLIENT_PROBES:-0}" != 1 ]; then
    "$HERE/pg-ha-client-failover.sh" probe-once
  fi
  log "✔ pg-ha fleet resize complete; leader=$candidate targets=${BOX_VCPU}/${BOX_MEM}/${BOX_DISK}"
}

case "$ACTION" in
  register-all) register_all ;;
  compute-peers) compute_peers ;;
  create-all) create_all ;;
  deploy-all) deploy_all ;;
  prime-all) prime_all ;;
  bind-all) bind_all ;;
  verify-all) verify_all ;;
  verify-ha) verify_ha ;;
  verify-failover) verify_failover ;;
  verify-isolation-all) verify_isolation_all ;;
  verify-agent) verify_agent ;;
  switchover) switchover "$ARG3" ;;
  cycle-replica) cycle_replica "$ARG3" ;;
  resize) resize_member "$ARG3" ;;
  resize-all) resize_all ;;
  update) update_member "$ARG3"; verify_ha ;;
  update-only) update_member "$ARG3" ;;   # diagnostic roll without the verify-ha gate
  update-all) update_all ;;
  all) deploy_all; prime_all; bind_all; verify_all; verify_ha; verify_isolation_all; verify_agent ;;
  *) die "unknown action: $ACTION" ;;
esac
