#!/usr/bin/env bash
# Synclave AttestMesh node on the self-hosted on-chain dstack box.
#
# Deploys Synclave (console.attestmesh.xyz + tenant apps at *.app.attestmesh.xyz)
# as a full, on-chain-anchored AttestMesh node via the canonical Path-A flow:
#   deploy (stock DstackApp + sealed env + bridge CreateVm, gateway ON)
#     -> prime (allowlist compose_hash + app_id on the cluster)
#     -> bind  (upgradeToAndCall the proxy to ClusterMember, box deployer key)
#     -> verify (sidecar self-registers -> memberIdOf(X) != 0)
#     -> verify-app / verify-daemon (service reachability; daemon lives on webhost)
#
# Day-2 rolls: `update` recomputes the compose_hash, allowlists it FIRST, then does
# an in-place, DISK-PRESERVING UpgradeApp (BOX_FRESH_DISK=1 forces a wipe).
#
# Secrets are read at runtime from $SECRETS_FILE and passed in-memory over SSH as
# E_* vars; nothing secret is written to disk or committed.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: synclave-node.sh <node-name> [deploy|prime|bind|verify|verify-app|verify-daemon|update|setup|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/synclave-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"

# Secrets file (git-ignored, never committed). Sourced into the shell so its
# values can be forwarded in-memory as E_* to the box. Expected keys:
#   POSTGRES_PASSWORD TEE_DAEMON_TOKEN SESSION_SECRET PRIVY_APP_ID PRIVY_APP_SECRET
#   GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET CLOUDFLARE_API_TOKEN ADMIN_API_KEY
#   TLS_FULLCHAIN_B64 TLS_KEY_B64   (optional: DATABASE_URL)
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/synclave.env}"

# Non-secret config (overridable), sealed alongside the secrets for one measured surface.
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://console.attestmesh.xyz}"
CORS_ORIGIN="${CORS_ORIGIN:-https://console.attestmesh.xyz}"
CONSOLE_HOST="${CONSOLE_HOST:-console.attestmesh.xyz}"
APP_DOMAIN="${APP_DOMAIN:-app.attestmesh.xyz}"
GITHUB_OAUTH_CALLBACK_URL="${GITHUB_OAUTH_CALLBACK_URL:-https://console.attestmesh.xyz/api/v1/auth/github/callback}"
# mesh-state-api runs on the self-hosted box and is reachable from the Synclave
# CVM via a bridge-only listener on dstack-br0.
CLUSTER_API_URL="${CLUSTER_API_URL:-http://10.0.100.1:8787}"
CLUSTER_NETWORK_ID="${CLUSTER_NETWORK_ID:-net_attestmesh_live}"
CLUSTER_NAME="${CLUSTER_NAME:-AttestMesh C3}"
# CF app-fronting (non-secret): the attestmesh.xyz zone + the origin the proxied
# <slug>.app records point at (the box haproxy public IP).
CLOUDFLARE_ZONE_ID="${CLOUDFLARE_ZONE_ID:-5b276342195bda12c978f20ed38a3757}"
CLOUDFLARE_ORIGIN_IP="${CLOUDFLARE_ORIGIN_IP:-173.231.234.133}"

# CVM sizing (tee-daemon spawns tenant containers → give it headroom; confirm vs live).
export BOX_VCPU="${BOX_VCPU:-4}" BOX_MEM="${BOX_MEM:-8192}" BOX_DISK="${BOX_DISK:-60}"
# No host port-forwards: bridge mode → the box haproxy / dstack gateway reach
# tlsproxy:443 at the CVM's dstack-br0 IP, not via a host map.
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

STATE="$LOGDIR/synclave-node-${NODE}.state"
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

