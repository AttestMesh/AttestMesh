#!/usr/bin/env bash
# Open webhost AttestMesh node on the self-hosted on-chain dstack box.
#
# Fresh-cluster flow:
#   deploy  -> stock DstackApp + sealed env + bridge CreateVm
#   cluster -> DeployCluster diamond with the webhost compose hash seeded
#   patha   -> install Path-A DstackFacet + ClusterMember impl on that cluster
#   prime   -> allowlist the webhost app_id
#   bind    -> upgrade stock proxy X to ClusterMember and bind it to the cluster
#   verify  -> wait for sidecar self-registration
#
# RunYard is deployed as a second member by exporting CLUSTER and MEMBER_IMPL from
# this state file into deploy/runyard-node.sh. After that, set RUNYARD_HUB_URL and
# RUNYARD_HUB_TOKEN in ~/.attestmesh/webhost.env and run `update`.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: webhost-node.sh <node-name> [preflight|deploy|cluster|patha|prime|bind|verify|verify-app|verify-daemon|verify-release|update|rollback|setup|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
BOX_KMS_ROOT_SIGNER="${BOX_KMS_ROOT_SIGNER:-0x7fa63d99495be2129cf28eee54e2ef2724e3aa2e}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/webhost-node.yaml}"
ROLLBACK_COMPOSE="${ROLLBACK_COMPOSE:-$ROOT/deploy/compose/webhost-node-v1.1.3-rollback.yaml}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
RECEIPT="$ROOT/contracts/script/deployments/${CHAIN_ID}.json"

WEBHOST_RELEASE_VERSION="v1.1.5"
WEBHOST_RELEASE_COMMIT="11d25aea81233d9c62acfd1941ff0b2c0263b9ae"
WEBHOST_RELEASE_IDENTITY="https://github.com/dmvt/webhost-control/.github/workflows/release.yml@refs/tags/${WEBHOST_RELEASE_VERSION}"
WEBHOST_RELEASE_ISSUER="https://token.actions.githubusercontent.com"
WEBHOST_CONTROL_IMAGE="ghcr.io/dmvt/webhost-control-control-plane@sha256:9faee0607e7d40df9af9e8a2c6f754ff5e10ef36568ddb072542a9b7b70c437f"
WEBHOST_STORAGE_IMAGE="ghcr.io/dmvt/webhost-control-storage-helper@sha256:91ee79f2b553266336a393d6e7b484ed23d9735f4dc27eaff858658b5bc87cf0"
WEBHOST_TLS_IMAGE="ghcr.io/dmvt/webhost-control-tlsproxy@sha256:06c17f112eebc792639c9990191b8d306c206ad4e9329732114c2c36a1966720"

SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/webhost.env}"
SYNCLAVE_SECRETS="${SYNCLAVE_SECRETS:-$HOME/.attestmesh/synclave.env}"
REDPILL_KEY_FILE="${REDPILL_KEY_FILE:-$HOME/.attestmesh/redpill-key}"
CLOUDFLARE_TOML="${CLOUDFLARE_TOML:-$HOME/.attestmesh/cloudflare-attestmesh-xyz.toml}"
CLOUDFLARE_SYNCLAVE_TOML="${CLOUDFLARE_SYNCLAVE_TOML:-$HOME/.attestmesh/cloudflare-synclave-net.toml}"

