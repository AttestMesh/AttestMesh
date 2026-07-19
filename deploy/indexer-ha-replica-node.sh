#!/usr/bin/env bash
# Deploy one worker in the dedicated Stage-A Indexer signer cluster.
#
# This wrapper intentionally has no Matrix-cluster fallback and no action that writes
# IndexerRegistry. Safe-owned app-id admission is a human approval boundary:
# `prepare` emits exact Safe transaction JSON, then `finish` waits for that transaction
# before binding, starting, registering, and validating the warmed candidate.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"

NODE="${1:?usage: indexer-ha-replica-node.sh <replica-name> <action>}"
ACTION="${2:-prepare}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/indexer-ha-replica-node.yaml}"
PRIVATE_STATE_DIR="${INDEXER_HA_STATE_DIR:-$HOME/.attestmesh/indexer-ha}"
CLUSTER_STATE="${INDEXER_HA_CLUSTER_STATE:-$PRIVATE_STATE_DIR/cluster.state}"
REPLICA_STATE_DIR="${INDEXER_HA_REPLICA_STATE_DIR:-$PRIVATE_STATE_DIR}"
REPLICA_STATE="$REPLICA_STATE_DIR/generic-node-${NODE}.state"
SAFE_PAYLOAD="${INDEXER_HA_SAFE_PAYLOAD:-$PRIVATE_STATE_DIR/safe-admission-${NODE}.json}"
DRAIN_STATE="${INDEXER_HA_DRAIN_STATE:-$PRIVATE_STATE_DIR/drain-${NODE}.json}"
DRAIN_LOCK_ROOT="${INDEXER_HA_DRAIN_LOCK_ROOT:-$(dirname "$DRAIN_STATE")}"
MEASURED_COMPOSE_NAME="${INDEXER_HA_COMPOSE_NAME:-attestmesh-indexer-ha-replica}"
REQUESTED_CLUSTER_NAME="${INDEXER_HA_CLUSTER_NAME:-attestmesh-indexer-ha}"
CLUSTER_NAME="$REQUESTED_CLUSTER_NAME"
INDEXER_DEVICE_IDS_JSON="${INDEXER_DEVICE_IDS_JSON:-}"
MATRIX_STATE="${MATRIX_STATE:-$LOGDIR/matrix-node-matrix-node.state}"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
ZERO_ADDRESS=0x0000000000000000000000000000000000000000
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000
ENTRY_POINT_V07=0x0000000071727De22E5E9d8BAf0edAc6f37da032
STAGE_A_SIDECAR_DIGEST=87d7ee2b1a85e903d80791551e813001cfdd7b2cecf131c8d7e407d2c7d7ecfb
STAGE_A_INDEXER_DIGEST=5f74139bd53b04d1a9152dacd6e8bd4104f75c04a4fc37195ed43784766448a5

export COMPOSE GATEWAY_DOMAIN
export BOX_COMPOSE_NAME="$MEASURED_COMPOSE_NAME"
export BOX_VCPU="${BOX_VCPU:-4}" BOX_MEM="${BOX_MEM:-8192}" BOX_DISK="${BOX_DISK:-80}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}"
export BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

[[ "$NODE" =~ ^[a-zA-Z0-9._-]+$ ]] || die "replica name contains unsupported characters"
[[ "$MEASURED_COMPOSE_NAME" =~ ^[a-zA-Z0-9._-]+$ ]] \
  || die "INDEXER_HA_COMPOSE_NAME contains unsupported characters"

_tools() {
  local tool
  for tool in cast curl flock jq python3 ssh scp; do
    command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
  done
}

