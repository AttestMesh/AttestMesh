#!/usr/bin/env bash
# Synclave AttestMesh node on the self-hosted on-chain dstack box.
#
# Deploys Synclave (console.attestmesh.xyz + tenant apps at *.app.attestmesh.xyz)
# as a full, on-chain-anchored AttestMesh node via the canonical Path-A flow:
#   deploy (stock DstackApp + sealed env + bridge CreateVm, gateway ON)
#     -> prime (allowlist compose_hash + app_id on the cluster)
#     -> bind  (upgradeToAndCall the proxy to ClusterMember, box deployer key)
#     -> verify (sidecar self-registers -> memberIdOf(X) != 0)
#     -> verify-app / verify-daemon (service reachability)
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
_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/synclave-node-box.py" "$BOX_HOST:/tmp/synclave-node-box.py"
  ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    E_CHAIN_ID='$CHAIN_ID' E_RPC_URL='$RPC_URL' E_BUNDLER_URL='${BUNDLER_URL:-$RPC_URL}' E_GAS_POLICY_ID='${GAS_POLICY_ID:-}' E_INDEXER_REGISTRY_ADDR='$INDEXER_REGISTRY_ADDR' E_GATEWAY_DOMAIN='$GATEWAY_DOMAIN' \
    E_POSTGRES_PASSWORD='$POSTGRES_PASSWORD' E_TEE_DAEMON_TOKEN='$TEE_DAEMON_TOKEN' E_SESSION_SECRET='$SESSION_SECRET' E_DATABASE_URL='$DATABASE_URL' \
    E_PRIVY_APP_ID='$PRIVY_APP_ID' E_PRIVY_APP_SECRET='$PRIVY_APP_SECRET' E_GITHUB_CLIENT_ID='$GITHUB_CLIENT_ID' E_GITHUB_CLIENT_SECRET='$GITHUB_CLIENT_SECRET' E_GITHUB_OAUTH_CALLBACK_URL='$GITHUB_OAUTH_CALLBACK_URL' \
    E_PUBLIC_BASE_URL='$PUBLIC_BASE_URL' E_APP_DOMAIN='$APP_DOMAIN' E_CORS_ORIGIN='$CORS_ORIGIN' E_CONSOLE_HOST='$CONSOLE_HOST' \
    E_CLOUDFLARE_API_TOKEN='$CLOUDFLARE_API_TOKEN' E_ADMIN_API_KEY='$ADMIN_API_KEY' E_TLS_FULLCHAIN_B64='$TLS_FULLCHAIN_B64' E_TLS_KEY_B64='$TLS_KEY_B64' \
    E_DSTACK_DOCKER_USERNAME='${guser:-dmvt}' E_DSTACK_DOCKER_PASSWORD='$gtok' E_DSTACK_DOCKER_REGISTRY='ghcr.io' \
    $BOX_PY /tmp/synclave-node-box.py $mode $app_id $vm_id"
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

# Daemon reachability. The tee-daemon API is routed by the frontproxy ONLY on
# *.APP_DOMAIN hosts (/_api/* -> tee-daemon:8080); an unauthenticated GET to
# /_api/projects returning 401 proves the daemon answered (auth-gated).
# The public *.app path additionally depends on the dstack gateway routing the
# SNI (broken today: no _dstack-app-address TXT) — so with SYNCLAVE_CVM_IP set
# the probe runs ON THE BOX against the CVM's tlsproxy directly.
verify_daemon() {
  _load
  local probe_host="${DAEMON_PROBE_HOST:-verify-daemon.${APP_DOMAIN}}" i code
  for i in $(seq 1 30); do
    if [ -n "${SYNCLAVE_CVM_IP:-}" ]; then
      code=$(ssh_box "curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve ${probe_host}:443:${SYNCLAVE_CVM_IP} https://${probe_host}/_api/projects" 2>/dev/null || true)
    else
      code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "https://${probe_host}/_api/projects" 2>/dev/null || true)
    fi
    if [ "$code" = 401 ] || [ "$code" = 200 ]; then
      log "✔ tee-daemon reachable: ${probe_host}/_api/projects -> $code (401 = answered, auth-gated)"
      return 0
    fi
    log "… tee-daemon not reachable yet ($i/30, /_api/projects -> ${code:-000})"
    sleep 10
  done
  die "tee-daemon did not respond at https://${probe_host}/_api/projects"
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
    send_seq "synclave-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
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
