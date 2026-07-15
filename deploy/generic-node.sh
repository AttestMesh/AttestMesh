#!/usr/bin/env bash
# Generic workload AttestMesh node on the self-hosted dstack box.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: generic-node.sh <node-name> [deploy|prime|bind|verify|update|all]}"
ACTION="${2:-all}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
BOX_DEPLOYER_KEY="${BOX_DEPLOYER_KEY:-/root/.attestmesh/base-deployer.json}"
BOX_RPC="${BOX_RPC:-https://base-rpc.publicnode.com}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/generic-node.yaml}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"

export BOX_VCPU="${BOX_VCPU:-2}" BOX_MEM="${BOX_MEM:-4096}" BOX_DISK="${BOX_DISK:-40}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}" BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

STATE="$LOGDIR/generic-node-${NODE}.state"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000
ZERO_ADDRESS=0x0000000000000000000000000000000000000000
REQUESTED_CLUSTER="${CLUSTER:-}"
REQUESTED_MEMBER_IMPL="${MEMBER_IMPL:-}"
REQUESTED_KMS_ROOT="${KMS_ROOT:-}"
REQUESTED_GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-}"
GUEST_CONFIG_RESOLVED=0

[[ "$NODE" =~ ^[a-zA-Z0-9._-]+$ ]] || die "node name contains unsupported characters"
[[ "${BOX_COMPOSE_NAME:-$NODE}" =~ ^[a-zA-Z0-9._-]+$ ]] \
  || die "BOX_COMPOSE_NAME contains unsupported characters"

_clear_state() {
  STATE_SCHEMA=""
  UPDATED_AT=""
  STATE_PHASE="unknown"
  X=""
  H=""
  VM_ID=""
  CLUSTER="$REQUESTED_CLUSTER"
  MEMBER_IMPL="$REQUESTED_MEMBER_IMPL"
  KMS_ROOT="$REQUESTED_KMS_ROOT"
  GATEWAY_DOMAIN="$REQUESTED_GATEWAY_DOMAIN"
  GUEST_CONFIG_SHA256=""
}

_validate_optional_address() {
  local label="$1" value="$2"
  [ -z "$value" ] && return 0
  [[ "$value" =~ ^0x[0-9a-fA-F]{40}$ ]] || die "$label in $STATE is not an address"
  [ "${value,,}" != "$ZERO_ADDRESS" ] || die "$label in $STATE must be nonzero"
}