_ensure_private_parent() {
  python3 - "$1" <<'PY'
import os
import stat
import sys

parent = os.path.dirname(os.path.abspath(sys.argv[1]))
old_umask = os.umask(0o077)
try:
    os.makedirs(parent, mode=0o700, exist_ok=True)
finally:
    os.umask(old_umask)
fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise SystemExit(f"private state parent is not a directory: {parent}")
    if info.st_uid != os.getuid():
        raise SystemExit(f"private state parent is not owned by the current user: {parent}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise SystemExit(f"private state parent must have exact mode 0700: {parent}")
finally:
    os.close(fd)
ancestor = os.path.dirname(parent)
ancestor_fd = os.open(ancestor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    info = os.fstat(ancestor_fd)
    if info.st_uid != os.getuid():
        raise SystemExit(f"private state ancestor is not owned by the current user: {ancestor}")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise SystemExit(f"private state ancestor must not be group/world writable: {ancestor}")
finally:
    os.close(ancestor_fd)
PY
}

_durable_write_private_file() {
  local path="$1" value="$2"
  _ensure_private_parent "$path" || die "private state parent validation failed"
  python3 - "$path" 3< <(printf '%s' "$value") <<'PY'
import os
import stat
import sys
import tempfile

path = os.path.abspath(sys.argv[1])
parent, name = os.path.dirname(path), os.path.basename(path)
raw = os.fdopen(3, "rb").read()
if not raw.endswith(b"\n"):
    raw += b"\n"
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
tmp_path = ""
try:
    try:
        existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise SystemExit("refusing non-regular private state target")
    fd, tmp_path = tempfile.mkstemp(prefix=".attestmesh-state.", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            os.path.basename(tmp_path),
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        tmp_path = ""
        os.fsync(directory_fd)
    finally:
        if tmp_path:
            try:
                os.unlink(os.path.basename(tmp_path), dir_fd=directory_fd)
            except FileNotFoundError:
                pass
finally:
    os.close(directory_fd)
PY
}

_read_private_file() {
  local path="$1" limit="$2"
  _ensure_private_parent "$path" || die "private state parent validation failed"
  python3 - "$path" "$limit" <<'PY'
import os
import stat
import sys

path = os.path.abspath(sys.argv[1])
limit = int(sys.argv[2])
parent, name = os.path.dirname(path), os.path.basename(path)
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("private state is not a regular file")
        if info.st_uid != os.getuid():
            raise SystemExit("private state is not owned by the current user")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("private state must have exact mode 0600")
        if info.st_size < 1 or info.st_size > limit:
            raise SystemExit(f"private state must be between 1 and {limit} bytes")
        chunks = []
        size = 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise SystemExit(f"private state exceeds {limit} bytes")
        sys.stdout.buffer.write(b"".join(chunks))
    finally:
        os.close(fd)
finally:
    os.close(directory_fd)
PY
}

_acquire_drain_lock() {
  local owner
  _ensure_private_parent "$DRAIN_LOCK_ROOT/.lock-anchor" \
    || die "private drain lock root validation failed"
  [ ! -L "$DRAIN_LOCK_ROOT" ] \
    || die "refusing symlinked worker drain lock root: $DRAIN_LOCK_ROOT"
  [ -d "$DRAIN_LOCK_ROOT" ] \
    || die "worker drain lock root is not a directory: $DRAIN_LOCK_ROOT"
  owner=$(stat -c '%u' "$DRAIN_LOCK_ROOT") \
    || die "cannot read worker drain lock root owner"
  [ "$owner" = "$(id -u)" ] \
    || die "worker drain lock root is not owned by the current user"
  # Lock the already-open directory inode. This avoids following or reopening a
  # mutable lock-file pathname; drain operations are rare, so serializing workers
  # that share one LOGDIR is an intentional safety tradeoff.
  exec {DRAIN_LOCK_FD}<"$DRAIN_LOCK_ROOT" \
    || die "cannot open worker drain lock root: $DRAIN_LOCK_ROOT"
  flock -n "$DRAIN_LOCK_FD" \
    || die "another worker stop/release operation is already running in $DRAIN_LOCK_ROOT"
}

_require_reviewed_images() {
  local sidecar_count indexer_count
  [ -s "$COMPOSE" ] || die "missing compose file: $COMPOSE"
  sidecar_count=$(grep -Ec \
    "^[[:space:]]*image:[[:space:]]*ghcr.io/attestmesh/cluster-mesh-agent@sha256:${STAGE_A_SIDECAR_DIGEST}([[:space:]]|$)" \
    "$COMPOSE" || true)
  indexer_count=$(grep -Ec \
    "^[[:space:]]*image:[[:space:]]*ghcr.io/attestmesh/attestmesh-indexer@sha256:${STAGE_A_INDEXER_DIGEST}([[:space:]]|$)" \
    "$COMPOSE" || true)
  [ "$sidecar_count" = 1 ] && [ "$indexer_count" = 2 ] \
    || die "compose must use the reviewed Stage-A sidecar and Indexer OCI digests before cluster state, deploy, admission, or start"
}

_address() {
  local label="$1" value="$2"
  [[ "$value" =~ ^0x[0-9a-fA-F]{40}$ ]] || die "$label must be a 20-byte 0x address"
  [ "${value,,}" != "$ZERO_ADDRESS" ] || die "$label must be nonzero"
}

_bytes32() {
  local label="$1" value="$2"
  [[ "$value" =~ ^0x[0-9a-fA-F]{64}$ ]] || die "$label must be a 32-byte 0x value"
  [ "${value,,}" != "$ZERO32" ] || die "$label must be nonzero"
}

_canonicalize_device_ids() {
  local canonical
  canonical=$(printf '%s\n' "$INDEXER_DEVICE_IDS_JSON" | jq -ce '
    . as $ids
    | if type == "array"
        and length > 0
        and all(.[];
          type == "string"
          and test("^0x[0-9a-fA-F]{64}$")
          and ascii_downcase != ("0x" + ("0" * 64)))
        and (($ids | map(ascii_downcase) | unique | length) == ($ids | length))
      then map(ascii_downcase) | sort
      else error("invalid Indexer device-id set")
      end
  ') || die "INDEXER_DEVICE_IDS_JSON must be a non-empty unique nonzero bytes32 JSON array"
  INDEXER_DEVICE_IDS_JSON="$canonical"
}

_state_value() {
  local file="$1" key="$2"
  sed -n "s/^${key}=//p" "$file" 2>/dev/null | tail -1
}

_validate_cluster_values() {
  require CHAIN_ID RPC_URL CLUSTER CLUSTER_NAME INDEXER_DEVICE_IDS_JSON \
    DSTACK_FACET MEMBER_IMPL INDEXER_COMPOSE_HASH INDEXER_CLUSTER_OWNER KMS_ROOT
  [[ "$CHAIN_ID" =~ ^[1-9][0-9]*$ ]] || die "CHAIN_ID must be a positive decimal integer"
  _address CLUSTER "$CLUSTER"
  _address DSTACK_FACET "$DSTACK_FACET"
  _address MEMBER_IMPL "$MEMBER_IMPL"
  _address INDEXER_CLUSTER_OWNER "$INDEXER_CLUSTER_OWNER"
  _address KMS_ROOT "$KMS_ROOT"
  _bytes32 INDEXER_COMPOSE_HASH "$INDEXER_COMPOSE_HASH"
  [[ "$CLUSTER_NAME" =~ ^[a-zA-Z0-9._-]+$ ]] \
    || die "INDEXER_HA_CLUSTER_NAME contains unsupported characters"
  _canonicalize_device_ids
  [[ "$MEASURED_COMPOSE_NAME" =~ ^[a-zA-Z0-9._-]+$ ]] \
    || die "INDEXER_HA_COMPOSE_NAME contains unsupported characters"
}

_verify_cluster_policy() {
  local main_cluster chain
  _validate_cluster_values
  chain=$(cast chain-id --rpc-url "$RPC_URL") || die "RPC_URL is unavailable"
  [ "$chain" = "$CHAIN_ID" ] || die "RPC chain $chain does not match CHAIN_ID=$CHAIN_ID"
  export CHAIN_ID RPC_URL CLUSTER CLUSTER_NAME INDEXER_DEVICE_IDS_JSON
  export DSTACK_FACET MEMBER_IMPL INDEXER_COMPOSE_HASH INDEXER_CLUSTER_OWNER KMS_ROOT
  "$HERE/onchain.sh" indexer-stage-a-verify "$CLUSTER" "$CLUSTER_NAME" \
    || die "dedicated Indexer cluster failed the complete read-only Stage-A verification"
  if [ -f "$MATRIX_STATE" ]; then
    main_cluster="$(_state_value "$MATRIX_STATE" CLUSTER)"
    if [ -n "$main_cluster" ] && [ "${main_cluster,,}" = "${CLUSTER,,}" ]; then
      die "dedicated Indexer CLUSTER must not be the Matrix/general C3 cluster"
    fi
  fi
}

_verify_fresh_cluster() {
  local members commitment
  members=$(cast call "$CLUSTER" 'memberCount()(uint256)' --rpc-url "$RPC_URL") \
    || die "cannot read dedicated Indexer cluster member count"
  commitment=$(cast call "$CLUSTER" 'cskCommitment()(bytes32)' --rpc-url "$RPC_URL") \
    || die "cannot read dedicated Indexer cluster CSK commitment"
  [ "$members" = 0 ] \
    || die "cluster state must be saved before the first member registers (memberCount=$members)"
  [ "${commitment,,}" = "$ZERO32" ] \
    || die "cluster state must be saved before the first CSK commitment exists"
}

save_cluster_state() {
  local state_value
  _tools
  _require_reviewed_images
  _verify_cluster_policy
  _verify_fresh_cluster
  verify_rendered_hash
  if { [ -e "$CLUSTER_STATE" ] || [ -L "$CLUSTER_STATE" ]; } \
      && [ "${FORCE:-0}" != 1 ]; then
    die "$CLUSTER_STATE already exists; set FORCE=1 only for an intentional replacement"
  fi
  printf -v state_value '%s\n' \
    "UPDATED_AT=$(ts)" \
    "CHAIN_ID=$CHAIN_ID" \
    "CLUSTER=$CLUSTER" \
    "CLUSTER_NAME=$CLUSTER_NAME" \
    "INDEXER_DEVICE_IDS_JSON=$INDEXER_DEVICE_IDS_JSON" \
    "DSTACK_FACET=$DSTACK_FACET" \
    "MEMBER_IMPL=$MEMBER_IMPL" \
    "INDEXER_COMPOSE_HASH=${INDEXER_COMPOSE_HASH,,}" \
    "INDEXER_CLUSTER_OWNER=$INDEXER_CLUSTER_OWNER" \
    "KMS_ROOT=$KMS_ROOT" \
    "MEASURED_COMPOSE_NAME=$MEASURED_COMPOSE_NAME"
  _durable_write_private_file "$CLUSTER_STATE" "$state_value" \
    || die "could not durably persist dedicated Indexer cluster state"
  log "saved dedicated Indexer cluster state -> $CLUSTER_STATE"
}

_load_cluster_state() {
  local line key value required content
  content=$( { _read_private_file "$CLUSTER_STATE" 65536; rc=$?; printf '\034'; exit "$rc"; } ) \
    || die "missing or unsafe dedicated state $CLUSTER_STATE; run save-cluster-state with explicit values"
  content="${content%$'\034'}"
  declare -A state=() seen=()
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || die "blank line in dedicated cluster state: $CLUSTER_STATE"
    [[ "$line" == *=* ]] || die "malformed line in dedicated cluster state: $CLUSTER_STATE"
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      UPDATED_AT|CHAIN_ID|CLUSTER|CLUSTER_NAME|INDEXER_DEVICE_IDS_JSON|DSTACK_FACET|MEMBER_IMPL|INDEXER_COMPOSE_HASH|INDEXER_CLUSTER_OWNER|KMS_ROOT|MEASURED_COMPOSE_NAME) ;;
      *) die "unknown dedicated cluster state field '$key' in $CLUSTER_STATE" ;;
    esac
    [ -z "${seen[$key]+present}" ] \
      || die "duplicate dedicated cluster state field '$key' in $CLUSTER_STATE"
    seen[$key]=1
    state[$key]="$value"
  done < <(printf '%s' "$content")
  for required in UPDATED_AT CHAIN_ID CLUSTER CLUSTER_NAME INDEXER_DEVICE_IDS_JSON DSTACK_FACET MEMBER_IMPL INDEXER_COMPOSE_HASH INDEXER_CLUSTER_OWNER KMS_ROOT MEASURED_COMPOSE_NAME; do
    [ -n "${seen[$required]+present}" ] \
      || die "dedicated cluster state is missing $required: $CLUSTER_STATE"
  done
  [[ "${state[UPDATED_AT]}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
    || die "dedicated cluster state UPDATED_AT is malformed"
  CHAIN_ID="${state[CHAIN_ID]}"
  CLUSTER="${state[CLUSTER]}"
  CLUSTER_NAME="${state[CLUSTER_NAME]}"
  INDEXER_DEVICE_IDS_JSON="${state[INDEXER_DEVICE_IDS_JSON]}"
  DSTACK_FACET="${state[DSTACK_FACET]}"
  MEMBER_IMPL="${state[MEMBER_IMPL]}"
  INDEXER_COMPOSE_HASH="${state[INDEXER_COMPOSE_HASH]}"
  INDEXER_CLUSTER_OWNER="${state[INDEXER_CLUSTER_OWNER]}"
  KMS_ROOT="${state[KMS_ROOT]}"
  MEASURED_COMPOSE_NAME="${state[MEASURED_COMPOSE_NAME]}"
  [ "${MEASURED_COMPOSE_NAME:-}" = "$BOX_COMPOSE_NAME" ] \
    || die "cluster state compose name differs from INDEXER_HA_COMPOSE_NAME=$BOX_COMPOSE_NAME"
  [ "$CLUSTER_NAME" = "$REQUESTED_CLUSTER_NAME" ] \
    || die "cluster state name differs from INDEXER_HA_CLUSTER_NAME=$REQUESTED_CLUSTER_NAME"
  local persisted_device_ids="$INDEXER_DEVICE_IDS_JSON"
  _validate_cluster_values
  [ "$INDEXER_DEVICE_IDS_JSON" = "$persisted_device_ids" ] \
    || die "cluster state INDEXER_DEVICE_IDS_JSON is not canonical lowercase/sorted/minified JSON"
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$(
    jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json"
  )}"
  require INDEXER_REGISTRY_ADDR GATEWAY_DOMAIN
  _address INDEXER_REGISTRY_ADDR "$INDEXER_REGISTRY_ADDR"
  export CHAIN_ID CLUSTER CLUSTER_NAME INDEXER_DEVICE_IDS_JSON DSTACK_FACET MEMBER_IMPL
  export INDEXER_COMPOSE_HASH INDEXER_CLUSTER_OWNER KMS_ROOT
  export INDEXER_REGISTRY_ADDR GATEWAY_DOMAIN
}

_runtime_env() {
  require PRIVATE_KEY DEPLOYER_ADDR RPC_URL GAS_POLICY_ID
  [ -n "${GAS_POLICY_ID//[[:space:]]/}" ] || die "GAS_POLICY_ID must not be blank"
  local rpc="${RPC_URL%/}" guest_rpc="${CVM_RPC_URL:-$RPC_URL}" bundler_compare
  GUEST_BUNDLER_URL="${CVM_BUNDLER_URL:-${BUNDLER_URL:-$RPC_URL}}"
  bundler_compare="${GUEST_BUNDLER_URL%/}"
  guest_rpc="${guest_rpc%/}"
  [ -n "$GUEST_BUNDLER_URL" ] || die "CVM_BUNDLER_URL/BUNDLER_URL must not be blank"
  [ "$bundler_compare" != "$rpc" ] || die "sealed guest BUNDLER_URL must not equal RPC_URL"
  [ "$bundler_compare" != "$guest_rpc" ] \
    || die "sealed guest BUNDLER_URL must not equal CVM_RPC_URL"
  export GUEST_BUNDLER_URL
}

_bundler_rpc() {
  local method="$1"
  curl -fsS --max-time 15 -H 'content-type: application/json' \
    --data "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$method\",\"params\":[]}" \
    "$GUEST_BUNDLER_URL"
}

preflight_bundler() {
  local entries chain expected
  _runtime_env
  entries="$(_bundler_rpc eth_supportedEntryPoints)" \
    || die "sealed guest BUNDLER_URL did not answer eth_supportedEntryPoints"
  echo "$entries" | jq -e --arg entry "${ENTRY_POINT_V07,,}" \
    '.result
     | type == "array"
       and length > 0
       and all(.[]; test("^0x[0-9a-fA-F]{40}$"))
       and (map(ascii_downcase) | index($entry) != null)' \
    >/dev/null \
    || die "sealed guest BUNDLER_URL does not support the sidecar's canonical v0.7 EntryPoint $ENTRY_POINT_V07"
  chain="$(_bundler_rpc eth_chainId)" || die "sealed guest BUNDLER_URL did not answer eth_chainId"
  chain=$(echo "$chain" | jq -er '.result | select(type == "string")') \
    || die "sealed guest BUNDLER_URL returned an invalid chain id"
  expected=$(printf '0x%x' "$CHAIN_ID")
  [ "${chain,,}" = "$expected" ] \
    || die "sealed guest BUNDLER_URL chain id $chain does not match CHAIN_ID=$CHAIN_ID"
  log "ERC-4337 bundler preflight passed (canonical v0.7 entry point + chain id; gas policy present)"
}

preflight_guest_rpc() {
  local guest_rpc="${CVM_RPC_URL:-$RPC_URL}" chain
  chain=$(cast chain-id --rpc-url "$guest_rpc") \
    || die "sealed CVM_RPC_URL is unavailable"
  [ "$chain" = "$CHAIN_ID" ] \
    || die "sealed CVM_RPC_URL chain $chain does not match CHAIN_ID=$CHAIN_ID"
  log "sealed guest RPC preflight passed (chain id $chain)"
}

_generic() {
  local action="$1" allow_gateway_drift=0
  case "$action" in
    cleanup|stop) allow_gateway_drift=1 ;;
  esac
  COMPOSE="$COMPOSE" BOX_COMPOSE_NAME="$BOX_COMPOSE_NAME" \
    GENERIC_STATE_DIR="$REPLICA_STATE_DIR" REQUIRE_PRIVATE_GENERIC_STATE=1 \
    EXPECTED_GENERIC_STATE_SHA256="${EXPECTED_REPLICA_STATE_SHA256:-}" \
    ALLOW_GENERIC_GATEWAY_DRIFT="$allow_gateway_drift" \
    REQUIRE_GUEST_CONFIG_FINGERPRINT=1 STRICT_GENERIC_STATE_BINDINGS=1 \
    "$HERE/generic-node.sh" "$NODE" "$action"
}

rendered_hash() {
  require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR
  local hash
  hash=$(_generic hash | grep -oE '^[0-9a-fA-F]{64}$' | tail -1)
  [ -n "$hash" ] || die "could not render the shared replica compose hash"
  printf '0x%s\n' "${hash,,}"
}

verify_rendered_hash() {
  local actual
  actual=$(rendered_hash)
  [ "$actual" = "${INDEXER_COMPOSE_HASH,,}" ] \
    || die "rendered compose hash $actual does not match signer-cluster hash $INDEXER_COMPOSE_HASH"
  log "rendered replica hash matches signer cluster: $actual"
}

preflight() {
  _tools
  _load_cluster_state
  _runtime_env
  _require_reviewed_images
  preflight_guest_rpc
  preflight_bundler
  _verify_cluster_policy
  verify_rendered_hash
  _generic preflight
}

_load_replica_state() {
  local drift_mode="${1:-enforce-config}"
  local expected_cluster="$CLUSTER" expected_impl="$MEMBER_IMPL"
  local expected_kms="$KMS_ROOT" expected_hash="${INDEXER_COMPOSE_HASH,,}"
  local actual_hash current_guest_hash line key value required content
  declare -A state=() seen=()
  content=$( { _read_private_file "$REPLICA_STATE" 65536; rc=$?; printf '\034'; exit "$rc"; } ) \
    || die "missing or unsafe replica state $REPLICA_STATE; run deploy first"
  content="${content%$'\034'}"
  REPLICA_STATE_SHA256=$(printf '%s' "$content" | sha256sum | awk '{print $1}')
  [[ "$REPLICA_STATE_SHA256" =~ ^[0-9a-f]{64}$ ]] \
    || die "could not fingerprint the immutable replica state snapshot"
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || die "blank line in replica state: $REPLICA_STATE"
    [[ "$line" == *=* ]] || die "malformed line in replica state: $REPLICA_STATE"
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      STATE_SCHEMA|UPDATED_AT|STATE_PHASE|X|H|VM_ID|CLUSTER|MEMBER_IMPL|KMS_ROOT|GATEWAY_DOMAIN|GUEST_CONFIG_SHA256) ;;
      *) die "unknown replica state field '$key' in $REPLICA_STATE" ;;
    esac
    [ -z "${seen[$key]+present}" ] || die "duplicate replica state field '$key' in $REPLICA_STATE"
    seen[$key]=1
    state[$key]="$value"
  done < <(printf '%s' "$content")
  for required in STATE_SCHEMA UPDATED_AT STATE_PHASE X H VM_ID CLUSTER MEMBER_IMPL KMS_ROOT GATEWAY_DOMAIN GUEST_CONFIG_SHA256; do
    [ -n "${seen[$required]+present}" ] || die "replica state is missing $required: $REPLICA_STATE"
  done
  [ "${state[STATE_SCHEMA]}" = 2 ] || die "replica state must use schema 2"
  [[ "${state[UPDATED_AT]}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
    || die "replica state UPDATED_AT is malformed"
  case "${state[STATE_PHASE]}" in
    preflighted|deployed-stopped|primed|bound|started|registered|cleaned) ;;
    *) die "replica state STATE_PHASE is invalid: ${state[STATE_PHASE]}" ;;
  esac
  X="${state[X]}"
  H="${state[H]}"
  VM_ID="${state[VM_ID]}"
  _address X "$X"
  _bytes32 H "0x${H#0x}"
  [[ "$VM_ID" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$ ]] \
    || die "replica state VM_ID contains unsupported characters"
  [[ "${state[GATEWAY_DOMAIN]}" =~ ^[a-zA-Z0-9._:-]+$ ]] \
    || die "replica state GATEWAY_DOMAIN contains unsupported characters"
  [ "${state[CLUSTER],,}" = "${expected_cluster,,}" ] \
    || die "replica state belongs to cluster ${state[CLUSTER]}, expected $expected_cluster"
  [ "${state[MEMBER_IMPL],,}" = "${expected_impl,,}" ] \
    || die "replica state MEMBER_IMPL differs from dedicated cluster state"
  [ "${state[KMS_ROOT],,}" = "${expected_kms,,}" ] \
    || die "replica state KMS_ROOT differs from dedicated cluster state"
  if [ "$drift_mode" != allow-config-drift ]; then
    [ "${state[GATEWAY_DOMAIN]}" = "$GATEWAY_DOMAIN" ] \
      || die "replica state GATEWAY_DOMAIN differs from the requested gateway"
  fi
  actual_hash="0x${H#0x}"
  actual_hash="${actual_hash,,}"
  [ "$actual_hash" = "$expected_hash" ] \
    || die "replica compose hash $actual_hash differs from dedicated cluster state"
  [[ "${state[GUEST_CONFIG_SHA256]}" =~ ^[0-9a-fA-F]{64}$ ]] \
    || die "replica state GUEST_CONFIG_SHA256 is malformed"
  if [ "$drift_mode" != allow-config-drift ]; then
    current_guest_hash=$(_generic guest-config-sha256 | grep -oE '^[0-9a-fA-F]{64}$' | tail -1)
    [ -n "$current_guest_hash" ] || die "could not fingerprint the current sealed guest configuration"
    [ "${state[GUEST_CONFIG_SHA256],,}" = "${current_guest_hash,,}" ] \
      || die "sealed guest configuration drifted since replica deployment; redeploy instead of continuing"
  fi
  H="${H#0x}"
  INDEXER_COMPOSE_HASH="$expected_hash"
  EXPECTED_REPLICA_STATE_SHA256="$REPLICA_STATE_SHA256"
}

