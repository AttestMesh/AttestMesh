#!/usr/bin/env bash
# Standalone PostgreSQL node deploy on the self-hosted dstack box.
#
# This node has no Tailscale. It joins the Matrix node's AttestMesh cluster and
# reaches Matrix through the mesh-only listener on the Matrix sidecar.
# Order-sensitive, logged, re-entrant via a state file. The Smithers workflow in
# deploy/workflows/postgres-node.tsx shells out to these subcommands.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: postgres-node.sh <node-name> [deploy|prime|bind|verify|verify-agent|verify-metrics|verify-mesh-endpoint|verify-isolation|update|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/postgres-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
export BOX_VCPU="${BOX_VCPU:-2}" BOX_MEM="${BOX_MEM:-4096}" BOX_DISK="${BOX_DISK:-40}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
TS_SUFFIX="${TS_SUFFIX:-tail39cb2e.ts.net}"

STATE="$LOGDIR/postgres-node-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000

_save() {
  umask 077
  cat > "$STATE" <<EOF
X=${X:-}
H=${H:-}
VM_ID=${VM_ID:-}
CLUSTER=${CLUSTER:-}
MEMBER_IMPL=${MEMBER_IMPL:-}
MATRIX_X=${MATRIX_X:-}
MATRIX_MEMBER_ID=${MATRIX_MEMBER_ID:-}
MATRIX_MESH_IP=${MATRIX_MESH_IP:-}
PGPW=${PGPW:-}
BOTPASSWORD=${BOTPASSWORD:-}
BOT_USER_ID=${BOT_USER_ID:-}
MATRIX_ROOM_ID=${MATRIX_ROOM_ID:-}
MATRIX_ADMIN_MXIDS=${MATRIX_ADMIN_MXIDS:-}
EOF
}

_load() { [ -f "$STATE" ] && source "$STATE" || true; }

ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }

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

_ipv4_from_u32() {
  local n="$1"
  printf '%d.%d.%d.%d' "$(( (n >> 24) & 255 ))" "$(( (n >> 16) & 255 ))" "$(( (n >> 8) & 255 ))" "$(( n & 255 ))"
}

_matrix_server_name() {
  printf '%s.gateway.attestmesh.xyz' "$(printf '%s' "${MATRIX_X#0x}" | tr A-Z a-z)"
}

_postgres_bot_user_id() {
  printf '@postgres-admin-agent:%s' "$(_matrix_server_name)"
}

_inherit_bot_password() {
  if [ -z "${BOTPASSWORD:-}" ]; then
    BOTPASSWORD="${BOT_PASSWORD:-${POSTGRES_BOT_PASSWORD:-}}"
  fi
}

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
  MATRIX_MEMBER_ID="${MATRIX_MEMBER_ID:-$(cast call "$CLUSTER" 'memberIdOf(address)(bytes32)' "$MATRIX_X" --rpc-url "$RPC_URL" 2>/dev/null)}"
  [ -n "$MATRIX_MEMBER_ID" ] && [ "$MATRIX_MEMBER_ID" != "$ZERO32" ] || die "Matrix app is not registered in cluster $CLUSTER"

  local mesh_u32
  mesh_u32=$(cast call "$CLUSTER" 'meshIpOf(bytes32)(uint32)' "$MATRIX_MEMBER_ID" --rpc-url "$RPC_URL" 2>/dev/null | awk '{print int($1)}')
  [ -n "$mesh_u32" ] && [ "$mesh_u32" != 0 ] || die "could not resolve Matrix mesh IP"
  MATRIX_MESH_IP="${MATRIX_MESH_IP:-$(_ipv4_from_u32 "$mesh_u32")}"

  local server_name
  server_name="$(_matrix_server_name)"
  MATRIX_ROOM_ID="${MATRIX_ROOM_ID:-!QlbJvhWoxMNcJvVwCr:${server_name}}"
  BOT_USER_ID="${BOT_USER_ID:-${POSTGRES_BOT_USER_ID:-$(_postgres_bot_user_id)}}"
  MATRIX_ADMIN_MXIDS="${MATRIX_ADMIN_MXIDS:-@lsdan:${server_name}}"
}