_validate_state_values() {
  [ -z "$STATE_SCHEMA" ] || [ "$STATE_SCHEMA" = 2 ] \
    || die "unsupported generic state schema in $STATE: $STATE_SCHEMA"
  [[ "$UPDATED_AT" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
    || die "UPDATED_AT in $STATE is malformed"
  case "$STATE_PHASE" in
    unknown|preflighted|deployed-stopped|primed|bound|started|registered|cleaned) ;;
    *) die "STATE_PHASE in $STATE is invalid: $STATE_PHASE" ;;
  esac
  _validate_optional_address X "$X"
  if [ -n "$H" ]; then
    [[ "$H" =~ ^(0x)?[0-9a-fA-F]{64}$ ]] || die "H in $STATE is not a compose hash"
    [ "${H#0x}" != "${ZERO32#0x}" ] || die "H in $STATE must be nonzero"
  fi
  if [ -n "$VM_ID" ]; then
    [[ "$VM_ID" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$ ]] \
      || die "VM_ID in $STATE contains unsupported characters"
    [ "$VM_ID" != null ] || die "VM_ID in $STATE must not be null"
  fi
  _validate_optional_address CLUSTER "$CLUSTER"
  _validate_optional_address MEMBER_IMPL "$MEMBER_IMPL"
  _validate_optional_address KMS_ROOT "$KMS_ROOT"
  if [ -n "$GATEWAY_DOMAIN" ]; then
    [[ "$GATEWAY_DOMAIN" =~ ^[a-zA-Z0-9._:-]+$ ]] \
      || die "GATEWAY_DOMAIN in $STATE contains unsupported characters"
  fi
  if [ -n "$GUEST_CONFIG_SHA256" ]; then
    [[ "$GUEST_CONFIG_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] \
      || die "GUEST_CONFIG_SHA256 in $STATE is malformed"
    GUEST_CONFIG_SHA256="${GUEST_CONFIG_SHA256,,}"
  fi
}

_save() {
  local tmp
  umask 077
  STATE_SCHEMA=2
  UPDATED_AT=$(ts)
  _validate_state_values
  tmp="${STATE}.tmp.$$"
  if ! {
    printf 'STATE_SCHEMA=2\n'
    printf 'UPDATED_AT=%s\n' "$UPDATED_AT"
    printf 'STATE_PHASE=%s\n' "${STATE_PHASE:-unknown}"
    printf 'X=%s\n' "${X:-}"
    printf 'H=%s\n' "${H:-}"
    printf 'VM_ID=%s\n' "${VM_ID:-}"
    printf 'CLUSTER=%s\n' "${CLUSTER:-}"
    printf 'MEMBER_IMPL=%s\n' "${MEMBER_IMPL:-}"
    printf 'KMS_ROOT=%s\n' "${KMS_ROOT:-}"
    printf 'GATEWAY_DOMAIN=%s\n' "${GATEWAY_DOMAIN:-}"
    printf 'GUEST_CONFIG_SHA256=%s\n' "${GUEST_CONFIG_SHA256:-}"
  } >"$tmp"; then
    rm -f -- "$tmp"
    die "could not write generic state temporary file: $tmp"
  fi
  chmod 0600 "$tmp"
  sync -d "$tmp" || { rm -f -- "$tmp"; die "could not flush generic state: $tmp"; }
  mv -f -- "$tmp" "$STATE"
  sync -f "$(dirname "$STATE")" || die "could not durably commit generic state: $STATE"
}

_load() {
  _clear_state
  [ -f "$STATE" ] || return 0
  local line key value required
  declare -A seen=()
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || die "blank line in generic state: $STATE"
    [[ "$line" == *=* ]] || die "malformed line in generic state: $STATE"
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      STATE_SCHEMA|UPDATED_AT|STATE_PHASE|X|H|VM_ID|CLUSTER|MEMBER_IMPL|KMS_ROOT|GATEWAY_DOMAIN|GUEST_CONFIG_SHA256) ;;
      *) die "unknown generic state field '$key' in $STATE" ;;
    esac
    [ -z "${seen[$key]+present}" ] || die "duplicate generic state field '$key' in $STATE"
    seen[$key]=1
    printf -v "$key" '%s' "$value"
  done <"$STATE"
  for required in UPDATED_AT STATE_PHASE X H VM_ID CLUSTER MEMBER_IMPL KMS_ROOT GATEWAY_DOMAIN; do
    [ -n "${seen[$required]+present}" ] || die "generic state is missing $required: $STATE"
  done
  if [ -n "${seen[STATE_SCHEMA]+present}" ]; then
    [ "$STATE_SCHEMA" = 2 ] || die "unsupported generic state schema in $STATE: $STATE_SCHEMA"
    [ -n "${seen[GUEST_CONFIG_SHA256]+present}" ] \
      || die "schema-2 generic state is missing GUEST_CONFIG_SHA256: $STATE"
  elif [ -n "${seen[GUEST_CONFIG_SHA256]+present}" ]; then
    die "legacy generic state unexpectedly contains GUEST_CONFIG_SHA256: $STATE"
  fi
  _validate_state_values
  if [ "${STRICT_GENERIC_STATE_BINDINGS:-0}" = 1 ]; then
    [ -z "$REQUESTED_CLUSTER" ] || [ "${CLUSTER,,}" = "${REQUESTED_CLUSTER,,}" ] \
      || die "generic state CLUSTER differs from the requested cluster"
    [ -z "$REQUESTED_MEMBER_IMPL" ] \
      || [ "${MEMBER_IMPL,,}" = "${REQUESTED_MEMBER_IMPL,,}" ] \
      || die "generic state MEMBER_IMPL differs from the requested implementation"
    [ -z "$REQUESTED_KMS_ROOT" ] || [ "${KMS_ROOT,,}" = "${REQUESTED_KMS_ROOT,,}" ] \
      || die "generic state KMS_ROOT differs from the requested root"
    [ "${ALLOW_GENERIC_GATEWAY_DRIFT:-0}" = 1 ] \
      || [ -z "$REQUESTED_GATEWAY_DOMAIN" ] \
      || [ "$GATEWAY_DOMAIN" = "$REQUESTED_GATEWAY_DOMAIN" ] \
      || die "generic state GATEWAY_DOMAIN differs from the requested gateway"
  fi
}

ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }

