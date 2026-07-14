#!/usr/bin/env bash
# R2-host AttestMesh node on the self-hosted on-chain dstack box.
#
# Deploys the encrypting S3 gateway (docs/specs/r2-host-node.md) as a full,
# on-chain-anchored AttestMesh node via the canonical Path-A flow:
#   deploy (stock DstackApp + sealed env + bridge CreateVm, gateway OFF)
#     -> prime (allowlist compose_hash + app_id on the cluster)
#     -> bind  (upgradeToAndCall the proxy to ClusterMember, box deployer key)
#     -> verify (sidecar self-registers -> memberIdOf(X) != 0)
#     -> verify-health (box-side :9090; the port only binds POST-bind)
#     -> verify-s3 (over the WIREGUARD MESH via the ssh-node's sshd-mesh jump:
#        auth gate + plaintext PUT/GET roundtrip — the node has NO tailscale and
#        NO public HTTP; the S3 listener exists ONLY on the mesh IP)
#     -> verify-r2 (operator-side: the R2 bucket holds ONLY ciphertext w/ opaque names)
#     -> verify-isolation (box-side: :19000 refuses at the bridge IP)
#
# Day-2 rolls: `update` recomputes the compose_hash, allowlists it FIRST, then does
# an in-place UpgradeApp. BOX_FRESH_DISK=1 is ALWAYS safe here (the disk holds only
# the VFS cache — R2 is authoritative) and doubles as the recovery drill: the fresh
# CVM re-derives the CSK-based crypt key and must read old objects back.
#
# Gateway OFF + no_instance_id=true is the runyard pairing (dstack 0.5.11 boot-loops
# on no_instance_id WITH gateway). Secrets are read at runtime from ~/.attestmesh/
# and piped to the box over ssh STDIN (printf %q; webhost pattern) — never on argv.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: r2-host-node.sh <node-name> [deploy|prime|bind|verify|verify-health|verify-s3|verify-r2|verify-recovery|verify-isolation|update|setup|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/r2-host-node.yaml}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"

# The mesh jump host: the ssh-node's sshd-mesh (port 1023, sidecar netns — it sits
# ON the wg mesh). See deploy/compose/ssh-node.yaml + ~/.ssh/config.
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"

# Mesh-client S3 creds: generated ONCE (never regenerated — clients hold them).
SECRETS_FILE="${SECRETS_FILE:-$HOME/.attestmesh/r2-host.env}"
# R2 side: dedicated bucket + bucket-scoped token, same toml schema as the matrix
# backups file (endpoint/bucket/region/access_key_id/secret_access_key).
R2_CREDS="${R2_CREDS:-$HOME/.attestmesh/r2-host-r2.toml}"

# CVM sizing: RAM headroom for rclone serve-s3 multipart-in-memory; disk is mostly
# the VFS write-back cache (compose caps it at 40G — keep <=40-60% of BOX_DISK).
export BOX_VCPU="${BOX_VCPU:-4}" BOX_MEM="${BOX_MEM:-8192}" BOX_DISK="${BOX_DISK:-100}"
# No host port-forwards; bridge mode; gateway OFF (mesh-only node — runyard pairing
# with no_instance_id, which keeps the disk key app-bound; see header note).
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-false}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"
export BOX_NO_INSTANCE_ID="${BOX_NO_INSTANCE_ID:-true}"

STATE="$LOGDIR/r2-host-node-${NODE}.state"
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
MESH_IP=${MESH_IP:-}
SMOKE_KEY=${SMOKE_KEY:-}
SMOKE_SHA=${SMOKE_SHA:-}
EOF
}

_load() { [ -f "$STATE" ] && source "$STATE" || true; }
ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }
ssh_mesh() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$MESH_SSH_HOST" "$@"; }

