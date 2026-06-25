#!/usr/bin/env bash
# Matrix node (single-node app) bring-up on the SELF-HOSTED on-chain dstack box (attestmesh.xyz).
# Mirrors node-pathA.sh but for the BOX's MCP deploy_app flow (not Phala). Order-sensitive, logged,
# re-entrant via a state file. Encodes every fix from deploy/matrix-node-steps-log.md.
#
#   deploy  → register a stock DstackApp + seal env + CreateVm on the box (app_id X + compose_hash H),
#             then WAIT for synapse /_matrix to be live. (The first member of a fresh cluster is the
#             IMMUTABLE CSK originator and the contracts have no removeMember, so the node must be
#             fully working BEFORE it registers.)
#   cluster → DeployCluster diamond (this machine; kmsRootSigner = THIS box's; initialComposeHashes=[H]).
#   patha   → onchain.sh patha-upgrade (Path-A DstackFacet + ClusterMember impl, cut in). [needed: the
#             8453.json clusterMemberImpl lacks reinitializeFromDstackApp]
#   prime   → addAllowedAppId(cluster, X)   (hash already seeded at `cluster`).
#   bind    → on the box (box deployer key): upgradeToAndCall(X, impl, reinitializeFromDstackApp(cluster)).
#   verify  → poll memberCount/memberIdOf(X) + Synapse /versions over the tailnet.
#   verify-agent     → matrix-admin-agent /healthz (admin token + matrix sync + EGRESS LOCKED).
#   verify-client    → the EXACT Element path: login → follow the login well_known → initial sync 200
#                      (catches a bad public_baseurl that would hang clients on "Syncing").
#   verify-isolation → from the BOX, the CVM's private ports must REFUSE (host-isolation invariant).
#   update  → IN-PLACE roll (new compose/env) REUSING X: addComposeHash(cluster, H') → stop old CVM →
#             CreateVm(app_id=X). Preserves membership + CSK originator. No new cluster. Self-verifies
#             agent + client + isolation after the roll.
#
#   source deploy/env.sh \
#     && TS_AUTHKEY=tskey-… [BOX_KMS_ROOT_SIGNER=0x7fa6… MESH_CIDR_IP=…] \
#        deploy/matrix-node.sh <node-name> \
#          [all|deploy|cluster|patha|prime|bind|verify|verify-agent|verify-client|verify-isolation|update]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: matrix-node.sh <node-name> [all|deploy|cluster|patha|prime|bind|verify|update]}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
BOX_KMS_ROOT_SIGNER="${BOX_KMS_ROOT_SIGNER:-0x7fa63d99495be2129cf28eee54e2ef2724e3aa2e}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/matrix-node.yaml}"
MESH_CIDR_IP="${MESH_CIDR_IP:-168951808}"          # 10.18.0.0/16 — pick a UNIQUE /16 per cluster
MESH_CIDR_PREFIX="${MESH_CIDR_PREFIX:-16}"
export BOX_VCPU="${BOX_VCPU:-4}" BOX_MEM="${BOX_MEM:-8192}" BOX_DISK="${BOX_DISK:-60}"
# Bridge networking: the CVM is routable (TAP on the host bridge dstack-br0) so its OWN Tailscale gets a
# DIRECT path (fast, no DERP relay). No host port-maps (forward_service_enabled=false → none created),
# so the host opens nothing toward the CVM; KMS reached via the host DNAT 10.0.2.2:9101 (cert SAN).
export BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
export BOX_PORTS="${BOX_PORTS:-[]}"
# Matrix is PRIVATE: gateway OFF (never publish to the public dstack gateway); reachable only over the
# tailnet. Override BOX_GATEWAY_ENABLED=true only to re-expose intentionally.
export BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-false}"
# Bridge-mode CVMs have NO host port-maps → verify reaches the live CVM over the TAILNET (this host has a
# direct path). _cvm_fqdn returns the matrix-attestmesh* peer whose Synapse answers.
TS_SUFFIX="${TS_SUFFIX:-tail39cb2e.ts.net}"
_cvm_fqdn() {
  local n
  for n in $(tailscale status 2>/dev/null | awk 'tolower($2) ~ /^matrix-attestmesh/ {print $2}'); do
    curl -sS --max-time 6 "https://$n.$TS_SUFFIX/_matrix/client/versions" 2>/dev/null | grep -q '"versions"' && { echo "$n.$TS_SUFFIX"; return 0; }
  done
  return 1
}
GW_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
# ── wal-g → Cloudflare R2 backups (deploy/postgres-walg). OFF by default; set BACKUP_ENABLED=true to turn
#    on encrypted base+WAL PITR backups. R2 creds are read from the toml below (NEVER committed) and sealed
#    into the CVM at deploy; the backup is encrypted with the cluster shared key (fetched in-CVM), so a
#    re-provisioned node of the same app_id can decrypt it (recovery from total CVM loss). BACKUP_PREFIX
#    defaults to the app_id → backups live under <bucket>/<app_id>/. ──
export BACKUP_ENABLED="${BACKUP_ENABLED:-false}"
BACKUP_CREDS="${BACKUP_CREDS:-$HOME/.attestmesh/matrix-node-backups.toml}"
_load_backup_creds() {
  [ -f "$BACKUP_CREDS" ] || die "BACKUP_ENABLED=true but no R2 creds at $BACKUP_CREDS (see deploy/matrix-node-deploy.md)"
  export R2_ENDPOINT="$(sed -nE 's/^endpoint *= *"?([^"]+)"?.*/\1/p' "$BACKUP_CREDS")"
  export R2_BUCKET="$(sed -nE 's/^bucket *= *"?([^"]+)"?.*/\1/p' "$BACKUP_CREDS")"
  export R2_REGION="$(sed -nE 's/^region *= *"?([^"]+)"?.*/\1/p' "$BACKUP_CREDS")"; : "${R2_REGION:=auto}"
  export R2_ACCESS_KEY_ID="$(sed -nE 's/^access_key_id *= *"?([^"]+)"?.*/\1/p' "$BACKUP_CREDS")"
  export R2_SECRET_ACCESS_KEY="$(sed -nE 's/^secret_access_key *= *"?([^"]+)"?.*/\1/p' "$BACKUP_CREDS")"
  [ -n "$R2_ENDPOINT" ] && [ -n "$R2_ACCESS_KEY_ID" ] && [ -n "$R2_SECRET_ACCESS_KEY" ] || die "R2 creds incomplete in $BACKUP_CREDS"
}
_prep_backup_env() {   # called before a deploy/update: load creds + default the prefix when backups are on
  [ "${BACKUP_ENABLED:-false}" = "true" ] || return 0
  _load_backup_creds
  local p="${BACKUP_PREFIX:-${X#0x}}"; export BACKUP_PREFIX="$(printf '%s' "$p" | tr 'A-Z' 'a-z')"
  log "backups ENABLED → R2 bucket=$R2_BUCKET prefix=$BACKUP_PREFIX"
}
RECEIPT="$ROOT/contracts/script/deployments/${CHAIN_ID}.json"
STATE="$LOGDIR/matrix-node-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000

_save() { printf 'X=%s\nH=%s\nVM_ID=%s\nCLUSTER=%s\nMEMBER_IMPL=%s\nPGPW=%s\nIAPW=%s\n' \
  "${X:-}" "${H:-}" "${VM_ID:-}" "${CLUSTER:-}" "${MEMBER_IMPL:-}" "${PGPW:-}" "${IAPW:-}" > "$STATE"; }
_load() { [ -f "$STATE" ] && source "$STATE" || true; }

ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }
_vmm() { ssh_box "curl -s --max-time 30 http://127.0.0.1:9080/prpc/$1?json -H 'content-type: application/json' -d '$2'"; }

send_seq() {  # cast send with a FRESHLY-fetched nonce + one retry — handles RPC nonce lag right
              # after forge scripts (DeployCluster/patha) where `cast nonce` can read stale.
  local label="$1"; shift
  local nonce
  nonce=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL")
  run_step "$label" cast send "$@" --nonce "$nonce" --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY" && return 0
  log "↻ $label: refetching nonce + retrying (likely RPC nonce lag after a forge script)"
  sleep 4; nonce=$(cast nonce "$DEPLOYER_ADDR" --rpc-url "$RPC_URL")
  run_step "${label}-retry" cast send "$@" --nonce "$nonce" --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY"
}

# Run the box-side helper (matrix-node-box.py) with the sealed secret env passed over SSH (in-memory,
# never written to disk). Usage: _box_run <deploy|hash|update> [app_id]. Echoes the helper's stdout.
_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok
  : "${TS_AUTHKEY:?set TS_AUTHKEY (operator tailscale key)}"
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/matrix-node-box.py" "$BOX_HOST:/tmp/matrix-node-box.py"
  ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' \
    E_RPC_URL='$RPC_URL' E_BUNDLER_URL='${BUNDLER_URL:-$RPC_URL}' E_GAS_POLICY_ID='${GAS_POLICY_ID:-}' \
    E_POSTGRES_PASSWORD='$PGPW' E_TS_AUTHKEY='$TS_AUTHKEY' E_DSTACK_DOCKER_USERNAME='${guser:-dmvt}' E_DSTACK_DOCKER_PASSWORD='$gtok' \
    E_BOT_USERNAME='${BOT_USERNAME:-admin-agent}' E_BOT_PASSWORD='${BOT_PASSWORD:-}' \
    E_MATRIX_ADMIN_MXIDS='${MATRIX_ADMIN_MXIDS:-}' E_MATRIX_ADMIN_SENDERS='${MATRIX_ADMIN_SENDERS:-}' \
    E_INITIAL_ADMIN='${INITIAL_ADMIN:-}' E_INITIAL_ADMIN_PASSWORD='${INITIAL_ADMIN_PASSWORD:-}' \
    E_LLM_BASE_URL='${LLM_BASE_URL:-}' E_LLM_MODEL='${LLM_MODEL:-}' E_LLM_API_KEY='${LLM_API_KEY:-}' \
    E_BACKUP_ENABLED='${BACKUP_ENABLED:-false}' E_BACKUP_PREFIX='${BACKUP_PREFIX:-}' E_BACKUP_RESTORE='${BACKUP_RESTORE:-}' E_BACKUP_RESTORE_TARGET_TIME='${BACKUP_RESTORE_TARGET_TIME:-}' \
    E_R2_ENDPOINT='${R2_ENDPOINT:-}' E_R2_BUCKET='${R2_BUCKET:-}' E_R2_REGION='${R2_REGION:-}' E_R2_ACCESS_KEY_ID='${R2_ACCESS_KEY_ID:-}' E_R2_SECRET_ACCESS_KEY='${R2_SECRET_ACCESS_KEY:-}' \
    $BOX_PY /tmp/matrix-node-box.py $mode $app_id $vm_id"
}

# Required matrix-admin-agent env (secrets in-memory, like TS_AUTHKEY). MATRIX_ADMIN_SENDERS +
# INITIAL_ADMIN are optional (empty → on-chain channel off / no declared admin).
_require_agent_env() {
  local v missing=""
  for v in BOT_PASSWORD MATRIX_ADMIN_MXIDS LLM_BASE_URL LLM_MODEL LLM_API_KEY; do
    [ -n "${!v:-}" ] || missing="$missing $v"
  done
  [ -z "$missing" ] || die "missing required matrix-admin-agent env:$missing  (pass them in the invocation, e.g. BOT_PASSWORD=… LLM_API_KEY=…)"
}

# Stable initial-admin password: generated ONCE (if INITIAL_ADMIN is set and none was provided),
# persisted in the state file, and reused across rolls so the operator's login doesn't change.
_ensure_iapw() {
  if [ -n "${INITIAL_ADMIN:-}" ] && [ -z "${INITIAL_ADMIN_PASSWORD:-}" ]; then
    INITIAL_ADMIN_PASSWORD="${IAPW:-$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 28)}"
  fi
  IAPW="${INITIAL_ADMIN_PASSWORD:-}"; export INITIAL_ADMIN_PASSWORD
}

# Poll the box loopback (8080 → nginx → Matrix) for synapse readiness.
_wait_synapse() {
  local i fqdn
  for i in $(seq 1 60); do
    fqdn=$(_cvm_fqdn) && { CVM_FQDN="$fqdn"; log "✔ synapse live over tailnet ($fqdn)"; return 0; }
    log "… synapse not reachable on tailnet yet ($i/60)"; sleep 12
  done
  die "synapse never reachable over the tailnet — check tailscale join / vm_logs $VM_ID"
}

# 1. Register stock DstackApp + CreateVm via the box, then wait for Matrix to be live.
deploy_cvm() {
  _load; _require_agent_env; _ensure_iapw; _prep_backup_env
  PGPW="${PGPW:-$(openssl rand -hex 24)}"; _save
  log "▶ box deploy_app node=$NODE compose=$COMPOSE"
  local out; out=$(_box_run deploy) || die "box deploy failed"
  local j; j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id); H=$(echo "$j" | jq -r .compose_hash); VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  local xb="${X#0x}"
  log "✔ deployed app_id(X)=$X compose_hash(H)=$H vm=$VM_ID  →  https://${xb,,}.${GW_DOMAIN}"
  _wait_synapse
}

# 2. DeployCluster diamond — kmsRootSigner = THIS box's; compose hash H seeded at init.
deploy_cluster() {
  _load; [ -n "${H:-}" ] || die "no compose_hash; run 'deploy' first"
  local salt cfg lf; salt=$(cast keccak "attestmesh-cluster-${NODE}")
  cfg="$ROOT/contracts/script/clusters/${NODE}.json"; lf="$LOGDIR/matrix-cluster-${NODE}.$(ts).log"
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
  _save; log "✔ cluster=$CLUSTER"
}

# 3. Path-A facet + a fresh ClusterMember impl (the 8453.json impl lacks reinitializeFromDstackApp).
patha_upgrade() {
  _load; [ -n "${CLUSTER:-}" ] || die "no cluster; run 'cluster' first"
  bash -c "cd '$ROOT' && source deploy/env.sh >/dev/null && deploy/onchain.sh patha-upgrade $CLUSTER" || die "patha-upgrade failed"
  local b="$ROOT/contracts/broadcast/UpgradeDstackFacetPathA.s.sol/${CHAIN_ID}/run-latest.json"
  MEMBER_IMPL=$(jq -r '[.transactions[]|select(.transactionType=="CREATE" and .contractName=="ClusterMember")][-1].contractAddress' "$b" 2>/dev/null)
  [ -n "$MEMBER_IMPL" ] && [ "$MEMBER_IMPL" != null ] || die "could not parse ClusterMember impl from $b"
  _save; log "✔ patha-upgrade done; MEMBER_IMPL=$MEMBER_IMPL"
}

# 4. Prime the cluster gate (app_id; hash already seeded at `cluster`).
prime_gate() {
  _load; [ -n "${CLUSTER:-}" ] && [ -n "${X:-}" ] || die "need cluster+app (run deploy+cluster first)"
  send_seq "prime-allowAppId-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  log "allowedAppIds($X)=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL")"
}

# 5. Upgrade stock proxy X → ClusterMember + bind cluster (on the box, box deployer key).
bind_member() {
  _load; [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit; reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind X=$X → impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/matrix-bind-${NODE}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --rpc-url $BOX_RPC --private-key "\$KEY" 2>&1 | grep -iE "^status|^transactionHash|error|FailedCall" | head -3
SCRIPT
  local c=""
  for _ in 1 2 3 4 5 6 7 8; do
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    [ "${c,,}" = "${CLUSTER,,}" ] && break
  done
  log "X.cluster()=$c (expect $CLUSTER)"
  [ "${c,,}" = "${CLUSTER,,}" ] || die "bind did not stick (X.cluster()=$c)"
  log "✔ bound X → $CLUSTER"
}

# 6. Poll the chain for the sidecar's self-registration; show sidecar + Matrix health.
verify() {
  _load; [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ $NODE registered: memberId=$id memberCount=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL")"
      local fqdn="${CVM_FQDN:-$(_cvm_fqdn)}"
      log "  matrix:  $(curl -sS --max-time 6 "https://${fqdn:-unknown}/_matrix/client/versions" 2>/dev/null | head -c 60)"
      log "  url:     https://${fqdn:-<tailnet>}/  (private tailnet — NOT the public gateway)"
      return 0
    fi
    log "… not registered yet ($i/45, memberCount=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null))"
    sleep 20
  done
  die "$NODE not registered after timeout"
}

# 6b. Verify the matrix-admin-agent. The CVM is a sealed TEE (no exec; the box can't fetch
# per-container logs — steps-log §6), so the agent SELF-checks its egress and folds the result into
# /healthz (exposed on the box loopback like the sidecar). A `"status":"ok"` means: admin token
# acquired + Matrix synced + EGRESS CONFIRMED LOCKED. If egress isn't locked the agent fails closed
# (exits, no /healthz) — so this poll catches an un-firewalled agent.
verify_agent() {
  _load; [ -n "${X:-}" ] || die "need X (run deploy first)"
  local i body fqdn="${CVM_FQDN:-$(_cvm_fqdn)}"
  [ -n "$fqdn" ] || die "no tailnet FQDN for the CVM (synapse not reachable over the tailnet?)"
  for i in $(seq 1 45); do
    body=$(curl -sS --max-time 6 "https://$fqdn/_agent/healthz" 2>/dev/null)
    if echo "$body" | grep -q '"status": *"ok"'; then
      log "✔ matrix-admin-agent ready (admin token + matrix sync + egress LOCKED) over tailnet"
      return 0
    fi
    log "… agent not ready ($i/45): ${body:-<no response>}"
    sleep 12
  done
  die "matrix-admin-agent never reported ready over the tailnet — vm_logs $VM_ID; check agent bootstrap + agent-egress-fw (fails CLOSED if egress not locked)"
}

# 6c. Verify the CLIENT path the way Element does. Synapse echoes public_baseurl back in the LOGIN
# response's m.homeserver well_known, and a Matrix client SWITCHES its base_url to whatever that says —
# so a bad public_baseurl (e.g. the now-dead public gateway) makes Element hang forever on "Syncing"
# even though the server is perfectly healthy. This logs in, follows that login well_known, and runs the
# initial sync against it — the exact sequence Element runs — and asserts it lands on the LIVE tailnet
# URL (the nginx homeserver.invalid→$host rewrite), not the sentinel and not the gateway domain.
# Uses INITIAL_ADMIN's localpart + the initial-admin password (IAPW from the state file).
verify_client() {
  _load; local fqdn="${CVM_FQDN:-$(_cvm_fqdn)}"
  [ -n "$fqdn" ] || die "no tailnet FQDN for the CVM (synapse not reachable over the tailnet?)"
  local swk; swk=$(curl -sS --max-time 8 "https://$fqdn/.well-known/matrix/client" 2>/dev/null)
  echo "$swk" | grep -q "$fqdn" || die "served /.well-known/matrix/client does not reflect \$host: $swk"
  log "✔ served client well-known → $fqdn (CORS path)"
  local user pw resp token wk base code
  user=$(printf '%s' "${INITIAL_ADMIN:-}" | sed -nE 's/^@([^:]+):.*/\1/p')
  pw="${INITIAL_ADMIN_PASSWORD:-${IAPW:-}}"
  if [ -z "$user" ] || [ -z "$pw" ]; then
    log "⚠ no INITIAL_ADMIN/IAPW available — skipping the authenticated login→well_known→sync check"; return 0
  fi
  resp=$(curl -sS --max-time 12 -X POST "https://$fqdn/_matrix/client/v3/login" -H 'content-type: application/json' \
    -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"$user\"},\"password\":\"$pw\"}")
  token=$(printf '%s' "$resp" | jq -r '.access_token // empty')
  wk=$(printf '%s' "$resp" | jq -r '.well_known."m.homeserver".base_url // empty' | sed 's:/*$::')
  [ -n "$token" ] || die "login failed for '$user' (check INITIAL_ADMIN / IAPW): $resp"
  log "login well_known base_url = ${wk:-<none>}"
  case "$wk" in
    "https://$fqdn")          : ;;  # exactly the live tailnet URL — correct
    "")                        log "⚠ login response carries no well_known (client keeps the URL it used)" ;;
    *homeserver.invalid*)      die "login well_known leaks the sentinel — nginx sub_filter not applied" ;;
    *gateway.attestmesh.xyz*)  die "login well_known points at the DEAD gateway — public_baseurl fix missing → clients hang on Syncing" ;;
    *)                         die "login well_known points off-tailnet ($wk) — clients will follow it and fail" ;;
  esac
  base="${wk:-https://$fqdn}"
  code=$(curl -sS --max-time 12 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" "$base/_matrix/client/v3/sync?timeout=0")
  [ "$code" = 200 ] || die "initial sync against the login well_known ($base) returned $code — Element would hang on 'Syncing'"
  log "✔ client path OK: login → well_known ($base) → initial sync 200 (Element leaves the Syncing screen)"
}

# 6d. Host-isolation invariant: from the BOX, the CVM's private services must be UNREACHABLE. Bridge
# mode + forward_service_enabled=false (global) + no compose ports ⇒ the host opens nothing toward the
# CVM. Maps the CVM's qemu (by VM_ID) → its TAP MAC → bridge IP, and asserts every private port refuses.
verify_isolation() {
  _load; [ -n "${VM_ID:-}" ] || die "no VM_ID in state (run deploy/update first)"
  log "▶ host-isolation check for vm=$VM_ID"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/matrix-isolation-${NODE}.$(ts).log"
set -u
VMID="$VM_ID"
MAC=\$(ps -eo args | grep -F "\$VMID" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2)
[ -n "\$MAC" ] || { echo "ISOLATION: could not find qemu for \$VMID"; exit 3; }
IP=\$(ip neigh show dev dstack-br0 | grep -i "\$MAC" | grep -oE '^10\.0\.[0-9]+\.[0-9]+' | head -1)
[ -n "\$IP" ] || { echo "ISOLATION: no bridge IP for MAC \$MAC yet (CVM mid-boot?)"; exit 4; }
echo "ISOLATION: vm=\$VMID mac=\$MAC bridge_ip=\$IP"
bad=0
for p in 80 443 9100 9090 51900; do
  if curl -sS --max-time 3 -o /dev/null "http://\$IP:\$p/" 2>/dev/null; then
    echo "  !! \$IP:\$p REACHABLE from host — INVARIANT VIOLATION"; bad=1
  else echo "  \$IP:\$p refused from host (good)"; fi
done
[ \$bad -eq 0 ] && echo "ISOLATION: PASS — host opens nothing toward the CVM" || { echo "ISOLATION: FAIL"; exit 5; }
SCRIPT
  local rc=${PIPESTATUS[0]}
  [ "$rc" = 0 ] || die "host-isolation check failed (rc=$rc) — see the log above"
  log "✔ host-isolation invariant holds (CVM private ports refuse from the host)"
}

# Day-2: roll a new compose/env onto the LIVE node, REUSING its app_id (keeps membership + CSK
# originator). Allowlists the new hash FIRST, stops the old CVM, then CreateVm(app_id=X).
update_member() {
  _load; [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster (do a full deploy first)"
  _require_agent_env; _ensure_iapw; _prep_backup_env
  PGPW="${PGPW:-$(openssl rand -hex 24)}"
  local nh; nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  if [ "$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL")" = "true" ]; then
    log "hash already allowlisted"
  else
    send_seq "update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  # box.py owns the VM lifecycle: with VM_ID it does StopVm→UpgradeApp→StartVm IN PLACE (keeps the disk →
  # data + tailscale name survive); BOX_FRESH_DISK=1 forces a fresh-disk CreateVm (deliberate wipe).
  local out; out=$(_box_run update "$X" "${VM_ID:-}") || die "in-place update failed"
  log "$out"
  echo "$out" | grep -qE '"(upgrade_status|createvm_status)": *200' || die "vmm update did not return 200 — see output above"
  local mode; mode=$(echo "$out" | grep -oE '"mode": *"[^"]*"' | sed -E 's/.*"mode": *"([^"]*)".*/\1/')
  VM_ID=$(echo "$out" | grep -oE '"vm_id": *"[^"]*"' | head -1 | sed -E 's/.*"vm_id": *"([^"]*)".*/\1/')
  H="$nh"; _save
  if [ "$mode" = upgrade ]; then
    log "✔ in-place UpgradeApp: app X=$X vm=$VM_ID — DISK + DATA PRESERVED (membership/CSK kept). Waiting for synapse…"
  else
    log "✔ fresh-disk CreateVm: app X=$X vm=$VM_ID — data wiped (membership/CSK kept). Waiting for synapse…"
  fi
  _wait_synapse
  verify_agent
  verify_client
  verify_isolation
}

# Day-2 DISASTER RECOVERY (manual): redeploy onto a FRESH disk and restore Postgres from R2 — the latest
# base backup + WAL replay to a point-in-time (RESTORE_TO=<SQL timestamp>, else the latest WAL). Reuses the
# app_id so the node keeps its on-chain identity; the CSK is re-derived in-CVM to decrypt. NOTE: only the
# Postgres DB is restored — synapse-data (signing key) + tailscale-state are NOT in the backup, so the node
# comes back with a fresh signing key + a new tailnet name (expected for full recovery).
restore_member() {
  _load; [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster (deploy first)"
  [ "${BACKUP_ENABLED:-false}" = "true" ] || die "set BACKUP_ENABLED=true to restore"
  export BOX_FRESH_DISK=true                          # restore requires an EMPTY data dir
  export BACKUP_RESTORE="${BACKUP_RESTORE:-LATEST}"
  [ -n "${RESTORE_TO:-}" ] && export BACKUP_RESTORE_TARGET_TIME="$RESTORE_TO"
  log "▶ RESTORE (fresh disk, reuse app_id): BACKUP_RESTORE=$BACKUP_RESTORE target=${BACKUP_RESTORE_TARGET_TIME:-latest WAL}"
  update_member
}

# Verify backups exist in R2 (dev-box side via the toml creds): base-backup count + latest, WAL count.
backup_status() {
  _load; _load_backup_creds
  local prefix="${BACKUP_PREFIX:-${X#0x}}"; prefix="$(printf '%s' "$prefix" | tr 'A-Z' 'a-z')"
  BACKUP_PREFIX="$prefix" python3 - <<'PY'
import os, sys
try:
    import boto3
except Exception:
    sys.exit("backup-status needs boto3 on this host (pip install --user boto3)")
s3 = boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT'], aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
                  aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'], region_name=os.environ.get('R2_REGION', 'auto'))
bk, pfx = os.environ['R2_BUCKET'], os.environ['BACKUP_PREFIX']
def ls(sub):
    out, tok = [], None
    while True:
        kw = dict(Bucket=bk, Prefix=f"{pfx}/{sub}")
        if tok: kw['ContinuationToken'] = tok
        r = s3.list_objects_v2(**kw); out += r.get('Contents', [])
        if not r.get('IsTruncated'): break
        tok = r.get('NextContinuationToken')
    return out
bases = [o for o in ls('basebackups_005/') if 'backup_stop_sentinel' in o['Key']]
wal = ls('wal_005/')
print(f"R2: s3://{bk}/{pfx}/")
print(f"  base backups: {len(bases)}" + (f"  (latest {max(o['LastModified'] for o in bases).isoformat()})" if bases else "  — NONE yet"))
print(f"  WAL segments: {len(wal)}" + (f"  (newest {max(o['LastModified'] for o in wal).isoformat()})" if wal else "  — NONE yet"))
PY
}

# `restore --to "<SQL timestamp>"` → point-in-time recovery target.
[ "${2:-}" = "restore" ] && [ "${3:-}" = "--to" ] && RESTORE_TO="${4:-}"

log "=== matrix-node bring-up: $NODE ==="
case "${2:-all}" in
  deploy)  deploy_cvm ;;
  cluster) deploy_cluster ;;
  patha)   patha_upgrade ;;
  prime)   prime_gate ;;
  bind)    bind_member ;;
  verify)  verify ;;
  verify-agent) verify_agent ;;
  verify-client) verify_client ;;
  verify-isolation) verify_isolation ;;
  update)  update_member ;;
  restore) restore_member ;;
  backup-status) backup_status ;;
  setup)   deploy_cvm; deploy_cluster; patha_upgrade; prime_gate; bind_member ;;  # on-chain path, no register wait
  all)     deploy_cvm; deploy_cluster; patha_upgrade; prime_gate; bind_member; verify; verify_agent; verify_client; verify_isolation ;;
  *) die "usage: matrix-node.sh <node-name> [all|deploy|cluster|patha|prime|bind|verify|verify-agent|verify-client|verify-isolation|update|restore [--to <ts>]|backup-status|setup]" ;;
esac