_require_tools() {
  local tool
  for tool in jq cast ssh scp sha256sum sync; do
    command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
  done
}

_resolve_guest_config() {
  [ "${GUEST_CONFIG_RESOLVED:-0}" = 1 ] && return 0
  local indexer
  indexer=$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json" 2>/dev/null)
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$indexer}"
  [ -n "$INDEXER_REGISTRY_ADDR" ] && [ "$INDEXER_REGISTRY_ADDR" != null ] \
    || die "missing INDEXER_REGISTRY_ADDR"
  GUEST_RPC_URL="${CVM_RPC_URL:-${RPC_URL:-}}"
  GUEST_BUNDLER_URL="${CVM_BUNDLER_URL:-${BUNDLER_URL:-${RPC_URL:-}}}"
  [ -n "$GUEST_RPC_URL" ] || die "missing sealed guest RPC URL"
  [ -n "$GUEST_BUNDLER_URL" ] \
    || die "missing CVM_BUNDLER_URL/BUNDLER_URL (cluster members need an EIP-4337 bundler)"
  APP_ENV_B64="${APP_ENV_B64:-}"
  GHCR_USER=$(grep -E '^\s*username\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null \
    | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  GHCR_TOKEN=$(grep -E '^\s*token\s*=' "$HOME/.teesql/ghcr-pull.toml" 2>/dev/null \
    | head -1 | sed -E 's/.*=\s*//' | tr -d "\"' ")
  GHCR_USER="${GHCR_USER:-dmvt}"
  [ -n "$GHCR_TOKEN" ] || die "no ghcr token in ~/.teesql/ghcr-pull.toml"
  GUEST_CONFIG_RESOLVED=1
}

_guest_config_sha256() {
  _resolve_guest_config
  printf '%s\0' \
    'schema=attestmesh.generic-guest.v1' \
    "CHAIN_ID=${CHAIN_ID:-}" \
    "RPC_URL=$GUEST_RPC_URL" \
    "BUNDLER_URL=$GUEST_BUNDLER_URL" \
    "GAS_POLICY_ID=${GAS_POLICY_ID:-}" \
    "INDEXER_REGISTRY_ADDR=${INDEXER_REGISTRY_ADDR:-}" \
    "GATEWAY_DOMAIN=${GATEWAY_DOMAIN:-}" \
    "CLUSTER=${CLUSTER:-}" \
    "MEMBER_IMPL=${MEMBER_IMPL:-}" \
    "APP_ENV_B64=$APP_ENV_B64" \
    "DSTACK_DOCKER_USERNAME=$GHCR_USER" \
    "DSTACK_DOCKER_PASSWORD=$GHCR_TOKEN" \
    'DSTACK_DOCKER_REGISTRY=ghcr.io' \
    | sha256sum | awk '{print $1}'
}