APP_DOMAIN="${APP_DOMAIN:-app.synclave.net}"
DIRECTORY_HOST="${DIRECTORY_HOST:-apps.synclave.net}"
WEBHOST_ADMIN_HOST="${WEBHOST_ADMIN_HOST:-daemon.synclave.net}"
CONSOLE_HOST="${CONSOLE_HOST:-$DIRECTORY_HOST}"
NEXTAUTH_URL="${NEXTAUTH_URL:-https://${CONSOLE_HOST}}"
REDPILL_BASE_URL="${REDPILL_BASE_URL:-https://api.redpill.ai/v1}"
REDPILL_MODEL="${REDPILL_MODEL:-deepseek/deepseek-v3.2}"
VENICE_BASE_URL="${VENICE_BASE_URL:-$REDPILL_BASE_URL}"
VENICE_MODEL="${VENICE_MODEL:-$REDPILL_MODEL}"
RUNYARD_PRIVACY_AUDIT_CAPABILITY="${RUNYARD_PRIVACY_AUDIT_CAPABILITY:-privacy-audit}"
RUNYARD_PRIVACY_PREFLIGHT_CAPABILITY="${RUNYARD_PRIVACY_PREFLIGHT_CAPABILITY:-privacy-preflight}"
RUNYARD_EXECUTION_MODE="${RUNYARD_EXECUTION_MODE:-remote}"
RUNYARD_RUNNER_LOCATION="${RUNYARD_RUNNER_LOCATION:-vps}"
RUNYARD_PRIVACY_AUDIT_LLM="${RUNYARD_PRIVACY_AUDIT_LLM:-auto}"
RUNYARD_CALLBACK_URL="${RUNYARD_CALLBACK_URL:-https://${CONSOLE_HOST}/api/runyard/privacy-audit-callback}"

# Unique /16 for this new cluster: 10.19.0.0/16.
MESH_CIDR_IP="${MESH_CIDR_IP:-169017344}"
MESH_CIDR_PREFIX="${MESH_CIDR_PREFIX:-16}"

export BOX_VCPU="${BOX_VCPU:-8}" BOX_MEM="${BOX_MEM:-16384}" BOX_DISK="${BOX_DISK:-120}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

STATE="$LOGDIR/webhost-node-${NODE}.state"
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

