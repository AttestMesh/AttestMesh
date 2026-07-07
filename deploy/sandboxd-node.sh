#!/usr/bin/env bash
# confidential-sandboxes sandboxd node in the isolated Cluster 2.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: sandboxd-node.sh <node-name> [deploy|prime|bind|start|verify-health|smoke|update|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/sandboxd-node.yaml}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
WEBHOST_STATE="${WEBHOST_STATE:-$LOGDIR/webhost-node-open-webhost.state}"
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/sandboxd.env}"

export BOX_VCPU="${BOX_VCPU:-4}" BOX_MEM="${BOX_MEM:-8192}" BOX_DISK="${BOX_DISK:-80}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

STATE="$LOGDIR/sandboxd-node-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000

_save() {
  umask 077
  cat > "$STATE" <<EOF
X=${X:-}
H=${H:-}
VM_ID=${VM_ID:-}
CLUSTER=${CLUSTER:-}
MEMBER_IMPL=${MEMBER_IMPL:-}
GATEWAY_URL=${GATEWAY_URL:-}
GATEWAY_DOMAIN=${GATEWAY_DOMAIN:-}
EOF
}

_load() { [ -f "$STATE" ] && source "$STATE" || true; }
ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }

_ensure_secrets() {
  mkdir -p "$(dirname "$SECRETS_FILE")"
  if [ ! -f "$SECRETS_FILE" ]; then
    umask 077
    cat > "$SECRETS_FILE" <<EOF
SANDBOX_DAEMON_TOKEN=$(openssl rand -hex 32)
EOF
    log "generated sandboxd secrets at $SECRETS_FILE"
  fi
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  [ -n "${SANDBOX_DAEMON_TOKEN:-}" ] || die "SANDBOX_DAEMON_TOKEN missing in $SECRETS_FILE"
}

_require_env() {
  local wh_cluster wh_member_impl indexer
  wh_cluster=$(sed -nE 's/^CLUSTER=(.*)$/\1/p' "$WEBHOST_STATE" 2>/dev/null | tail -1)
  wh_member_impl=$(sed -nE 's/^MEMBER_IMPL=(.*)$/\1/p' "$WEBHOST_STATE" 2>/dev/null | tail -1)
  CLUSTER="${CLUSTER:-$wh_cluster}"
  MEMBER_IMPL="${MEMBER_IMPL:-$wh_member_impl}"
  CLUSTER="${CLUSTER:?missing CLUSTER (expected in $WEBHOST_STATE)}"
  MEMBER_IMPL="${MEMBER_IMPL:?missing MEMBER_IMPL (expected in $WEBHOST_STATE)}"
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  [ -s "$COMPOSE" ] || die "missing compose file: $COMPOSE"
  _ensure_secrets
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
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok public_base
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  public_base="${GATEWAY_URL:-}"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/sandboxd-node-box.py" "$BOX_HOST:/tmp/sandboxd-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n' "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n' "${CVM_RPC_URL:-$RPC_URL}"
    printf 'E_BUNDLER_URL=%q\n' "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
    printf 'E_GAS_POLICY_ID=%q\n' "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "$INDEXER_REGISTRY_ADDR"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_CLUSTER=%q\n' "$CLUSTER"
    printf 'E_PEER_ENVELOPE_FALLBACK=%q\n' "true"
    printf 'E_SANDBOX_DAEMON_TOKEN=%q\n' "$SANDBOX_DAEMON_TOKEN"
    printf 'E_PUBLIC_BASE_URL=%q\n' "$public_base"
    printf 'E_ONCHAIN_CLUSTER_DIAMOND=%q\n' "$CLUSTER"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/sandboxd-node-box.py $mode $app_id $vm_id'"
}

deploy_cvm() {
  _load; _require_env
  _save
  log "▶ box deploy_app sandboxd node=$NODE compose=$COMPOSE (stopped)"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  GATEWAY_URL=$(echo "$j" | jq -r .gateway_url)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed sandboxd app_id=$X compose_hash=$H vm=$VM_ID gateway=$GATEWAY_URL"
}

prime_gate() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  if [ "$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)" != true ]; then
    send_seq "sandboxd-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  else
    log "compose hash already allowlisted"
  fi
  if [ "$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)" != true ]; then
    send_seq "sandboxd-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  else
    log "app id already allowlisted"
  fi
  log "✔ cluster gate primed for sandboxd app_id=$X"
}

