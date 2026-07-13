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

NODE="${1:?usage: webhost-node.sh <node-name> [deploy|cluster|patha|prime|bind|verify|verify-app|verify-daemon|update|setup|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
BOX_KMS_ROOT_SIGNER="${BOX_KMS_ROOT_SIGNER:-0x7fa63d99495be2129cf28eee54e2ef2724e3aa2e}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/webhost-node.yaml}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
RECEIPT="$ROOT/contracts/script/deployments/${CHAIN_ID}.json"

SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/webhost.env}"
SYNCLAVE_SECRETS="${SYNCLAVE_SECRETS:-$HOME/.attestmesh/synclave.env}"
REDPILL_KEY_FILE="${REDPILL_KEY_FILE:-$HOME/.attestmesh/redpill-key}"
CLOUDFLARE_TOML="${CLOUDFLARE_TOML:-$HOME/.attestmesh/cloudflare-attestmesh-xyz.toml}"
CLOUDFLARE_SYNCLAVE_TOML="${CLOUDFLARE_SYNCLAVE_TOML:-$HOME/.attestmesh/cloudflare-synclave-net.toml}"

APP_DOMAIN="${APP_DOMAIN:-app.s.n}"
DIRECTORY_HOST="${DIRECTORY_HOST:-apps.synclave.net}"
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
  for k in TEE_DAEMON_TOKEN WEBHOST_MCP_TOKEN NEXTAUTH_SECRET GITHUB_ID GITHUB_SECRET CLOUDFLARE_API_TOKEN CLOUDFLARE_SYNCLAVE_API_TOKEN REDPILL_API_KEY VENICE_API_KEY RUNYARD_CALLBACK_SECRET; do
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
  if [ -z "$cvm_ip" ] && [ -n "${VM_ID:-}" ]; then
    cvm_ip=$(ssh_box "mac=\$(pgrep -af qemu-system | grep '/${VM_ID}/' | sed -nE 's/.*mac=([0-9a-f:]{17}).*/\\1/p' | head -1); [ -n \"\$mac\" ] && sudo awk -v mac=\"\$mac\" '\$2 == mac { print \$3 }' /var/lib/misc/dnsmasq-dstack-br0.leases | tail -1" 2>/dev/null || true)
  fi
  [ -n "$cvm_ip" ] || die "could not resolve Webhost CVM bridge IP; set WEBHOST_CVM_IP"
  for i in $(seq 1 30); do
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

update_member() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  local nh allowed out j mode
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" != true ]; then
    send_seq "webhost-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  else
    log "compose hash already allowlisted"
  fi
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r '.vm_id // empty'); [ -n "$VM_ID" ] || { _load; : "${VM_ID:=}"; }
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ webhost node update complete mode=$mode vm=$VM_ID"
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
  update) update_member ;;
  setup) deploy_cvm; deploy_cluster; patha_upgrade; prime_gate; bind_member ;;
  all) deploy_cvm; deploy_cluster; patha_upgrade; prime_gate; bind_member; verify ;;
  *) die "usage: webhost-node.sh <node-name> [deploy|cluster|patha|prime|bind|verify|verify-app|verify-daemon|update|setup|all]" ;;
esac