deploy_replica() {
  preflight
  _generic deploy
  # generic-node persists CLUSTER/H/VM_ID under the unique NODE name; LB named
  # backend resolution consumes that exact state without any Matrix fallback.
  _load_replica_state
  log "deployed stopped shared replica app=$X vm=$VM_ID measured_name=$BOX_COMPOSE_NAME"
}

safe_admission() {
  local dedicated_cluster calldata created payload
  _load_cluster_state
  _require_reviewed_images
  _verify_cluster_policy
  dedicated_cluster="$CLUSTER"
  _load_replica_state
  calldata=$(cast calldata 'addAllowedAppId(address)' "$X")
  created=$(( $(date +%s) * 1000 ))
  payload=$(jq -c \
    -n \
    --arg version "1.0" \
    --arg chainId "$CHAIN_ID" \
    --argjson createdAt "$created" \
    --arg safe "$INDEXER_CLUSTER_OWNER" \
    --arg name "Admit $NODE to dedicated Indexer signer cluster" \
    --arg to "$dedicated_cluster" \
    --arg data "$calldata" \
    --arg appId "$X" \
    --arg composeHash "0x${H#0x}" \
    '{version:$version, chainId:$chainId, createdAt:$createdAt,
      meta:{name:$name,description:"Safe approval required before the replica may boot",
            txBuilderVersion:"1.18.0",createdFromSafeAddress:$safe,
            checksum:"",appId:$appId,composeHash:$composeHash},
      transactions:[{to:$to,value:"0",data:$data,
                     contractMethod:null,contractInputsValues:null}]}') \
    || die "could not encode Safe app-admission payload"
  _durable_write_private_file "$SAFE_PAYLOAD" "$payload" \
    || die "could not durably persist Safe app-admission payload"
  printf 'SAFE_ADDRESS=%s\nSAFE_TARGET=%s\nSAFE_VALUE=0\nSAFE_CALLDATA=%s\nSAFE_JSON=%s\n' \
    "$INDEXER_CLUSTER_OWNER" "$dedicated_cluster" "$calldata" "$SAFE_PAYLOAD"
  log "Safe-ready app-id admission emitted; do not start $NODE before it is executed"
}

