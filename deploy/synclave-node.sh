#!/usr/bin/env bash
# Synclave AttestMesh node on the self-hosted on-chain dstack box.
#
# Deploys the Synclave control plane at synclave.net. Tenant apps run in the
# isolated webhost cluster and are not served by this node.
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
#   POSTGRES_PASSWORD (Synclave pg-ha role password) TEE_DAEMON_TOKEN SESSION_SECRET PRIVY_APP_ID PRIVY_APP_SECRET
#   GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET CLOUDFLARE_API_TOKEN ADMIN_API_KEY
#   TLS_FULLCHAIN_B64 TLS_KEY_B64   (optional: DATABASE_URL)
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/synclave.env}"
WEBHOST_SECRETS_FILE="${WEBHOST_SECRETS_FILE:-$HOME/.attestmesh/webhost.env}"
SYNCLAVE_CF_SECRETS_FILE="${SYNCLAVE_CF_SECRETS_FILE:-$HOME/.attestmesh/cloudflare-synclave-net.toml}"
CUSTOM_DOMAIN_CF_SECRETS_FILE="${CUSTOM_DOMAIN_CF_SECRETS_FILE:-$HOME/.attestmesh/cloudflare-synclave-name.toml}"

# confidential-sandboxes (sandboxd) wiring for the "Provision sandbox" button. URL/image/plan are
# non-secret config; the token is the sandboxd daemon secret (reused from its own secrets file).
SANDBOX_DAEMON_URL="${SANDBOX_DAEMON_URL:-https://2105a8086e4700e611092aaa3efd37e1e302ffd6-8080.gateway.attestmesh.xyz}"
SANDBOX_DEFAULT_IMAGE="${SANDBOX_DEFAULT_IMAGE:-ghcr.io/attestmesh/synclave-workloads@sha256:eeeab97469edf54f2d5b9582a0a1c6b49866af931573324919a3dcc6b23a0b4e}"
SANDBOX_DEFAULT_PLAN="${SANDBOX_DEFAULT_PLAN:-std-1-4-128}"
SANDBOX_APPS_DOMAIN="${SANDBOX_APPS_DOMAIN:-synclave.net}"
SANDBOX_DAEMON_TOKEN="${SANDBOX_DAEMON_TOKEN:-$(sed -nE 's/^SANDBOX_DAEMON_TOKEN=//p' "$HOME/.attestmesh/sandboxd.env" 2>/dev/null)}"