# r2-host joins the existing Matrix cluster (C3) by default: reuses the already-
# deployed Path-A ClusterMember impl. Override CLUSTER + MEMBER_IMPL for another.
_default_cluster_env() {
  if [ -z "${CLUSTER:-}" ] || [ -z "${MEMBER_IMPL:-}" ]; then
    [ -f "$MATRIX_STATE" ] || die "missing cluster state: $MATRIX_STATE (set CLUSTER + MEMBER_IMPL to override)"
    CLUSTER="${CLUSTER:-$(grep '^CLUSTER=' "$MATRIX_STATE" | cut -d= -f2-)}"
    MEMBER_IMPL="${MEMBER_IMPL:-$(grep '^MEMBER_IMPL=' "$MATRIX_STATE" | cut -d= -f2-)}"
  fi
  [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "could not resolve CLUSTER/MEMBER_IMPL"
}

# Generated ONCE into the 0600 secrets file and re-read every run. Regenerating
# would invalidate every mesh client's credentials, so existing values always win.
_ensure_secrets() {
  umask 077
  mkdir -p "$(dirname "$SECRETS_FILE")"
  if [ ! -s "$SECRETS_FILE" ]; then
    cat > "$SECRETS_FILE" <<EOF
S3GW_ACCESS_KEY_ID=am$(openssl rand -hex 8)
S3GW_SECRET_ACCESS_KEY=$(openssl rand -hex 32)
EOF
    log "generated fresh S3 gateway creds -> $SECRETS_FILE"
  fi
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  [ -n "${S3GW_ACCESS_KEY_ID:-}" ] && [ -n "${S3GW_SECRET_ACCESS_KEY:-}" ] || die "incomplete secrets in $SECRETS_FILE"
}

_load_r2_creds() {
  [ -f "$R2_CREDS" ] || die "no R2 creds at $R2_CREDS (endpoint/bucket/region/access_key_id/secret_access_key toml)"
  export R2_ENDPOINT="$(sed -nE 's/^endpoint *= *"?([^"]+)"?.*/\1/p' "$R2_CREDS")"
  export R2_BUCKET="$(sed -nE 's/^bucket *= *"?([^"]+)"?.*/\1/p' "$R2_CREDS")"
  export R2_REGION="$(sed -nE 's/^region *= *"?([^"]+)"?.*/\1/p' "$R2_CREDS")"; : "${R2_REGION:=auto}"
  export R2_ACCESS_KEY_ID="$(sed -nE 's/^access_key_id *= *"?([^"]+)"?.*/\1/p' "$R2_CREDS")"
  export R2_SECRET_ACCESS_KEY="$(sed -nE 's/^secret_access_key *= *"?([^"]+)"?.*/\1/p' "$R2_CREDS")"
  [ -n "$R2_ENDPOINT" ] && [ -n "$R2_BUCKET" ] && [ -n "$R2_ACCESS_KEY_ID" ] && [ -n "$R2_SECRET_ACCESS_KEY" ] \
    || die "R2 creds incomplete in $R2_CREDS"
}

_require_env() {
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "${BUNDLER_URL:-}" ] || BUNDLER_URL="$RPC_URL"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] || die "missing INDEXER_REGISTRY_ADDR"
  _ensure_secrets
  _load_r2_creds
}

send_seq() {
  local label="$1"; shift
  send_with_nonce_retry "$label" "$@"
}

# Forward compose + helper to the box and run a box-side mode. Secrets ride ssh
# STDIN as printf-%q'd assignments sourced by the remote shell (webhost pattern) —
# they never appear on the remote argv/ps/sudo log. Only BOX_* knobs ride argv.
_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}" guser gtok
  guser=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  gtok=$(grep  -E '^\s*token\s*='    "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  [ -n "$gtok" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/r2-host-node-box.py" "$BOX_HOST:/tmp/r2-host-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n' "$CHAIN_ID"
    printf 'E_RPC_URL=%q\n' "${CVM_RPC_URL:-$RPC_URL}"
    printf 'E_BUNDLER_URL=%q\n' "${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
    printf 'E_GAS_POLICY_ID=%q\n' "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "$INDEXER_REGISTRY_ADDR"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_R2_ENDPOINT=%q\n' "$R2_ENDPOINT"
    printf 'E_R2_BUCKET=%q\n' "$R2_BUCKET"
    printf 'E_R2_REGION=%q\n' "${R2_REGION:-auto}"
    printf 'E_R2_ACCESS_KEY_ID=%q\n' "$R2_ACCESS_KEY_ID"
    printf 'E_R2_SECRET_ACCESS_KEY=%q\n' "$R2_SECRET_ACCESS_KEY"
    printf 'E_S3GW_ACCESS_KEY_ID=%q\n' "$S3GW_ACCESS_KEY_ID"
    printf 'E_S3GW_SECRET_ACCESS_KEY=%q\n' "$S3GW_SECRET_ACCESS_KEY"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "${guser:-dmvt}"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$gtok"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' BOX_NO_INSTANCE_ID='$BOX_NO_INSTANCE_ID' BOX_FRESH_DISK='${BOX_FRESH_DISK:-}' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/r2-host-node-box.py $mode $app_id $vm_id'"
}

deploy_cvm() {
  _load; _default_cluster_env; _require_env
  _save
  log "▶ box deploy_app r2-host node=$NODE compose=$COMPOSE cluster=$CLUSTER bucket=$R2_BUCKET"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  [ -n "$X" ] && [ "$X" != null ] || die "could not parse app_id from box deploy: $out"
  _save
  log "✔ deployed r2-host node app_id=$X compose_hash=$H vm=$VM_ID"
  log "mesh-only: S3 endpoint reachable ONLY at <mesh-ip>:19000 by cluster members"
}

prime_gate() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  local allowed_hash allowed_app
  allowed_hash=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_hash" = true ]; then
    log "compose hash already allowlisted"
  else
    send_seq "r2host-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  fi
  allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed_app" = true ]; then
    log "app id already allowlisted"
  else
    send_seq "r2host-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  fi
}

bind_member() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind r2-host X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/r2host-bind-${NODE}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --async --rpc-url $BOX_RPC --private-key "\$KEY"
SCRIPT
  confirm_latest_transaction "r2host-bind-${NODE}" "$RPC_URL" "$LOGDIR/r2host-bind-${NODE}.*.log" || die "bind transaction not confirmed"
  local c=""
  for _ in 1 2 3 4 5 6 7 8; do
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    [ "${c,,}" = "${CLUSTER,,}" ] && break
    sleep 2
  done
  log "X.cluster()=$c (expect $CLUSTER)"
  [ "${c,,}" = "${CLUSTER,,}" ] || die "bind did not stick (X.cluster()=$c)"
  log "✔ bound r2-host node X -> $CLUSTER"
}

verify() {
  _load; _default_cluster_env
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count
  for i in $(seq 1 45); do
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ r2-host node registered: memberId=$id memberCount=$count"
      return 0
    fi
    log "… r2-host node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  die "r2-host node did not register"
}

# Box-side bridge-IP resolver for this VM (qemu args -> TAP MAC -> ip neigh).
_bridge_ip_snippet() {
  cat <<'SNIP'
MAC=$(ps -eo args | grep -F "$VMID" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2)
[ -n "$MAC" ] || { echo "NO_QEMU"; exit 3; }
IP=$(ip neigh show dev dstack-br0 | grep -i "$MAC" | grep -oE '^10\.0\.[0-9]+\.[0-9]+' | head -1)
[ -n "$IP" ] || { echo "NO_IP"; exit 4; }
SNIP
}

# POST-BIND observability: this agent build binds :9090 only AFTER member.cluster()
# resolves — pre-bind the port is closed while the sidecar loops on "cluster not
# resolvable yet". Meaningful only after `bind` (hindsight lesson, 2026-07-01).
verify_health() {
  _load
  [ -n "${VM_ID:-}" ] || die "need VM_ID (run deploy first)"
  local i out
  for i in $(seq 1 40); do
    out=$(ssh_box "sudo bash -s" <<SCRIPT 2>/dev/null
set -u
VMID="$VM_ID"
$(_bridge_ip_snippet)
curl -sS --max-time 5 "http://\$IP:9090/healthz" 2>/dev/null
SCRIPT
)
    if echo "$out" | grep -q '"phase"'; then
      log "✔ sidecar healthz: $out"
      return 0
    fi
    log "… sidecar not answering yet ($i/40): ${out:-<no response>}"
    sleep 15
  done
  die "sidecar :9090 never answered at the bridge IP — check vm_logs $VM_ID"
}

# Discover the r2-host node's mesh IP from the jump host: enumerate wg peer
# allowed-ips and probe :19000 (rclone answers even unauthenticated — any HTTP
# status counts; only the r2-host node serves this port).
_mesh_discover_snippet() {
  cat <<'SNIP'
IF=$(wg show interfaces 2>/dev/null | awk '{print $1; exit}')
CAND=""
[ -n "$IF" ] && CAND=$(wg show "$IF" allowed-ips 2>/dev/null | awk '{print $2}' | cut -d/ -f1)
[ -n "$CAND" ] || CAND=$(ip -o route show 2>/dev/null | awk '/dev wg/ {print $1}' | cut -d/ -f1)
for ip in $CAND; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://$ip:19000/" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    echo "MESH_IP=$ip"
    exit 0
  fi
done
echo "MESH_IP="
SNIP
}

# End-to-end over the WIREGUARD MESH: auth gate (unauthenticated -> 403) + a full
# plaintext PUT/GET roundtrip via curl's built-in sigv4 signer (no aws-cli needed
# on the jump host). Creds travel inside the ssh-encrypted script text, never argv.
# Persists SMOKE_KEY/SMOKE_SHA so the recovery drill can re-GET the same object
# after a BOX_FRESH_DISK roll.
verify_s3() {
  _load; _ensure_secrets
  local i out ip
  for i in $(seq 1 45); do
    out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
$(_mesh_discover_snippet)
SCRIPT
)
    ip=$(echo "$out" | grep -oE '^MESH_IP=.*' | cut -d= -f2)
    if [ -n "$ip" ]; then
      MESH_IP="$ip"; _save
      break
    fi
    log "… r2-host S3 not reachable over the mesh yet ($i/45)"
    sleep 20
  done
  [ -n "${MESH_IP:-}" ] || die "S3 endpoint never answered over the mesh — check sshd-mesh jump ($MESH_SSH_HOST), wg peering, and the CVM"
  log "✔ S3 endpoint discovered on the mesh at $MESH_IP:19000"

  local stamp key
  stamp=$(ts)
  key="${SMOKE_KEY:-smoke/smoke-${stamp}.bin}"
  out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
set -u
B="http://$MESH_IP:19000"
SIG() { curl -sS --max-time 60 --user "$S3GW_ACCESS_KEY_ID:$S3GW_SECRET_ACCESS_KEY" --aws-sigv4 "aws:amz:us-east-1:s3" "\$@"; }
NOAUTH=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "\$B/")
echo "NOAUTH: \$NOAUTH"
F=\$(mktemp); G=\$(mktemp)
dd if=/dev/urandom of="\$F" bs=1M count=4 2>/dev/null
SHA_UP=\$(sha256sum "\$F" | cut -d' ' -f1)
SIG -X PUT "\$B/smoke" -o /dev/null -w 'MKBUCKET: %{http_code}\n' || true
SIG -X PUT --data-binary @"\$F" "\$B/$key" -o /dev/null -w 'PUT: %{http_code}\n'
# Read-after-write lags by the VFS write-back window (~5s) + upload time: the
# object 404s until rclone finishes flushing it to R2 (live-verified 2026-07-02).
gc=000
for _ in \$(seq 1 18); do
  gc=\$(SIG -o "\$G" -w '%{http_code}' "\$B/$key")
  [ "\$gc" = "200" ] && break
  sleep 5
done
echo "GET: \$gc"
SHA_DOWN=\$(sha256sum "\$G" | cut -d' ' -f1)
echo "SHA_UP: \$SHA_UP"
echo "SHA_DOWN: \$SHA_DOWN"
[ "\$SHA_UP" = "\$SHA_DOWN" ] && echo "ROUNDTRIP: OK" || echo "ROUNDTRIP: MISMATCH"
rm -f "\$F" "\$G"
SCRIPT
)
  echo "$out" | tee "$LOGDIR/r2host-s3-${NODE}.$(ts).log" >&2
  # rclone serve s3 answers 400 to a missing Authorization header, 403 to a bad
  # signature (verified locally 2026-07-02) — anything but 2xx/3xx proves the gate.
  echo "$out" | grep -qE '^NOAUTH: *(400|401|403)' || die "API answered WITHOUT creds — auth gate not active"
  echo "$out" | grep -qE '^PUT: *200'          || die "PUT failed"
  echo "$out" | grep -qE '^GET: *200'          || die "GET failed"
  echo "$out" | grep -q  '^ROUNDTRIP: OK'      || die "roundtrip sha mismatch"
  SMOKE_KEY="$key"
  SMOKE_SHA=$(echo "$out" | grep -oE '^SHA_UP: [0-9a-f]+' | cut -d' ' -f2)
  _save
  log "✔ S3 E2E OK over the mesh: auth gate + 4MB PUT/GET roundtrip ($key)"
}

# Recovery drill companion: GET the object PUT by a PREVIOUS verify-s3 run and
# compare its sha. Run after `BOX_FRESH_DISK=1 … update` — success proves the
# fresh CVM re-derived the same CSK-based crypt key and can read old ciphertext.
verify_recovery() {
  _load; _ensure_secrets
  [ -n "${SMOKE_KEY:-}" ] && [ -n "${SMOKE_SHA:-}" ] || die "no SMOKE_KEY/SMOKE_SHA in state (run verify-s3 first)"
  [ -n "${MESH_IP:-}" ] || die "no MESH_IP in state (run verify-s3 first)"
  local i out
  for i in $(seq 1 30); do
    out=$(ssh_mesh "bash -s" <<SCRIPT 2>/dev/null
G=\$(mktemp)
code=\$(curl -sS --max-time 60 --user "$S3GW_ACCESS_KEY_ID:$S3GW_SECRET_ACCESS_KEY" --aws-sigv4 "aws:amz:us-east-1:s3" -o "\$G" -w '%{http_code}' "http://$MESH_IP:19000/$SMOKE_KEY")
echo "GET: \$code"
echo "SHA: \$(sha256sum "\$G" | cut -d' ' -f1)"
rm -f "\$G"
SCRIPT
)
    if echo "$out" | grep -q '^GET: 200' && echo "$out" | grep -q "^SHA: $SMOKE_SHA"; then
      log "✔ RECOVERY OK: pre-roll object $SMOKE_KEY readable + sha-identical after fresh-disk roll"
      return 0
    fi
    log "… recovery GET not green yet ($i/30): $(echo "$out" | tr '\n' ' ')"
    sleep 15
  done
  die "recovery drill failed: $SMOKE_KEY not readable/sha-identical after the roll"
}

# Operator-side ciphertext check straight against R2: the bucket must contain ONLY
# opaque (encrypted) names — the smoke object's plaintext key must NOT appear.
# R2_BUCKET may be "bucket" or "bucket/prefix" (rclone path form); scope the listing.
verify_r2() {
  _load; _load_r2_creds
  local bucket="${R2_BUCKET%%/*}" prefix="" listing
  [ "$bucket" != "$R2_BUCKET" ] && prefix="&prefix=${R2_BUCKET#*/}/"
  listing=$(curl -sS --max-time 30 --user "$R2_ACCESS_KEY_ID:$R2_SECRET_ACCESS_KEY" \
    --aws-sigv4 "aws:amz:auto:s3" "$R2_ENDPOINT/$bucket?list-type=2$prefix" 2>&1) \
    || die "R2 ListObjectsV2 failed"
  local nkeys
  nkeys=$(echo "$listing" | grep -o '<Key>' | wc -l)
  log "R2 bucket $R2_BUCKET holds $nkeys object(s)"
  [ "$nkeys" -gt 0 ] || die "R2 bucket is empty — write-through did not reach R2 (run verify-s3 first; check egress-fw/rclone.log)"
  if echo "$listing" | grep -qE '<Key>[^<]*smoke[^<]*</Key>'; then
    die "PLAINTEXT KEY VISIBLE IN R2 — filename encryption is not active"
  fi
  log "✔ R2 holds only opaque (encrypted) object names — ciphertext-at-rest confirmed"
}

# Host-isolation: from the BOX, the S3 port must be UNREACHABLE at the CVM's bridge
# IP. 9090 (sidecar health) + 51900 (wg transport) are the ONLY published ports.
verify_isolation() {
  _load; [ -n "${VM_ID:-}" ] || die "no VM_ID in state (run deploy first)"
  log "▶ host-isolation check for vm=$VM_ID"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/r2host-isolation-${NODE}.$(ts).log"
set -u
VMID="$VM_ID"
$(_bridge_ip_snippet)
echo "ISOLATION: vm=\$VMID bridge_ip=\$IP"
bad=0
if curl -sS --max-time 3 -o /dev/null "http://\$IP:19000/" 2>/dev/null; then
  echo "  !! \$IP:19000 REACHABLE from host — INVARIANT VIOLATION"; bad=1
else echo "  \$IP:19000 refused from host (good)"; fi
curl -sS --max-time 3 "http://\$IP:9090/healthz" >/dev/null 2>&1 \
  && echo "  \$IP:9090 sidecar health answers (expected, published)" \
  || { echo "  !! \$IP:9090 sidecar health NOT answering"; bad=1; }
[ \$bad -eq 0 ] && echo "ISOLATION: PASS" || { echo "ISOLATION: FAIL"; exit 5; }
SCRIPT
  local rc=${PIPESTATUS[0]}
  [ "$rc" = 0 ] || die "host-isolation check failed (rc=$rc) — see the log above"
  log "✔ isolation holds: S3 refuses from the host; only sidecar health + wg are published"
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
    send_seq "r2host-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  fi
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r '.vm_id // empty'); [ -n "$VM_ID" ] || VM_ID="$(_load; echo "${VM_ID:-}")"
  [ -n "$H" ] && [ "$H" != null ] || H="$nh"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ r2-host node update complete mode=$mode vm=$VM_ID"
  # NOTE: service checks (verify / verify-health / verify-s3) are separate steps.
}

log "=== R2-host AttestMesh node: $NODE ==="
case "$ACTION" in
  deploy) deploy_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  verify-health) verify_health ;;
  verify-s3) verify_s3 ;;
  verify-r2) verify_r2 ;;
  verify-recovery) verify_recovery ;;
  verify-isolation) verify_isolation ;;
  update) update_member ;;
  setup) deploy_cvm; prime_gate; bind_member ;;
  all) deploy_cvm; prime_gate; bind_member; verify; verify_health; verify_s3; verify_r2; verify_isolation ;;
  *) die "usage: r2-host-node.sh <node-name> [deploy|prime|bind|verify|verify-health|verify-s3|verify-r2|verify-recovery|verify-isolation|update|setup|all]" ;;
esac