_matrix_verify_credentials() {
  _load
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
  local label="$1" command="$2" expect_re="$3" fqdn
  _matrix_verify_credentials
  fqdn="$(_matrix_fqdn)" || die "could not find live Matrix tailnet FQDN (set MATRIX_TAILNET_FQDN=...)"
  log "▶ Matrix room check: $label via https://$fqdn"
  if ! MATRIX_PROBE_FQDN="$fqdn" \
    MATRIX_PROBE_ROOM_ID="$MATRIX_ROOM_ID" \
    MATRIX_PROBE_USER="$MATRIX_VERIFY_LOCALPART" \
    MATRIX_PROBE_PASSWORD="$MATRIX_VERIFY_PASSWORD_RESOLVED" \
    MATRIX_PROBE_BOT="$BOT_USER_ID" \
    MATRIX_PROBE_COMMAND="$command" \
    MATRIX_PROBE_EXPECT_RE="$expect_re" \
    python3 - <<'PY'
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

base = "https://" + os.environ["MATRIX_PROBE_FQDN"].rstrip("/")
room_id = os.environ["MATRIX_PROBE_ROOM_ID"]
user = os.environ["MATRIX_PROBE_USER"]
password = os.environ["MATRIX_PROBE_PASSWORD"]
bot = os.environ["MATRIX_PROBE_BOT"]
command = os.environ["MATRIX_PROBE_COMMAND"]
expect = re.compile(os.environ["MATRIX_PROBE_EXPECT_RE"], re.I | re.S)

def request(method, path, payload=None, token=None, timeout=15):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    for _ in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body.decode() or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 429:
                try:
                    retry_ms = int(json.loads(body).get("retry_after_ms") or 5000)
                except Exception:
                    retry_ms = 5000
                time.sleep(max(1, min(300, (retry_ms + 999) // 1000)))
                continue
            raise SystemExit(f"Matrix API {method} {path} failed: HTTP {exc.code}: {body[:500]}")
    raise SystemExit(f"Matrix API {method} {path} remained rate-limited")

login = request("POST", "/_matrix/client/v3/login", {
    "type": "m.login.password",
    "identifier": {"type": "m.id.user", "user": user},
    "password": password,
})
token = login.get("access_token")
if not token:
    raise SystemExit("Matrix login did not return an access token")

room_path = urllib.parse.quote(room_id, safe="")
txn = uuid.uuid4().hex
sent = request("PUT", f"/_matrix/client/v3/rooms/{room_path}/send/m.room.message/{txn}", {
    "msgtype": "m.text",
    "body": command,
}, token=token)
sent_event_id = sent.get("event_id")
if not sent_event_id:
    raise SystemExit("Matrix send did not return an event_id")

deadline = time.time() + 90
while time.time() < deadline:
    qs = urllib.parse.urlencode({"dir": "b", "limit": "100"})
    events = request("GET", f"/_matrix/client/v3/rooms/{room_path}/messages?" + qs, token=token, timeout=12).get("chunk", [])
    for event in events:
        if event.get("event_id") == sent_event_id:
            break
        if event.get("type") != "m.room.message" or event.get("sender") != bot:
            continue
        body = str(event.get("content", {}).get("body", ""))
        if expect.search(body):
            print(body[:800])
            sys.exit(0)
    time.sleep(3)
raise SystemExit(f"timed out waiting for {bot} reply matching /{expect.pattern}/")
PY
  then
    die "Matrix room check failed: $label"
  fi
  log "✔ Matrix room check passed: $label"
}

_postgres_mesh_ip() {
  _load
  _default_matrix_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local id mesh_u32
  id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
  [ -n "$id" ] && [ "$id" != "$ZERO32" ] || die "postgres node is not registered"
  mesh_u32=$(cast call "$CLUSTER" 'meshIpOf(bytes32)(uint32)' "$id" --rpc-url "$RPC_URL" 2>/dev/null | awk '{print int($1)}')
  [ -n "$mesh_u32" ] && [ "$mesh_u32" != 0 ] || die "could not resolve postgres mesh IP"
  _ipv4_from_u32 "$mesh_u32"
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
  [ -z "$missing" ] || die "missing required env:$missing"
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
  scp -o BatchMode=yes -q "$HERE/postgres-node-box.py" "$BOX_HOST:/tmp/postgres-node-box.py"
  ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    E_CHAIN_ID='$CHAIN_ID' E_RPC_URL='$RPC_URL' E_BUNDLER_URL='${BUNDLER_URL:-$RPC_URL}' E_GAS_POLICY_ID='${GAS_POLICY_ID:-}' E_INDEXER_REGISTRY_ADDR='$INDEXER_REGISTRY_ADDR' E_GATEWAY_DOMAIN='$GATEWAY_DOMAIN' \
    E_POSTGRES_PASSWORD='$PGPW' E_MATRIX_MESH_IP='$MATRIX_MESH_IP' \
    E_MATRIX_USER_ID='$BOT_USER_ID' E_MATRIX_PASSWORD='$BOTPASSWORD' E_MATRIX_ROOM_ID='$MATRIX_ROOM_ID' E_MATRIX_ADMIN_MXIDS='$MATRIX_ADMIN_MXIDS' \
    E_LLM_BASE_URL='$LLM_BASE_URL' E_LLM_MODEL='$LLM_MODEL' E_LLM_API_KEY='$LLM_API_KEY' \
    E_DSTACK_DOCKER_USERNAME='${guser:-dmvt}' E_DSTACK_DOCKER_PASSWORD='$gtok' E_DSTACK_DOCKER_REGISTRY='ghcr.io' \
    $BOX_PY /tmp/postgres-node-box.py $mode $app_id $vm_id"
}

deploy_cvm() {
  _load; _default_matrix_env; _require_env
  _inherit_bot_password
  PGPW="${PGPW:-$(openssl rand -hex 24)}"
  [ -n "${BOTPASSWORD:-}" ] || die "missing BOTPASSWORD; pass BOTPASSWORD=... or BOT_PASSWORD=... for first deploy"
  _save
  log "▶ box deploy_app postgres node=$NODE compose=$COMPOSE matrix_mesh=$MATRIX_MESH_IP"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed postgres app_id=$X compose_hash=$H vm=$VM_ID"
}

prime_gate() {
  _load; _default_matrix_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  local allowed_hash allowed_app
  allowed_hash=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_hash" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "postgres-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  fi
  allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_app" = true ]; then
    log "app id already allowlisted"
  else
    send_seq "postgres-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  fi
}

bind_member() {
  _load; _default_matrix_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind postgres X=$X → impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/postgres-bind-${NODE}.$(ts).log"
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
  log "✔ bound postgres X → $CLUSTER"
}

verify() {
  _load; _default_matrix_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ postgres node registered: memberId=$id memberCount=$count matrix_mesh=$MATRIX_MESH_IP"
      return 0
    fi
    log "… postgres node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  die "postgres node did not register"
}

verify_agent() {
  _default_matrix_env
  _matrix_expect_reply "postgres-admin-agent status" "$BOT_USER_ID !pg status" "Postgres is up"
}

verify_metrics() {
  _default_matrix_env
  _matrix_expect_reply "postgres-admin-agent metrics" "$BOT_USER_ID !pg metrics" "Node metrics:"
}

verify_mesh_endpoint() {
  local mesh_ip
  mesh_ip="$(_postgres_mesh_ip)"
  log "✔ postgres mesh DB endpoint resolved: ${mesh_ip}:5432 (direct AttestMesh socket)"
}

verify_isolation() {
  _load
  [ -n "${VM_ID:-}" ] || die "no VM_ID in state (run deploy/update first)"
  log "▶ host-isolation check for postgres vm=$VM_ID"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/postgres-isolation-${NODE}.$(ts).log"
set -u
VMID="$VM_ID"
MAC=\$(ps -eo args | grep -F "\$VMID" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2)
[ -n "\$MAC" ] || { echo "ISOLATION: could not find qemu for \$VMID"; exit 3; }
IP=\$(ip neigh show dev dstack-br0 | grep -i "\$MAC" | grep -oE '^10\.0\.[0-9]+\.[0-9]+' | head -1)
[ -n "\$IP" ] || { echo "ISOLATION: no bridge IP for MAC \$MAC yet (CVM mid-boot?)"; exit 4; }
echo "ISOLATION: vm=\$VMID mac=\$MAC bridge_ip=\$IP"
bad=0
for p in 5432 8080 9090 9100 18080; do
  if timeout 3 bash -c "</dev/tcp/\$IP/\$p" 2>/dev/null; then
    echo "  !! \$IP:\$p REACHABLE from host — INVARIANT VIOLATION"; bad=1
  else echo "  \$IP:\$p refused from host (good)"; fi
done
if timeout 3 bash -c "</dev/tcp/\$IP/51900" 2>/dev/null; then
  echo "  \$IP:51900 reachable (expected AttestMesh wg-over-TCP transport)"
else
  echo "  \$IP:51900 not reachable from host bridge (warning: peers may still reach it through dstack gateway)"
fi
[ \$bad -eq 0 ] && echo "ISOLATION: PASS — DB/admin/metrics ports are not host-reachable" || { echo "ISOLATION: FAIL"; exit 5; }
SCRIPT
  local rc=${PIPESTATUS[0]}
  [ "$rc" = 0 ] || die "host-isolation check failed (rc=$rc) — see the log above"
  log "✔ host-isolation invariant holds (DB/admin/metrics ports refuse from the host)"
}

update_member() {
  _load; _default_matrix_env; _require_env
  _inherit_bot_password
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  [ -n "${PGPW:-}" ] && [ -n "${BOTPASSWORD:-}" ] || die "need PGPW/BOTPASSWORD in $STATE"
  local nh allowed out j
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "postgres-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  local mode
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  if [ "$mode" = createvm ]; then
    log "✔ fresh-disk CreateVm: app X=$X vm=$VM_ID — membership kept, data reset"
  else
    log "✔ in-place UpgradeApp: app X=$X vm=$VM_ID — disk/data preserved"
  fi
  verify
  verify_mesh_endpoint
  verify_agent
  verify_metrics
  verify_isolation
}

case "$ACTION" in
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  verify-agent) verify_agent ;;
  verify-metrics) verify_metrics ;;
  verify-mesh-endpoint) verify_mesh_endpoint ;;
  verify-isolation) verify_isolation ;;
  update) update_member ;;
  all) deploy_cvm; prime_gate; bind_member; verify; verify_mesh_endpoint; verify_agent; verify_metrics; verify_isolation ;;
  *) die "usage: postgres-node.sh <node-name> [deploy|prime|bind|verify|verify-agent|verify-metrics|verify-mesh-endpoint|verify-isolation|update|all]" ;;
esac