wait_admission() {
  _load_cluster_state
  local dedicated_cluster="$CLUSTER" attempts="${INDEXER_ADMISSION_ATTEMPTS:-180}" i allowed
  _load_replica_state
  [[ "$attempts" =~ ^[0-9]+$ ]] && [ "$attempts" -gt 0 ] \
    || die "INDEXER_ADMISSION_ATTEMPTS must be a positive integer"
  for i in $(seq 1 "$attempts"); do
    allowed=$(cast call "$dedicated_cluster" 'allowedAppIds(address)(bool)' "$X" \
      --rpc-url "$RPC_URL" 2>/dev/null || true)
    if [ "$allowed" = true ]; then
      _verify_cluster_policy
      log "Safe admission verified on-chain for $NODE app=$X"
      return 0
    fi
    log "waiting for Safe app-id admission ($i/$attempts)"
    sleep 5
  done
  die "Safe did not admit app $X to cluster $dedicated_cluster"
}

bind_replica() {
  _load_cluster_state
  local dedicated_cluster="$CLUSTER" current
  _load_replica_state
  wait_admission
  current=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null || true)
  if [ "${current,,}" = "${dedicated_cluster,,}" ]; then
    log "replica app is already bound to dedicated cluster"
    return 0
  fi
  _generic bind
}