_require_env() {
  _require_tools
  _resolve_guest_config
  [ -n "${CLUSTER:-}" ] || die "missing CLUSTER"
  [ -n "${MEMBER_IMPL:-}" ] || die "missing MEMBER_IMPL"
  [ -n "${KMS_ROOT:-}" ] || die "missing KMS_ROOT"
  [ -n "${GAS_POLICY_ID:-}" ] \
    || die "missing GAS_POLICY_ID (cluster members need paymaster sponsorship)"
  [ "$GUEST_BUNDLER_URL" != "$GUEST_RPC_URL" ] \
    || log "sealed guest bundler equals guest RPC; continuing because some providers multiplex both"
  [ -s "$COMPOSE" ] || die "missing compose file: $COMPOSE"
  CURRENT_GUEST_CONFIG_SHA256=$(_guest_config_sha256)
}

_enforce_guest_config() {
  [ -n "${CURRENT_GUEST_CONFIG_SHA256:-}" ] \
    || die "current guest configuration was not fingerprinted"
  if [ -n "${GUEST_CONFIG_SHA256:-}" ]; then
    [ "${GUEST_CONFIG_SHA256,,}" = "$CURRENT_GUEST_CONFIG_SHA256" ] \
      || die "sealed guest configuration drifted from $STATE; redeploy instead of starting with unverified settings"
  elif [ -n "${VM_ID:-}" ] && [ "${REQUIRE_GUEST_CONFIG_FINGERPRINT:-0}" = 1 ]; then
    die "deployed state lacks a sealed guest configuration fingerprint: $STATE"
  elif [ -z "${VM_ID:-}" ]; then
    GUEST_CONFIG_SHA256="$CURRENT_GUEST_CONFIG_SHA256"
  else
    log "legacy deployed state has no guest configuration fingerprint; continuing without drift verification"
  fi
}

preflight() {
  _load; _require_env
  _enforce_guest_config
  log "▶ preflight generic node=$NODE compose=$COMPOSE cluster=$CLUSTER"
  cast chain-id --rpc-url "$RPC_URL" >/dev/null || die "RPC_URL is not reachable"
  cast code "$CLUSTER" --rpc-url "$RPC_URL" | grep -Eq '^0x[0-9a-fA-F]{4,}$' || die "CLUSTER has no code: $CLUSTER"
  cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" >/dev/null || die "CLUSTER does not expose memberCount(): $CLUSTER"
  cast code "$MEMBER_IMPL" --rpc-url "$RPC_URL" | grep -Eq '^0x[0-9a-fA-F]{4,}$' || die "MEMBER_IMPL has no code: $MEMBER_IMPL"
  ssh_box "sudo test -x '$BOX_PY' && sudo test -r '$BOX_DEPLOYER_KEY'" >/dev/null || die "box prerequisites missing on $BOX_HOST"
  _box_run hash >/dev/null || die "box cannot render/hash compose $COMPOSE"
  STATE_PHASE="preflighted"; _save
  log "✔ preflight passed for generic node=$NODE"
}

send_seq() {
  local label="$1"; shift
  send_with_nonce_retry "$label" "$@"
}