_ensure_secrets() {
  mkdir -p "$(dirname "$SECRETS_FILE")"
  if [ ! -f "$SECRETS_FILE" ]; then
    local gh_id="" gh_secret="" cf_token="" redpill=""
    if [ -f "$SYNCLAVE_SECRETS" ]; then
      # shellcheck disable=SC1090
      source "$SYNCLAVE_SECRETS"
      gh_id="${GITHUB_CLIENT_ID:-}"
      gh_secret="${GITHUB_CLIENT_SECRET:-}"
      cf_token="${CLOUDFLARE_API_TOKEN:-}"
    fi
    if [ -z "$cf_token" ] && [ -f "$CLOUDFLARE_TOML" ]; then
      cf_token="$(sed -nE 's/^api_token *= *"?([^"]+)"?.*/\1/p' "$CLOUDFLARE_TOML" | head -1)"
    fi
    if [ -f "$REDPILL_KEY_FILE" ]; then
      redpill="$(tr -d '\r\n' < "$REDPILL_KEY_FILE")"
    fi
    umask 077
    cat > "$SECRETS_FILE" <<EOF
TEE_DAEMON_TOKEN=$(openssl rand -hex 32)
WEBHOST_MCP_TOKEN=$(openssl rand -hex 32)
NEXTAUTH_SECRET=$(openssl rand -hex 32)
RUNYARD_CALLBACK_SECRET=$(openssl rand -hex 32)
GITHUB_ID=$gh_id
GITHUB_SECRET=$gh_secret
CLOUDFLARE_API_TOKEN=$cf_token
ACME_EMAIL=admin@synclave.net
REDPILL_API_KEY=$redpill
VENICE_API_KEY=$redpill
RUNYARD_HUB_URL=
RUNYARD_HUB_TOKEN=
BACKUP_STORAGE=
BACKUP_S3_ENDPOINT=
BACKUP_S3_BUCKET=
BACKUP_S3_REGION=auto
BACKUP_S3_ACCESS_KEY_ID=
BACKUP_S3_SECRET_ACCESS_KEY=
BACKUP_PREFIX=webhost
BACKUP_APP_ID=
BACKUP_INTERVAL_SECONDS=3600
EOF
    log "generated webhost secrets at $SECRETS_FILE"
  fi
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  if [ -z "${CLOUDFLARE_SYNCLAVE_API_TOKEN:-}" ] && [ -f "$CLOUDFLARE_SYNCLAVE_TOML" ]; then
    CLOUDFLARE_SYNCLAVE_API_TOKEN="$(sed -nE 's/^api_token *= *"?([^" ]+)"?.*/\1/p' "$CLOUDFLARE_SYNCLAVE_TOML" | head -1)"
  fi
  local k
  for k in TEE_DAEMON_TOKEN WEBHOST_MCP_TOKEN NEXTAUTH_SECRET GITHUB_ID GITHUB_SECRET CLOUDFLARE_API_TOKEN CLOUDFLARE_SYNCLAVE_API_TOKEN ACME_EMAIL REDPILL_API_KEY VENICE_API_KEY RUNYARD_CALLBACK_SECRET; do
    [ -n "${!k:-}" ] || die "secret $k not set in $SECRETS_FILE"
  done
}

_require_env() {
  local indexer
  indexer=$(jq -r .indexerRegistry "$RECEIPT" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  _ensure_secrets
}

send_seq() {
  local label="$1"; shift
  send_with_nonce_retry "$label" "$@"
}

_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/webhost-node-box.py" "$BOX_HOST:/tmp/webhost-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n' "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n' "${CVM_RPC_URL:-$RPC_URL}"
    printf 'E_BUNDLER_URL=%q\n' "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
    printf 'E_GAS_POLICY_ID=%q\n' "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "$INDEXER_REGISTRY_ADDR"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_TEE_DAEMON_TOKEN=%q\n' "$TEE_DAEMON_TOKEN"
    printf 'E_WEBHOST_MCP_TOKEN=%q\n' "$WEBHOST_MCP_TOKEN"
    printf 'E_GITHUB_ID=%q\n' "$GITHUB_ID"
    printf 'E_GITHUB_SECRET=%q\n' "$GITHUB_SECRET"
    printf 'E_NEXTAUTH_SECRET=%q\n' "$NEXTAUTH_SECRET"
    printf 'E_NEXTAUTH_URL=%q\n' "$NEXTAUTH_URL"
    printf 'E_APP_DOMAIN=%q\n' "$APP_DOMAIN"
    printf 'E_DIRECTORY_HOST=%q\n' "$DIRECTORY_HOST"
    printf 'E_WEBHOST_ADMIN_HOST=%q\n' "$WEBHOST_ADMIN_HOST"
    printf 'E_ACME_EMAIL=%q\n' "$ACME_EMAIL"
    printf 'E_CONSOLE_HOST=%q\n' "$CONSOLE_HOST"
    printf 'E_REDPILL_API_KEY=%q\n' "$REDPILL_API_KEY"
    printf 'E_REDPILL_BASE_URL=%q\n' "$REDPILL_BASE_URL"
    printf 'E_REDPILL_MODEL=%q\n' "$REDPILL_MODEL"
    printf 'E_VENICE_API_KEY=%q\n' "$VENICE_API_KEY"
    printf 'E_VENICE_BASE_URL=%q\n' "$VENICE_BASE_URL"
    printf 'E_VENICE_MODEL=%q\n' "$VENICE_MODEL"
    printf 'E_RUNYARD_HUB_URL=%q\n' "${RUNYARD_HUB_URL:-}"
    printf 'E_RUNYARD_HUB_TOKEN=%q\n' "${RUNYARD_HUB_TOKEN:-}"
    printf 'E_RUNYARD_PRIVACY_PREFLIGHT_CAPABILITY=%q\n' "$RUNYARD_PRIVACY_PREFLIGHT_CAPABILITY"
    printf 'E_RUNYARD_PRIVACY_AUDIT_CAPABILITY=%q\n' "$RUNYARD_PRIVACY_AUDIT_CAPABILITY"
    printf 'E_RUNYARD_EXECUTION_MODE=%q\n' "$RUNYARD_EXECUTION_MODE"
    printf 'E_RUNYARD_RUNNER_LOCATION=%q\n' "$RUNYARD_RUNNER_LOCATION"
    printf 'E_RUNYARD_PRIVACY_AUDIT_LLM=%q\n' "$RUNYARD_PRIVACY_AUDIT_LLM"
    printf 'E_RUNYARD_CALLBACK_SECRET=%q\n' "$RUNYARD_CALLBACK_SECRET"
    printf 'E_RUNYARD_CALLBACK_URL=%q\n' "$RUNYARD_CALLBACK_URL"
    printf 'E_CLOUDFLARE_API_TOKEN=%q\n' "$CLOUDFLARE_API_TOKEN"
    printf 'E_CLOUDFLARE_SYNCLAVE_API_TOKEN=%q\n' "$CLOUDFLARE_SYNCLAVE_API_TOKEN"
    printf 'E_BACKUP_STORAGE=%q\n' "${BACKUP_STORAGE:-}"
    printf 'E_BACKUP_S3_ENDPOINT=%q\n' "${BACKUP_S3_ENDPOINT:-}"
    printf 'E_BACKUP_S3_BUCKET=%q\n' "${BACKUP_S3_BUCKET:-}"
    printf 'E_BACKUP_S3_REGION=%q\n' "${BACKUP_S3_REGION:-auto}"
    printf 'E_BACKUP_S3_ACCESS_KEY_ID=%q\n' "${BACKUP_S3_ACCESS_KEY_ID:-}"
    printf 'E_BACKUP_S3_SECRET_ACCESS_KEY=%q\n' "${BACKUP_S3_SECRET_ACCESS_KEY:-}"
    printf 'E_BACKUP_PREFIX=%q\n' "${BACKUP_PREFIX:-webhost}"
    printf 'E_BACKUP_APP_ID=%q\n' "${BACKUP_APP_ID:-${X:-}}"
    printf 'E_BACKUP_INTERVAL_SECONDS=%q\n' "${BACKUP_INTERVAL_SECONDS:-3600}"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/webhost-node-box.py $mode $app_id $vm_id'"
}

deploy_cvm() {
  _load; _require_env
  _save
  log "▶ box deploy_app webhost node=$NODE compose=$COMPOSE"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed webhost node app_id=$X compose_hash=$H vm=$VM_ID"
}

deploy_cluster() {
  _load; [ -n "${H:-}" ] || die "no compose_hash; run deploy first"
  local salt cfg lf
  salt=$(cast keccak "attestmesh-webhost-cluster-${NODE}")
  cfg="$ROOT/contracts/script/clusters/${NODE}.json"
  lf="$LOGDIR/webhost-cluster-${NODE}.$(ts).log"
  cat > "$cfg" <<JSON
{ "clusterOwner": "$DEPLOYER_ADDR", "kmsRootSigner": "$BOX_KMS_ROOT_SIGNER",
  "initialComposeHashes": ["0x${H#0x}"], "initialDeviceIds": [],
  "allowAnyDevice": true, "requireTcbUpToDate": false,
  "meshCidrIp": $MESH_CIDR_IP, "meshCidrPrefix": $MESH_CIDR_PREFIX, "salt": "$salt" }
JSON
  log "▶ DeployCluster ($cfg, mesh ${MESH_CIDR_IP}/${MESH_CIDR_PREFIX})"
  CLUSTER_FACTORY=$(jq -r .clusterDiamondFactory "$RECEIPT") \
  MEMBER_FACTORY=$(jq -r .clusterMemberFactory "$RECEIPT") \
  CLUSTER_CONFIG="script/clusters/${NODE}.json" \
    bash -c "cd '$ROOT/contracts' && forge script script/DeployCluster.s.sol:DeployCluster --rpc-url '$RPC_URL' --broadcast" 2>&1 | tee "$lf"
  CLUSTER=$(grep -iE 'Cluster deployed:' "$lf" | grep -oE '0x[0-9a-fA-F]{40}' | head -1)
  [ -n "$CLUSTER" ] || die "could not parse cluster address (see $lf)"
  _save
  log "✔ cluster=$CLUSTER"
}

patha_upgrade() {
  _load; [ -n "${CLUSTER:-}" ] || die "no cluster; run cluster first"
  bash -c "cd '$ROOT' && source deploy/env.sh >/dev/null && deploy/onchain.sh patha-upgrade $CLUSTER" || die "patha-upgrade failed"
  local b="$ROOT/contracts/broadcast/UpgradeDstackFacetPathA.s.sol/${CHAIN_ID}/run-latest.json"
  MEMBER_IMPL=$(jq -r '[.transactions[]|select(.transactionType=="CREATE" and .contractName=="ClusterMember")][-1].contractAddress' "$b" 2>/dev/null)
  [ -n "$MEMBER_IMPL" ] && [ "$MEMBER_IMPL" != null ] || die "could not parse ClusterMember impl from $b"
  _save
  log "✔ patha-upgrade done; MEMBER_IMPL=$MEMBER_IMPL"
}

prime_gate() {
  _load
  [ -n "${CLUSTER:-}" ] && [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need cluster+app+hash"
  if [ "$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)" != true ]; then
    send_seq "webhost-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  fi
  if [ "$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)" != true ]; then
    send_seq "webhost-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  fi
  log "✔ cluster gate primed for webhost app_id=$X"
}

bind_member() {
  _load
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind webhost X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/webhost-bind-${NODE}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --async --rpc-url $BOX_RPC --private-key "\$KEY"
SCRIPT
  confirm_latest_transaction "webhost-bind-${NODE}" "$RPC_URL" "$LOGDIR/webhost-bind-${NODE}.*.log" || die "bind transaction not confirmed"
  local c=""
  for _ in 1 2 3 4 5 6 7 8; do
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    [ "${c,,}" = "${CLUSTER,,}" ] && break
    sleep 2
  done
  log "X.cluster()=$c (expect $CLUSTER)"
  [ "${c,,}" = "${CLUSTER,,}" ] || die "bind did not stick (X.cluster()=$c)"
  log "✔ bound webhost node X -> $CLUSTER"
}

verify() {
  _load
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ webhost node registered: memberId=$id memberCount=$count"
      return 0
    fi
    log "… webhost node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  die "webhost node did not register"
}

verify_app() {
  _load
  local host="$CONSOLE_HOST" i code resolve=()
  if [ -n "${WEBHOST_CVM_IP:-}" ]; then
    resolve=(--resolve "${host}:443:${WEBHOST_CVM_IP}")
    log "verify-app: CVM-direct via --resolve ${host}->${WEBHOST_CVM_IP}"
  else
    log "verify-app: public https://${host}"
  fi
  for i in $(seq 1 30); do
    if [ -n "${WEBHOST_CVM_IP:-}" ]; then
      code=$(ssh_box "curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve ${host}:443:${WEBHOST_CVM_IP} https://${host}/" 2>/dev/null || true)
    else
      code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "${resolve[@]}" "https://${host}/" 2>/dev/null || true)
    fi
    if [ "$code" = 200 ] || [ "$code" = 302 ]; then
      log "✔ webhost console reachable: https://${host}/ -> HTTP $code"
      return 0
    fi
    log "… webhost console not ready ($i/30, / -> ${code:-000})"
    sleep 10
  done
  die "webhost console did not become reachable at https://${host}"
}

verify_daemon() {
  _load
  local probe_host="${DAEMON_PROBE_HOST:-health-probe.${APP_DOMAIN}}" cvm_ip="${WEBHOST_CVM_IP:-}" i code
  # The directory host intentionally routes every path to the public UI, so it
  # cannot prove daemon reachability. Resolve the running CVM's bridge address
  # from its QEMU MAC and the box DHCP lease, then probe frontproxy over the
  # private bridge with an app-domain Host header.
  for i in $(seq 1 30); do
    [ -n "$cvm_ip" ] || cvm_ip=$(_cvm_ip)
    if [ -z "$cvm_ip" ]; then
      log "… Webhost CVM bridge lease not ready ($i/30)"
      sleep 10
      continue
    fi
    code=$(ssh_box "curl -sS -o /dev/null -w '%{http_code}' --max-time 10 -H 'Host: ${probe_host}' http://${cvm_ip}/_api/projects" 2>/dev/null || true)
    if [ "$code" = 401 ] || [ "$code" = 200 ]; then
      log "✔ tee-daemon reachable: ${probe_host} via ${cvm_ip}/_api/projects -> $code"
      return 0
    fi
    log "… tee-daemon not ready ($i/30, /_api/projects -> ${code:-000})"
    sleep 10
  done
  die "tee-daemon did not respond via ${cvm_ip}/_api/projects"
}

_cvm_ip() {
  local cvm_ip="${WEBHOST_CVM_IP:-}"
  if [ -z "$cvm_ip" ] && [ -n "${VM_ID:-}" ]; then
    cvm_ip=$(ssh_box "mac=\$(pgrep -af qemu-system | grep '/${VM_ID}/' | sed -nE 's/.*mac=([0-9a-f:]{17}).*/\\1/p' | head -1); [ -n \"\$mac\" ] && sudo awk -v mac=\"\$mac\" '\$2 == mac { print \$3 }' /var/lib/misc/dnsmasq-dstack-br0.leases | tail -1" 2>/dev/null || true)
  fi
  printf '%s\n' "$cvm_ip"
}

_render_compose() {
  local file="$1"
  env \
    CHAIN_ID="$CHAIN_ID" \
    RPC_URL="$RPC_URL" \
    BUNDLER_URL="${BUNDLER_URL:-$RPC_URL}" \
    GAS_POLICY_ID="${GAS_POLICY_ID:-}" \
    INDEXER_REGISTRY_ADDR="$INDEXER_REGISTRY_ADDR" \
    GATEWAY_DOMAIN="$GATEWAY_DOMAIN" \
    TEE_DAEMON_TOKEN="$TEE_DAEMON_TOKEN" \
    WEBHOST_MCP_TOKEN="$WEBHOST_MCP_TOKEN" \
    GITHUB_ID="$GITHUB_ID" \
    GITHUB_SECRET="$GITHUB_SECRET" \
    NEXTAUTH_SECRET="$NEXTAUTH_SECRET" \
    NEXTAUTH_URL="$NEXTAUTH_URL" \
    APP_DOMAIN="$APP_DOMAIN" \
    DIRECTORY_HOST="$DIRECTORY_HOST" \
    CONSOLE_HOST="$CONSOLE_HOST" \
    WEBHOST_ADMIN_HOST="$WEBHOST_ADMIN_HOST" \
    ACME_EMAIL="$ACME_EMAIL" \
    REDPILL_API_KEY="$REDPILL_API_KEY" \
    REDPILL_BASE_URL="$REDPILL_BASE_URL" \
    REDPILL_MODEL="$REDPILL_MODEL" \
    VENICE_API_KEY="$VENICE_API_KEY" \
    VENICE_BASE_URL="$VENICE_BASE_URL" \
    VENICE_MODEL="$VENICE_MODEL" \
    RUNYARD_HUB_URL="${RUNYARD_HUB_URL:-}" \
    RUNYARD_HUB_TOKEN="${RUNYARD_HUB_TOKEN:-}" \
    RUNYARD_CALLBACK_SECRET="$RUNYARD_CALLBACK_SECRET" \
    RUNYARD_CALLBACK_URL="$RUNYARD_CALLBACK_URL" \
    CLOUDFLARE_API_TOKEN="$CLOUDFLARE_API_TOKEN" \
    CLOUDFLARE_SYNCLAVE_API_TOKEN="$CLOUDFLARE_SYNCLAVE_API_TOKEN" \
    BACKUP_STORAGE="${BACKUP_STORAGE:-}" \
    BACKUP_S3_ENDPOINT="${BACKUP_S3_ENDPOINT:-}" \
    BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:-}" \
    BACKUP_S3_REGION="${BACKUP_S3_REGION:-auto}" \
    BACKUP_S3_ACCESS_KEY_ID="${BACKUP_S3_ACCESS_KEY_ID:-}" \
    BACKUP_S3_SECRET_ACCESS_KEY="${BACKUP_S3_SECRET_ACCESS_KEY:-}" \
    BACKUP_PREFIX="${BACKUP_PREFIX:-webhost}" \
    BACKUP_APP_ID="${BACKUP_APP_ID:-${X:-}}" \
    BACKUP_INTERVAL_SECONDS="${BACKUP_INTERVAL_SECONDS:-3600}" \
    docker compose -f "$file" config --quiet
}

preflight() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  [ -z "${BOX_FRESH_DISK:-}" ] || die "BOX_FRESH_DISK is forbidden for the state-preserving Webhost migration"
  [ -f "$COMPOSE" ] && [ ! -L "$COMPOSE" ] || die "candidate Compose must be a regular non-symlink file"
  [ -f "$ROLLBACK_COMPOSE" ] && [ ! -L "$ROLLBACK_COMPOSE" ] || die "rollback Compose must be a regular non-symlink file"
  local command image first_name second_name candidate_hash rollback_hash saved_compose
  for command in cast cosign docker jq scp ssh; do
    command -v "$command" >/dev/null 2>&1 || die "required command unavailable: $command"
  done
  [[ "$ACME_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]] || die "ACME_EMAIL must be a valid email address"
  local secret_names=(TEE_DAEMON_TOKEN WEBHOST_MCP_TOKEN RUNYARD_HUB_TOKEN RUNYARD_CALLBACK_SECRET)
  local first second
  for ((first = 0; first < ${#secret_names[@]}; first++)); do
    first_name="${secret_names[$first]}"
    [ -n "${!first_name:-}" ] || continue
    for ((second = first + 1; second < ${#secret_names[@]}; second++)); do
      second_name="${secret_names[$second]}"
      if [ -n "${!second_name:-}" ] && [ "${!first_name}" = "${!second_name}" ]; then
        die "security boundary secrets must be distinct: $first_name and $second_name"
      fi
    done
  done
  grep -Fq "$WEBHOST_CONTROL_IMAGE" "$COMPOSE" || die "candidate Compose does not bind the approved control-plane digest"
  grep -Fq "$WEBHOST_STORAGE_IMAGE" "$COMPOSE" || die "candidate Compose does not bind the approved storage-helper digest"
  grep -Fq "$WEBHOST_TLS_IMAGE" "$COMPOSE" || die "candidate Compose does not bind the approved TLS-proxy digest"
  grep -Fq "WEBHOST_VERSION: $WEBHOST_RELEASE_VERSION" "$COMPOSE" || die "candidate Compose does not bind the release version"
  grep -Fq "WEBHOST_BUILD_COMMIT: $WEBHOST_RELEASE_COMMIT" "$COMPOSE" || die "candidate Compose does not bind the release commit"
  _render_compose "$COMPOSE" || die "candidate Compose failed semantic rendering"
  _render_compose "$ROLLBACK_COMPOSE" || die "rollback Compose failed semantic rendering"
  for image in "$WEBHOST_CONTROL_IMAGE" "$WEBHOST_STORAGE_IMAGE" "$WEBHOST_TLS_IMAGE"; do
    cosign verify \
      --certificate-identity "$WEBHOST_RELEASE_IDENTITY" \
      --certificate-oidc-issuer "$WEBHOST_RELEASE_ISSUER" \
      "$image" >/dev/null || die "Cosign verification failed for an approved Webhost image"
  done
  ssh_box true >/dev/null || die "production box is unreachable: $BOX_HOST"
  saved_compose="$COMPOSE"
  candidate_hash=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  COMPOSE="$ROLLBACK_COMPOSE"
  rollback_hash=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  COMPOSE="$saved_compose"
  [ -n "$candidate_hash" ] && [ -n "$rollback_hash" ] || die "could not compute candidate and rollback compose hashes"
  log "✔ Webhost ${WEBHOST_RELEASE_VERSION} preflight candidate=0x${candidate_hash} rollback=0x${rollback_hash} vm=${VM_ID}"
}

_upgrade_compose() {
  local file="$1" label="$2" saved_compose="$COMPOSE" nh allowed out j mode
  [ -z "${BOX_FRESH_DISK:-}" ] || die "BOX_FRESH_DISK is forbidden for the state-preserving Webhost migration"
  COMPOSE="$file"
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  if [ -z "$nh" ]; then
    COMPOSE="$saved_compose"
    return 1
  fi
  log "$label compose_hash=0x$nh"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" != true ]; then
    if ! send_seq "webhost-${label}-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"; then
      COMPOSE="$saved_compose"
      return 1
    fi
  else
    log "$label compose hash already allowlisted"
  fi
  if ! out=$(_box_run update "$X" "$VM_ID"); then
    COMPOSE="$saved_compose"
    return 1
  fi
  COMPOSE="$saved_compose"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r '.compose_hash // empty')
  [ -n "$H" ] || H="$nh"
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  _save
  log "✔ $label complete mode=$mode vm=$VM_ID compose_hash=0x${H#0x}"
}

_candidate_smoke() {
  local cvm_ip i ready substrate unauth wrong authorized
  for i in $(seq 1 36); do
    cvm_ip=$(_cvm_ip)
    if [ -n "$cvm_ip" ]; then
      ready=$(ssh_box "curl -sS --max-time 10 --resolve ${WEBHOST_ADMIN_HOST}:443:${cvm_ip} https://${WEBHOST_ADMIN_HOST}/readyz" 2>/dev/null || true)
      substrate=$(ssh_box "curl -sS --max-time 10 --resolve ${DIRECTORY_HOST}:443:${cvm_ip} https://${DIRECTORY_HOST}/_api/substrate" 2>/dev/null || true)
      unauth=$(ssh_box "curl -sS -o /dev/null -w '%{http_code}' --max-time 10 --resolve ${WEBHOST_ADMIN_HOST}:443:${cvm_ip} https://${WEBHOST_ADMIN_HOST}/_api/projects" 2>/dev/null || true)
      wrong=$(ssh_box "curl -sS -o /dev/null -w '%{http_code}' --max-time 10 --resolve ${WEBHOST_ADMIN_HOST}:443:${cvm_ip} -H 'Authorization: Bearer deliberately-wrong-token' https://${WEBHOST_ADMIN_HOST}/_api/projects" 2>/dev/null || true)
      authorized=$(printf '%s\n' "$TEE_DAEMON_TOKEN" | ssh_box "read -r token; curl -sS -o /dev/null -w '%{http_code}' --max-time 10 --resolve ${WEBHOST_ADMIN_HOST}:443:${cvm_ip} -H \"Authorization: Bearer \$token\" https://${WEBHOST_ADMIN_HOST}/_api/projects" 2>/dev/null || true)
      if printf '%s' "$ready" | jq -e '.ready == true' >/dev/null 2>&1 \
        && printf '%s' "$substrate" | jq -e --arg version "$WEBHOST_RELEASE_VERSION" --arg commit "$WEBHOST_RELEASE_COMMIT" '.version == $version and .buildCommit == $commit' >/dev/null 2>&1 \
        && [ "$unauth" = 401 ] && [ "$wrong" = 403 ] && [ "$authorized" = 200 ]; then
        log "✔ Webhost ${WEBHOST_RELEASE_VERSION} ready on ${cvm_ip}; auth boundaries and build commit verified"
        return 0
      fi
    fi
    log "… Webhost ${WEBHOST_RELEASE_VERSION} candidate not ready ($i/36)"
    sleep 10
  done
  return 1
}

verify_release() {
  _load; _require_env
  _candidate_smoke || die "Webhost ${WEBHOST_RELEASE_VERSION} readiness/version/auth smoke failed"
}

rollback_member() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  _upgrade_compose "$ROLLBACK_COMPOSE" rollback || die "state-preserving Webhost rollback failed"
  verify_app
  verify_daemon
  log "✔ legacy Webhost topology restored from the quiesced migration snapshot"
}

update_member() {
  preflight
  _upgrade_compose "$COMPOSE" update || die "in-place Webhost ${WEBHOST_RELEASE_VERSION} update failed"
  if _candidate_smoke; then
    log "✔ Webhost ${WEBHOST_RELEASE_VERSION} production update complete"
    return 0
  fi
  log "candidate smoke failed; executing automatic same-VM rollback"
  rollback_member
  die "Webhost ${WEBHOST_RELEASE_VERSION} failed smoke and was rolled back"
}

log "=== Open webhost AttestMesh node: $NODE ==="
case "$ACTION" in
  deploy) deploy_cvm ;;
  cluster) deploy_cluster ;;
  patha) patha_upgrade ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  verify-app) verify_app ;;
  verify-daemon) verify_daemon ;;
  verify-release) verify_release ;;
  preflight) preflight ;;
  update) update_member ;;
  rollback) rollback_member ;;
  setup) deploy_cvm; deploy_cluster; patha_upgrade; prime_gate; bind_member ;;
  all) deploy_cvm; deploy_cluster; patha_upgrade; prime_gate; bind_member; verify ;;
  *) die "usage: webhost-node.sh <node-name> [preflight|deploy|cluster|patha|prime|bind|verify|verify-app|verify-daemon|verify-release|update|rollback|setup|all]" ;;
esac