# Synclave joins the existing Matrix cluster by default (reuses the already-deployed
# Path-A ClusterMember impl). Override CLUSTER + MEMBER_IMPL to target a fresh cluster.
_default_cluster_env() {
  if [ -z "${CLUSTER:-}" ] || [ -z "${MEMBER_IMPL:-}" ]; then
    [ -f "$MATRIX_STATE" ] || die "missing cluster state: $MATRIX_STATE (set CLUSTER + MEMBER_IMPL to override)"
    CLUSTER="${CLUSTER:-$(grep '^CLUSTER=' "$MATRIX_STATE" | cut -d= -f2-)}"
    MEMBER_IMPL="${MEMBER_IMPL:-$(grep '^MEMBER_IMPL=' "$MATRIX_STATE" | cut -d= -f2-)}"
  fi
  [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "could not resolve CLUSTER/MEMBER_IMPL"
}

_require_env() {
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"

  [ -f "$SECRETS_FILE" ] || die "missing secrets file: $SECRETS_FILE"
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  local k
  for k in POSTGRES_PASSWORD TEE_DAEMON_TOKEN SESSION_SECRET PRIVY_APP_ID PRIVY_APP_SECRET \
           GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET CLOUDFLARE_API_TOKEN ADMIN_API_KEY \
           TLS_FULLCHAIN_B64 TLS_KEY_B64; do
    [ -n "${!k:-}" ] || die "secret $k not set in $SECRETS_FILE"
  done
  # DATABASE_URL defaults to the in-compose Postgres unless the secrets file overrides it.
  DATABASE_URL="${DATABASE_URL:-postgres://synclave:${POSTGRES_PASSWORD}@postgres:5432/synclave}"
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

# Forward compose + helper to the box and run a box-side mode with sealed E_* env.
# HARDENED: the E_* values (secrets included) are piped over ssh stdin as a
# %q-quoted env payload and sourced by the remote shell — they never appear on
# the remote argv (`ps`), in sudo logs, or in shell history. Only the non-secret
# BOX_* knobs ride the command line.
_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/synclave-node-box.py" "$BOX_HOST:/tmp/synclave-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n'                 "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n'                  "$RPC_URL"
    printf 'E_BUNDLER_URL=%q\n'              "${BUNDLER_URL:-$RPC_URL}"
    printf 'E_GAS_POLICY_ID=%q\n'            "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n'    "$INDEXER_REGISTRY_ADDR"
    printf 'E_GATEWAY_DOMAIN=%q\n'           "$GATEWAY_DOMAIN"
    printf 'E_POSTGRES_PASSWORD=%q\n'        "$POSTGRES_PASSWORD"
    printf 'E_TEE_DAEMON_TOKEN=%q\n'         "$TEE_DAEMON_TOKEN"
    printf 'E_SESSION_SECRET=%q\n'           "$SESSION_SECRET"
    printf 'E_DATABASE_URL=%q\n'             "$DATABASE_URL"
    printf 'E_PRIVY_APP_ID=%q\n'             "$PRIVY_APP_ID"
    printf 'E_PRIVY_APP_SECRET=%q\n'         "$PRIVY_APP_SECRET"
    printf 'E_GITHUB_CLIENT_ID=%q\n'         "$GITHUB_CLIENT_ID"
    printf 'E_GITHUB_CLIENT_SECRET=%q\n'     "$GITHUB_CLIENT_SECRET"
    printf 'E_GITHUB_OAUTH_CALLBACK_URL=%q\n' "$GITHUB_OAUTH_CALLBACK_URL"
    printf 'E_DAEMON_URL=%q\n'               "${DAEMON_URL:-}"
    printf 'E_PUBLIC_BASE_URL=%q\n'          "$PUBLIC_BASE_URL"
    printf 'E_APP_DOMAIN=%q\n'               "$APP_DOMAIN"
    printf 'E_CLUSTER_API_URL=%q\n'          "$CLUSTER_API_URL"
    printf 'E_CLUSTER_NETWORK_ID=%q\n'       "$CLUSTER_NETWORK_ID"
    printf 'E_CLUSTER_NAME=%q\n'             "$CLUSTER_NAME"
    printf 'E_CLUSTER_SELF_APP_ID=%q\n'      "${CLUSTER_SELF_APP_ID:-${X:-}}"
    printf 'E_CLUSTER_NETWORKS=%q\n'         "${CLUSTER_NETWORKS:-}"
    printf 'E_CLUSTER_ORCHESTRATOR_URL=%q\n' "${CLUSTER_ORCHESTRATOR_URL:-}"
    printf 'E_CLUSTER_ORCHESTRATOR_TOKEN=%q\n' "${CLUSTER_ORCHESTRATOR_TOKEN:-}"
    printf 'E_CORS_ORIGIN=%q\n'              "$CORS_ORIGIN"
    printf 'E_CONSOLE_HOST=%q\n'             "$CONSOLE_HOST"
    printf 'E_CLOUDFLARE_API_TOKEN=%q\n'     "$CLOUDFLARE_API_TOKEN"
    printf 'E_CLOUDFLARE_ZONE_ID=%q\n'       "$CLOUDFLARE_ZONE_ID"
    printf 'E_CLOUDFLARE_ORIGIN_IP=%q\n'     "$CLOUDFLARE_ORIGIN_IP"
    printf 'E_ADMIN_API_KEY=%q\n'            "$ADMIN_API_KEY"
    printf 'E_LABELS_API_URL=%q\n'           "${LABELS_API_URL:-}"
    printf 'E_LABELS_API_TOKEN=%q\n'         "${LABELS_API_TOKEN:-}"
    printf 'E_TLS_FULLCHAIN_B64=%q\n'        "$TLS_FULLCHAIN_B64"
    printf 'E_TLS_KEY_B64=%q\n'              "$TLS_KEY_B64"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n'   "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n'   "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n'   "ghcr.io"
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/synclave-node-box.py $mode $app_id $vm_id'"
}

deploy_cvm() {
  _load; _default_cluster_env; _require_env
  _save
  log "▶ box deploy_app synclave node=$NODE compose=$COMPOSE cluster=$CLUSTER"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed synclave node app_id=$X compose_hash=$H vm=$VM_ID"
  log "gateway url: https://${X#0x}.gateway.attestmesh.xyz (lowercase); tlsproxy:443 also at the CVM dstack-br0 IP"
}

prime_gate() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  local allowed_hash allowed_app
  allowed_hash=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_hash" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "synclave-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  fi
  allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_app" = true ]; then
    log "app id already allowlisted"
  else
    send_seq "synclave-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  fi
}