start_replica() {
  _load_cluster_state
  local dedicated_cluster="$CLUSTER" current
  _load_replica_state
  _runtime_env
  _require_reviewed_images
  preflight_guest_rpc
  preflight_bundler
  wait_admission
  current=$(cast call "$X" 'cluster()(address)' --rpc-url "$RPC_URL" 2>/dev/null || true)
  [ "${current,,}" = "${dedicated_cluster,,}" ] \
    || die "replica app is not bound to the dedicated cluster; run bind first"
  _generic start
}

register_direct() {
  _load_cluster_state
  _load_replica_state
  _verify_cluster_policy
  _generic register-direct
  _verify_cluster_policy
}

verify_member() {
  _load_cluster_state
  _load_replica_state
  _verify_cluster_policy
  _generic verify
  _verify_cluster_policy
}

verify_candidate() {
  _load_cluster_state
  local dedicated_cluster="$CLUSTER" expected_hash expected_member host attempts min_clusters i status=""
  _load_replica_state
  _verify_cluster_policy
  expected_hash="0x${H#0x}"
  expected_member=$(cast call "$dedicated_cluster" 'memberIdOf(address)(bytes32)' "$X" \
    --rpc-url "$RPC_URL") || die "cannot resolve serving member id"
  _bytes32 servingMemberId "$expected_member"
  host="$(printf '%s' "${X#0x}" | tr '[:upper:]' '[:lower:]')-9090.${GATEWAY_DOMAIN}"
  attempts="${INDEXER_VERIFY_ATTEMPTS:-180}"
  min_clusters="${INDEXER_MIN_CLUSTER_COUNT:-1}"
  [[ "$attempts" =~ ^[0-9]+$ ]] && [ "$attempts" -gt 0 ] \
    || die "INDEXER_VERIFY_ATTEMPTS must be a positive integer"
  [[ "$min_clusters" =~ ^[0-9]+$ ]] && [ "$min_clusters" -gt 0 ] \
    || die "INDEXER_MIN_CLUSTER_COUNT must be a positive integer"
  for i in $(seq 1 "$attempts"); do
    status=$(curl -fsm 10 "https://${host}/status" 2>/dev/null || true)
    if echo "$status" | jq -e \
      --arg code "${expected_hash,,}" \
      --arg cluster "${dedicated_cluster,,}" \
      --arg member "${expected_member,,}" \
      --argjson chain "$CHAIN_ID" \
      --argjson min "$min_clusters" '
        .chainId == $chain
        and .identityMode == "cluster-shared"
        and ((.codeId // "") | ascii_downcase) == $code
        and ((.indexerCluster // "") | ascii_downcase) == $cluster
        and ((.servingMemberId // "") | ascii_downcase) == $member
        and ((.pubKey // "") | test("^0x[0-9a-fA-F]{64}$"))
        and .pubKey != "0x0000000000000000000000000000000000000000000000000000000000000000"
        and .health.rpcReachable == true
        and (.health.grpcAccepting | type == "boolean")
        and (.health.chainHeadLagBlocks | type == "number")
        and .health.chainHeadLagBlocks >= 0
        and .health.chainHeadLagBlocks < 10
        and (.readModel.atBlock | type == "number")
        and (.readModel.clusterCount | type == "number")
        and .readModel.clusterCount >= $min
        and (.readModel.memberCount | type == "number")
        and .readModel.memberCount > 0
      ' >/dev/null 2>&1; then
      _verify_cluster_policy
      echo "$status" | jq \
        '{pubKey,codeId,identityMode,indexerCluster,servingMemberId,health,readModel}'
      log "warmed shared candidate verified (grpcAccepting may remain false until registry rotation)"
      return 0
    fi
    log "waiting for exact shared identity and caught-up read model ($i/$attempts)"
    sleep 10
  done
  die "shared candidate failed exact identity/readiness validation: ${status:-no status}"
}

_rfc1918_backend() {
  python3 - "$1" <<'PY'
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
allowed = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"
))
if address.version != 4 or not any(address in network for network in allowed):
    raise SystemExit(1)
PY
}

_durable_write_drain_state() {
  local value="$1"
  value=$(echo "$value" | jq -ceS .) \
    || die "refusing to persist malformed worker drain JSON"
  _durable_write_private_file "$DRAIN_STATE" "$value"
}

_durable_remove_drain_state() {
  _ensure_private_parent "$DRAIN_STATE" || die "private drain state parent validation failed"
  python3 - "$DRAIN_STATE" <<'PY'
import os
import stat
import sys

path = os.path.abspath(sys.argv[1])
parent, name = os.path.dirname(path), os.path.basename(path)
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit("refusing non-regular worker drain state")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit("refusing unsafe worker drain state")
    os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

_load_drain_state() {
  local content normalized
  content=$( { _read_private_file "$DRAIN_STATE" 16384; rc=$?; printf '\034'; exit "$rc"; } ) \
    || die "missing or unsafe worker drain state: $DRAIN_STATE"
  content="${content%$'\034'}"
  normalized=$(printf '%s' "$content" | jq -ceS --arg worker "$NODE" '
    . as $state
    | select(
      ($state | keys) == ["active_backends","backend","created_at","drained","lb_node",
        "operation_id","release_token","reservation_id","schema","worker"]
      and $state.schema == "attestmesh.indexer-worker-drain.v1"
      and $state.worker == $worker
      and ($state.lb_node | type == "string" and test("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"))
      and ($state.backend | type == "string")
      and ($state.operation_id | type == "string" and test("^[0-9a-f]{64}$"))
      and ($state.reservation_id | type == "string" and test("^[0-9a-f]{64}$"))
      and ($state.release_token | type == "string" and test("^[0-9a-f]{64}$"))
      and $state.drained == true
      and ($state.created_at | type == "number" and floor == . and . >= 0)
      and ($state.active_backends | type == "array" and all(.[]; type == "string"))
      and ($state.active_backends | index($state.backend) == null)
    )
  ') || die "worker drain state is malformed or not bound to $NODE"
  DRAIN_LB_NODE=$(echo "$normalized" | jq -r .lb_node)
  DRAIN_BACKEND=$(echo "$normalized" | jq -r .backend)
  DRAIN_OPERATION_ID=$(echo "$normalized" | jq -r .operation_id)
  DRAIN_RESERVATION_ID=$(echo "$normalized" | jq -r .reservation_id)
  DRAIN_RELEASE_TOKEN=$(echo "$normalized" | jq -r .release_token)
  DRAIN_STATE_JSON="$normalized"
  _rfc1918_backend "$DRAIN_BACKEND" \
    || die "worker drain state backend is not an RFC1918 IPv4 address"
}

_normalize_drain_proof() {
  local proof="$1" lb_node="$2" operation="$3" normalized backend
  normalized=$(echo "$proof" | jq -ceS \
    --arg worker "$NODE" --arg lb "$lb_node" --arg operation "$operation" '
      select(
        .drained == true
        and .operation_id == $operation
        and .reserved_at_operation_id == $operation
        and (.backend | type == "string")
        and (.reservation_id | type == "string" and test("^[0-9a-f]{64}$"))
        and (.release_token | type == "string" and test("^[0-9a-f]{64}$"))
        and (.created_at | type == "number" and floor == . and . >= 0)
        and (.active_backends | type == "array" and all(.[]; type == "string"))
        and (.backend as $backend | .active_backends | index($backend) == null)
      )
      | {schema:"attestmesh.indexer-worker-drain.v1",worker:$worker,
         lb_node:$lb,backend,operation_id,drained,active_backends,
         reservation_id,release_token,created_at}
    ') || die "Indexer LB returned an invalid or mismatched drain reservation"
  backend=$(echo "$normalized" | jq -r .backend)
  _rfc1918_backend "$backend" \
    || die "Indexer LB drain reservation did not name an RFC1918 IPv4 backend"
  printf '%s\n' "$normalized"
}

_assert_lb_drained() {
  local operation="${INDEXER_LB_ACTIVE_OPERATION_ID:-}" lb_node="${INDEXER_LB_NODE:-}"
  local target="$NODE" proof normalized existing="" expected_state_hash="$REPLICA_STATE_SHA256"
  if [ -e "$DRAIN_STATE" ] || [ -L "$DRAIN_STATE" ]; then
    _load_drain_state
    [ -z "$lb_node" ] || [ "$lb_node" = "$DRAIN_LB_NODE" ] \
      || die "existing drain state belongs to LB $DRAIN_LB_NODE; release it before using $lb_node"
    [ -z "$operation" ] || [ "$operation" = "$DRAIN_OPERATION_ID" ] \
      || die "existing drain state belongs to operation $DRAIN_OPERATION_ID; release it before using $operation"
    lb_node="$DRAIN_LB_NODE"
    operation="$DRAIN_OPERATION_ID"
    target="$DRAIN_BACKEND"
    expected_state_hash=""
    existing="$DRAIN_STATE_JSON"
  else
    [ -n "$lb_node" ] || die "registered worker stop requires explicit INDEXER_LB_NODE"
    [[ "$lb_node" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$ ]] \
      || die "INDEXER_LB_NODE must be a safe node name of at most 128 characters"
    [[ "$operation" =~ ^[0-9a-f]{64}$ ]] \
      || die "registered worker stop requires INDEXER_LB_ACTIVE_OPERATION_ID as exactly 64 lowercase hex characters"
  fi
  proof=$(INDEXER_LB_ACTIVE_OPERATION_ID="$operation" \
    INDEXER_BACKEND_STATE_DIR="$REPLICA_STATE_DIR" \
    INDEXER_EXPECTED_BACKEND_STATE_SHA256="$expected_state_hash" \
    "$HERE/indexer-lb-node.sh" "$lb_node" assert-drained "$target") \
    || die "authenticated Indexer LB drain reservation failed; refusing to stop registered worker $NODE"
  normalized=$(_normalize_drain_proof "$proof" "$lb_node" "$operation")
  if [ -n "$existing" ]; then
    [ "$normalized" = "$existing" ] \
      || die "LB drain reservation differs from durable worker proof; run release-drain before retrying stop"
  else
    _durable_write_drain_state "$normalized" \
      || die "could not durably persist worker drain reservation; refusing VM stop"
    _load_drain_state
  fi
  log "durable LB drain reservation accepted for worker=$NODE backend=$(echo "$normalized" | jq -r .backend) operation=$operation"
}

release_drain_reservation() {
  local released="" reservations matching
  _tools
  _acquire_drain_lock
  _load_drain_state
  if released=$("$HERE/indexer-lb-node.sh" "$DRAIN_LB_NODE" release-drain \
      "$DRAIN_BACKEND" "$DRAIN_RESERVATION_ID" "$DRAIN_RELEASE_TOKEN"); then
    echo "$released" | jq -e \
      --arg backend "$DRAIN_BACKEND" --arg reservation "$DRAIN_RESERVATION_ID" '
        .released == true and .backend == $backend
        and .reservation_id == $reservation
        and (.operation_id | type == "string" and test("^[0-9a-f]{64}$"))
      ' >/dev/null || die "LB returned an invalid drain release response; local proof retained"
  else
    log "drain release response was unsuccessful or lost; reconciling the authenticated reservation list"
  fi
  reservations=$("$HERE/indexer-lb-node.sh" "$DRAIN_LB_NODE" drain-reservations) \
    || die "cannot reconcile LB drain reservations; local proof retained"
  matching=$(echo "$reservations" | jq -ce --arg backend "$DRAIN_BACKEND" \
    'select(
       type == "object"
       and keys == ["reservations"]
       and (.reservations | type == "array")
       and (.reservations | all(.[];
         type == "object"
         and (.backend | type == "string")
         and (.reservation_id | type == "string" and test("^[0-9a-f]{64}$"))
         and (.release_token | type == "string" and test("^[0-9a-f]{64}$"))))
     )
     | [.reservations[] | select(.backend == $backend)]') \
    || die "LB returned a malformed drain reservation list; local proof retained"
  if [ "$(echo "$matching" | jq length)" -ne 0 ]; then
    echo "$matching" | jq -e \
      --arg reservation "$DRAIN_RESERVATION_ID" --arg token "$DRAIN_RELEASE_TOKEN" '
        length == 1
        and .[0].reservation_id == $reservation
        and .[0].release_token == $token
      ' >/dev/null \
      || die "LB holds a different reservation for $DRAIN_BACKEND; local proof retained"
    die "LB drain reservation remains active for $DRAIN_BACKEND; local proof retained"
  fi
  _durable_remove_drain_state \
    || die "LB reservation is released but local proof could not be durably removed"
  log "released LB drain reservation for worker=$NODE backend=$DRAIN_BACKEND"
}

stop_replica() {
  _tools
  _acquire_drain_lock
  _load_cluster_state
  _load_replica_state allow-config-drift
  local member_id stop_state_hash="$REPLICA_STATE_SHA256"
  member_id=$(cast call "$CLUSTER" 'memberIdOf(address)(bytes32)' "$X" \
    --rpc-url "$RPC_URL") || die "cannot verify worker membership before stop"
  [[ "$member_id" =~ ^0x[0-9a-fA-F]{64}$ ]] \
    || die "worker membership query returned a malformed member id"
  if [ "${member_id,,}" != "$ZERO32" ]; then
    _assert_lb_drained
  else
    log "worker never registered and cannot have opened shared gRPC; LB drain confirmation is not required"
  fi
  _load_replica_state allow-config-drift
  [ "$REPLICA_STATE_SHA256" = "$stop_state_hash" ] \
    || die "replica state changed between drain proof and VM stop; refusing to stop"
  EXPECTED_REPLICA_STATE_SHA256="$stop_state_hash"
  # The dedicated cluster is Safe-owned. Generic cleanup is used only for its
  # idempotent VM stop and is forbidden from attempting an EOA allowlist write.
  FORCE_CLEANUP=1 SKIP_APP_ALLOWLIST_CLEANUP=1 _generic stop
  log "worker VM is proven stopped or gone; Safe app admission remains unchanged"
}

bootstrap_help() {
  cat <<EOF
Dedicated cluster handoff (state path: $CLUSTER_STATE):
  1. Compute INDEXER_COMPOSE_HASH with: $0 $NODE hash
  2. Run deploy/onchain.sh indexer-cluster and capture its "Cluster deployed:" address.
  3. Have INDEXER_CLUSTER_OWNER execute acceptOwnership() on that cluster.
  4. Prepare the exact Safe-owned Path-A cut (this only deploys implementations):
       deploy/onchain.sh patha-safe-prepare "\$CLUSTER" "\$INDEXER_CLUSTER_OWNER"
     Review and execute the bundle's target/value/calldata through the Safe, then extract:
       PATHA_BUNDLE="contracts/script/deployments/\${CHAIN_ID}-patha-safe-\${CLUSTER,,}.json"
       DSTACK_FACET=\$(jq -r .dstackFacet "\$PATHA_BUNDLE")
       MEMBER_IMPL=\$(jq -r .clusterMemberImplementation "\$PATHA_BUNDLE")
  5. Persist the exact handoff with:
     CLUSTER=<Cluster-deployed-address> DSTACK_FACET=<new-DstackFacet> \\
     MEMBER_IMPL=<new-ClusterMember-impl> \\
     INDEXER_COMPOSE_HASH=<hash> INDEXER_CLUSTER_OWNER=<Safe> KMS_ROOT=<root> \\
     INDEXER_DEVICE_IDS_JSON='<canonical-device-id-array>' \\
     $0 $NODE save-cluster-state

The persisted schema is CHAIN_ID, CLUSTER, CLUSTER_NAME,
INDEXER_DEVICE_IDS_JSON, DSTACK_FACET, MEMBER_IMPL, INDEXER_COMPOSE_HASH,
INDEXER_CLUSTER_OWNER, KMS_ROOT, and MEASURED_COMPOSE_NAME. Replica actions
never fall back to Matrix state and run the complete read-only Stage-A verifier.
EOF
}

prepare() {
  preflight
  local vm
  vm="$(_state_value "$REPLICA_STATE" VM_ID)"
  if [ -n "$vm" ]; then
    _load_replica_state
    log "reusing existing stopped replica state vm=$VM_ID"
  else
    _generic deploy
    _load_replica_state
  fi
  safe_admission
}

finish() {
  _load_cluster_state
  _runtime_env
  preflight_bundler
  bind_replica
  start_replica
  register_direct
  verify_member
  verify_candidate
  log "shared Indexer candidate ready; IndexerRegistry was not modified"
}

log "=== dedicated Indexer HA replica: $NODE action=$ACTION ==="
case "$ACTION" in
  hash) rendered_hash ;;
  save-cluster-state) save_cluster_state ;;
  verify-cluster) _tools; _load_cluster_state; _verify_cluster_policy ;;
  preflight) preflight ;;
  deploy) deploy_replica ;;
  safe-admission) safe_admission ;;
  wait-admission) wait_admission ;;
  bind) bind_replica ;;
  start) start_replica ;;
  register-direct) register_direct ;;
  verify-member) verify_member ;;
  verify-candidate) verify_candidate ;;
  stop|cleanup) stop_replica ;;
  release-drain) release_drain_reservation ;;
  bootstrap-help|help) bootstrap_help ;;
  prepare) prepare ;;
  finish|candidate) finish ;;
  *)
    die "usage: $0 <replica-name> {hash|save-cluster-state|verify-cluster|preflight|deploy|safe-admission|wait-admission|bind|start|register-direct|verify-member|verify-candidate|stop|release-drain|bootstrap-help|prepare|finish}"
    ;;
esac