_box_run() {
  local mode="$1" app_id="${2:-}" vm_id="${3:-}"
  _resolve_guest_config
  scp -o BatchMode=yes -q "$COMPOSE" "$BOX_HOST:/tmp/${NODE}.yaml"
  scp -o BatchMode=yes -q "$HERE/generic-node-box.py" "$BOX_HOST:/tmp/generic-node-box.py"
  {
    printf 'E_CHAIN_ID=%q\n' "${CHAIN_ID:-}"
    printf 'E_BOX_RPC=%q\n' "${BOX_RPC:-}"
    printf 'E_RPC_URL=%q\n' "$GUEST_RPC_URL"
    printf 'E_BUNDLER_URL=%q\n' "$GUEST_BUNDLER_URL"
    printf 'E_GAS_POLICY_ID=%q\n' "${GAS_POLICY_ID:-}"
    printf 'E_INDEXER_REGISTRY_ADDR=%q\n' "${INDEXER_REGISTRY_ADDR:-}"
    printf 'E_GATEWAY_DOMAIN=%q\n' "$GATEWAY_DOMAIN"
    printf 'E_CLUSTER=%q\n' "${CLUSTER:-}"
    printf 'E_MEMBER_IMPL=%q\n' "${MEMBER_IMPL:-}"
    printf 'E_APP_ENV_B64=%q\n' "$APP_ENV_B64"
    printf 'E_DSTACK_DOCKER_USERNAME=%q\n' "$GHCR_USER"
    printf 'E_DSTACK_DOCKER_PASSWORD=%q\n' "$GHCR_TOKEN"
    printf 'E_DSTACK_DOCKER_REGISTRY=%q\n' "ghcr.io"
  } | ssh_box "sudo BOX_NAME='$NODE' BOX_COMPOSE_NAME='${BOX_COMPOSE_NAME:-$NODE}' BOX_COMPOSE='/tmp/${NODE}.yaml' BOX_VCPU=$BOX_VCPU BOX_MEM=$BOX_MEM BOX_DISK=$BOX_DISK BOX_PORTS='$BOX_PORTS' BOX_GATEWAY_ENABLED='$BOX_GATEWAY_ENABLED' BOX_NET_MODE='$BOX_NET_MODE' \
    bash -c 'set -a; . /dev/stdin; set +a; exec $BOX_PY /tmp/generic-node-box.py $mode $app_id $vm_id'"
}

compose_hash() {
  _require_tools
  [ -s "$COMPOSE" ] || die "missing compose file: $COMPOSE"
  APP_ENV_B64="${APP_ENV_B64:-}"
  _box_run hash
}

guest_config_sha256() {
  _require_env
  printf '%s\n' "$CURRENT_GUEST_CONFIG_SHA256"
}

_box_stop_vm() {
  local vm_id="${1:?vm_id required}" out
  [[ "$vm_id" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$ ]] \
    || die "VM_ID contains unsupported characters"
  scp -o BatchMode=yes -q "$HERE/generic-node-box.py" "$BOX_HOST:/tmp/generic-node-box.py"
  out=$(ssh_box "sudo BOX_NAME='$NODE' $BOX_PY /tmp/generic-node-box.py stop '$vm_id'") \
    || return 1
  printf '%s\n' "$out" | jq -e \
    --arg vm "$vm_id" '
      .vm_id == $vm
      and .stopped == true
      and (
        (.found == false and .status == "gone")
        or
        (.found == true and ((.status | ascii_downcase) | test("^(stopped|exited|dead)$")))
      )
    ' \
    >/dev/null || return 1
  printf '%s\n' "$out"
}

_box_vm_json() {
  [ -n "${VM_ID:-}" ] || return 0
  ssh_box "sudo VM_ID='$VM_ID' $BOX_PY - <<'PY'
import json, os, sys
sys.path.insert(0, '/opt/dstack-mcp')
import mcp_dstack as m
try:
    resp = m.vmm('GetInfo', {'id': os.environ['VM_ID']})
    info = resp.get('info') or {}
    print(json.dumps({
        'found': bool(resp.get('found')),
        'status': info.get('status'),
        'boot_progress': info.get('boot_progress'),
        'boot_error': info.get('boot_error'),
    }))
except Exception as exc:
    print(json.dumps({'found': None, 'error': str(exc)}))
PY"
}

_box_boot_detail() {
  [ -n "${VM_ID:-}" ] || return 0
  ssh_box "sudo VM_ID='$VM_ID' $BOX_PY - <<'PY'
import os, sys
sys.path.insert(0, '/opt/dstack-mcp')
import mcp_dstack as m
try:
    log = m.vm_logs(vm_id=os.environ['VM_ID'], lines=700, channel='serial')
except Exception as exc:
    print(f'unable to fetch serial log: {exc}')
    raise SystemExit(0)
needles = (
    'Error response from daemon:',
    'OCI runtime create failed:',
    'dependency failed',
    'unhealthy',
    'failed to start containers',
    'Failed to start App Compose Service',
)
matches = [line.strip() for line in log.splitlines() if any(n in line for n in needles)]
print(' | '.join(matches[-4:]))
PY"
}

