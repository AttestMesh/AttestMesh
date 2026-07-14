#!/usr/bin/env bash
# Redis HA cluster deploy on the self-hosted dstack box: N nodes (default 3) of
# Redis + Sentinel + HAProxy, joined to the Matrix node's AttestMesh cluster (C3).
# Structural copy of deploy/pg-ha-node.sh (same 3-phase chain split); order-sensitive,
# logged, re-entrant via state files.
#
# CVM deployment is split register -> compute-peers -> create so every node's mesh IP is
# derived off-chain (AttestFacet.meshIpOf math) BEFORE any CVM boots, giving redis/sentinel
# a static REDIS_PEERS list with zero off-chain coordination.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: redis-ha-node.sh <name> [register-all|compute-peers|create-all|prime|bind|verify|verify-ha|verify-failover|verify-isolation|update <rN>|update-all|all]}"
ACTION="${2:-all}"
ARG3="${3:-}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/redis-ha-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
SSH_STATE="${SSH_STATE:-$LOGDIR/ssh-node-ssh-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
REDISHA_COUNT="${REDISHA_COUNT:-3}"

# Redis is light; small CVMs. Mesh-only: gateway OFF, bridge, app-bound disk key
# (no_instance_id) so redisdata survives fresh-disk rolls of the same app.
export BOX_VCPU="${BOX_VCPU:-2}" BOX_MEM="${BOX_MEM:-2048}" BOX_DISK="${BOX_DISK:-20}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
export BOX_NO_INSTANCE_ID="${BOX_NO_INSTANCE_ID:-false}"

CSTATE="$LOGDIR/redis-ha-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000
[ $((REDISHA_COUNT % 2)) -eq 0 ] && log "⚠ REDISHA_COUNT=$REDISHA_COUNT is EVEN — sentinel quorum wants 3 or 5"

_nodes() { local i; for i in $(seq 1 "$REDISHA_COUNT"); do echo "r$i"; done; }
_node_state() { echo "$LOGDIR/redis-ha-node-${NODE}-$1.state"; }