bind_member() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind synclave X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/synclave-bind-${NODE}.$(ts).log"
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
  log "✔ bound synclave node X -> $CLUSTER"
}

verify() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ synclave node registered: memberId=$id memberCount=$count"
      return 0
    fi
    log "… synclave node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  die "synclave node did not register"
}

# App reachability. Post-repoint: the public console URL. Pre-repoint: hit the CVM's
# tlsproxy:443 directly from the box (override SYNCLAVE_CVM_IP or pass the bridge IP;
# find it on the box with `ip neigh show dev dstack-br0`).
# NOTE: the console has no JSON health route — /healthz is served by the SPA (200 +
# app HTML). Pass = /healthz returns HTTP 200 AND / serves <title>Synclave</title>.
verify_app() {
  _load
  local url="${VERIFY_URL:-$PUBLIC_BASE_URL}" host="$CONSOLE_HOST" i code resolve=()
  if [ -n "${SYNCLAVE_CVM_IP:-}" ]; then
    resolve=(--resolve "${host}:443:${SYNCLAVE_CVM_IP}")
    log "verify-app: CVM-direct via --resolve ${host}->${SYNCLAVE_CVM_IP}"
  else
    log "verify-app: public https://${host} (needs the box haproxy backend re-pointed to this CVM)"
  fi
  for i in $(seq 1 30); do
    code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "${resolve[@]}" "https://${host}/healthz" 2>/dev/null || true)
    if [ "$code" = 200 ]; then
      local root; root=$(curl -sk --max-time 10 "${resolve[@]}" "https://${host}/" 2>/dev/null || true)
      if printf '%s' "$root" | grep -qi '<title>Synclave</title>'; then
        log "✔ synclave app healthy: /healthz 200 + console <title>Synclave</title>"
        return 0
      fi
      log "… /healthz 200 but console title not seen yet ($i/30)"
    else
      log "… synclave app not ready ($i/30, /healthz -> ${code:-000})"
    fi
    sleep 10
  done
  die "synclave app did not become healthy at https://${host}"
}

# Daemon reachability. Synclave is control-plane only now; the app-hosting
# daemon lives in the isolated Open Webhost node. Keep this action for the
# canonical Synclave deploy checklist, but delegate to the daemon owner.
verify_daemon() {
  local webhost_node="${WEBHOST_NODE:-webhost}"
  log "verify-daemon: delegating to Open Webhost node ${webhost_node}"
  "$HERE/webhost-node.sh" "$webhost_node" verify-daemon
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
    # FATAL on failure: rolling a compose the cluster hasn't allowlisted is the #1 trap —
    # the KMS refuses keys and the CVM can't unseal (hit live on the v14 roll: RPC 403 here
    # while the script sailed on to UpgradeApp). Verify on-chain before touching the VM.
    send_seq "synclave-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh" \
      || die "addComposeHash failed — NOT proceeding to UpgradeApp (unallowlisted compose bricks the boot)"
    allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL" 2>/dev/null)
    [ "$allowed" = true ] || die "compose hash still not allowlisted after send — aborting before UpgradeApp"
  fi
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r '.vm_id // empty'); [ -n "$VM_ID" ] || VM_ID="$(_load; echo "${VM_ID:-}")"
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ synclave node update complete mode=$mode vm=$VM_ID"
  # NOTE: service checks (verify / verify-app) are run as explicit separate steps
  # so they can be individually gated; `update` performs the roll only.
}

log "=== Synclave AttestMesh node: $NODE ==="
case "$ACTION" in
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  verify-app) verify_app ;;
  verify-daemon) verify_daemon ;;
  update) update_member ;;
  setup) deploy_cvm; prime_gate; bind_member ;;
  all) deploy_cvm; prime_gate; bind_member; verify; verify_app; verify_daemon ;;
  *) die "usage: synclave-node.sh <node-name> [deploy|prime|bind|verify|verify-app|verify-daemon|update|setup|all]" ;;
esac