deploy_cvm() {
  _load; _require_env
  _enforce_guest_config
  if [ -n "${VM_ID:-}" ]; then
    log "existing VM_ID in $STATE; deploy is not destructive. Run cleanup first or use update."
    die "refusing to create a second VM for node=$NODE"
  fi
  _save
  log "▶ box deploy_app generic node=$NODE compose=$COMPOSE cluster=$CLUSTER (create stopped)"
  local out j
  out=$(_box_run deploy) || die "box deploy failed"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  X=$(echo "$j" | jq -r .app_id)
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r .vm_id)
  _validate_optional_address X "$X"
  [ -n "$X" ] || die "could not parse app_id from box deploy: $out"
  [[ "$H" =~ ^[0-9a-fA-F]{64}$ ]] && [ "${H,,}" != "${ZERO32#0x}" ] \
    || die "box deploy returned an invalid compose_hash"
  [[ "$VM_ID" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$ ]] && [ "$VM_ID" != null ] \
    || die "box deploy returned an invalid VM_ID"
  STATE_PHASE="deployed-stopped"
  _save
  log "✔ deployed generic node app_id=$X compose_hash=$H vm=$VM_ID"
}

start_cvm() {
  _load; _require_env
  _enforce_guest_config
  [ -n "${VM_ID:-}" ] || die "need VM_ID (run deploy first)"
  log "▶ start generic VM vm=$VM_ID after allowlist/bind"
  ssh_box "sudo VM_ID='$VM_ID' $BOX_PY - <<'PY'
import os, sys
sys.path.insert(0, '/opt/dstack-mcp')
import mcp_dstack as m
m.vmm('StartVm', {'id': os.environ['VM_ID']})
print('started ' + os.environ['VM_ID'])
PY"
  STATE_PHASE="started"; _save
  log "✔ start requested for generic VM vm=$VM_ID"
}

prime_gate() {
  _load; _require_env
  _enforce_guest_config
  [ -n "${X:-}" ] && [ -n "${H:-}" ] || die "need X/H (run deploy first)"
  if [ "$(cast call "$CLUSTER" 'allowedKmsRoots(address)(bool)' "$KMS_ROOT" --rpc-url "$RPC_URL" 2>/dev/null)" != true ]; then
    send_seq "generic-addKmsRoot-${NODE}" "$CLUSTER" "addAllowedKmsRoot(address)" "$KMS_ROOT"
  else
    log "KMS root already allowlisted"
  fi
  if [ "$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null)" != true ]; then
    send_seq "generic-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x${H#0x}"
  else
    log "compose hash already allowlisted"
  fi
  if [ "$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null)" != true ]; then
    send_seq "generic-addApp-${NODE}" "$CLUSTER" "addAllowedAppId(address)" "$X"
  else
    log "app id already allowlisted"
  fi
  STATE_PHASE="primed"; _save
}

bind_member() {
  _load; _require_env
  _enforce_guest_config
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] || die "need X+cluster+impl"
  local reinit
  reinit=$(cast calldata "reinitializeFromDstackApp(address)" "$CLUSTER")
  log "▶ bind generic X=$X -> impl $MEMBER_IMPL (box deployer)"
  ssh_box "sudo bash -s" <<SCRIPT 2>&1 | tee "$LOGDIR/generic-bind-${NODE}.$(ts).log"