# Non-secret config (overridable), sealed alongside the secrets for one measured surface.
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://synclave.net}"
CORS_ORIGIN="${CORS_ORIGIN:-https://synclave.net}"
CONSOLE_HOST="${CONSOLE_HOST:-synclave.net}"
APP_DOMAIN="${APP_DOMAIN:-synclave.net}"
INDEXER_URL="${INDEXER_URL:-http://10.0.100.1:8787}"
GITHUB_OAUTH_CALLBACK_URL="${GITHUB_OAUTH_CALLBACK_URL:-https://synclave.net/api/v1/auth/github/callback}"
# Fleet owns exact public DNS for both workload classes in synclave.net. Each class can target a
# different outbound Cloudflare Tunnel. The origin IP remains an app-only migration fallback.
CLOUDFLARE_ZONE_ID="${CLOUDFLARE_ZONE_ID:-9c618e211544dcbd8b63dee67dcd2adb}"
CLOUDFLARE_ORIGIN_IP="${CLOUDFLARE_ORIGIN_IP:-173.231.234.133}"
CLOUDFLARE_APP_CNAME_TARGET="${CLOUDFLARE_APP_CNAME_TARGET:-9e9fed47-327a-4398-a43c-cfa51716b473.cfargotunnel.com}"
CLOUDFLARE_SANDBOX_CNAME_TARGET="${CLOUDFLARE_SANDBOX_CNAME_TARGET:-b11c9505-f0be-4243-9d55-e09bfbc72050.cfargotunnel.com}"
SYNCLAVE_RELEASE_COMMIT="c292082aead696a3a28a5d3eb1795625fe2f4fe4"
SYNCLAVE_RELEASE_VERSION="sha-c292082aead696a3a28a5d3eb1795625fe2f4fe4"
SYNCLAVE_RELEASE_IMAGE="ghcr.io/attestmesh/synclave-app@sha256:f3d7d4888b11db59c2606e7ca4b849c0e26ef938e1ae9373726dcbd4044f43e2"
CUSTOM_DOMAIN_KV_PROPAGATION_SEC="${CUSTOM_DOMAIN_KV_PROPAGATION_SEC:-60}"
CLOUDFLARE_SAAS_ZONE_ID="${CLOUDFLARE_SAAS_ZONE_ID:-e58a44e83160efd73bc2a6be8c2bc309}"
CUSTOM_DOMAIN_CNAME_ZONE="${CUSTOM_DOMAIN_CNAME_ZONE:-synclave.name}"
CLOUDFLARE_ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID:-29754b422e1b4541962d00b9abd21543}"
CLOUDFLARE_CUSTOM_DOMAIN_KV_NAMESPACE_ID="${CLOUDFLARE_CUSTOM_DOMAIN_KV_NAMESPACE_ID:-5e9329901de94c43a6283163e81da30c}"

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
  # Use the authenticated box-local Base node for the long-running sidecar.
  # Public endpoints rate-limit its bounded startup reads and can strand pg-ha.
  SYNCLAVE_CVM_RPC_URL="${SYNCLAVE_CVM_RPC_URL:-$(box_local_rpc_url "$BOX_HOST" synclave)}"

  [ -f "$SECRETS_FILE" ] || die "missing secrets file: $SECRETS_FILE"
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  DAEMON_URL="${DAEMON_URL:-https://daemon.synclave.net}"
  if ! [[ "$DAEMON_URL" =~ ^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?([:][0-9]{1,5})?/?$ ]]; then
    die "DAEMON_URL must be a credential-free HTTPS origin (got an invalid value)"
  fi
  [ -f "$WEBHOST_SECRETS_FILE" ] || die "missing Webhost secrets file for token-parity check: $WEBHOST_SECRETS_FILE"
  local webhost_daemon_token
  webhost_daemon_token=$(bash -c 'unset TEE_DAEMON_TOKEN; source "$1"; printf %s "${TEE_DAEMON_TOKEN:-}"' _ "$WEBHOST_SECRETS_FILE") \
    || die "could not read TEE_DAEMON_TOKEN from $WEBHOST_SECRETS_FILE"
  [ -n "$webhost_daemon_token" ] || die "TEE_DAEMON_TOKEN is empty in $WEBHOST_SECRETS_FILE"
  [ "$TEE_DAEMON_TOKEN" = "$webhost_daemon_token" ] \
    || die "TEE_DAEMON_TOKEN mismatch between Synclave and Webhost secret files"
  unset webhost_daemon_token
  SYNCLAVE_CLOUDFLARE_API_TOKEN="${SYNCLAVE_CLOUDFLARE_API_TOKEN:-$(sed -nE 's/^[[:space:]]*api_token[[:space:]]*=[[:space:]]*"?([^"#[:space:]]+)"?.*/\1/p' "$SYNCLAVE_CF_SECRETS_FILE" 2>/dev/null)}"
  local custom_domain_cloudflare_token
  custom_domain_cloudflare_token=$(sed -nE 's/^[[:space:]]*api_token[[:space:]]*=[[:space:]]*"?([^"#[:space:]]+)"?.*/\1/p' "$CUSTOM_DOMAIN_CF_SECRETS_FILE" 2>/dev/null)
  CLOUDFLARE_SAAS_API_TOKEN="${CLOUDFLARE_SAAS_API_TOKEN:-$custom_domain_cloudflare_token}"
  CLOUDFLARE_KV_API_TOKEN="${CLOUDFLARE_KV_API_TOKEN:-$custom_domain_cloudflare_token}"
  local k
  for k in POSTGRES_PASSWORD TEE_DAEMON_TOKEN SESSION_SECRET PRIVY_APP_ID PRIVY_APP_SECRET \
           GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET CLOUDFLARE_API_TOKEN SYNCLAVE_CLOUDFLARE_API_TOKEN ADMIN_API_KEY \
           CLOUDFLARE_SAAS_API_TOKEN CLOUDFLARE_SAAS_ZONE_ID CUSTOM_DOMAIN_CNAME_ZONE \
           CLOUDFLARE_KV_API_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_CUSTOM_DOMAIN_KV_NAMESPACE_ID \
           TLS_FULLCHAIN_B64 TLS_KEY_B64; do
    [ -n "${!k:-}" ] || die "secret $k not set in $SECRETS_FILE"
  done
  [ -n "${SANDBOX_DAEMON_TOKEN:-}" ] || die "SANDBOX_DAEMON_TOKEN empty (expected in \$HOME/.attestmesh/sandboxd.env); required for the Provision button"
  [ "$APP_DOMAIN" = "synclave.net" ] || die "APP_DOMAIN must be Fleet's shared synclave.net zone"
  [ "$SANDBOX_APPS_DOMAIN" = "$APP_DOMAIN" ] \
    || die "SANDBOX_APPS_DOMAIN must equal APP_DOMAIN for the central hostname broker"
  [[ "${SANDBOX_DEFAULT_IMAGE:-}" =~ ^ghcr\.io/attestmesh/synclave-workloads@sha256:[0-9a-f]{64}$ ]] \
    || die "SANDBOX_DEFAULT_IMAGE must use the approved ghcr.io/attestmesh/synclave-workloads repository"
  # Synclave's DB defaults to the C3 pg-ha cluster. The compose exposes pg-ha via
  # sidecar-netns forwarders because the app container is not itself in the WG netns.
  DATABASE_URL="${DATABASE_URL:-postgresql://synclave:${POSTGRES_PASSWORD}@sidecar:15431/synclave}"
}

send_seq() {
  local label="$1"; shift
  send_with_nonce_retry "$label" "$@"
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
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml" \
    || die "failed to copy Synclave compose to $BOX_HOST"
  scp -o BatchMode=yes -q "$HERE/synclave-node-box.py" "$BOX_HOST:/tmp/synclave-node-box.py" \
    || die "failed to copy Synclave box helper to $BOX_HOST"
  {
    printf 'E_CHAIN_ID=%q\n'                 "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n'                  "$SYNCLAVE_CVM_RPC_URL"
    printf 'E_BUNDLER_URL=%q\n'              "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
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
    printf 'E_DAEMON_URL=%q\n'               "$DAEMON_URL"
    printf 'E_PUBLIC_BASE_URL=%q\n'          "$PUBLIC_BASE_URL"
    printf 'E_APP_DOMAIN=%q\n'               "$APP_DOMAIN"
    printf 'E_INDEXER_URL=%q\n'              "$INDEXER_URL"
    printf 'E_CLUSTER_NETWORKS=%q\n'         "${CLUSTER_NETWORKS:-}"
    printf 'E_CLUSTER_ORCHESTRATOR_URL=%q\n' "${CLUSTER_ORCHESTRATOR_URL:-}"
    printf 'E_CLUSTER_ORCHESTRATOR_TOKEN=%q\n' "${CLUSTER_ORCHESTRATOR_TOKEN:-}"
    printf 'E_CORS_ORIGIN=%q\n'              "$CORS_ORIGIN"
    printf 'E_CONSOLE_HOST=%q\n'             "$CONSOLE_HOST"
    printf 'E_PLATFORM_ADMIN_EMAILS=%q\n'    "${PLATFORM_ADMIN_EMAILS:-}"
    printf 'E_TELEGRAM_BOT_TOKEN=%q\n'       "${TELEGRAM_BOT_TOKEN:-}"
    printf 'E_TELEGRAM_WAITLIST_CHAT_ID=%q\n' "${TELEGRAM_WAITLIST_CHAT_ID:-}"
    printf 'E_TELEGRAM_FEEDBACK_CHAT_ID=%q\n' "${TELEGRAM_FEEDBACK_CHAT_ID:-}"
    printf 'E_STRIPE_SECRET_KEY=%q\n'        "${STRIPE_SECRET_KEY:-}"
    printf 'E_STRIPE_WEBHOOK_SECRET=%q\n'    "${STRIPE_WEBHOOK_SECRET:-}"
    printf 'E_STRIPE_ALLOW_TEST_MODE=%q\n'   "${STRIPE_ALLOW_TEST_MODE:-}"
    printf 'E_BILLING_LIVE_ENABLED=%q\n'     "${BILLING_LIVE_ENABLED:-false}"
    printf 'E_STRIPE_AUTOMATIC_TAX_ENABLED=%q\n' "${STRIPE_AUTOMATIC_TAX_ENABLED:-false}"
    printf 'E_STRIPE_MANAGED_PAYMENTS_ENABLED=%q\n' "${STRIPE_MANAGED_PAYMENTS_ENABLED:-false}"
    printf 'E_BILLING_METERING_ENABLED=%q\n' "${BILLING_METERING_ENABLED:-false}"
    printf 'E_BILLING_WORKER_INTERVAL_SEC=%q\n' "${BILLING_WORKER_INTERVAL_SEC:-10}"
    printf 'E_BILLING_CATALOG_RECONCILE_INTERVAL_SEC=%q\n' "${BILLING_CATALOG_RECONCILE_INTERVAL_SEC:-3600}"
    printf 'E_CLOUDFLARE_API_TOKEN=%q\n'     "$CLOUDFLARE_API_TOKEN"
    printf 'E_SYNCLAVE_CLOUDFLARE_API_TOKEN=%q\n' "$SYNCLAVE_CLOUDFLARE_API_TOKEN"
    printf 'E_CLOUDFLARE_ZONE_ID=%q\n'       "$CLOUDFLARE_ZONE_ID"
    printf 'E_CLOUDFLARE_ORIGIN_IP=%q\n'     "$CLOUDFLARE_ORIGIN_IP"
    printf 'E_CLOUDFLARE_APP_CNAME_TARGET=%q\n' "$CLOUDFLARE_APP_CNAME_TARGET"
    printf 'E_CLOUDFLARE_SANDBOX_CNAME_TARGET=%q\n' "$CLOUDFLARE_SANDBOX_CNAME_TARGET"
    printf 'E_CLOUDFLARE_SAAS_API_TOKEN=%q\n' "${CLOUDFLARE_SAAS_API_TOKEN:-}"
    printf 'E_CLOUDFLARE_SAAS_ZONE_ID=%q\n'  "${CLOUDFLARE_SAAS_ZONE_ID:-}"
    printf 'E_CUSTOM_DOMAIN_CNAME_ZONE=%q\n' "${CUSTOM_DOMAIN_CNAME_ZONE:-}"
    printf 'E_CLOUDFLARE_KV_API_TOKEN=%q\n'  "${CLOUDFLARE_KV_API_TOKEN:-}"
    printf 'E_CLOUDFLARE_ACCOUNT_ID=%q\n'    "${CLOUDFLARE_ACCOUNT_ID:-}"
    printf 'E_CLOUDFLARE_CUSTOM_DOMAIN_KV_NAMESPACE_ID=%q\n' "${CLOUDFLARE_CUSTOM_DOMAIN_KV_NAMESPACE_ID:-}"
    printf 'E_CUSTOM_DOMAIN_KV_PROPAGATION_SEC=%q\n' "$CUSTOM_DOMAIN_KV_PROPAGATION_SEC"
    printf 'E_ADMIN_API_KEY=%q\n'            "$ADMIN_API_KEY"
    printf 'E_LABELS_WRITE_TOKENS=%q\n'      "${LABELS_WRITE_TOKENS:-}"
    printf 'E_TLS_FULLCHAIN_B64=%q\n'        "$TLS_FULLCHAIN_B64"
    printf 'E_TLS_KEY_B64=%q\n'              "$TLS_KEY_B64"
    printf 'E_SANDBOX_DAEMON_URL=%q\n'       "$SANDBOX_DAEMON_URL"
    printf 'E_SANDBOX_DAEMON_TOKEN=%q\n'     "$SANDBOX_DAEMON_TOKEN"
    printf 'E_SANDBOX_DEFAULT_IMAGE=%q\n'    "$SANDBOX_DEFAULT_IMAGE"
    printf 'E_SANDBOX_DEFAULT_PLAN=%q\n'     "$SANDBOX_DEFAULT_PLAN"
    printf 'E_SANDBOX_APPS_DOMAIN=%q\n'      "$SANDBOX_APPS_DOMAIN"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n'   "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n'   "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n'   "ghcr.io"
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/synclave-node-box.py $mode $app_id $vm_id'"
}

_verify_local_release_image() {
  local labels
  grep -Fq "image: $SYNCLAVE_RELEASE_IMAGE" "$COMPOSE" \
    || die "Synclave Compose does not bind the locally approved image digest"
  docker pull --quiet --platform linux/amd64 "$SYNCLAVE_RELEASE_IMAGE" >/dev/null \
    || die "could not pull the locally approved Synclave image"
  labels=$(docker image inspect --format '{{json .Config.Labels}}' "$SYNCLAVE_RELEASE_IMAGE") \
    || die "could not inspect the locally approved Synclave image"
  jq -e \
    --arg source "https://github.com/AttestMesh/synclave" \
    --arg revision "$SYNCLAVE_RELEASE_COMMIT" \
    --arg version "$SYNCLAVE_RELEASE_VERSION" \
    '."org.opencontainers.image.source" == $source and
     ."org.opencontainers.image.revision" == $revision and
     ."org.opencontainers.image.version" == $version' \
    <<<"$labels" >/dev/null \
    || die "local release labels do not bind the Synclave image to the reviewed source, commit, and version"
}

deploy_cvm() {
  _load; _default_cluster_env; _require_env
  _verify_local_release_image
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
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --async --rpc-url $BOX_RPC --private-key "\$KEY"
SCRIPT
  confirm_latest_transaction "synclave-bind-${NODE}" "$RPC_URL" "$LOGDIR/synclave-bind-${NODE}.*.log" || die "bind transaction not confirmed"
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
  local host="$CONSOLE_HOST" i api root headers
  if [ -n "${SYNCLAVE_CVM_IP:-}" ]; then
    log "verify-app: CVM-direct via --resolve ${host}->${SYNCLAVE_CVM_IP}"
  else
    log "verify-app: public https://${host} (needs the box haproxy backend re-pointed to this CVM)"
  fi
  for i in $(seq 1 30); do
    if [ -n "${SYNCLAVE_CVM_IP:-}" ]; then
      api=$(ssh_box "curl --fail --silent --show-error --proto '=https' --tlsv1.2 --max-time 10 --resolve '${host}:443:${SYNCLAVE_CVM_IP}' 'https://${host}/api/v1/healthz'" 2>/dev/null || true)
      root=$(ssh_box "curl --fail --silent --show-error --proto '=https' --tlsv1.2 --max-time 10 --resolve '${host}:443:${SYNCLAVE_CVM_IP}' 'https://${host}/'" 2>/dev/null || true)
      headers=$(ssh_box "curl --fail --silent --show-error --proto '=https' --tlsv1.2 --max-time 10 --resolve '${host}:443:${SYNCLAVE_CVM_IP}' --dump-header - --output /dev/null 'https://${host}/'" 2>/dev/null || true)
    else
      api=$(curl --fail --silent --show-error --proto '=https' --tlsv1.2 --max-time 10 "https://${host}/api/v1/healthz" 2>/dev/null || true)
      root=$(curl --fail --silent --show-error --proto '=https' --tlsv1.2 --max-time 10 "https://${host}/" 2>/dev/null || true)
      headers=$(curl --fail --silent --show-error --proto '=https' --tlsv1.2 --max-time 10 --dump-header - --output /dev/null "https://${host}/" 2>/dev/null || true)
    fi
    if printf '%s' "$api" | jq -e '.status == "ok" and .db != "down"' >/dev/null 2>&1 \
       && printf '%s' "$root" | grep -qi '<title>Synclave' \
       && printf '%s' "$headers" | grep -qi '^strict-transport-security:'; then
      log "✔ Synclave UI and DB-backed API are healthy over verified TLS"
      return 0
    fi
    log "… Synclave UI/API/TLS readiness not complete ($i/30)"
    sleep 10
  done
  die "Synclave UI/API did not become healthy over verified TLS at https://${host}"
}

# Daemon reachability. Synclave is control-plane only now; the app-hosting
# daemon lives in the isolated Open Webhost node. Keep this action for the
# canonical Synclave deploy checklist, but delegate to the daemon owner.
verify_daemon() {
  local webhost_node="${WEBHOST_NODE:-open-webhost}"
  log "verify-daemon: delegating to Open Webhost node ${webhost_node}"
  "$HERE/webhost-node.sh" "$webhost_node" verify-daemon
}

update_member() {
  _load; _default_cluster_env; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  _verify_local_release_image
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
  wait_box_local_allowlist_propagation \
    "$BOX_HOST" "$SYNCLAVE_CVM_RPC_URL" "$CLUSTER" "$nh" "$RPC_URL" 300
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

# Stale deploy files are the #1 way to brick this roll (see lib.sh). Gate every mutating
# action on the roll source matching origin/main; read-only verifies stay unguarded.
case "$ACTION" in
  deploy|prime|bind|update|setup|all)
    verify_roll_source_matches_main "$ROOT" \
      deploy/synclave-node.sh \
      deploy/synclave-node-box.py \
      deploy/lib.sh \
      deploy/compose/synclave-node.yaml
    ;;
esac

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