_save_cluster() {
  umask 077
  cat > "$CSTATE" <<EOF
CLUSTER=${CLUSTER:-}
MEMBER_IMPL=${MEMBER_IMPL:-}
CIDR_IP=${CIDR_IP:-}
CIDR_PREFIX=${CIDR_PREFIX:-}
MESH_CIDR_STR=${MESH_CIDR_STR:-}
REDIS_PEERS=${REDIS_PEERS:-}
REDISHA_VERIFY_PASSWORD=${REDISHA_VERIFY_PASSWORD:-}
REDISHA_INITIALIZED=${REDISHA_INITIALIZED:-}
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

# redis-ha joins the existing Matrix cluster (C3) by default.
_default_cluster_env() {
  if [ -z "${CLUSTER:-}" ] || [ -z "${MEMBER_IMPL:-}" ]; then
    [ -f "$MATRIX_STATE" ] || die "missing cluster state: $MATRIX_STATE (set CLUSTER + MEMBER_IMPL to override)"
    CLUSTER="${CLUSTER:-$(grep '^CLUSTER=' "$MATRIX_STATE" | cut -d= -f2-)}"
    MEMBER_IMPL="${MEMBER_IMPL:-$(grep '^MEMBER_IMPL=' "$MATRIX_STATE" | cut -d= -f2-)}"
  fi
  [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "could not resolve CLUSTER/MEMBER_IMPL"
}

_require_env() {
  local missing="" indexer
  BUNDLER_URL="${BUNDLER_URL:-$RPC_URL}"
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  for v in BUNDLER_URL INDEXER_REGISTRY_ADDR; do
    [ -n "${!v:-}" ] && [ "${!v:-}" != null ] || missing="$missing $v"
  done
  [ -z "$missing" ] || die "missing required env:$missing"
}

send_seq() {
  local label="$1"; shift
  send_with_nonce_retry "$label" "$@"
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
  local mode="$1" node="${2:-r1}" app_id="${3:-}" vm_id="${4:-}" guser gtok bootstrap
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  bootstrap="${NODE_BOOTSTRAP:-new}"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/redis-ha-node-box.py" "$BOX_HOST:/tmp/redis-ha-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n'              "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n'               "${CVM_RPC_URL:-$RPC_URL}"
    printf 'E_BUNDLER_URL=%q\n'           "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
    printf 'E_GAS_POLICY_ID=%q\n'         "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "${INDEXER_REGISTRY_ADDR:-}"
    printf 'E_GATEWAY_DOMAIN=%q\n'        "$GATEWAY_DOMAIN"
    printf 'E_REDISHA_NODE_NAME=%q\n'     "$node"
    printf 'E_REDIS_PEERS=%q\n'           "${REDIS_PEERS:-}"
    printf 'E_REDISHA_BOOTSTRAP=%q\n'     "$bootstrap"
    printf 'E_REDISHA_MESH_CIDR=%q\n'     "${MESH_CIDR_STR:-}"
    printf 'E_REDISHA_VERIFY_PASSWORD=%q\n' "${REDISHA_VERIFY_PASSWORD:-}"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "sudo BOX_APP_NAME='${NODE}' BOX_NAME='${NODE}-${node}' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_NO_INSTANCE_ID='$BOX_NO_INSTANCE_ID' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/redis-ha-node-box.py $mode $app_id $vm_id'"
}

# ── pipeline: register-all -> compute-peers -> create-all ───────────────────────────────

register_all() {
  _load_cluster; _default_cluster_env; _require_env
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
  _load_cluster; _default_cluster_env; _mesh_math_init
  local used=" " n mid ip attempts out j existing id
  # Our own nodes' member IDs — so a RE-RUN (resume) doesn't treat a node we already
  # registered as an external collision and needlessly register a throwaway app_id.
  declare -A OURS
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$X" ] && OURS["$(_member_id_for_app "$X" | tr 'A-Z' 'a-z')"]=1
  done
  # Existing members' IPs are reserved (collision would break BOTH nodes' meshes). Derive
  # each IP locally from its memberId with the same math (verify cross-checks on-chain),
  # so this costs ONE listMembers() call instead of one meshIpOf() RPC per member.
  existing=$(cast call "$CLUSTER" 'listMembers()(bytes32[])' --rpc-url "$RPC_URL" | tr -d '[] ' | tr ',' '\n')
  for id in $existing; do
    [ -n "$id" ] || continue
    [ -n "${OURS[${id,,}]:-}" ] && continue
    used="$used$(_mesh_ip_for_member "$id") "
  done
  log "reserved (non-redisha) member IPs:$used"
  REDIS_PEERS=""
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
    REDIS_PEERS="${REDIS_PEERS:+$REDIS_PEERS,}$n=$ip"
    log "✔ $n → memberId=$mid meshIp=$ip"
  done
  _save_cluster
  log "✔ REDIS_PEERS=$REDIS_PEERS (mesh $MESH_CIDR_STR)"
}

create_all() {
  _load_cluster; _default_cluster_env; _require_env; _mesh_math_init
  [ -n "${REDIS_PEERS:-}" ] || die "no REDIS_PEERS — run compute-peers first"
  REDISHA_VERIFY_PASSWORD="${REDISHA_VERIFY_PASSWORD:-$(openssl rand -hex 24)}"
  _save_cluster
  local n out j bootstrap
  bootstrap=new
  [ -n "${REDISHA_INITIALIZED:-}" ] && bootstrap=join   # scale-out onto a live cluster
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
  _load_cluster; _default_cluster_env
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
    send_seq "redisha-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${h#0x}"
  fi
  for n in $(_nodes); do
    _nload "$n"
    allowed=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    if [ "$allowed" = true ]; then
      log "· $n app id already allowlisted"
    else
      send_seq "redisha-addApp-${NODE}-${n}" "$CLUSTER" "addAllowedAppId(address)" "$X"
    fi
  done
}

bind_all() {
  _load_cluster; _default_cluster_env
  [ -n "${MEMBER_IMPL:-}" ] || die "need MEMBER_IMPL"
  local n reinit c
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$X" ] || die "$n has no app_id"
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ "${c,,}" = "${CLUSTER,,}" ]; then log "· $n already bound"; continue; fi
    log "▶ bind $n X=$X → impl $MEMBER_IMPL (box deployer)"
    ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/redisha-bind-${NODE}-${n}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --async --rpc-url $BOX_RPC --private-key "\$KEY"
SCRIPT
    confirm_latest_transaction "redisha-bind-${NODE}-${n}" "$RPC_URL" "$LOGDIR/redisha-bind-${NODE}-${n}.*.log" || die "bind transaction not confirmed for $n"
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
  _load_cluster; _default_cluster_env; _mesh_math_init
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
    # The redis/sentinel bootstrap ran with the PRECOMPUTED IP — a mismatch here means the
    # off-chain math drifted from AttestFacet.meshIpOf and the mesh addressing is wrong.
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
      -o UserKnownHostsFile="$LOGDIR/redisha-mesh-shell.known_hosts" \
      -o ProxyCommand="openssl s_client -quiet -connect ${host}:443 -servername ${host} 2>/dev/null" \
      "root@$host" "$@"
}

verify_ha() {
  _load_cluster
  [ -n "${REDIS_PEERS:-}" ] && [ -n "${REDISHA_VERIFY_PASSWORD:-}" ] || die "need REDIS_PEERS + REDISHA_VERIFY_PASSWORD in $CSTATE"
  log "▶ HA verification from the mesh shell (peers: $REDIS_PEERS)"
  {
    printf 'PEERS=%q\nVPW=%q\nEXPECT=%q\n' "$REDIS_PEERS" "$REDISHA_VERIFY_PASSWORD" "$REDISHA_COUNT"
    cat <<'RSCRIPT'
set -u
if ! command -v redis-cli >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq redis-tools >/dev/null 2>&1
fi
command -v redis-cli >/dev/null 2>&1 || { echo "HA: FAIL - redis-cli unavailable on the mesh shell"; exit 9; }
rcli() { redis-cli --no-auth-warning -u "redis://meshverify:$VPW@$1" "${@:2}"; }
declare -A IP; NAMES=()
IFS=',' read -ra PAIRS <<< "$PEERS"
for p in "${PAIRS[@]}"; do n="${p%%=*}"; IP[$n]="${p#*=}"; NAMES+=("$n"); done
FIRST="${IP[${NAMES[0]}]}"

echo "HA: waiting for one master + $((EXPECT - 1)) connected replicas"
ok=""
for i in $(seq 1 60); do
  info="$(rcli "$FIRST:6379" INFO replication 2>/dev/null | tr -d '\r')"
  role=$(sed -n 's/^role://p' <<<"$info")
  slaves=$(sed -n 's/^connected_slaves://p' <<<"$info")
  if [ "$role" = master ] && [ "${slaves:-0}" -ge $((EXPECT - 1)) ]; then ok=1; break; fi
  echo "  … ($i/60) role=${role:-?} connected_slaves=${slaves:-?}"
  sleep 15
done
[ -n "$ok" ] || { echo "HA: FAIL - cluster never converged"; exit 2; }
echo "$info" | grep -E '^(role|connected_slaves|slave[0-9]+):'

# Sentinel view: every node's sentinel must see the master with quorum-worthy peers.
for n in "${NAMES[@]}"; do
  m="$(redis-cli --no-auth-warning -a "$VPW" -h "${IP[$n]}" -p 26379 SENTINEL master redisha 2>/dev/null | tr -d '\r')"
  echo "$m" | grep -q '^master' || { echo "HA: FAIL - sentinel on $n does not report the master"; exit 3; }
  nsent=$(redis-cli --no-auth-warning -a "$VPW" -h "${IP[$n]}" -p 26379 SENTINEL master redisha 2>/dev/null \
    | tr -d '\r' | grep -A1 '^num-other-sentinels$' | tail -1)
  echo "  $n sentinel OK (num-other-sentinels=${nsent:-?})"
done
echo "HA: sentinels healthy on all ${#NAMES[@]} nodes"

# HAProxy routing on every node: :6379 must be role:master, :6381 role:slave.
for n in "${NAMES[@]}"; do
  r=$(rcli "${IP[$n]}:6379" INFO replication 2>/dev/null | tr -d '\r' | sed -n 's/^role://p')
  [ "$r" = master ] || { echo "HA: FAIL - $n :6379 did not route to the master (got: $r)"; exit 5; }
  r=$(rcli "${IP[$n]}:6381" INFO replication 2>/dev/null | tr -d '\r' | sed -n 's/^role://p')
  [ "$r" = slave ] || { echo "HA: FAIL - $n :6381 did not route to a replica (got: $r)"; exit 6; }
done
echo "HA: HAProxy routing correct on every node (6379→master, 6381→replica)"

stamp="smoke-$(date -u +%s)-$RANDOM"
rcli "$FIRST:6379" SET "verify:smoke" "$stamp" >/dev/null || { echo "HA: FAIL - write via :6379 failed"; exit 7; }
for n in "${NAMES[@]}"; do
  seen=""
  for i in $(seq 1 20); do
    got=$(rcli "${IP[$n]}:6381" GET "verify:smoke" 2>/dev/null | tr -d '\r')
    [ "$got" = "$stamp" ] && { seen=1; break; }
    sleep 3
  done
  [ -n "$seen" ] || { echo "HA: FAIL - write did not replicate to $n within 60s"; exit 8; }
done
echo "HA: PASS - write on :6379 visible on every node's :6381 replica path"
RSCRIPT
  } | _mesh_ssh "bash -s" 2>&1 | tee "$LOGDIR/redisha-ha-${NODE}.$(ts).log"
  local rc=${PIPESTATUS[1]}
  [ "$rc" = 0 ] || die "HA verification failed (rc=$rc)"
  _load_cluster
  if [ -z "${REDISHA_INITIALIZED:-}" ]; then
    REDISHA_INITIALIZED=1
    _save_cluster
    log "cluster marked initialized (future create-all runs will join, not bootstrap)"
  fi
  log "✔ HA verification passed"
}

verify_failover() {
  _load_cluster
  [ -n "${REDIS_PEERS:-}" ] && [ -n "${REDISHA_VERIFY_PASSWORD:-}" ] || die "need REDIS_PEERS + REDISHA_VERIFY_PASSWORD"
  local n master="" master_ip mip
  # Find which node currently masters (probe each peer's redis :6380 role via :6379 HAProxy
  # is identical everywhere, so ask sentinel for the master ADDRESS and map it to a name).
  local first_ip
  first_ip="${REDIS_PEERS#*=}"; first_ip="${first_ip%%,*}"
  master_ip=$(_mesh_ssh "redis-cli --no-auth-warning -a '$REDISHA_VERIFY_PASSWORD' -h $first_ip -p 26379 SENTINEL get-master-addr-by-name redisha 2>/dev/null | head -1" | tr -d '\r')
  [ -n "$master_ip" ] || die "sentinel did not report a master address via $first_ip"
  local pair
  IFS=',' read -ra _PAIRS <<< "$REDIS_PEERS"
  for pair in "${_PAIRS[@]}"; do
    mip="${pair#*=}"
    [ "$mip" = "$master_ip" ] && master="${pair%%=*}"
  done
  [ -n "$master" ] || die "master IP $master_ip is not one of our peers ($REDIS_PEERS)"
  _nload "$master"
  [ -n "$VM_ID" ] || die "no VM_ID recorded for master $master"
  log "▶ failover drill: current master=$master ($master_ip, vm $VM_ID) — stopping it"
  _box_run stop "$master" "" "$VM_ID" >/dev/null || die "StopVm failed"
  {
    printf 'PEERS=%q\nVPW=%q\nOLD=%q\nOLDIP=%q\n' "$REDIS_PEERS" "$REDISHA_VERIFY_PASSWORD" "$master" "$master_ip"
    cat <<'RSCRIPT'
set -u
rcli() { redis-cli --no-auth-warning -u "redis://meshverify:$VPW@$1" "${@:2}"; }
declare -A IP; NAMES=()
IFS=',' read -ra PAIRS <<< "$PEERS"
for p in "${PAIRS[@]}"; do n="${p%%=*}"; IP[$n]="${p#*=}"; NAMES+=("$n"); done
new=""
for i in $(seq 1 30); do
  for n in "${NAMES[@]}"; do
    [ "$n" = "$OLD" ] && continue
    cand=$(redis-cli --no-auth-warning -a "$VPW" -h "${IP[$n]}" -p 26379 SENTINEL get-master-addr-by-name redisha 2>/dev/null | head -1 | tr -d '\r')
    if [ -n "$cand" ] && [ "$cand" != "$OLDIP" ]; then new="$cand"; break 2; fi
  done
  echo "  … ($i/30) waiting for sentinel to promote a new master"
  sleep 5
done
[ -n "$new" ] || { echo "FAILOVER: FAIL - no new master within 150s"; exit 2; }
echo "FAILOVER: new master = $new (was $OLDIP)"
for n in "${NAMES[@]}"; do
  [ "$n" = "$OLD" ] && continue
  ok=""
  for i in $(seq 1 12); do
    if rcli "${IP[$n]}:6379" SET "verify:failover" "failover-$(date -u +%s)-$RANDOM" >/dev/null 2>&1; then
      ok=1; break
    fi
    sleep 5
  done
  [ -n "$ok" ] || { echo "FAILOVER: FAIL - writes via $n :6379 did not recover"; exit 3; }
done
echo "FAILOVER: PASS - writes recovered through the surviving nodes"
RSCRIPT
  } | _mesh_ssh "bash -s" 2>&1 | tee "$LOGDIR/redisha-failover-${NODE}.$(ts).log"
  local rc=${PIPESTATUS[1]}
  log "▶ restarting old master vm $VM_ID"
  _box_run start "$master" "" "$VM_ID" >/dev/null || die "StartVm failed (cluster is running degraded on $((REDISHA_COUNT - 1)) nodes!)"
  [ "$rc" = 0 ] || die "failover drill failed (rc=$rc) — old master restarted"
  # Confirm the old master rejoins as a replica.
  {
    printf 'VPW=%q\nOLDIP=%q\n' "$REDISHA_VERIFY_PASSWORD" "$master_ip"
    cat <<'RSCRIPT'
set -u
for i in $(seq 1 60); do
  role=$(redis-cli --no-auth-warning -u "redis://meshverify:$VPW@$OLDIP:6380" INFO replication 2>/dev/null \
    | tr -d '\r' | sed -n 's/^role://p')
  link=$(redis-cli --no-auth-warning -u "redis://meshverify:$VPW@$OLDIP:6380" INFO replication 2>/dev/null \
    | tr -d '\r' | sed -n 's/^master_link_status://p')
  if [ "$role" = slave ] && [ "$link" = up ]; then
    echo "REJOIN: PASS - old master is back as replica (link up)"
    exit 0
  fi
  echo "  … ($i/60) old master role=${role:-?} link=${link:-?}"
  sleep 10
done
echo "REJOIN: FAIL - old master did not rejoin within 600s"
exit 4
RSCRIPT
  } | _mesh_ssh "bash -s" 2>&1 | tee -a "$LOGDIR/redisha-failover-${NODE}.$(ts).log"
  rc=${PIPESTATUS[1]}
  [ "$rc" = 0 ] || die "old master did not rejoin (rc=$rc)"
  log "✔ failover drill passed: promote + write-recovery + rejoin"
}

verify_isolation_all() {
  _load_cluster
  local n rc
  for n in $(_nodes); do
    _nload "$n"
    [ -n "$VM_ID" ] || die "no VM_ID for $n"
    log "▶ host-isolation check for $n vm=$VM_ID"
    ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/redisha-isolation-${NODE}-${n}.$(ts).log"
set -u
VMID="$VM_ID"
MAC=\$(ps -eo args | grep -F "\$VMID" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2)
[ -n "\$MAC" ] || { echo "ISOLATION: could not find qemu for \$VMID"; exit 3; }
IP=\$(ip neigh show dev dstack-br0 | grep -i "\$MAC" | grep -oE '^10\.0\.[0-9]+\.[0-9]+' | head -1)
[ -n "\$IP" ] || { echo "ISOLATION: no bridge IP for MAC \$MAC yet (CVM mid-boot?)"; exit 4; }
echo "ISOLATION: vm=\$VMID mac=\$MAC bridge_ip=\$IP"
bad=0
for p in 6379 6380 6381 26379 8009; do
  if timeout 3 bash -c "</dev/tcp/\$IP/\$p" 2>/dev/null; then
    echo "  !! \$IP:\$p REACHABLE from host — INVARIANT VIOLATION"; bad=1
  else echo "  \$IP:\$p refused from host (good)"; fi
done
[ \$bad -eq 0 ] && echo "ISOLATION: PASS" || { echo "ISOLATION: FAIL"; exit 5; }
SCRIPT
    rc=${PIPESTATUS[0]}
    [ "$rc" = 0 ] || die "host-isolation check failed for $n (rc=$rc)"
  done
  log "✔ host-isolation invariant holds on all $REDISHA_COUNT nodes"
}

# ── day-2 ────────────────────────────────────────────────────────────────────────────────

update_member() {
  local n="${1:?usage: redis-ha-node.sh <name> update <rN>}"
  _load_cluster; _default_cluster_env; _require_env; _mesh_math_init
  [ -n "${REDIS_PEERS:-}" ] || die "need REDIS_PEERS in $CSTATE"
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
    send_seq "redisha-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  # BOOTSTRAP=join is safe on every roll: preserved data short-circuits it, and a fresh
  # disk (BOX_FRESH_DISK=1) must re-join the established replication anyway.
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
    log "✔ $n fresh-disk CreateVm: vm=$VM_ID — membership + mesh IP kept, local data reset (redis will re-sync)"
  else
    log "✔ $n in-place UpgradeApp: vm=$VM_ID — disk/data preserved"
  fi
}

update_all() {
  # Serialized: one node at a time, re-proving HA health before touching the next.
  local n
  for n in $(_nodes); do
    update_member "$n"
    verify_ha
  done
  log "✔ rolled all $REDISHA_COUNT nodes"
}

case "$ACTION" in
  register-all) register_all ;;
  compute-peers) compute_peers ;;
  create-all) create_all ;;
  deploy-all) deploy_all ;;
  prime|prime-all) prime_all ;;
  bind|bind-all) bind_all ;;
  verify|verify-all) verify_all ;;
  verify-ha) verify_ha ;;
  verify-failover) verify_failover ;;
  verify-isolation|verify-isolation-all) verify_isolation_all ;;
  update) update_member "$ARG3"; verify_ha ;;
  update-only) update_member "$ARG3" ;;   # diagnostic roll without the verify-ha gate
  update-all) update_all ;;
  all) deploy_all; prime_all; bind_all; verify_all; verify_ha; verify_isolation_all ;;
  *) die "unknown action: $ACTION" ;;
esac