export PATH=\$PATH:/root/.foundry/bin
KEY=\$(jq -r '.[0].private_key' $BOX_DEPLOYER_KEY)
cast send $X "upgradeToAndCall(address,bytes)" $MEMBER_IMPL "$reinit" --async --rpc-url $BOX_RPC --private-key "\$KEY"
SCRIPT
  confirm_latest_transaction "generic-bind-${NODE}" "$RPC_URL" "$LOGDIR/generic-bind-${NODE}.*.log" || die "bind transaction not confirmed"
  local c=""
  for _ in 1 2 3 4 5 6 7 8; do
    c=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null)
    [ "${c,,}" = "${CLUSTER,,}" ] && break
    sleep 2
  done
  log "X.cluster()=$c (expect $CLUSTER)"
  [ "${c,,}" = "${CLUSTER,,}" ] || die "bind did not stick (X.cluster()=$c)"
  STATE_PHASE="bound"; _save
  log "✔ bound generic node X -> $CLUSTER"
}

verify() {
  _load; _require_env
  _enforce_guest_config
  [ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X+cluster"
  local i id count vm found boot_error boot_progress owner root_allowed hash_allowed app_allowed
  for i in $(seq 1 45); do
    if [ -n "${VM_ID:-}" ]; then
      vm=$(_box_vm_json || true)
      found=$(echo "$vm" | jq -r '.found // empty' 2>/dev/null)
      boot_error=$(echo "$vm" | jq -r '.boot_error // empty' 2>/dev/null)
      boot_progress=$(echo "$vm" | jq -r '.boot_progress // empty' 2>/dev/null)
      [ "$found" != false ] || die "enclave VM disappeared from dstack while waiting for mesh registration (vm=$VM_ID)"
      if [ "$i" -gt 6 ] && [ -n "$boot_error" ] && [ "$boot_error" != null ]; then
        local detail
        detail=$(_box_boot_detail || true)
        if [ -n "$detail" ]; then
          die "enclave VM boot failed before mesh registration: $boot_error (progress: ${boot_progress:-unknown}, vm=$VM_ID; serial: $detail)"
        fi
        die "enclave VM boot failed before mesh registration: $boot_error (progress: ${boot_progress:-unknown}, vm=$VM_ID)"
      fi
    fi
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null)
    count=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL" 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "$ZERO32" ]; then
      log "✔ generic node registered: memberId=$id memberCount=$count"
      STATE_PHASE="registered"; _save
      return 0
    fi
    log "… generic node not registered yet ($i/45, memberCount=${count:-?})"
    sleep 20
  done
  owner=$(cast call "$X" 'owner()(address)' --rpc-url "$RPC_URL" 2>/dev/null || true)
  root_allowed=$(cast call "$CLUSTER" 'allowedKmsRoots(address)(bool)' "$KMS_ROOT" --rpc-url "$RPC_URL" 2>/dev/null || true)
  hash_allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x${H#0x}" --rpc-url "$RPC_URL" 2>/dev/null || true)
  app_allowed=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null || true)
  die "generic node did not register (owner=${owner:-?}, allowedKmsRoots[$KMS_ROOT]=${root_allowed:-?}, allowedComposeHash=${hash_allowed:-?}, allowedAppId=${app_allowed:-?})"
}

cleanup() {
  _load
  _require_tools
  local id="" force="${FORCE_CLEANUP:-0}" allowed_app=""
  if [ -n "${CLUSTER:-}" ] && [ -n "${X:-}" ] && [ -n "${RPC_URL:-}" ]; then
    id=$(cast call "$CLUSTER" "memberIdOf(address)(bytes32)" "$X" --rpc-url "$RPC_URL" 2>/dev/null || true)
  fi
  if [ -n "$id" ] && [ "$id" != "$ZERO32" ] && [ "$force" != "1" ]; then
    log "cleanup skipped: node is registered (memberId=$id). Set FORCE_CLEANUP=1 to stop the VM anyway."
    STATE_PHASE="registered"; _save
    return 0
  fi

  if [ -n "${VM_ID:-}" ]; then
    log "▶ cleanup stop VM_ID=$VM_ID force=$force"
    if _box_stop_vm "$VM_ID"; then
      log "✔ cleanup stop completed for vm=$VM_ID"
    else
      log "cleanup stop reported an error for vm=$VM_ID; keeping state for manual follow-up"
      return 1
    fi
  else
    log "cleanup no-op: no VM_ID in $STATE"
  fi

  if [ "${SKIP_APP_ALLOWLIST_CLEANUP:-0}" != 1 ] \
    && [ -n "${CLUSTER:-}" ] && [ -n "${X:-}" ] \
    && [ -n "${RPC_URL:-}" ] && [ -n "${PRIVATE_KEY:-}" ]; then
    allowed_app=$(cast call "$CLUSTER" 'allowedAppIds(address)(bool)' "$X" --rpc-url "$RPC_URL" 2>/dev/null || true)
    if [ "$allowed_app" = true ] && { [ -z "$id" ] || [ "$id" = "$ZERO32" ]; }; then
      send_seq "generic-removeApp-${NODE}" "$CLUSTER" "removeAllowedAppId(address)" "$X" || log "removeAllowedAppId failed; app remains allowlisted"
    fi
  fi

  STATE_PHASE="cleaned"; _save
}