bind_member() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind sandboxd X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/sandboxd-bind-${NODE}.$(ts).log"
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
  log "✔ bound sandboxd app X -> $CLUSTER"
}

start_cvm() {
  _load; _require_env
  [ -n "${VM_ID:-}" ] || die "need VM_ID (run deploy first)"
  log "▶ start sandboxd VM vm=$VM_ID"
  _box_run start "" "$VM_ID" || die "start failed"
  log "✔ start requested for sandboxd vm=$VM_ID"
}

verify_health() {
  _load; _require_env
  [ -n "${GATEWAY_URL:-}" ] || die "need GATEWAY_URL (run deploy first)"
  local i code
  for i in $(seq 1 45); do
    code=$(curl -sk -o /tmp/sandboxd-health.json -w '%{http_code}' --max-time 10 "$GATEWAY_URL/healthz" 2>/dev/null || true)
    if [ "$code" = 200 ]; then
      log "✔ sandboxd health reachable: $GATEWAY_URL/healthz"
      cat /tmp/sandboxd-health.json
      return 0
    fi
    log "… sandboxd health not ready ($i/45, code=${code:-000})"
    sleep 10
  done
  die "sandboxd health did not become reachable at $GATEWAY_URL/healthz"
}

update_member() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  local nh allowed out j
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" != true ]; then
    send_seq "sandboxd-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  _save
  log "✔ sandboxd update complete vm=$VM_ID"
}

smoke() {
  _load; _require_env
  [ -n "${GATEWAY_URL:-}" ] || die "need GATEWAY_URL"
  TOK="$SANDBOX_DAEMON_TOKEN" GW="$GATEWAY_URL" python3 - <<'PY'
import hashlib, json, os, subprocess, sys, time

tok = os.environ["TOK"]
gw = os.environ["GW"].rstrip("/")
body = '{"owner":"dan","runtime":{"kind":"justbash","command":"echo hi"},"egress":{"allow":[]}}'

def curl(args, input_text=None):
    p = subprocess.run(["curl", "-sk", *args], input=input_text, text=True, capture_output=True, timeout=30)
    return p.returncode, p.stdout, p.stderr

rc, out, err = curl(["-H", f"Authorization: Bearer {tok}", "-H", "Content-Type: application/json", "-X", "POST", f"{gw}/_api/sandboxes", "-d", body])
print("create_http_body", out)
if rc != 0:
    print(err, file=sys.stderr)
    sys.exit(2)
try:
    created = json.loads(out)
except Exception:
    print("create response was not JSON", file=sys.stderr)
    sys.exit(2)
if "id" not in created:
    print("create failed; no id", file=sys.stderr)
    sys.exit(3)
sid = created["id"]
rc, out, err = curl([f"{gw}/s/{sid}/attestation"])
att = json.loads(out)
events = att.get("event_log") or []
if isinstance(events, str):
    events = json.loads(events)
report_data = att.get("report_data") or ""
expected = hashlib.sha256(b"cs-attest-v1" + bytes.fromhex(sid) + bytes.fromhex(att["manifest_hash"])).hexdigest()
summary = {
    "id": sid,
    "quote": bool(att.get("quote")),
    "report_data": report_data,
    "expected_report_data_prefix": expected,
    "report_data_prefix_matches": report_data.startswith(expected),
    "onchain": att.get("onchain"),
    "events": [e.get("event") for e in events if isinstance(e, dict)],
    "has_create_event": any(isinstance(e, dict) and e.get("event") == "cs.sandbox.create" for e in events),
}
print("attestation_summary", json.dumps(summary, indent=2))
rc, out, err = curl(["-H", f"Authorization: Bearer {tok}", f"{gw}/_api/telemetry"])
print("telemetry", out)
if not summary["quote"] or not summary["report_data_prefix_matches"] or not summary["has_create_event"]:
    sys.exit(4)
PY
}

log "=== confidential-sandboxes node: $NODE ==="
case "$ACTION" in
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  start) start_cvm ;;
  verify-health) verify_health ;;
  smoke) smoke ;;
  update) update_member ;;
  setup) deploy_cvm; prime_gate; bind_member; start_cvm ;;
  all) deploy_cvm; prime_gate; bind_member; start_cvm; verify_health; smoke ;;
  *) die "usage: sandboxd-node.sh <node-name> [deploy|prime|bind|start|verify-health|smoke|update|setup|all]" ;;
esac