update_member() {
  _load; _require_env
  [ -n "${X:-}" ] && [ -n "${VM_ID:-}" ] && [ -n "${CLUSTER:-}" ] || die "need X/VM_ID/CLUSTER in $STATE"
  local nh allowed out j mode remote_x
  nh=$(_box_run hash | grep -oE '^[0-9a-f]{64}$' | tail -1)
  [ -n "$nh" ] || die "could not compute new compose_hash"
  log "new compose_hash=0x$nh"
  allowed=$(cast call "$CLUSTER" 'allowedComposeHashes(bytes32)(bool)' "0x$nh" --rpc-url "$RPC_URL" 2>/dev/null)
  if [ "$allowed" != true ]; then
    send_seq "generic-update-addHash-${NODE}" "$CLUSTER" "addComposeHash(bytes32)" "0x$nh"
  else
    log "compose hash already allowlisted"
  fi
  out=$(_box_run update "$X" "$VM_ID") || die "in-place update failed"
  echo "$out"
  j=$(echo "$out" | grep '"app_id"' | tail -1)
  remote_x=$(echo "$j" | jq -r '.app_id // empty')
  H=$(echo "$j" | jq -r .compose_hash)
  VM_ID=$(echo "$j" | jq -r '.vm_id // empty')
  _validate_optional_address X "$remote_x"
  [ -n "$remote_x" ] && [ "${remote_x,,}" = "${X,,}" ] \
    || die "box update returned an invalid or mismatched app_id"
  [[ "$H" =~ ^[0-9a-fA-F]{64}$ ]] && [ "${H,,}" != "${ZERO32#0x}" ] \
    || die "box update returned an invalid compose_hash"
  [[ "$VM_ID" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$ ]] && [ "$VM_ID" != null ] \
    || die "box update returned an invalid VM_ID"
  GUEST_CONFIG_SHA256="$CURRENT_GUEST_CONFIG_SHA256"
  _save
  mode=$(echo "$j" | jq -r '.mode // "upgrade"')
  log "✔ generic node update complete mode=$mode vm=$VM_ID"
}

register_direct() {
  "$HERE/indexer-member-node.sh" "$NODE" register-member-direct
}

log "=== generic AttestMesh node: $NODE ==="
case "$ACTION" in
  preflight) preflight ;;
  hash) compose_hash ;;
  guest-config-sha256) guest_config_sha256 ;;
  deploy) deploy_cvm ;;
  start) start_cvm ;;
  prime) prime_gate ;;
  bind) bind_member ;;
  verify) verify ;;
  register-direct) register_direct ;;
  update) update_member ;;
  cleanup|stop) cleanup ;;
  all) preflight; deploy_cvm; prime_gate; bind_member; start_cvm; register_direct; verify ;;
  *) die "usage: generic-node.sh <node-name> [hash|guest-config-sha256|preflight|deploy|start|prime|bind|verify|register-direct|update|cleanup|stop|all]" ;;
esac
