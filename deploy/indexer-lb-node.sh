#!/usr/bin/env bash
# Stable public HAProxy front door for AttestMesh Indexer blue/green cutovers and
# Stage A shared-identity worker pools.
#
# The LB is deployed once as a C3 member. Candidate Indexers are deployed with
# `indexer-member-node.sh <name> candidate`; this driver then performs a two-phase
# switch around the IndexerRegistry signing-key update:
#
#   deploy/indexer-lb-node.sh attestmesh-indexer-lb all attestmesh-indexer-c3-green
#   deploy/indexer-lb-node.sh attestmesh-indexer-lb switch attestmesh-indexer-c3-next
#   deploy/indexer-lb-node.sh attestmesh-indexer-lb switch indexer-ha-r1,indexer-ha-r2
#
# The stable registry endpoint is the LB app gateway on :50052. The control API is
# mesh-only on :50053. Existing gRPC streams continue during prepare/registry update;
# commit swaps the backend/pool and closes them so sidecars re-read the registry.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL CHAIN_ID DEPLOYER_ADDR

NODE="${1:?usage: indexer-lb-node.sh <node-name> [setup|preflight|deploy|prime|bind|start|register-direct|verify|verify-health|verify-lb|active|drain-reservations|assert-drained|release-drain|abort|recover|switch|update|stop|all] [target-or-operation-id]}"
[[ "$NODE" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] \
  || die "LB node name must be a safe token of at most 128 characters"
ACTION="${2:-all}"
TARGET="${3:-}"
ARG4="${4:-}"
ARG5="${5:-}"
COMPOSE="${COMPOSE:-$ROOT/deploy/compose/indexer-lb-node.yaml}"
LB_PRIVATE_STATE_DIR="${INDEXER_LB_STATE_DIR:-$HOME/.attestmesh/indexer-lb}"
GENERIC_STATE="$LB_PRIVATE_STATE_DIR/generic-node-${NODE}.state"
LB_STATE="$LB_PRIVATE_STATE_DIR/indexer-lb-node-${NODE}.state"
TXN_STATE="$LB_PRIVATE_STATE_DIR/indexer-lb-transaction-${NODE}.json"
CUTOVER_LOCK="$LB_PRIVATE_STATE_DIR/indexer-lb-${NODE}.lock"
if [ -n "${SECRETS_FILE+x}" ]; then
  SECRETS_FILE_IS_DEFAULT=0
else
  SECRETS_FILE="$LB_PRIVATE_STATE_DIR/control.env"
  SECRETS_FILE_IS_DEFAULT=1
fi
LEGACY_SECRETS_FILE="$HOME/.attestmesh/indexer-lb.env"
LEGACY_GENERIC_STATE="$LOGDIR/generic-node-${NODE}.state"
LEGACY_LB_STATE="$LOGDIR/indexer-lb-node-${NODE}.state"
LEGACY_TXN_STATE="$LOGDIR/indexer-lb-transaction-${NODE}.json"
GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-gateway.attestmesh.xyz}"
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
REGISTRY="$(jq -r .indexerRegistry "$ROOT/contracts/script/deployments/${CHAIN_ID}.json")"
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000
ZERO_ADDRESS=0x0000000000000000000000000000000000000000
MAX_INDEXER_BACKENDS=8
INDEXER_LB_TX_CONFIRMATIONS="${INDEXER_LB_TX_CONFIRMATIONS:-2}"
INDEXER_LB_TX_WAIT_SECONDS="${INDEXER_LB_TX_WAIT_SECONDS:-1800}"

export BOX_VCPU="${BOX_VCPU:-1}" BOX_MEM="${BOX_MEM:-1536}" BOX_DISK="${BOX_DISK:-12}"
export BOX_PORTS="${BOX_PORTS:-[]}" BOX_GATEWAY_ENABLED="${BOX_GATEWAY_ENABLED:-true}"
export BOX_NET_MODE="${BOX_NET_MODE:-bridge}"

ssh_box() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "$@"; }
ssh_mesh() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$MESH_SSH_HOST" "$@"; }

_ensure_lb_state_dir() {
  local ancestor
  umask 077
  if [ ! -e "$LB_PRIVATE_STATE_DIR" ]; then
    mkdir -p "$LB_PRIVATE_STATE_DIR"
  fi
  [ ! -L "$LB_PRIVATE_STATE_DIR" ] \
    || die "refusing symlinked Indexer LB state directory: $LB_PRIVATE_STATE_DIR"
  [ -d "$LB_PRIVATE_STATE_DIR" ] \
    || die "Indexer LB state path is not a directory: $LB_PRIVATE_STATE_DIR"
  [ "$(realpath -e "$LB_PRIVATE_STATE_DIR")" = "$(realpath -m "$LB_PRIVATE_STATE_DIR")" ] \
    || die "Indexer LB state directory contains a symlinked path component"
  [ "$(stat -c '%u' "$LB_PRIVATE_STATE_DIR")" = "$(id -u)" ] \
    || die "Indexer LB state directory is not owned by the current user"
  [ "$(stat -c '%a' "$LB_PRIVATE_STATE_DIR")" = 700 ] \
    || die "Indexer LB state directory must have exact mode 0700"
  ancestor=$(dirname "$(realpath -e "$LB_PRIVATE_STATE_DIR")")
  [ "$(stat -c '%u' "$ancestor")" = "$(id -u)" ] \
    || die "Indexer LB state ancestor is not owned by the current user: $ancestor"
  [ "$((8#$(stat -c '%a' "$ancestor") & 8#022))" -eq 0 ] \
    || die "Indexer LB state ancestor must not be group/world writable: $ancestor"
}

_read_private_kv_json() {
  local path="$1" allowed="$2"
  python3 - "$path" "$allowed" <<'PY'
import json
import os
import stat
import sys

path = os.path.abspath(sys.argv[1])
allowed = set(sys.argv[2].split(","))
parent, name = os.path.dirname(path), os.path.basename(path)
if os.path.realpath(parent) != parent:
    raise SystemExit("private state directory contains a symlink")
ancestor_fd = os.open(os.path.dirname(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    ancestor_info = os.fstat(ancestor_fd)
    if ancestor_info.st_uid != os.getuid() or stat.S_IMODE(ancestor_info.st_mode) & 0o022:
        raise SystemExit("private state ancestor owner/mode mismatch")
finally:
    os.close(ancestor_fd)
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    directory_info = os.fstat(directory_fd)
    if directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise SystemExit("private state directory owner/mode mismatch")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("private state is not a regular file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("private state must be current-user-owned mode 0600")
        if info.st_size < 1 or info.st_size > 65536:
            raise SystemExit("private state size is invalid")
        raw = b""
        while len(raw) <= 65536:
            chunk = os.read(fd, 65537 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > 65536:
            raise SystemExit("private state exceeds 64KiB")
    finally:
        os.close(fd)
finally:
    os.close(directory_fd)
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError as exc:
    raise SystemExit("private state is not UTF-8") from exc
value = {}
for line in text.splitlines():
    if not line or "=" not in line:
        raise SystemExit("private state contains a malformed line")
    key, item = line.split("=", 1)
    if key not in allowed:
        raise SystemExit(f"private state contains unknown key: {key}")
    if key in value:
        raise SystemExit(f"private state contains duplicate key: {key}")
    value[key] = item
print(json.dumps(value, separators=(",", ":"), sort_keys=True))
PY
}

_read_legacy_owned_kv_json() {
  local path="$1" allowed="$2" expected_sha256="${3:-}"
  python3 - "$path" "$allowed" "$expected_sha256" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys

path = os.path.abspath(sys.argv[1])
allowed = set(sys.argv[2].split(","))
expected_sha256 = sys.argv[3]
parent, name = os.path.dirname(path), os.path.basename(path)
if os.path.realpath(parent) != parent:
    raise SystemExit("legacy state directory contains a symlink")
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("legacy state is not a regular file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("legacy state must be current-user-owned mode 0600")
        if info.st_size < 1 or info.st_size > 65536:
            raise SystemExit("legacy state size is invalid")
        raw = b""
        while len(raw) <= 65536:
            chunk = os.read(fd, 65537 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > 65536:
            raise SystemExit("legacy state exceeds 64KiB")
    finally:
        os.close(fd)
finally:
    os.close(directory_fd)
if expected_sha256:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise SystemExit("expected legacy state SHA256 is malformed")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise SystemExit(
            f"legacy state SHA256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError as exc:
    raise SystemExit("legacy state is not UTF-8") from exc
value = {}
for line in text.splitlines():
    if not line or "=" not in line:
        raise SystemExit("legacy state contains a malformed line")
    key, item = line.split("=", 1)
    if key not in allowed:
        raise SystemExit(f"legacy state contains unknown key: {key}")
    if key in value:
        raise SystemExit(f"legacy state contains duplicate key: {key}")
    value[key] = item
print(json.dumps(value, separators=(",", ":"), sort_keys=True))
PY
}

_read_legacy_owned_json() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import os
import stat
import sys

path = os.path.abspath(sys.argv[1])
parent, name = os.path.dirname(path), os.path.basename(path)
if os.path.realpath(parent) != parent:
    raise SystemExit("legacy journal directory contains a symlink")
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("legacy journal is not a regular file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("legacy journal must be current-user-owned mode 0600")
        if info.st_size < 1 or info.st_size > 65536:
            raise SystemExit("legacy journal size is invalid")
        raw = b""
        while len(raw) <= 65536:
            chunk = os.read(fd, 65537 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > 65536:
            raise SystemExit("legacy journal exceeds 64KiB")
    finally:
        os.close(fd)
finally:
    os.close(directory_fd)
try:
    value = json.loads(raw)
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit("legacy journal is malformed") from exc
if not isinstance(value, dict):
    raise SystemExit("legacy journal root must be an object")
print(json.dumps(value, separators=(",", ":"), sort_keys=True))
PY
}

_read_private_json() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import os
import stat
import sys

path = os.path.abspath(sys.argv[1])
parent, name = os.path.dirname(path), os.path.basename(path)
if os.path.realpath(parent) != parent:
    raise SystemExit("private JSON directory contains a symlink")
ancestor_fd = os.open(os.path.dirname(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    ancestor_info = os.fstat(ancestor_fd)
    if ancestor_info.st_uid != os.getuid() or stat.S_IMODE(ancestor_info.st_mode) & 0o022:
        raise SystemExit("private JSON ancestor owner/mode mismatch")
finally:
    os.close(ancestor_fd)
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    directory_info = os.fstat(directory_fd)
    if directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise SystemExit("private JSON directory owner/mode mismatch")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("private JSON is not a regular file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("private JSON must be current-user-owned mode 0600")
        if info.st_size < 1 or info.st_size > 65536:
            raise SystemExit("private JSON size is invalid")
        raw = b""
        while len(raw) <= 65536:
            chunk = os.read(fd, 65537 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > 65536:
            raise SystemExit("private JSON exceeds 64KiB")
    finally:
        os.close(fd)
finally:
    os.close(directory_fd)
try:
    value = json.loads(raw)
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit("private JSON is malformed") from exc
if not isinstance(value, dict):
    raise SystemExit("private JSON root must be an object")
print(json.dumps(value, separators=(",", ":"), sort_keys=True))
PY
}

_read_private_secret() {
  local path="$1"
  python3 - "$path" <<'PY'
import os
import re
import stat
import sys

path = os.path.abspath(sys.argv[1])
parent, name = os.path.dirname(path), os.path.basename(path)
if os.path.realpath(parent) != parent:
    raise SystemExit("control-secret directory contains a symlink")
ancestor_fd = os.open(os.path.dirname(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    ancestor_info = os.fstat(ancestor_fd)
    if ancestor_info.st_uid != os.getuid() or stat.S_IMODE(ancestor_info.st_mode) & 0o022:
        raise SystemExit("control-secret ancestor owner/mode mismatch")
finally:
    os.close(ancestor_fd)
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    directory_info = os.fstat(directory_fd)
    if directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise SystemExit("control-secret directory must be current-user-owned mode 0700")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("control secret is not a regular file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("control secret must be current-user-owned mode 0600")
        if info.st_size < 1 or info.st_size > 4096:
            raise SystemExit("control-secret size is invalid")
        raw = b""
        while len(raw) <= 4096:
            chunk = os.read(fd, 4097 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > 4096:
            raise SystemExit("control secret exceeds 4KiB")
    finally:
        os.close(fd)
finally:
    os.close(directory_fd)
try:
    lines = raw.decode("utf-8").splitlines()
except UnicodeDecodeError as exc:
    raise SystemExit("control secret is not UTF-8") from exc
values = []
for line in lines:
    if not line or line.startswith("#"):
        continue
    prefix = "INDEXER_LB_ADMIN_KEY="
    if not line.startswith(prefix):
        raise SystemExit("control-secret file contains an unexpected field")
    values.append(line[len(prefix):])
if len(values) != 1 or not re.fullmatch(r"ilb_[0-9a-f]{64}", values[0]):
    raise SystemExit("control-secret file must contain exactly one valid key")
print(values[0])
PY
}

_durable_private_text_write() {
  local path="$1" value="$2"
  python3 - "$path" 3< <(printf '%s' "$value") <<'PY'
import os
import stat
import sys
import tempfile

path = os.path.abspath(sys.argv[1])
raw = os.fdopen(3, "rb").read(65537)
if len(raw) > 65536:
    raise SystemExit("private state payload exceeds 64KiB")
try:
    value = raw.decode("utf-8")
except UnicodeDecodeError as exc:
    raise SystemExit("private state payload is not UTF-8") from exc
if not value.endswith("\n"):
    value += "\n"
parent, name = os.path.dirname(path), os.path.basename(path)
if os.path.realpath(parent) != parent:
    raise SystemExit("private state directory contains a symlink")
ancestor_fd = os.open(os.path.dirname(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    ancestor_info = os.fstat(ancestor_fd)
    if ancestor_info.st_uid != os.getuid() or stat.S_IMODE(ancestor_info.st_mode) & 0o022:
        raise SystemExit("private state ancestor owner/mode mismatch")
finally:
    os.close(ancestor_fd)
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    directory_info = os.fstat(directory_fd)
    if directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise SystemExit("private state directory owner/mode mismatch")
    try:
        existing_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        existing_fd = None
    if existing_fd is not None:
        try:
            info = os.fstat(existing_fd)
            if not stat.S_ISREG(info.st_mode):
                raise SystemExit("existing private state is not a regular file")
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise SystemExit("existing private state owner/mode mismatch")
        finally:
            os.close(existing_fd)
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
finally:
    os.close(directory_fd)
PY
}

_prepare_cutover_lock() {
  python3 - "$CUTOVER_LOCK" <<'PY'
import os
import stat
import sys

path = os.path.abspath(sys.argv[1])
parent, name = os.path.dirname(path), os.path.basename(path)
if os.path.realpath(parent) != parent:
    raise SystemExit("lock directory contains a symlink")
ancestor_fd = os.open(os.path.dirname(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    ancestor_info = os.fstat(ancestor_fd)
    if ancestor_info.st_uid != os.getuid() or stat.S_IMODE(ancestor_info.st_mode) & 0o022:
        raise SystemExit("lock ancestor owner/mode mismatch")
finally:
    os.close(ancestor_fd)
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    directory_info = os.fstat(directory_fd)
    if directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise SystemExit("lock directory owner/mode mismatch")
    fd = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("cutover lock is not a regular file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("cutover lock owner/mode mismatch")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

_state_value() {
  local file="$1" key="$2"
  sed -n "s/^${key}=//p" "$file" 2>/dev/null | tail -1
}

_load_generic() {
  local value schema updated phase state_cluster state_member_impl state_kms_root state_gateway
  [ -e "$GENERIC_STATE" ] || [ -L "$GENERIC_STATE" ] \
    || die "missing LB node state: $GENERIC_STATE"
  value=$(_read_private_kv_json "$GENERIC_STATE" \
    "STATE_SCHEMA,UPDATED_AT,STATE_PHASE,X,H,VM_ID,CLUSTER,MEMBER_IMPL,KMS_ROOT,GATEWAY_DOMAIN,GUEST_CONFIG_SHA256") \
    || die "could not safely read LB node state: $GENERIC_STATE"
  echo "$value" | jq -e '
    has("STATE_SCHEMA") and has("UPDATED_AT") and has("STATE_PHASE")
    and has("X") and has("H") and has("VM_ID") and has("CLUSTER")
    and has("MEMBER_IMPL") and has("KMS_ROOT") and has("GATEWAY_DOMAIN")
    and has("GUEST_CONFIG_SHA256")
  ' >/dev/null || die "LB node state is missing a schema-2 field: $GENERIC_STATE"
  schema=$(echo "$value" | jq -r .STATE_SCHEMA)
  [ "$schema" = 2 ] || die "unsupported LB generic state schema: $schema"
  updated=$(echo "$value" | jq -r .UPDATED_AT)
  [[ "$updated" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
    || die "LB generic state UPDATED_AT is malformed"
  phase=$(echo "$value" | jq -r .STATE_PHASE)
  case "$phase" in
    deployed-stopped|primed|bound|started|registered) ;;
    *) die "LB generic state is not a deployed node (phase=$phase)" ;;
  esac
  X="$(_validate_address "$(echo "$value" | jq -r .X)" "LB X")"
  _validate_bytes32 "$(echo "$value" | jq -r .H)" "LB H" >/dev/null
  VM_ID="$(_validate_vm_id "$(echo "$value" | jq -r .VM_ID)" "LB VM_ID")"
  state_cluster="$(_validate_address "$(echo "$value" | jq -r .CLUSTER)" "LB CLUSTER")"
  state_member_impl="$(_validate_address "$(echo "$value" | jq -r .MEMBER_IMPL)" "LB MEMBER_IMPL")"
  state_kms_root="$(_validate_address "$(echo "$value" | jq -r .KMS_ROOT)" "LB KMS_ROOT")"
  state_gateway=$(echo "$value" | jq -r .GATEWAY_DOMAIN)
  [[ "$state_gateway" =~ ^[A-Za-z0-9._:-]+$ ]] \
    || die "LB GATEWAY_DOMAIN in private state is malformed"
  [ -z "${CLUSTER:-}" ] || [ "${CLUSTER,,}" = "$state_cluster" ] \
    || die "LB private state CLUSTER differs from the requested cluster"
  [ -z "${MEMBER_IMPL:-}" ] || [ "${MEMBER_IMPL,,}" = "$state_member_impl" ] \
    || die "LB private state MEMBER_IMPL differs from the requested implementation"
  [ -z "${KMS_ROOT:-}" ] || [ "${KMS_ROOT,,}" = "$state_kms_root" ] \
    || die "LB private state KMS_ROOT differs from the requested root"
  [ "$GATEWAY_DOMAIN" = "$state_gateway" ] \
    || die "LB private state GATEWAY_DOMAIN differs from the requested gateway"
  CLUSTER="$state_cluster"
  MEMBER_IMPL="$state_member_impl"
  KMS_ROOT="$state_kms_root"
}

_load_lb() {
  local value key item
  local -a _lb_backends=() _lb_nodes=() _lb_members=()
  MESH_IP=""
  ACTIVE_BACKEND=""
  ACTIVE_BACKEND_NODE=""
  ACTIVE_BACKENDS=""
  ACTIVE_BACKEND_NODES=""
  ACTIVE_PUBKEY=""
  ACTIVE_CODE_ID=""
  ACTIVE_INDEXER_CLUSTER=""
  ACTIVE_MEMBER_IDS=""
  STABLE_ENDPOINT=""
  if [ ! -e "$LB_STATE" ] && [ ! -L "$LB_STATE" ]; then
    return 0
  fi
  value=$(_read_private_kv_json "$LB_STATE" \
    "STATE_SCHEMA,UPDATED_AT,MESH_IP,ACTIVE_BACKEND,ACTIVE_BACKEND_NODE,ACTIVE_BACKENDS,ACTIVE_BACKEND_NODES,ACTIVE_PUBKEY,ACTIVE_CODE_ID,ACTIVE_INDEXER_CLUSTER,ACTIVE_MEMBER_IDS,STABLE_ENDPOINT") \
    || die "could not safely read LB state: $LB_STATE"
  echo "$value" | jq -e '
    has("STATE_SCHEMA") and has("UPDATED_AT") and has("MESH_IP")
    and has("ACTIVE_BACKEND") and has("ACTIVE_BACKEND_NODE")
    and has("ACTIVE_BACKENDS") and has("ACTIVE_BACKEND_NODES")
    and has("ACTIVE_PUBKEY") and has("ACTIVE_CODE_ID")
    and has("ACTIVE_INDEXER_CLUSTER") and has("ACTIVE_MEMBER_IDS")
    and has("STABLE_ENDPOINT")
  ' >/dev/null || die "LB state is missing a required field: $LB_STATE"
  [ "$(echo "$value" | jq -r .STATE_SCHEMA)" = 1 ] \
    || die "unsupported LB state schema: $LB_STATE"
  [[ "$(echo "$value" | jq -r .UPDATED_AT)" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
    || die "LB state UPDATED_AT is malformed"
  for key in MESH_IP ACTIVE_BACKEND ACTIVE_BACKEND_NODE ACTIVE_BACKENDS \
    ACTIVE_BACKEND_NODES ACTIVE_PUBKEY ACTIVE_CODE_ID ACTIVE_INDEXER_CLUSTER \
    ACTIVE_MEMBER_IDS STABLE_ENDPOINT; do
    printf -v "$key" '%s' "$(echo "$value" | jq -r --arg key "$key" '.[$key]')"
  done
  [ -z "$MESH_IP" ] || MESH_IP="$(_validate_private_ipv4 "$MESH_IP")"
  [ -z "$ACTIVE_BACKEND" ] || ACTIVE_BACKEND="$(_validate_private_ipv4 "$ACTIVE_BACKEND")"
  if [ -n "$ACTIVE_BACKENDS" ]; then
    IFS=',' read -r -a _lb_backends <<<"$ACTIVE_BACKENDS"
    [ "${#_lb_backends[@]}" -le "$MAX_INDEXER_BACKENDS" ] \
      || die "LB state contains too many active backends"
    for item in "${_lb_backends[@]}"; do
      [ -n "$item" ] || die "LB state contains an empty active backend"
      _validate_private_ipv4 "$item" >/dev/null
    done
    [ -z "$ACTIVE_BACKEND" ] || [ "$ACTIVE_BACKEND" = "${_lb_backends[0]}" ] \
      || die "LB state primary backend differs from its active pool"
  fi
  [ -z "$ACTIVE_BACKEND_NODES" ] || IFS=',' read -r -a _lb_nodes <<<"$ACTIVE_BACKEND_NODES"
  _lb_nodes=("$ACTIVE_BACKEND_NODE" "${_lb_nodes[@]}")
  for item in "${_lb_nodes[@]}"; do
    if [ -n "$item" ]; then
      [[ "$item" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] \
        || die "LB state contains an invalid backend node name"
    fi
  done
  [ -z "$ACTIVE_PUBKEY" ] || ACTIVE_PUBKEY="$(_validate_bytes32 "$ACTIVE_PUBKEY" "LB active pubkey")"
  [ -z "$ACTIVE_CODE_ID" ] || ACTIVE_CODE_ID="$(_validate_bytes32 "$ACTIVE_CODE_ID" "LB active code ID")"
  [ -z "$ACTIVE_INDEXER_CLUSTER" ] \
    || ACTIVE_INDEXER_CLUSTER="$(_validate_address "$ACTIVE_INDEXER_CLUSTER" "LB active Indexer cluster")"
  [ -z "$ACTIVE_MEMBER_IDS" ] || IFS=',' read -r -a _lb_members <<<"$ACTIVE_MEMBER_IDS"
  for item in "${_lb_members[@]}"; do
    _validate_bytes32 "$item" "LB active member ID" >/dev/null
  done
  [ -z "$STABLE_ENDPOINT" ] \
    || [[ "$STABLE_ENDPOINT" =~ ^https://[0-9a-f]{40}-50052\.[A-Za-z0-9._-]+$ ]] \
    || die "LB stable endpoint in private state is malformed"
}

_save_lb() {
  local value
  _ensure_lb_state_dir
  value=$(cat <<EOF
STATE_SCHEMA=1
UPDATED_AT=$(ts)
MESH_IP=${MESH_IP:-}
ACTIVE_BACKEND=${ACTIVE_BACKEND:-}
ACTIVE_BACKEND_NODE=${ACTIVE_BACKEND_NODE:-}
ACTIVE_BACKENDS=${ACTIVE_BACKENDS:-${ACTIVE_BACKEND:-}}
ACTIVE_BACKEND_NODES=${ACTIVE_BACKEND_NODES:-${ACTIVE_BACKEND_NODE:-}}
ACTIVE_PUBKEY=${ACTIVE_PUBKEY:-}
ACTIVE_CODE_ID=${ACTIVE_CODE_ID:-}
ACTIVE_INDEXER_CLUSTER=${ACTIVE_INDEXER_CLUSTER:-}
ACTIVE_MEMBER_IDS=${ACTIVE_MEMBER_IDS:-}
STABLE_ENDPOINT=${STABLE_ENDPOINT:-}
EOF
)
  _durable_private_text_write "$LB_STATE" "$value" \
    || die "could not durably write LB state: $LB_STATE"
}

_record_private_lb_response() {
  local kind="$1" value="$2" nonce path
  [[ "$kind" =~ ^(prepare|commit)$ ]] || die "invalid private LB response kind"
  nonce=$(openssl rand -hex 12) || die "could not randomize private LB response log"
  path="$LB_PRIVATE_STATE_DIR/indexer-lb-${kind}-${NODE}-$(ts)-${nonce}.log"
  _durable_private_text_write "$path" "$value" \
    || die "could not durably record private LB $kind response"
  printf '%s\n' "$value"
}

_default_cluster_env() {
  [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] \
    || die "missing CLUSTER/MEMBER_IMPL; export trusted values instead of relying on deploy/logs state"
  CLUSTER="$(_validate_address "$CLUSTER" CLUSTER)"
  MEMBER_IMPL="$(_validate_address "$MEMBER_IMPL" MEMBER_IMPL)"
  [ -n "${KMS_ROOT:-}" ] || die "missing KMS_ROOT (source deploy/env.sh)"
  KMS_ROOT="$(_validate_address "$KMS_ROOT" KMS_ROOT)"
  INDEXER_REGISTRY_ADDR="${INDEXER_REGISTRY_ADDR:-$REGISTRY}"
  [ -n "${BUNDLER_URL:-}" ] || die "missing BUNDLER_URL"
  [ -n "${GAS_POLICY_ID:-}" ] || die "missing GAS_POLICY_ID"
  export CLUSTER MEMBER_IMPL KMS_ROOT INDEXER_REGISTRY_ADDR GATEWAY_DOMAIN
}

_ensure_secrets() {
  local secret_dir secret
  umask 077
  _ensure_lb_state_dir
  secret_dir=$(dirname "$SECRETS_FILE")
  if [ ! -e "$secret_dir" ]; then
    # umask 077 applies to every newly created path component. Do not chmod an
    # existing operator-owned parent after the fact.
    mkdir -p "$secret_dir"
  fi
  [ ! -L "$secret_dir" ] || die "refusing symlinked Indexer LB secrets directory: $secret_dir"
  [ -d "$secret_dir" ] || die "Indexer LB secrets parent is not a directory: $secret_dir"
  [ "$(realpath -e "$secret_dir")" = "$(realpath -m "$secret_dir")" ] \
    || die "Indexer LB secrets directory contains a symlinked path component"
  [ "$(stat -c '%u' "$secret_dir")" = "$(id -u)" ] \
    || die "Indexer LB secrets directory is not owned by the current user: $secret_dir"
  [ "$(stat -c '%a' "$secret_dir")" = 700 ] \
    || die "Indexer LB secrets directory must have exact mode 0700: $secret_dir"
  if [ ! -e "$SECRETS_FILE" ] && [ ! -L "$SECRETS_FILE" ] \
    && [ "$SECRETS_FILE_IS_DEFAULT" = 1 ] \
    && { [ -e "$LEGACY_SECRETS_FILE" ] || [ -L "$LEGACY_SECRETS_FILE" ]; }; then
    secret=$(_read_private_secret "$LEGACY_SECRETS_FILE") \
      || die "legacy Indexer LB control secret is not safe to migrate: $LEGACY_SECRETS_FILE"
    _durable_private_text_write "$SECRETS_FILE" "INDEXER_LB_ADMIN_KEY=$secret" \
      || die "could not migrate legacy Indexer LB control secret"
    log "migrated Indexer LB control secret into private state; retained legacy rollback copy"
  fi
  if [ ! -e "$SECRETS_FILE" ] && [ ! -L "$SECRETS_FILE" ]; then
    secret="ilb_$(openssl rand -hex 32)"
    _durable_private_text_write "$SECRETS_FILE" "INDEXER_LB_ADMIN_KEY=$secret" \
      || die "could not durably create Indexer LB control secret"
    log "generated Indexer LB control secret -> $SECRETS_FILE"
  fi
  INDEXER_LB_ADMIN_KEY=$(_read_private_secret "$SECRETS_FILE") \
    || die "could not safely read Indexer LB control secret: $SECRETS_FILE"
}

_acquire_cutover_lock() {
  _prepare_cutover_lock || die "could not safely prepare cutover lock: $CUTOVER_LOCK"
  [ ! -L "$CUTOVER_LOCK" ] || die "refusing symlinked cutover lock: $CUTOVER_LOCK"
  exec {CUTOVER_LOCK_FD}<>"$CUTOVER_LOCK"
  flock -n "$CUTOVER_LOCK_FD" \
    || die "another mutating Indexer LB operation holds $CUTOVER_LOCK"
}

_require_protocol_v3_fleet_confirmation() {
  [ "${INDEXER_PROTOCOL_V3_FLEET_CONFIRMED:-}" = 1 ] \
    || die "shared Indexer pools require INDEXER_PROTOCOL_V3_FLEET_CONFIRMED=1 after every production sidecar has durable protocol-v3 exact-cursor support"
}

_seal_lb_env() {
  _ensure_secrets
  local initial="${INDEXER_LB_INITIAL_BACKEND:-}" target ip require_bridge=0 pinned_pubkey
  local -a initial_targets=() initial_ips=()
  local -A seen_initial_ips=()
  if [ -n "$initial" ]; then
    [[ "$initial" != ,* && "$initial" != *, && "$initial" != *,,* ]] \
      || die "INDEXER_LB_INITIAL_BACKEND contains an empty pool member"
    IFS=',' read -r -a initial_targets <<<"$initial"
    [ "${#initial_targets[@]}" -le "$MAX_INDEXER_BACKENDS" ] \
      || die "INDEXER_LB_INITIAL_BACKEND supports at most $MAX_INDEXER_BACKENDS members"
    if [ "${#initial_targets[@]}" -ge 2 ]; then
      _require_protocol_v3_fleet_confirmation
      require_bridge=1
      pinned_pubkey="${INDEXER_BACKEND_PUBKEY:-}"
      pinned_pubkey="${pinned_pubkey,,}"
      echo "$pinned_pubkey" | grep -Eq '^0x[0-9a-f]{64}$' \
        || die "a shared initial pool requires pinned INDEXER_BACKEND_PUBKEY"
      [ "$pinned_pubkey" != "$ZERO32" ] \
        || die "INDEXER_BACKEND_PUBKEY must be nonzero"
    fi
    for target in "${initial_targets[@]}"; do
      target="$(_trim "$target")"
      [ -n "$target" ] || die "INDEXER_LB_INITIAL_BACKEND contains an empty pool member"
      ip="$(_backend_ip "$target" "$require_bridge")" \
        || die "could not resolve initial backend $target"
      [ -z "${seen_initial_ips[$ip]:-}" ] \
        || die "INDEXER_LB_INITIAL_BACKEND resolves duplicate address $ip"
      seen_initial_ips[$ip]=1
      initial_ips+=("$ip")
    done
    initial=$(IFS=,; echo "${initial_ips[*]}")
  fi
  APP_ENV_B64="$({
    printf 'INDEXER_LB_ADMIN_KEY=%s\n' "$INDEXER_LB_ADMIN_KEY"
    printf 'INDEXER_LB_INITIAL_BACKEND=%s\n' "$initial"
    printf 'INDEXER_LB_CLUSTER=%s\n' "$CLUSTER"
    printf 'INDEXER_LB_PINNED_PUBKEY=%s\n' "${INDEXER_BACKEND_PUBKEY:-}"
    printf 'INDEXER_PROTOCOL_V3_FLEET_CONFIRMED=%s\n' "${INDEXER_PROTOCOL_V3_FLEET_CONFIRMED:-}"
  } | base64 | tr -d '\n')"
  export APP_ENV_B64
}

generic() {
  local action="$1"
  _default_cluster_env
  _seal_lb_env
  COMPOSE="$COMPOSE" GENERIC_STATE_DIR="$LB_PRIVATE_STATE_DIR" \
    REQUIRE_PRIVATE_GENERIC_STATE=1 STRICT_GENERIC_STATE_BINDINGS=1 \
    "$HERE/generic-node.sh" "$NODE" "$action"
}

_member_mesh_ip() {
  local app="$1" cluster="$2" member_id raw
  member_id=$(cast call "$cluster" "memberIdOf(address)(bytes32)" "$app" --rpc-url "$RPC_URL" 2>/dev/null) \
    || return 1
  [ -n "$member_id" ] && [ "$member_id" != "$ZERO32" ] || return 1
  raw=$(cast call "$cluster" "meshIpOf(bytes32)(uint32)" "$member_id" \
    --json --rpc-url "$RPC_URL" 2>/dev/null | jq -er '.[0]') || return 1
  python3 - "$raw" <<'PY'
import ipaddress, sys
print(ipaddress.IPv4Address(int(sys.argv[1], 0)))
PY
}

_discover_mesh_ip() {
  local persisted="" discovered
  _load_lb
  persisted="${MESH_IP:-}"
  _load_generic
  _default_cluster_env
  discovered="$(_member_mesh_ip "$X" "$CLUSTER")" || return 1
  discovered="$(_validate_private_ipv4 "$discovered")" || return 1
  [ -z "$persisted" ] || [ "$persisted" = "$discovered" ] \
    || die "persisted LB mesh IP differs from its on-chain cluster binding"
  MESH_IP="$discovered"
  [ "$persisted" = "$discovered" ] || _save_lb
}

_bridge_ip_for_vm() {
  local vm_id="$1"
  ssh_box "sudo VMID='$vm_id' bash -s" <<'SCRIPT'
set -u
MAC=$(ps axww -o args | grep -F "/srv/data/dstack/vm/$VMID/" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2 || true)
[ -n "$MAC" ] || exit 1
IP=$(ip neigh show dev dstack-br0 | grep -i "$MAC" | grep -oE '^10\.[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
[ -n "$IP" ] || exit 1
echo "$IP"
SCRIPT
}

_backend_state() {
  local target="$1"
  [[ "$target" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] \
    || die "backend node name must be a safe token of at most 128 characters"
  printf '%s\n' "${INDEXER_BACKEND_STATE_DIR:-$HOME/.attestmesh/indexer-ha}/generic-node-${target}.state"
}

_backend_state_json() {
  local target="$1" path="${2:-}" value schema updated phase vm app code cluster
  [ -n "$path" ] || path="$(_backend_state "$target")"
  [ -e "$path" ] || [ -L "$path" ] || die "missing Indexer candidate state: $path"
  value=$(_read_private_kv_json "$path" \
    "STATE_SCHEMA,UPDATED_AT,STATE_PHASE,X,H,VM_ID,CLUSTER,MEMBER_IMPL,KMS_ROOT,GATEWAY_DOMAIN,GUEST_CONFIG_SHA256") \
    || die "could not safely read named backend state: $path"
  echo "$value" | jq -e '
    has("STATE_SCHEMA") and has("UPDATED_AT") and has("STATE_PHASE")
    and has("X") and has("H") and has("VM_ID") and has("CLUSTER")
  ' >/dev/null || die "named backend state is missing a required schema-2 field: $path"
  schema=$(echo "$value" | jq -r .STATE_SCHEMA)
  [ "$schema" = 2 ] || die "unsupported named backend state schema: $schema"
  updated=$(echo "$value" | jq -r .UPDATED_AT)
  [[ "$updated" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
    || die "named backend state UPDATED_AT is malformed"
  phase=$(echo "$value" | jq -r .STATE_PHASE)
  case "$phase" in
    deployed-stopped|primed|bound|started|registered) ;;
    *) die "named backend state is not a deployed node (phase=$phase)" ;;
  esac
  vm="$(_validate_vm_id "$(echo "$value" | jq -r .VM_ID)" "backend VM_ID")"
  app="$(_validate_address "$(echo "$value" | jq -r .X)" "backend X")"
  code="$(_validate_bytes32 "$(echo "$value" | jq -r .H)" "backend H")"
  cluster="$(_validate_address "$(echo "$value" | jq -r .CLUSTER)" "backend CLUSTER")"
  jq -nc --arg vm "$vm" --arg app "$app" --arg code "$code" --arg cluster "$cluster" \
    '{VM_ID:$vm,X:$app,H:$code,CLUSTER:$cluster}'
}

_snapshot_expected_backend_state() {
  local target="$1" expected="$2" source
  source="$(_backend_state "$target")"
  python3 - "$source" "$LB_PRIVATE_STATE_DIR" "$target" "$expected" <<'PY'
import hashlib
import os
import stat
import sys
import tempfile

source = os.path.abspath(sys.argv[1])
destination_dir = os.path.abspath(sys.argv[2])
target, expected = sys.argv[3:]
if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
    raise SystemExit("expected backend state SHA256 must be 64 lowercase hex characters")
if os.path.realpath(os.path.dirname(source)) != os.path.dirname(source):
    raise SystemExit("backend state directory contains a symlink")
if os.path.realpath(destination_dir) != destination_dir:
    raise SystemExit("LB snapshot directory contains a symlink")
source_directory_fd = os.open(
    os.path.dirname(source),
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
)
destination_fd = os.open(
    destination_dir,
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
)
try:
    for label, candidate_fd in (
        ("backend state", source_directory_fd),
        ("LB snapshot", destination_fd),
    ):
        directory_info = os.fstat(candidate_fd)
        if directory_info.st_uid != os.getuid():
            raise SystemExit(f"{label} directory is not owned by the current user")
        if stat.S_IMODE(directory_info.st_mode) != 0o700:
            raise SystemExit(f"{label} directory must have exact mode 0700")
    fd = os.open(
        os.path.basename(source), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_directory_fd
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("backend state is not a regular file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("backend state must be current-user-owned mode 0600")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            total += len(chunk)
            if total > 65536:
                raise SystemExit("backend state exceeds 64KiB")
            chunks.append(chunk)
    finally:
        os.close(fd)
    value = b"".join(chunks)
    actual = hashlib.sha256(value).hexdigest()
    if actual != expected:
        raise SystemExit(f"backend state SHA256 mismatch: expected {expected}, got {actual}")
    snapshot_fd, snapshot = tempfile.mkstemp(
        prefix=f"indexer-lb-backend-{target}.", suffix=".state", dir=destination_dir
    )
    try:
        os.fchmod(snapshot_fd, 0o600)
        with os.fdopen(snapshot_fd, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(destination_fd)
    except BaseException:
        try:
            os.unlink(snapshot)
        except FileNotFoundError:
            pass
        raise
finally:
    os.close(destination_fd)
    os.close(source_directory_fd)
print(snapshot)
PY
}

_validate_backend_state_snapshot() {
  local snapshot="$1" expected="$2"
  python3 - "$snapshot" "$expected" <<'PY'
import hashlib
import os
import stat
import sys

path, expected = sys.argv[1:]
fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise SystemExit("backend state snapshot owner/type mismatch")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit("backend state snapshot must have exact mode 0600")
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 8192)
        if not chunk:
            break
        digest.update(chunk)
finally:
    os.close(fd)
if digest.hexdigest() != expected:
    raise SystemExit("backend state snapshot changed after validation")
PY
}

_validate_vm_id() {
  local value="$1" field="${2:-VM_ID}"
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] \
    || die "$field must be a safe token of at most 128 characters"
  printf '%s\n' "$value"
}

_validate_address() {
  local value="$1" field="$2"
  [[ "$value" =~ ^0x[0-9a-fA-F]{40}$ ]] \
    || die "$field must be a 20-byte 0x address"
  [ "${value,,}" != "$ZERO_ADDRESS" ] || die "$field must be nonzero"
  printf '%s\n' "${value,,}"
}

_validate_bytes32() {
  local value="$1" field="$2"
  value="0x${value#0x}"
  [[ "$value" =~ ^0x[0-9a-fA-F]{64}$ ]] \
    || die "$field must be a 32-byte 0x hex value"
  [ "${value,,}" != "$ZERO32" ] || die "$field must be nonzero"
  printf '%s\n' "${value,,}"
}

_validate_private_ipv4() {
  local value="$1"
  python3 - "$value" <<'PY'
import ipaddress, sys
try:
    ip = ipaddress.IPv4Address(sys.argv[1])
except ipaddress.AddressValueError as exc:
    raise SystemExit(f"invalid backend IPv4 address: {exc}")
private_nets = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
if not any(ip in network for network in private_nets):
    raise SystemExit("backend must be an RFC1918 bridge or mesh IPv4 address")
print(ip)
PY
}

_trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s\n' "$value"
}

_trusted_lb_app_for_migration() {
  local expected="${INDEXER_LB_EXPECTED_APP_ID:-}" registry endpoint app
  if [ -n "$expected" ]; then
    _validate_address "$expected" INDEXER_LB_EXPECTED_APP_ID
    return
  fi
  registry=$(cast call "$REGISTRY" 'current()(string,bytes32,bytes32,uint64)' \
    --json --rpc-url "$RPC_URL" 2>/dev/null) \
    || die "legacy LB migration requires INDEXER_LB_EXPECTED_APP_ID or a readable current registry"
  endpoint=$(echo "$registry" | jq -er '.[0] | select(type == "string" and length > 0)') \
    || die "current registry has no stable LB endpoint for legacy migration"
  [[ "$endpoint" == https://*-50052."$GATEWAY_DOMAIN" ]] \
    || die "current registry endpoint is not the expected stable LB gateway; set audited INDEXER_LB_EXPECTED_APP_ID for manual migration"
  app="${endpoint#https://}"
  app="${app%-50052.$GATEWAY_DOMAIN}"
  app="$(_validate_address "0x$app" "registry-derived LB app")"
  [ "$endpoint" = "https://${app#0x}-50052.$GATEWAY_DOMAIN" ] \
    || die "current registry stable endpoint is not canonical"
  printf '%s\n' "$app"
}

_normalize_generic_state_json() {
  local value="$1" require_lb_bindings="${2:-0}"
  local schema updated phase x code vm cluster member_impl kms_root gateway guest expected_lb_app
  echo "$value" | jq -e '
    has("UPDATED_AT") and has("STATE_PHASE") and has("X") and has("H")
    and has("VM_ID") and has("CLUSTER") and has("MEMBER_IMPL")
    and has("KMS_ROOT") and has("GATEWAY_DOMAIN")
  ' >/dev/null || die "legacy generic state is missing a required field"
  schema=$(echo "$value" | jq -r '.STATE_SCHEMA // ""')
  [ -z "$schema" ] || [ "$schema" = 2 ] \
    || die "unsupported legacy generic state schema: $schema"
  if [ "$schema" = 2 ]; then
    echo "$value" | jq -e 'has("GUEST_CONFIG_SHA256")' >/dev/null \
      || die "schema-2 legacy generic state is missing GUEST_CONFIG_SHA256"
  fi
  updated=$(echo "$value" | jq -r .UPDATED_AT)
  [[ "$updated" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
    || die "legacy generic state UPDATED_AT is malformed"
  phase=$(echo "$value" | jq -r .STATE_PHASE)
  case "$phase" in
    deployed-stopped|primed|bound|started|registered) ;;
    *) die "legacy generic state is not a deployed node (phase=$phase)" ;;
  esac
  x="$(_validate_address "$(echo "$value" | jq -r .X)" "legacy generic X")"
  code="$(_validate_bytes32 "$(echo "$value" | jq -r .H)" "legacy generic H")"
  vm="$(_validate_vm_id "$(echo "$value" | jq -r .VM_ID)" "legacy generic VM_ID")"
  cluster="$(_validate_address "$(echo "$value" | jq -r .CLUSTER)" "legacy generic CLUSTER")"
  member_impl="$(_validate_address "$(echo "$value" | jq -r .MEMBER_IMPL)" "legacy generic MEMBER_IMPL")"
  kms_root="$(_validate_address "$(echo "$value" | jq -r .KMS_ROOT)" "legacy generic KMS_ROOT")"
  gateway=$(echo "$value" | jq -r .GATEWAY_DOMAIN)
  [[ "$gateway" =~ ^[A-Za-z0-9._:-]+$ ]] \
    || die "legacy generic GATEWAY_DOMAIN is malformed"
  guest=$(echo "$value" | jq -r '.GUEST_CONFIG_SHA256 // ""')
  [ -z "$guest" ] || [[ "$guest" =~ ^[0-9a-fA-F]{64}$ ]] \
    || die "legacy generic GUEST_CONFIG_SHA256 is malformed"
  if [ "$require_lb_bindings" = 1 ]; then
    [ -n "${CLUSTER:-}" ] && [ -n "${MEMBER_IMPL:-}" ] && [ -n "${KMS_ROOT:-}" ] \
      || die "legacy LB state migration requires trusted CLUSTER/MEMBER_IMPL/KMS_ROOT exports"
    [ "$cluster" = "${CLUSTER,,}" ] \
      || die "legacy LB generic state CLUSTER differs from trusted CLUSTER"
    [ "$member_impl" = "${MEMBER_IMPL,,}" ] \
      || die "legacy LB generic state MEMBER_IMPL differs from trusted MEMBER_IMPL"
    [ "$kms_root" = "${KMS_ROOT,,}" ] \
      || die "legacy LB generic state KMS_ROOT differs from trusted KMS_ROOT"
    [ "$gateway" = "$GATEWAY_DOMAIN" ] \
      || die "legacy LB generic state gateway differs from trusted GATEWAY_DOMAIN"
    expected_lb_app="$(_trusted_lb_app_for_migration)"
    [ "$x" = "$expected_lb_app" ] \
      || die "legacy LB generic state X differs from the independently pinned LB app"
  fi
  printf 'STATE_SCHEMA=2\n'
  printf 'UPDATED_AT=%s\n' "$updated"
  printf 'STATE_PHASE=%s\n' "$phase"
  printf 'X=%s\n' "$x"
  printf 'H=%s\n' "$code"
  printf 'VM_ID=%s\n' "$vm"
  printf 'CLUSTER=%s\n' "$cluster"
  printf 'MEMBER_IMPL=%s\n' "$member_impl"
  printf 'KMS_ROOT=%s\n' "$kms_root"
  printf 'GATEWAY_DOMAIN=%s\n' "$gateway"
  printf 'GUEST_CONFIG_SHA256=%s\n' "${guest,,}"
}

_legacy_migration_needed() {
  { [ -e "$GENERIC_STATE" ] || [ -L "$GENERIC_STATE" ]; } \
    || { [ ! -e "$LEGACY_GENERIC_STATE" ] && [ ! -L "$LEGACY_GENERIC_STATE" ]; } \
    || return 0
  { [ -e "$LB_STATE" ] || [ -L "$LB_STATE" ]; } \
    || { [ ! -e "$LEGACY_LB_STATE" ] && [ ! -L "$LEGACY_LB_STATE" ]; } \
    || return 0
  { [ -e "$TXN_STATE" ] || [ -L "$TXN_STATE" ]; } \
    || { [ ! -e "$LEGACY_TXN_STATE" ] && [ ! -L "$LEGACY_TXN_STATE" ]; } \
    || return 0
  return 1
}

_migrate_legacy_lb_state() {
  local legacy normalized value mesh expected_mesh expected_endpoint key item control controller_snapshot
  local legacy_backends legacy_members legacy_pub legacy_code legacy_cluster
  local -a values=()
  if [ ! -e "$TXN_STATE" ] && [ ! -L "$TXN_STATE" ] \
    && { [ -e "$LEGACY_TXN_STATE" ] || [ -L "$LEGACY_TXN_STATE" ]; }; then
    legacy=$(_read_legacy_owned_json "$LEGACY_TXN_STATE") \
      || die "unsafe legacy Indexer LB transaction journal exists; refusing every new mutation: $LEGACY_TXN_STATE"
    echo "$legacy" | jq -e '
      (.operation_id | type == "string" and test("^[0-9a-f]{64}$"))
      and (.old_registry | type == "object")
      and (.new_registry | type == "object")
      and (.restore_payload | type == "object")
    ' >/dev/null \
      || die "unverifiable legacy Indexer LB transaction journal exists; explicit operator recovery is required: $LEGACY_TXN_STATE"
    die "unfinished pre-finality-protocol Indexer LB journal exists and has no exact signed transaction proof; resolve it with the prior driver or an audited manual procedure before removing $LEGACY_TXN_STATE"
  fi

  if [ ! -e "$GENERIC_STATE" ] && [ ! -L "$GENERIC_STATE" ] \
    && { [ -e "$LEGACY_GENERIC_STATE" ] || [ -L "$LEGACY_GENERIC_STATE" ]; }; then
    legacy=$(_read_legacy_owned_kv_json "$LEGACY_GENERIC_STATE" \
      "STATE_SCHEMA,UPDATED_AT,STATE_PHASE,X,H,VM_ID,CLUSTER,MEMBER_IMPL,KMS_ROOT,GATEWAY_DOMAIN,GUEST_CONFIG_SHA256") \
      || die "could not safely read legacy LB generic state: $LEGACY_GENERIC_STATE"
    normalized="$(_normalize_generic_state_json "$legacy" 1)"
    _durable_private_text_write "$GENERIC_STATE" "$normalized" \
      || die "could not durably migrate legacy LB generic state"
    log "migrated validated LB generic state into $GENERIC_STATE"
  fi

  if [ ! -e "$LB_STATE" ] && [ ! -L "$LB_STATE" ] \
    && { [ -e "$LEGACY_LB_STATE" ] || [ -L "$LEGACY_LB_STATE" ]; }; then
    value=$(_read_legacy_owned_kv_json "$LEGACY_LB_STATE" \
      "STATE_SCHEMA,UPDATED_AT,MESH_IP,ACTIVE_BACKEND,ACTIVE_BACKEND_NODE,ACTIVE_BACKENDS,ACTIVE_BACKEND_NODES,ACTIVE_PUBKEY,ACTIVE_CODE_ID,ACTIVE_INDEXER_CLUSTER,ACTIVE_MEMBER_IDS,STABLE_ENDPOINT") \
      || die "could not safely read legacy LB state: $LEGACY_LB_STATE"
    echo "$value" | jq -e '
      has("UPDATED_AT") and has("MESH_IP") and has("ACTIVE_BACKEND")
      and has("ACTIVE_BACKEND_NODE") and has("ACTIVE_BACKENDS")
      and has("ACTIVE_BACKEND_NODES") and has("ACTIVE_PUBKEY")
      and has("ACTIVE_CODE_ID") and has("ACTIVE_INDEXER_CLUSTER")
      and has("ACTIVE_MEMBER_IDS") and has("STABLE_ENDPOINT")
      and ((.STATE_SCHEMA // "1") == "1")
    ' >/dev/null || die "legacy LB state is missing or has an unsupported schema"
    [[ "$(echo "$value" | jq -r .UPDATED_AT)" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
      || die "legacy LB state UPDATED_AT is malformed"
    mesh=$(echo "$value" | jq -r .MESH_IP)
    [ -z "$mesh" ] || mesh="$(_validate_private_ipv4 "$mesh")"
    _load_generic
    _default_cluster_env
    expected_mesh="$(_member_mesh_ip "$X" "$CLUSTER")" \
      || die "could not bind legacy LB state to the registered LB member"
    expected_mesh="$(_validate_private_ipv4 "$expected_mesh")"
    [ -z "$mesh" ] || [ "$mesh" = "$expected_mesh" ] \
      || die "legacy LB state MESH_IP differs from the on-chain member binding"
    expected_endpoint="https://${X#0x}-50052.$GATEWAY_DOMAIN"
    [ "$(echo "$value" | jq -r .STABLE_ENDPOINT)" = "$expected_endpoint" ] \
      || die "legacy LB state stable endpoint is not exactly bound to the pinned LB app"
    for key in ACTIVE_BACKEND ACTIVE_BACKENDS; do
      IFS=',' read -r -a values <<<"$(echo "$value" | jq -r --arg key "$key" '.[$key]')"
      for item in "${values[@]}"; do
        [ -z "$item" ] || _validate_private_ipv4 "$item" >/dev/null
      done
    done
    for key in ACTIVE_BACKEND_NODE ACTIVE_BACKEND_NODES; do
      IFS=',' read -r -a values <<<"$(echo "$value" | jq -r --arg key "$key" '.[$key]')"
      for item in "${values[@]}"; do
        [ -z "$item" ] || [[ "$item" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] \
          || die "legacy LB state contains an invalid backend node"
      done
    done
    item=$(echo "$value" | jq -r .ACTIVE_PUBKEY)
    [ -z "$item" ] || _validate_bytes32 "$item" "legacy LB pubkey" >/dev/null
    item=$(echo "$value" | jq -r .ACTIVE_CODE_ID)
    [ -z "$item" ] || _validate_bytes32 "$item" "legacy LB code ID" >/dev/null
    item=$(echo "$value" | jq -r .ACTIVE_INDEXER_CLUSTER)
    [ -z "$item" ] || _validate_address "$item" "legacy LB Indexer cluster" >/dev/null
    IFS=',' read -r -a values <<<"$(echo "$value" | jq -r .ACTIVE_MEMBER_IDS)"
    for item in "${values[@]}"; do
      [ -z "$item" ] || _validate_bytes32 "$item" "legacy LB member ID" >/dev/null
    done
    legacy_backends=$(echo "$value" | jq -c '
      ((.ACTIVE_BACKENDS | select(length > 0)) // .ACTIVE_BACKEND)
      | split(",") | map(select(length > 0))
    ') || die "could not normalize legacy active backends"
    legacy_members=$(echo "$value" | jq -c '
      .ACTIVE_MEMBER_IDS | split(",") | map(select(length > 0) | ascii_downcase)
    ') || die "could not normalize legacy active members"
    legacy_pub=$(echo "$value" | jq -r '.ACTIVE_PUBKEY | ascii_downcase')
    legacy_code=$(echo "$value" | jq -r '.ACTIVE_CODE_ID | ascii_downcase')
    legacy_cluster=$(echo "$value" | jq -r '.ACTIVE_INDEXER_CLUSTER | ascii_downcase')
    control="$(_control_request_to "$expected_mesh" GET /active)" \
      || die "authenticated LB /active is unavailable; legacy routing state cannot be migrated"
    echo "$control" | jq -e '.prepared | type == "object" and length == 0' >/dev/null \
      || die "LB controller has a prepared generation but no recoverable transaction journal"
    controller_snapshot=$(echo "$control" | _controller_snapshot) \
      || die "LB controller returned an invalid active snapshot during migration"
    echo "$controller_snapshot" | jq -e \
      --argjson backends "$legacy_backends" \
      --argjson members "$legacy_members" \
      --arg pub "$legacy_pub" --arg code "$legacy_code" --arg cluster "$legacy_cluster" '
        .backends == $backends
        and (.pubkey | ascii_downcase) == $pub
        and (.code_id | ascii_downcase) == $code
        and (.cluster | ascii_downcase) == $cluster
        and ((.members | map(ascii_downcase)) == $members)
      ' >/dev/null \
      || die "legacy LB routing state does not match authenticated controller /active"
    normalized=$({
      printf 'STATE_SCHEMA=1\n'
      for key in UPDATED_AT MESH_IP ACTIVE_BACKEND ACTIVE_BACKEND_NODE ACTIVE_BACKENDS \
        ACTIVE_BACKEND_NODES ACTIVE_PUBKEY ACTIVE_CODE_ID ACTIVE_INDEXER_CLUSTER \
        ACTIVE_MEMBER_IDS STABLE_ENDPOINT; do
        printf '%s=%s\n' "$key" "$(echo "$value" | jq -r --arg key "$key" '.[$key]')"
      done
    })
    _durable_private_text_write "$LB_STATE" "$normalized" \
      || die "could not durably migrate legacy LB state"
    _load_lb
    log "migrated validated LB routing state into $LB_STATE"
  fi
}

_ingest_legacy_candidate_state() {
  local target="$1" source destination legacy normalized expected_hash expected_app actual_app
  source="$LOGDIR/generic-node-${target}.state"
  destination="$LB_PRIVATE_STATE_DIR/legacy-candidate-${target}.state"
  expected_hash="${INDEXER_EXPECTED_BACKEND_STATE_SHA256:-}"
  expected_app="${INDEXER_EXPECTED_BACKEND_APP_ID:-}"
  [ -n "$expected_hash" ] \
    || die "legacy LOGDIR candidate ingestion requires independent INDEXER_EXPECTED_BACKEND_STATE_SHA256; an app ID alone does not bind VM_ID and H"
  [[ "$expected_hash" =~ ^[0-9a-f]{64}$ ]] \
    || die "INDEXER_EXPECTED_BACKEND_STATE_SHA256 must be 64 lowercase hex characters"
  if [ -n "$expected_app" ]; then
    expected_app="$(_validate_address "$expected_app" "INDEXER_EXPECTED_BACKEND_APP_ID")"
  fi
  [ -e "$source" ] || [ -L "$source" ] \
    || die "missing private HA state and legacy single-C3 candidate state for $target"
  legacy=$(_read_legacy_owned_kv_json "$source" \
    "STATE_SCHEMA,UPDATED_AT,STATE_PHASE,X,H,VM_ID,CLUSTER,MEMBER_IMPL,KMS_ROOT,GATEWAY_DOMAIN,GUEST_CONFIG_SHA256" \
    "$expected_hash") \
    || die "could not safely ingest legacy candidate state: $source"
  actual_app="$(_validate_address "$(echo "$legacy" | jq -r .X)" "legacy candidate X")"
  [ -z "$expected_app" ] || [ "$actual_app" = "$expected_app" ] \
    || die "legacy candidate app ID differs from INDEXER_EXPECTED_BACKEND_APP_ID"
  normalized="$(_normalize_generic_state_json "$legacy" 0)"
  _durable_private_text_write "$destination" "$normalized" \
    || die "could not durably snapshot legacy candidate state"
  printf '%s\n' "$destination"
}

_named_backend_state_json() {
  local target="$1" mode="${2:-single}" path
  path="$(_backend_state "$target")"
  if [ -n "${INDEXER_BACKEND_STATE_DIR+x}" ] || [ "$mode" = shared ] \
    || [ -e "$path" ] || [ -L "$path" ]; then
    _backend_state_json "$target" "$path"
    return
  fi
  path="$(_ingest_legacy_candidate_state "$target")"
  _backend_state_json "$target" "$path"
}

_backend_ip() {
  local target="$1" require_bridge="${2:-0}" state_json="${3:-}" vm app cluster ip
  if [[ "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    _validate_private_ipv4 "$target"
    return 0
  fi
  if [ -z "$state_json" ]; then
    if [ "$require_bridge" = 1 ]; then
      state_json="$(_named_backend_state_json "$target" shared)"
    else
      state_json="$(_named_backend_state_json "$target" single)"
    fi
  fi
  if [ "$require_bridge" = 1 ]; then
    [ "${INDEXER_LB_BACKEND_MODE:-bridge}" = bridge ] \
      || die "shared named workers require same-host bridge routing; mesh mode is only valid for legacy single-C3 candidates"
    vm=$(echo "$state_json" | jq -er .VM_ID) || die "named backend state has no VM_ID"
    ip="$(_bridge_ip_for_vm "$vm" 2>/dev/null || true)"
    [ -n "$ip" ] \
      || die "shared worker $target has no same-host bridge address; pass an explicitly private routable IP instead"
    ip="$(_validate_private_ipv4 "$ip")"
    printf '%s\n' "$ip"
    return 0
  fi
  if [ "${INDEXER_LB_BACKEND_MODE:-bridge}" = bridge ]; then
    vm=$(echo "$state_json" | jq -er .VM_ID) || die "named backend state has no VM_ID"
    ip="$(_bridge_ip_for_vm "$vm" 2>/dev/null || true)"
    if [ -n "$ip" ]; then
      ip="$(_validate_private_ipv4 "$ip")"
      printf '%s\n' "$ip"
      return 0
    fi
  fi
  app=$(echo "$state_json" | jq -er .X) || die "named backend state has no app address"
  cluster=$(echo "$state_json" | jq -er .CLUSTER) || die "named backend state has no cluster"
  _member_mesh_ip "$app" "$cluster"
}

_backend_http() {
  local ip="$1" path="$2" out rc invalid_response=0
  if out=$(ssh_box "curl -fsS --noproxy '*' --max-time 12 --max-filesize 65536 --location --max-redirs 0 --proto '=http' 'http://$ip:9090$path'" 2>/dev/null); then
    printf '%s\n' "$out"
    return 0
  else
    rc=$?
    case "$rc" in 6|7|28|255) ;; *) invalid_response=1 ;; esac
  fi
  if out=$(ssh_mesh "curl -fsS --noproxy '*' --max-time 12 --max-filesize 65536 --location --max-redirs 0 --proto '=http' 'http://$ip:9090$path'" 2>/dev/null); then
    printf '%s\n' "$out"
    return 0
  else
    rc=$?
    case "$rc" in 6|7|28|255) ;; *) invalid_response=1 ;; esac
  fi
  # 75 means neither route reached the service; 76 means at least one route
  # reached it but received an HTTP error, redirect, oversized, or malformed
  # transfer. Recovery may tolerate only the former.
  [ "$invalid_response" -eq 0 ] || return 76
  return 75
}

_current_registry_cluster_count() {
  local value endpoint status
  value=$(cast call "$REGISTRY" 'current()(string,bytes32,bytes32,uint64)' \
    --json --rpc-url "$RPC_URL" 2>/dev/null) || return 1
  endpoint=$(echo "$value" | jq -er '.[0] | select(length > 0)') || return 1
  status=$(curl -fsS --noproxy '*' --max-time 12 --max-filesize 65536 --location --max-redirs 0 \
    --proto '=https' "${endpoint/-50052./-9090.}/status" 2>/dev/null) \
    || return 1
  echo "$status" | jq -er '.readModel.clusterCount | select(type == "number" and . > 0)'
}

_backend_metadata() {
  local target="$1" ip="$2" mode="${3:-single}" state_json="${4:-}" status health min_clusters
  local expected_code expected_cluster
  min_clusters="${INDEXER_MIN_CLUSTER_COUNT:-}"
  if [ -z "$min_clusters" ]; then
    min_clusters="$(_current_registry_cluster_count || true)"
    min_clusters="${min_clusters:-1}"
  fi
  status="$(_backend_http "$ip" /status)" || die "candidate status unavailable at $ip:9090"
  BACKEND_PUBKEY=$(echo "$status" | jq -r '.pubKey // empty' | tr 'A-F' 'a-f')
  echo "$BACKEND_PUBKEY" | grep -Eq '^0x[0-9a-f]{64}$' \
    || die "candidate returned invalid pubKey: ${BACKEND_PUBKEY:-<empty>}"
  echo "$status" | jq -e --argjson min "$min_clusters" \
    '.readModel.clusterCount >= $min and (.readModel.atBlock | type == "number")' >/dev/null \
    || die "candidate read model is not caught up or has fewer than $min_clusters cluster(s): $status"

  if [ "$mode" = single ] && echo "$status" | jq -e '.identityMode == "cluster-shared"' >/dev/null; then
    die "cluster-shared candidate $target must be selected as a pool of at least two workers"
  fi

  if [ "$mode" = shared ]; then
    echo "$status" | jq -e \
      '.identityMode == "cluster-shared"
       and .health.rpcReachable == true
       and (.health.chainHeadLagBlocks | type == "number")
       and .health.chainHeadLagBlocks >= 0
       and .health.chainHeadLagBlocks < 10' >/dev/null \
      || die "shared candidate must be cluster-shared, RPC-reachable, and fewer than 10 blocks behind: $status"

    BACKEND_CODE_ID=$(echo "$status" | jq -r '.codeId // empty' | tr 'A-F' 'a-f')
    BACKEND_INDEXER_CLUSTER=$(echo "$status" | jq -r '.indexerCluster // empty' | tr 'A-F' 'a-f')
    BACKEND_MEMBER_ID=$(echo "$status" | jq -r '.servingMemberId // empty' | tr 'A-F' 'a-f')
    echo "$BACKEND_CODE_ID" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "shared candidate returned invalid codeId: ${BACKEND_CODE_ID:-<empty>}"
    [ "$BACKEND_CODE_ID" != "$ZERO32" ] \
      || die "shared candidate returned a zero codeId"
    echo "$BACKEND_INDEXER_CLUSTER" | grep -Eq '^0x[0-9a-f]{40}$' \
      || die "shared candidate returned invalid indexerCluster: ${BACKEND_INDEXER_CLUSTER:-<empty>}"
    [ "$BACKEND_INDEXER_CLUSTER" != "$ZERO_ADDRESS" ] \
      || die "shared candidate returned a zero indexerCluster"
    echo "$BACKEND_MEMBER_ID" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "shared candidate returned invalid servingMemberId: ${BACKEND_MEMBER_ID:-<empty>}"
    [ "$BACKEND_MEMBER_ID" != "$ZERO32" ] \
      || die "shared candidate returned an empty servingMemberId"

    if [[ "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
      expected_code="${INDEXER_BACKEND_CODE_ID:-}"
      expected_cluster="${INDEXER_HA_CLUSTER:-${INDEXER_CLUSTER_ADDR:-}}"
      [ -n "$expected_code" ] \
        || die "literal shared backend IP requires INDEXER_BACKEND_CODE_ID"
      [ -n "$expected_cluster" ] \
        || die "literal shared backend IP requires INDEXER_HA_CLUSTER"
    else
      [ -n "$state_json" ] || state_json="$(_backend_state_json "$target")"
      expected_code=$(echo "$state_json" | jq -er .H) \
        || die "named backend state has no compose/code ID"
      expected_cluster=$(echo "$state_json" | jq -er .CLUSTER) \
        || die "named backend state has no cluster"
    fi
    expected_code="0x${expected_code#0x}"
    expected_code="${expected_code,,}"
    expected_cluster="0x${expected_cluster#0x}"
    expected_cluster="${expected_cluster,,}"
    echo "$expected_code" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "invalid expected shared candidate compose/code id: $expected_code"
    [ "$expected_code" != "$ZERO32" ] \
      || die "shared candidate compose/code id must be nonzero"
    echo "$expected_cluster" | grep -Eq '^0x[0-9a-f]{40}$' \
      || die "invalid expected dedicated Indexer cluster: $expected_cluster"
    [ "$expected_cluster" != "$ZERO_ADDRESS" ] \
      || die "dedicated Indexer cluster must be nonzero"
    [ "$BACKEND_CODE_ID" = "$expected_code" ] \
      || die "candidate /status codeId=$BACKEND_CODE_ID, expected $expected_code"
    [ "$BACKEND_INDEXER_CLUSTER" = "$expected_cluster" ] \
      || die "candidate /status indexerCluster=$BACKEND_INDEXER_CLUSTER, expected $expected_cluster"
    [ "$POOL_PINNED_PUBKEY" = "$BACKEND_PUBKEY" ] \
      || die "pinned INDEXER_BACKEND_PUBKEY does not match shared candidate /status"
    export BACKEND_PUBKEY BACKEND_CODE_ID BACKEND_INDEXER_CLUSTER BACKEND_MEMBER_ID
    return 0
  fi

  health="$(_backend_http "$ip" /healthz)" || die "candidate $target is not healthy at $ip:9090"
  echo "$health" | jq -e '.status == "ok"' >/dev/null \
    || die "candidate health is not ok: $health"
  echo "$status" | jq -e '.health.ok == true' >/dev/null \
    || die "candidate /status reports unhealthy: $status"
  if [[ "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    BACKEND_CODE_ID="${INDEXER_BACKEND_CODE_ID:-}"
    [ -n "$BACKEND_CODE_ID" ] || die "literal backend IP requires INDEXER_BACKEND_CODE_ID"
    if [ -n "${INDEXER_BACKEND_PUBKEY:-}" ] && [ "${INDEXER_BACKEND_PUBKEY,,}" != "$BACKEND_PUBKEY" ]; then
      die "INDEXER_BACKEND_PUBKEY does not match candidate /status"
    fi
  else
    [ -n "$state_json" ] || state_json="$(_backend_state_json "$target")"
    BACKEND_CODE_ID=$(echo "$state_json" | jq -er .H) \
      || die "named backend state has no compose/code ID"
  fi
  BACKEND_CODE_ID="0x${BACKEND_CODE_ID#0x}"
  BACKEND_CODE_ID="${BACKEND_CODE_ID,,}"
  echo "$BACKEND_CODE_ID" | grep -Eq '^0x[0-9a-fA-F]{64}$' \
    || die "invalid candidate compose/code id: $BACKEND_CODE_ID"
  BACKEND_INDEXER_CLUSTER=""
  BACKEND_MEMBER_ID=""
  export BACKEND_PUBKEY BACKEND_CODE_ID BACKEND_INDEXER_CLUSTER BACKEND_MEMBER_ID
}

_resolve_switch_pool() {
  local requested="$1" target backend state_json lb_bridge mode=single item
  local -a raw_targets=()
  local -A seen_targets=() seen_backends=() seen_members=()
  POOL_TARGETS=()
  POOL_BACKENDS=()
  POOL_MEMBER_IDS=()
  POOL_PUBKEY=""
  POOL_CODE_ID=""
  POOL_CLUSTER=""
  POOL_PINNED_PUBKEY=""
  POOL_SHARED_HA=0

  [ -n "${VM_ID:-}" ] || die "LB VM_ID is unavailable while excluding self-loop backends"
  lb_bridge="$(_bridge_ip_for_vm "$VM_ID" 2>/dev/null || true)"
  [ -n "$lb_bridge" ] \
    || die "could not resolve the LB's own bridge address; refusing backend admission"
  lb_bridge="$(_validate_private_ipv4 "$lb_bridge")"

  [[ "$requested" != ,* && "$requested" != *, && "$requested" != *,,* ]] \
    || die "backend pool contains an empty member"
  IFS=',' read -r -a raw_targets <<<"$requested"
  [ "${#raw_targets[@]}" -le "$MAX_INDEXER_BACKENDS" ] \
    || die "backend pool supports at most $MAX_INDEXER_BACKENDS members"
  if [ "${#raw_targets[@]}" -gt 1 ]; then
    mode=shared
    POOL_SHARED_HA=1
    _require_protocol_v3_fleet_confirmation
    POOL_PINNED_PUBKEY="${INDEXER_BACKEND_PUBKEY:-}"
    POOL_PINNED_PUBKEY="${POOL_PINNED_PUBKEY,,}"
    echo "$POOL_PINNED_PUBKEY" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "a shared HA pool requires pinned INDEXER_BACKEND_PUBKEY"
    [ "$POOL_PINNED_PUBKEY" != "$ZERO32" ] \
      || die "INDEXER_BACKEND_PUBKEY must be nonzero"
  fi
  [ "$mode" = single ] || [ "${#raw_targets[@]}" -ge 2 ] \
    || die "a shared HA pool requires at least two backends"

  for item in "${raw_targets[@]}"; do
    target="$(_trim "$item")"
    [ -n "$target" ] || die "backend pool contains an empty member"
    [ -z "${seen_targets[$target]:-}" ] || die "backend pool repeats target $target"
    seen_targets[$target]=1
    state_json=""
    if [[ ! "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
      state_json="$(_named_backend_state_json "$target" "$mode")"
    fi
    if [ "$mode" = shared ]; then
      backend="$(_backend_ip "$target" 1 "$state_json")" \
        || die "could not resolve shared candidate backend $target"
    else
      backend="$(_backend_ip "$target" 0 "$state_json")" \
        || die "could not resolve candidate backend $target"
    fi
    [ "$backend" != "${MESH_IP:-}" ] \
      || die "candidate backend $target resolves to the LB's own mesh address"
    [ "$backend" != "$lb_bridge" ] \
      || die "candidate backend $target resolves to the LB's own bridge address"
    [ -z "${seen_backends[$backend]:-}" ] \
      || die "backend pool resolves more than one target to $backend"
    seen_backends[$backend]=1
    _backend_metadata "$target" "$backend" "$mode" "$state_json"

    if [ -z "$POOL_PUBKEY" ]; then
      POOL_PUBKEY="$BACKEND_PUBKEY"
      POOL_CODE_ID="$BACKEND_CODE_ID"
      POOL_CLUSTER="$BACKEND_INDEXER_CLUSTER"
    else
      [ "$BACKEND_PUBKEY" = "$POOL_PUBKEY" ] \
        || die "shared HA backends do not expose the same pubKey"
      [ "$BACKEND_CODE_ID" = "$POOL_CODE_ID" ] \
        || die "shared HA backends do not expose the same nonzero codeId"
      [ "$BACKEND_INDEXER_CLUSTER" = "$POOL_CLUSTER" ] \
        || die "shared HA backends do not belong to the same dedicated Indexer cluster"
    fi
    if [ "$mode" = shared ]; then
      [ -z "${seen_members[$BACKEND_MEMBER_ID]:-}" ] \
        || die "shared HA backends repeat servingMemberId $BACKEND_MEMBER_ID"
      seen_members[$BACKEND_MEMBER_ID]=1
      POOL_MEMBER_IDS+=("$BACKEND_MEMBER_ID")
    fi
    POOL_TARGETS+=("$target")
    POOL_BACKENDS+=("$backend")
  done

  if [ "$mode" = shared ]; then
    [ "${POOL_CLUSTER,,}" != "${CLUSTER,,}" ] \
      || die "shared Indexer workers require a dedicated cluster; pool cluster equals the LB/C3 cluster $CLUSTER"
  fi
  POOL_TARGETS_CSV=$(IFS=,; echo "${POOL_TARGETS[*]}")
  POOL_BACKENDS_CSV=$(IFS=,; echo "${POOL_BACKENDS[*]}")
  POOL_MEMBER_IDS_CSV=$(IFS=,; echo "${POOL_MEMBER_IDS[*]}")
}

_control_request_to() {
  local mesh_ip="$1" method="$2" path="$3" payload="${4:-}" timeout remote_client remote_client_b64
  mesh_ip="$(_validate_private_ipv4 "$mesh_ip")"
  _ensure_secrets
  case "$method" in
    GET) timeout=15 ;;
    POST) timeout=150 ;;
    *) die "unsupported LB control method: $method" ;;
  esac
  remote_client=$(cat <<'PY'
import re
import sys
import urllib.error
import urllib.request

source = sys.stdin.buffer
key = source.readline(256).rstrip(b"\n").decode("ascii")
method = source.readline(16).rstrip(b"\n").decode("ascii")
url = source.readline(1024).rstrip(b"\n").decode("ascii")
timeout = int(source.readline(16).rstrip(b"\n"))
payload = source.read(65537)
if not re.fullmatch(r"ilb_[0-9a-f]{64}", key):
    raise SystemExit("invalid control key envelope")
if method not in ("GET", "POST") or len(payload) > 65536:
    raise SystemExit("invalid control request envelope")
headers = {"Authorization": f"Bearer {key}"}
data = None
if method == "POST":
    headers["Content-Type"] = "application/json"
    data = payload
elif payload:
    raise SystemExit("GET control envelope unexpectedly has a body")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

request = urllib.request.Request(url, data=data, headers=headers, method=method)
try:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    )
    with opener.open(request, timeout=timeout) as response:
        body = response.read(65537)
except urllib.error.HTTPError as exc:
    body = exc.read(65537)
    sys.stderr.buffer.write(body[:65536])
    raise SystemExit(22) from exc
except (OSError, urllib.error.URLError) as exc:
    raise SystemExit(f"control request failed: {exc}") from exc
if len(body) > 65536:
    raise SystemExit("control response exceeds 64KiB")
sys.stdout.buffer.write(body)
PY
)
  remote_client_b64=$(printf '%s' "$remote_client" | base64 | tr -d '\n') \
    || die "could not encode LB control client"
  [[ "$remote_client_b64" =~ ^[A-Za-z0-9+/=]+$ ]] \
    || die "encoded LB control client is malformed"
  {
    printf '%s\n' "$INDEXER_LB_ADMIN_KEY"
    printf '%s\n' "$method"
    printf 'http://%s:50053%s\n' "$mesh_ip" "$path"
    printf '%s\n' "$timeout"
    printf '%s' "$payload"
  } | ssh_mesh "python3 -c 'import base64,sys;exec(base64.b64decode(sys.argv[1]))' '$remote_client_b64'"
}

_control_request() {
  local method="$1" path="$2" payload="${3:-}"
  _discover_mesh_ip || die "LB is not registered with a mesh IP"
  _control_request_to "$MESH_IP" "$method" "$path" "$payload"
}

_control_get() {
  _control_request GET "$1"
}

_control_post() {
  _control_request POST "$1" "$2"
}

_registry_owner_preflight() {
  local owner rpc_chain configured_chain signer
  rpc_chain=$(cast chain-id --rpc-url "$RPC_URL" 2>/dev/null) \
    || die "could not read RPC chain ID"
  configured_chain=$(cast to-dec "$CHAIN_ID" 2>/dev/null) \
    || die "configured CHAIN_ID is invalid: $CHAIN_ID"
  rpc_chain=$(cast to-dec "$rpc_chain" 2>/dev/null) \
    || die "RPC returned an invalid chain ID"
  [ "$rpc_chain" = "$configured_chain" ] \
    || die "RPC chain ID=$rpc_chain but configured CHAIN_ID=$configured_chain"
  signer=$(cast wallet address --private-key "$PRIVATE_KEY" 2>/dev/null | tr 'A-F' 'a-f') \
    || die "could not derive address from configured PRIVATE_KEY"
  [ "$signer" = "${DEPLOYER_ADDR,,}" ] \
    || die "configured PRIVATE_KEY signs as $signer, not DEPLOYER_ADDR=${DEPLOYER_ADDR,,}"
  owner=$(cast call "$REGISTRY" 'owner()(address)' --rpc-url "$RPC_URL" 2>/dev/null | tr 'A-F' 'a-f') \
    || die "could not read IndexerRegistry.owner()"
  [ "$owner" = "${DEPLOYER_ADDR,,}" ] \
    || die "IndexerRegistry owner=$owner but configured deployer=${DEPLOYER_ADDR,,}; this driver does not yet emit Safe transactions"
}

_atomic_json_write() {
  local path="$1" value="$2"
  python3 - "$path" 3< <(printf '%s' "$value") <<'PY' \
    || die "could not durably write transaction journal: $path"
import json
import os
import stat
import sys
import tempfile

path = os.path.abspath(sys.argv[1])
raw = os.fdopen(3, "rb").read(65537)
if len(raw) > 65536:
    raise SystemExit("transaction journal payload exceeds 64KiB")
value = json.loads(raw)
if not isinstance(value, dict):
    raise SystemExit("transaction journal root must be an object")
parent = os.path.dirname(path) or "."
name = os.path.basename(path)
if os.path.realpath(parent) != parent:
    raise SystemExit("transaction journal directory contains a symlink")
ancestor_fd = os.open(os.path.dirname(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    ancestor_info = os.fstat(ancestor_fd)
    if ancestor_info.st_uid != os.getuid() or stat.S_IMODE(ancestor_info.st_mode) & 0o022:
        raise SystemExit("transaction journal ancestor owner/mode mismatch")
finally:
    os.close(ancestor_fd)
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    directory_info = os.fstat(directory_fd)
    if directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise SystemExit("transaction journal directory must be current-user-owned mode 0700")
    try:
        existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if not stat.S_ISREG(existing.st_mode):
            raise SystemExit("refusing non-regular transaction journal target")
        if existing.st_uid != os.getuid() or stat.S_IMODE(existing.st_mode) != 0o600:
            raise SystemExit("existing transaction journal owner/mode mismatch")
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, separators=(",", ":"), sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(
            os.path.basename(tmp), name,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
finally:
    os.close(directory_fd)
PY
}

_txn_read() {
  _read_private_json "$TXN_STATE" \
    || die "could not safely read transaction journal: $TXN_STATE"
}

_txn_update_phase() {
  local phase="$1" current value
  current="$(_txn_read)"
  value=$(jq -c --arg phase "$phase" --argjson at "$(date +%s)" \
    '.phase=$phase | .phase_updated_at=$at' <<<"$current") \
    || die "could not update transaction phase"
  _atomic_json_write "$TXN_STATE" "$value" \
    || die "could not persist transaction phase"
}

_txn_install_registry_tx() {
  local phase="$1" tx="$2" current value
  current="$(_txn_read)"
  value=$({ printf '%s\n' "$current"; printf '%s\n' "$tx"; } | jq -cs \
    --arg phase "$phase" \
    --argjson at "$(date +%s)" '
      .[0] as $state | .[1] as $tx
      | $state
      | .registry_tx_history = ((.registry_tx_history // []) +
          (if .active_registry_tx then [.active_registry_tx] else [] end))
      | .active_registry_tx = $tx
      | .phase = $phase
      | .phase_updated_at = $at
    ') || die "could not install signed registry transaction"
  _atomic_json_write "$TXN_STATE" "$value" \
    || die "could not persist signed registry transaction"
}

_txn_patch_active_registry_tx() {
  local patch="$1" phase="${2:-}" current value
  current="$(_txn_read)"
  value=$(jq -c \
    --arg phase "$phase" \
    --argjson at "$(date +%s)" \
    --argjson patch "$patch" '
      if (.active_registry_tx | type) != "object" then
        error("missing active_registry_tx")
      else
        .active_registry_tx = (.active_registry_tx + $patch)
        | (if $phase == "" then . else .phase = $phase end)
        | .phase_updated_at = $at
      end
    ' <<<"$current") || die "could not update registry transaction journal"
  _atomic_json_write "$TXN_STATE" "$value" \
    || die "could not persist registry transaction update"
}

_to_uint() {
  python3 - "$1" <<'PY'
import sys
value = sys.argv[1]
try:
    parsed = int(value, 0)
except ValueError:
    raise SystemExit(1)
if parsed < 0:
    raise SystemExit(1)
print(parsed)
PY
}

_encode_registry_tx() {
  local purpose="$1" nonce="$2" hash="$3" signed_at="$4" signed_at_block="$5"
  local confirmations="$6" endpoint="$7" code_id="$8" pubkey="$9" updated_at="${10}"
  local raw="${11}"
  python3 - "$purpose" "$nonce" "$hash" "$signed_at" "$signed_at_block" \
    "$confirmations" "$endpoint" "$code_id" "$pubkey" "$updated_at" \
    3< <(printf '%s' "$raw") <<'PY'
import json
import os
import re
import sys

(
    purpose, nonce, tx_hash, signed_at, signed_at_block, confirmations,
    endpoint, code_id, pubkey, updated_at,
) = sys.argv[1:]
raw = os.fdopen(3, "rb").read(32769)
if len(raw) > 32768:
    raise SystemExit("signed transaction exceeds 32KiB")
try:
    raw_tx = raw.decode("ascii")
except UnicodeDecodeError as exc:
    raise SystemExit("signed transaction is not ASCII") from exc
if not re.fullmatch(r"0x[0-9a-fA-F]+", raw_tx):
    raise SystemExit("signed transaction is not hex")
value = {
    "purpose": purpose,
    "nonce": int(nonce),
    "hash": tx_hash,
    "raw_tx": raw_tx,
    "desired_tuple": {
        "endpoint": endpoint,
        "code_id": code_id.lower(),
        "pubkey": pubkey.lower(),
        "updated_at": int(updated_at),
    },
    "state": "signed-not-published",
    "signed_at": int(signed_at),
    "signed_at_block": int(signed_at_block),
    "required_confirmations": int(confirmations),
    "publish_attempts": 0,
}
print(json.dumps(value, separators=(",", ":"), sort_keys=True))
PY
}

_rpc_publish_raw() {
  local raw="$1"
  python3 - "$RPC_URL" 3< <(printf '%s' "$raw") <<'PY'
import json
import os
import re
import sys
import urllib.error
import urllib.request

rpc_url = sys.argv[1]
raw = os.fdopen(3, "rb").read(32769)
if len(raw) > 32768:
    raise SystemExit("signed transaction exceeds 32KiB")
try:
    raw_tx = raw.decode("ascii")
except UnicodeDecodeError as exc:
    raise SystemExit("signed transaction is not ASCII") from exc
if not re.fullmatch(r"0x[0-9a-fA-F]+", raw_tx):
    raise SystemExit("signed transaction is not hex")
body = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [raw_tx]},
    separators=(",", ":"),
).encode("ascii")
request = urllib.request.Request(
    rpc_url, data=body, headers={"Content-Type": "application/json"}, method="POST"
)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

try:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    )
    with opener.open(request, timeout=45) as response:
        reply = response.read(65537)
except (OSError, urllib.error.URLError) as exc:
    raise SystemExit(f"raw transaction publication failed: {exc}") from exc
if len(reply) > 65536:
    raise SystemExit("raw transaction publication response exceeds 64KiB")
try:
    value = json.loads(reply)
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit("raw transaction publication returned malformed JSON") from exc
result = value.get("result") if isinstance(value, dict) else None
if not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", result):
    error = value.get("error") if isinstance(value, dict) else value
    raise SystemExit(f"raw transaction publication failed: {error}")
print(result.lower())
PY
}

_build_registry_tx() {
  local purpose="$1" endpoint="$2" code_id="$3" pubkey="$4"
  local nonce signed_at signed_at_block updated_at raw tx_hash tx
  [[ "$purpose" =~ ^(forward|rollback|recovery-rollback-[0-9]+)$ ]] \
    || die "invalid registry transaction purpose: $purpose"
  [[ "$INDEXER_LB_TX_CONFIRMATIONS" =~ ^[1-9][0-9]*$ ]] \
    || die "INDEXER_LB_TX_CONFIRMATIONS must be a positive integer"
  _registry_owner_preflight
  nonce=$(cast nonce "$DEPLOYER_ADDR" --block pending --rpc-url "$RPC_URL") \
    || return 1
  nonce="$(_to_uint "$nonce")" || return 1
  signed_at_block=$(cast block-number --rpc-url "$RPC_URL") || return 1
  signed_at_block="$(_to_uint "$signed_at_block")" || return 1
  updated_at=$(date +%s)
  if ! raw=$(cast mktx \
    "$REGISTRY" \
    'setIndexer((string,bytes32,bytes32,uint64))' \
    "($endpoint,$code_id,$pubkey,$updated_at)" \
    --private-key "$PRIVATE_KEY" \
    --nonce "$nonce" \
    --chain "$CHAIN_ID" \
    --rpc-url "$RPC_URL" 2>&1); then
    log "registry $purpose transaction construction failed before publication: $raw"
    return 1
  fi
  raw=$(printf '%s\n' "$raw" | grep -E '^0x[0-9a-fA-F]+$' | tail -1)
  [[ "$raw" =~ ^0x[0-9a-fA-F]+$ ]] && [ "${#raw}" -le 32768 ] \
    || die "cast mktx returned an invalid or oversized signed transaction"
  tx_hash=$(printf '%s' "$raw" | cast keccak 2>/dev/null) \
    || die "could not derive signed registry transaction hash"
  tx_hash="${tx_hash,,}"
  [[ "$tx_hash" =~ ^0x[0-9a-f]{64}$ ]] \
    || die "invalid signed registry transaction hash: $tx_hash"
  signed_at=$(date +%s)
  tx="$(_encode_registry_tx "$purpose" "$nonce" "$tx_hash" "$signed_at" \
    "$signed_at_block" "$INDEXER_LB_TX_CONFIRMATIONS" "$endpoint" "$code_id" \
    "$pubkey" "$updated_at" "$raw")" \
    || die "could not encode signed registry transaction journal"
  _txn_install_registry_tx "$purpose-signed" "$tx" \
    || die "signed registry transaction was not durably journaled"
  printf '%s\n' "$tx_hash"
}

_publish_active_registry_tx() {
  local current raw expected_hash actual_hash out rc returned_hash patch attempts
  current="$(_txn_read)"
  raw=$(jq -er '.active_registry_tx.raw_tx | select(type == "string" and test("^0x[0-9a-fA-F]+$"))' <<<"$current") \
    || die "journal has no valid signed raw registry transaction"
  expected_hash=$(jq -er '.active_registry_tx.hash | ascii_downcase | select(test("^0x[0-9a-f]{64}$"))' <<<"$current") \
    || die "journal has no valid registry transaction hash"
  actual_hash=$(printf '%s' "$raw" | cast keccak 2>/dev/null | tr 'A-F' 'a-f') \
    || die "could not recompute signed registry transaction hash"
  [ "$actual_hash" = "$expected_hash" ] \
    || die "signed raw transaction does not match its journaled hash; refusing broadcast"
  attempts=$(jq -er '.active_registry_tx.publish_attempts // 0' <<<"$current") \
    || die "journal has invalid publish attempt count"
  patch=$(jq -nc \
    --argjson attempts "$((attempts + 1))" \
    --argjson at "$(date +%s)" \
    '{state:"publish-attempting",publish_attempts:$attempts,last_publish_attempt_at:$at}') \
    || die "could not encode pre-publication transaction state"
  # Persist uncertainty before the first byte can leave this process.
  _txn_patch_active_registry_tx "$patch" registry-publish-attempting \
    || die "refusing broadcast without a durable publish-attempting journal"
  if out=$(_rpc_publish_raw "$raw" 2>&1); then
    rc=0
  else
    rc=$?
  fi
  returned_hash=$(printf '%s\n' "$out" | grep -oE '0x[0-9a-fA-F]{64}' | tail -1 | tr 'A-F' 'a-f') \
    || returned_hash=""
  if [ -n "$returned_hash" ] && [ "$returned_hash" != "$expected_hash" ]; then
    patch=$(jq -nc --argjson rc "$rc" --arg returned "$returned_hash" \
      '{state:"publish-uncertain",last_publish_rc:$rc,unexpected_returned_hash:$returned}') \
      || die "could not encode ambiguous registry publication result"
    _txn_patch_active_registry_tx "$patch" registry-publish-uncertain \
      || die "could not persist ambiguous registry publication result"
    return 0
  fi
  if [ "$rc" -eq 0 ]; then
    [ "$returned_hash" = "$expected_hash" ] \
      || die "RPC publish returned success without the journaled transaction hash"
    patch=$(jq -nc --argjson rc "$rc" --argjson at "$(date +%s)" \
      '{state:"published",last_publish_rc:$rc,published_at:$at}') \
      || die "could not encode registry publication result"
    _txn_patch_active_registry_tx "$patch" registry-published \
      || die "could not persist registry publication result"
    return 0
  fi
  patch=$(jq -nc --argjson rc "$rc" --argjson at "$(date +%s)" \
    '{state:"publish-uncertain",last_publish_rc:$rc,last_publish_error_at:$at}') \
    || die "could not encode ambiguous registry publication result"
  _txn_patch_active_registry_tx "$patch" registry-publish-uncertain \
    || die "could not persist ambiguous registry publication result"
  return 0
}

_classify_active_registry_tx() {
  local current hash nonce required receipt status receipt_block receipt_hash head confirmations patch
  local exact_tx finalized_nonce latest_nonce pending_nonce state attempts
  local replacement_block replacement_confirmations finalized_header finalized_block
  local canonical_header canonical_hash
  current="$(_txn_read)"
  hash=$(jq -er '.active_registry_tx.hash' <<<"$current") || return 1
  nonce=$(jq -er '.active_registry_tx.nonce' <<<"$current") || return 1
  required=$(jq -er '.active_registry_tx.required_confirmations' <<<"$current") || return 1
  state=$(jq -er '.active_registry_tx.state' <<<"$current") || return 1
  attempts=$(jq -er '.active_registry_tx.publish_attempts // 0' <<<"$current") || return 1

  if receipt=$(cast receipt "$hash" --json --async --rpc-url "$RPC_URL" 2>/dev/null) \
    && echo "$receipt" | jq -e '
      type == "object" and (.blockNumber != null) and (.status != null)
      and (.blockHash | type == "string" and test("^0x[0-9a-fA-F]{64}$"))
    ' >/dev/null 2>&1; then
    status=$(echo "$receipt" | jq -r '.status')
    receipt_block=$(echo "$receipt" | jq -r '.blockNumber')
    receipt_hash=$(echo "$receipt" | jq -r '.blockHash | ascii_downcase')
    status="$(_to_uint "$status")" || return 1
    receipt_block="$(_to_uint "$receipt_block")" || return 1
    head=$(cast block-number --rpc-url "$RPC_URL" 2>/dev/null) || {
      printf 'rpc-unavailable\n'; return 0;
    }
    head="$(_to_uint "$head")" || { printf 'rpc-unavailable\n'; return 0; }
    finalized_header=$(cast block finalized --json --rpc-url "$RPC_URL" 2>/dev/null) \
      || { printf 'rpc-unavailable\n'; return 0; }
    finalized_block=$(echo "$finalized_header" | jq -er '.number') \
      || { printf 'rpc-unavailable\n'; return 0; }
    finalized_block="$(_to_uint "$finalized_block")" \
      || { printf 'rpc-unavailable\n'; return 0; }
    confirmations=$((head - receipt_block + 1))
    [ "$confirmations" -ge 0 ] || confirmations=0
    if [ "$confirmations" -lt "$required" ] \
      || [ "$finalized_block" -lt "$receipt_block" ]; then
      patch=$(jq -nc \
        --argjson block "$receipt_block" \
        --argjson confirmations "$confirmations" \
        --argjson finalized "$finalized_block" \
        '{state:"mined-awaiting-finality",receipt_block:$block,
          confirmations:$confirmations,observed_finalized_block:$finalized}')
      _txn_patch_active_registry_tx "$patch" registry-awaiting-finality
      printf 'pending-finality\n'
      return 0
    fi
    canonical_header=$(cast block "$receipt_block" --json --rpc-url "$RPC_URL" 2>/dev/null) \
      || { printf 'rpc-unavailable\n'; return 0; }
    canonical_hash=$(echo "$canonical_header" | jq -er '
      .hash | ascii_downcase | select(test("^0x[0-9a-f]{64}$"))
    ') || { printf 'rpc-unavailable\n'; return 0; }
    if [ "$canonical_hash" != "$receipt_hash" ]; then
      patch=$(jq -nc \
        --arg receipt_hash "$receipt_hash" \
        --arg canonical_hash "$canonical_hash" \
        --argjson block "$receipt_block" '
          {state:"receipt-noncanonical",receipt_block:$block,
           receipt_block_hash:$receipt_hash,canonical_block_hash:$canonical_hash}')
      _txn_patch_active_registry_tx "$patch" registry-awaiting-finality
      printf 'receipt-noncanonical\n'
      return 0
    fi
    if [ "$status" -eq 1 ]; then
      patch=$(jq -nc \
        --argjson block "$receipt_block" \
        --argjson confirmations "$confirmations" \
        --argjson finalized "$finalized_block" \
        --argjson at "$(date +%s)" \
        '{state:"confirmed-success",resolution:"success",receipt_block:$block,
          confirmations:$confirmations,observed_finalized_block:$finalized,resolved_at:$at}')
      _txn_patch_active_registry_tx "$patch" registry-confirmed-success
      printf 'success\n'
      return 0
    fi
    if [ "$status" -eq 0 ]; then
      patch=$(jq -nc \
        --argjson block "$receipt_block" \
        --argjson confirmations "$confirmations" \
        --argjson finalized "$finalized_block" \
        --argjson at "$(date +%s)" \
        '{state:"confirmed-revert",resolution:"revert",receipt_block:$block,
          confirmations:$confirmations,observed_finalized_block:$finalized,resolved_at:$at}')
      _txn_patch_active_registry_tx "$patch" registry-confirmed-revert
      printf 'revert\n'
      return 0
    fi
    printf 'rpc-unavailable\n'
    return 0
  fi

  # An exact tx still visible without a receipt is pending/unknown, never failed.
  if exact_tx=$(cast tx "$hash" --json --rpc-url "$RPC_URL" 2>/dev/null) \
    && echo "$exact_tx" | jq -e 'type == "object"' >/dev/null 2>&1; then
    patch='{"state":"pending"}'
    _txn_patch_active_registry_tx "$patch" registry-pending
    printf 'pending\n'
    return 0
  fi

  finalized_nonce=$(cast nonce "$DEPLOYER_ADDR" --block finalized --rpc-url "$RPC_URL" 2>/dev/null) \
    || { printf 'rpc-unavailable\n'; return 0; }
  latest_nonce=$(cast nonce "$DEPLOYER_ADDR" --block latest --rpc-url "$RPC_URL" 2>/dev/null) \
    || { printf 'rpc-unavailable\n'; return 0; }
  pending_nonce=$(cast nonce "$DEPLOYER_ADDR" --block pending --rpc-url "$RPC_URL" 2>/dev/null) \
    || { printf 'rpc-unavailable\n'; return 0; }
  finalized_nonce="$(_to_uint "$finalized_nonce")" \
    || { printf 'rpc-unavailable\n'; return 0; }
  latest_nonce="$(_to_uint "$latest_nonce")" \
    || { printf 'rpc-unavailable\n'; return 0; }
  pending_nonce="$(_to_uint "$pending_nonce")" \
    || { printf 'rpc-unavailable\n'; return 0; }
  if [ "$latest_nonce" -gt "$nonce" ]; then
    head=$(cast block-number --rpc-url "$RPC_URL" 2>/dev/null) \
      || { printf 'rpc-unavailable\n'; return 0; }
    head="$(_to_uint "$head")" \
      || { printf 'rpc-unavailable\n'; return 0; }
    replacement_block=$(jq -er \
      '.active_registry_tx.replacement_observed_block
       | select(type == "number" and . >= 0)' <<<"$current" 2>/dev/null || true)
    if [ -z "$replacement_block" ] || [ "$replacement_block" -gt "$head" ]; then
      replacement_block="$head"
    fi
    replacement_confirmations=$((head - replacement_block + 1))
    [ "$replacement_confirmations" -ge 0 ] || replacement_confirmations=0
    if [ "$finalized_nonce" -gt "$nonce" ] \
      && [ "$replacement_confirmations" -ge "$required" ]; then
      patch=$(jq -nc \
        --argjson finalized "$finalized_nonce" \
        --argjson latest "$latest_nonce" \
        --argjson block "$replacement_block" \
        --argjson confirmations "$replacement_confirmations" \
        --argjson at "$(date +%s)" '
          {state:"confirmed-replaced",resolution:"replaced",
           observed_finalized_nonce:$finalized,observed_latest_nonce:$latest,
           replacement_observed_block:$block,
           replacement_confirmations:$confirmations,resolved_at:$at}')
      _txn_patch_active_registry_tx "$patch" registry-confirmed-replaced
      printf 'replaced\n'
    else
      patch=$(jq -nc \
        --argjson finalized "$finalized_nonce" \
        --argjson latest "$latest_nonce" \
        --argjson block "$replacement_block" \
        --argjson confirmations "$replacement_confirmations" '
          {state:"replacement-awaiting-finality",
           observed_finalized_nonce:$finalized,observed_latest_nonce:$latest,
           replacement_observed_block:$block,
           replacement_confirmations:$confirmations}')
      _txn_patch_active_registry_tx "$patch" registry-awaiting-finality
      printf 'pending-finality\n'
    fi
  elif [ "$pending_nonce" -gt "$nonce" ]; then
    patch=$(jq -nc --argjson pending "$pending_nonce" \
      '{state:"pending-or-replaced",observed_pending_nonce:$pending,
        replacement_observed_block:null,replacement_confirmations:0}')
    _txn_patch_active_registry_tx "$patch" registry-pending
    printf 'pending\n'
  elif [ "$attempts" -eq 0 ] && [ "$state" = signed-not-published ]; then
    patch='{"replacement_observed_block":null,"replacement_confirmations":0}'
    _txn_patch_active_registry_tx "$patch"
    printf 'signed\n'
  else
    patch='{"state":"publish-unseen","replacement_observed_block":null,"replacement_confirmations":0}'
    _txn_patch_active_registry_tx "$patch" registry-publish-unseen
    printf 'unseen\n'
  fi
}

_wait_active_registry_tx() {
  local deadline classification
  [[ "$INDEXER_LB_TX_WAIT_SECONDS" =~ ^[0-9]+$ ]] \
    || die "INDEXER_LB_TX_WAIT_SECONDS must be a nonnegative integer"
  deadline=$(( $(date +%s) + INDEXER_LB_TX_WAIT_SECONDS ))
  while :; do
    classification="$(_classify_active_registry_tx)" \
      || classification=rpc-unavailable
    case "$classification" in
      success|revert|replaced) printf '%s\n' "$classification"; return 0 ;;
    esac
    [ "$(date +%s)" -lt "$deadline" ] || {
      printf '%s\n' "$classification"
      return 75
    }
    sleep 3
  done
}

_txn_clear() {
  python3 - "$TXN_STATE" <<'PY'
import os
import stat
import sys

path = os.path.abspath(sys.argv[1])
parent, name = os.path.dirname(path), os.path.basename(path)
if os.path.realpath(parent) != parent:
    raise SystemExit("transaction journal directory contains a symlink")
ancestor_fd = os.open(os.path.dirname(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    ancestor_info = os.fstat(ancestor_fd)
    if ancestor_info.st_uid != os.getuid() or stat.S_IMODE(ancestor_info.st_mode) & 0o022:
        raise SystemExit("transaction journal ancestor owner/mode mismatch")
finally:
    os.close(ancestor_fd)
directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    directory_info = os.fstat(directory_fd)
    if directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise SystemExit("transaction journal directory must be current-user-owned mode 0700")
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise SystemExit(0)
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit("refusing to unlink a non-regular transaction journal")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit("transaction journal owner/mode mismatch during clear")
    os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

_operation_status() {
  local operation_id="$1"
  _control_get "/operation?operation_id=$operation_id" | jq -er '.status'
}

_registry_snapshot() {
  local block="${1:-}" value
  local -a cast_args=(
    call "$REGISTRY" 'current()(string,bytes32,bytes32,uint64)'
    --json --rpc-url "$RPC_URL"
  )
  [ -z "$block" ] || cast_args+=(--block "$block")
  value=$(cast "${cast_args[@]}") \
    || return 1
  echo "$value" | jq -ce '
    if type == "array" and length >= 4
       and (.[0] | type) == "string"
       and (.[1] | type) == "string"
       and (.[2] | type) == "string"
    then [.[0], (.[1] | ascii_downcase), (.[2] | ascii_downcase), (.[3] | tostring)]
    else error("invalid registry tuple") end
  '
}

_controller_snapshot() {
  jq -ce '
    if (.active_backends | type) == "array"
       and all(.active_backends[]; type == "string")
       and ((.active_pubkey // "") | type) == "string"
       and ((.active_code_id // "") | type) == "string"
       and ((.active_cluster // "") | type) == "string"
       and (.active_members | type) == "array"
       and all(.active_members[]; type == "string")
       and ((.active_operation_id // "") | type) == "string"
    then {
      backends:.active_backends,
      pubkey:(.active_pubkey // ""),
      code_id:(.active_code_id // ""),
      cluster:(.active_cluster // ""),
      members:.active_members,
      operation_id:(.active_operation_id // "")
    }
    else error("invalid controller active snapshot") end
  '
}

_registry_tuple_kind() {
  local current="$1" txn old new old_match new_match
  txn="$(_txn_read)"
  old=$(jq -c '[.old_registry.endpoint,.old_registry.code_id,.old_registry.pubkey]' <<<"$txn")
  new=$(jq -c '[.new_registry.endpoint,.new_registry.code_id,.new_registry.pubkey]' <<<"$txn")
  current=$(echo "$current" | jq -c '[.[0],.[1],.[2]]')
  old_match=0; new_match=0
  [ "$current" != "$old" ] || old_match=1
  [ "$current" != "$new" ] || new_match=1
  if [ "$old_match" -eq 1 ] && [ "$new_match" -eq 1 ]; then
    printf 'both\n'
  elif [ "$old_match" -eq 1 ]; then
    printf 'old\n'
  elif [ "$new_match" -eq 1 ]; then
    printf 'new\n'
  else
    printf 'third\n'
  fi
}

_restore_payload_from_previous() {
  local operation_id="$1" previous="$2"
  jq -nc \
    --arg operation "$operation_id" \
    --argjson previous "$previous" '
      {
        operation_id:$operation,
        backends:$previous.backends,
        active_pubkey:$previous.pubkey,
        active_code_id:$previous.code_id,
        active_cluster:$previous.cluster,
        active_members:$previous.members,
        active_operation_id:$previous.operation_id
      }
    '
}

_stable_endpoint() {
  _load_generic
  printf 'https://%s-50052.%s\n' "${X#0x}" "$GATEWAY_DOMAIN"
}

_journal_authoritative_prepare() {
  local previous="$1" restore_payload="$2" prepare_response="$3" current value
  current="$(_txn_read)"
  value=$(jq -c \
    --argjson previous "$previous" \
    --argjson restore "$restore_payload" \
    --argjson response "$prepare_response" \
    --argjson at "$(date +%s)" '
      .controller_previous = $previous
      | .restore_payload = $restore
      | .prepare_response = $response
      | .phase = "prepared-authoritative"
      | .phase_updated_at = $at
    ' <<<"$current") || die "could not journal authoritative controller prepare"
  _atomic_json_write "$TXN_STATE" "$value"
}

_controller_matches_previous() {
  local previous="$1" control snapshot prepared
  control="$(_control_get /active)" || return 1
  snapshot=$(echo "$control" | _controller_snapshot | jq -cS .) || return 1
  previous=$(echo "$previous" | jq -cS .) || return 1
  prepared=$(echo "$control" | jq -er '.prepared | type == "object" and length == 0') \
    || return 1
  [ "$prepared" = true ] && [ "$snapshot" = "$previous" ]
}

_restore_previous_and_clear() {
  local current operation_id status restore_payload previous payload
  current="$(_txn_read)" || return 1
  operation_id=$(jq -er '.operation_id' <<<"$current") || return 1
  restore_payload=$(jq -ce '.restore_payload' <<<"$current") || return 1
  previous=$(jq -ce '.controller_previous' <<<"$current") || return 1
  status="$(_operation_status "$operation_id")" || return 1
  payload=$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')
  if [ "$status" = prepared ]; then
    _control_post /abort "$payload" >/dev/null || return 1
  elif [ "$status" = unknown ] && _controller_matches_previous "$previous"; then
    :
  else
    _control_post /restore "$restore_payload" >/dev/null || return 1
  fi
  _controller_matches_previous "$previous" || return 1
  _txn_clear
  log "✔ previous controller generation restored from authoritative prepare snapshot"
}

_begin_registry_rollback() {
  local current count purpose endpoint code pub
  current="$(_txn_read)"
  count=$(jq -er '((.registry_tx_history // []) | length) + 1' <<<"$current") \
    || die "invalid registry transaction history"
  [ "$count" -le 8 ] \
    || die "too many registry rollback attempts; refusing automatic mutation"
  purpose=rollback
  [ "$count" -le 1 ] || purpose="recovery-rollback-$count"
  endpoint=$(jq -r '.old_registry.endpoint' <<<"$current")
  code=$(jq -r '.old_registry.code_id' <<<"$current")
  pub=$(jq -r '.old_registry.pubkey' <<<"$current")
  log "▶ signing $purpose registry transaction before publication"
  _build_registry_tx "$purpose" "$endpoint" "$code" "$pub" >/dev/null \
    || die "rollback construction failed before publication; controller remains paused"
  _publish_active_registry_tx
  recover_transaction
}

switch_backend() {
  local requested="${1:-${TARGET:-}}" stable before before_intent
  local old_endpoint old_code old_pub old_updated control_before controller_snapshot
  local prepare_payload prepare_out prepare_rc authoritative_previous restore_payload
  local pool_json targets_json members_json shared_label operation_id journal value tx_hash
  [ -n "$requested" ] || die "usage: $0 $NODE switch <indexer-node-or-ip[,indexer-node-or-ip...]>"
  if [ -e "$TXN_STATE" ] || [ -L "$TXN_STATE" ]; then
    _txn_read >/dev/null
    die "unfinished Indexer LB transaction journal exists; run '$0 $NODE recover' first: $TXN_STATE"
  fi
  _load_generic
  _default_cluster_env
  _ensure_secrets
  _registry_owner_preflight
  _discover_mesh_ip || die "LB mesh IP unavailable"
  _resolve_switch_pool "$requested"
  stable="$(_stable_endpoint)"
  control_before="$(_control_get /active)" || die "could not snapshot active LB pool"
  echo "$control_before" | jq -e '.prepared | type == "object" and length == 0' >/dev/null \
    || die "controller already has a prepared generation"
  controller_snapshot=$(echo "$control_before" | _controller_snapshot | jq -cS .) \
    || die "LB returned an invalid active snapshot"
  before="$(_registry_snapshot)" || die "could not snapshot IndexerRegistry.current()"
  old_endpoint=$(echo "$before" | jq -r '.[0]')
  old_code=$(echo "$before" | jq -r '.[1]')
  old_pub=$(echo "$before" | jq -r '.[2]')
  old_updated=$(echo "$before" | jq -r '.[3]')
  operation_id=$(openssl rand -hex 32)
  pool_json=$(printf '%s\n' "${POOL_BACKENDS[@]}" | jq -Rsc 'split("\n") | map(select(length > 0))')
  targets_json=$(printf '%s\n' "${POOL_TARGETS[@]}" | jq -Rsc 'split("\n") | map(select(length > 0))')
  members_json=$(printf '%s\n' "${POOL_MEMBER_IDS[@]}" | jq -Rsc 'split("\n") | map(select(length > 0))')

  if [ "$POOL_SHARED_HA" = 1 ]; then
    prepare_payload=$(jq -nc \
      --argjson backends "$pool_json" \
      --arg pubkey "$POOL_PUBKEY" \
      --arg code "$POOL_CODE_ID" \
      --arg cluster "$POOL_CLUSTER" \
      --arg operation "$operation_id" \
      --argjson previous "$controller_snapshot" '
        {backends:$backends,expected_pubkey:$pubkey,expected_code_id:$code,
         expected_cluster:$cluster,protocol_v3_fleet_confirmed:true,
         operation_id:$operation,expected_previous:$previous}')
    shared_label="shared pool nodes=$POOL_TARGETS_CSV backends=$POOL_BACKENDS_CSV cluster=$POOL_CLUSTER members=$POOL_MEMBER_IDS_CSV"
  else
    prepare_payload=$(jq -nc \
      --arg backend "${POOL_BACKENDS[0]}" \
      --arg pubkey "$POOL_PUBKEY" \
      --arg code "$POOL_CODE_ID" \
      --arg operation "$operation_id" \
      --argjson previous "$controller_snapshot" '
        {backend:$backend,expected_pubkey:$pubkey,expected_code_id:$code,
         operation_id:$operation,expected_previous:$previous}')
    shared_label="candidate=${POOL_TARGETS[0]} backend=${POOL_BACKENDS[0]}"
  fi

  journal=$(jq -nc \
    --arg operation "$operation_id" \
    --arg requested "$requested" \
    --arg stable "$stable" \
    --arg old_endpoint "$old_endpoint" \
    --arg old_code "$old_code" \
    --arg old_pub "$old_pub" \
    --arg old_updated "$old_updated" \
    --arg new_code "$POOL_CODE_ID" \
    --arg new_pub "$POOL_PUBKEY" \
    --arg cluster "$POOL_CLUSTER" \
    --argjson old_snapshot "$before" \
    --argjson controller_snapshot "$controller_snapshot" \
    --argjson targets "$targets_json" \
    --argjson backends "$pool_json" \
    --argjson members "$members_json" \
    --argjson prepare "$prepare_payload" \
    --argjson created "$(date +%s)" \
    --arg shared "$POOL_SHARED_HA" '
      {schema:2,operation_id:$operation,phase:"created",created_at:$created,
       requested:$requested,stable_endpoint:$stable,
       old_registry_snapshot:$old_snapshot,
       old_registry:{endpoint:$old_endpoint,code_id:$old_code,pubkey:$old_pub,updated_at:$old_updated},
       new_registry:{endpoint:$stable,code_id:$new_code,pubkey:$new_pub},
       controller_snapshot:$controller_snapshot,
       pool:{targets:$targets,backends:$backends,members:$members,pubkey:$new_pub,
             code_id:$new_code,cluster:$cluster,shared:($shared == "1")},
       prepare_payload:$prepare,registry_tx_history:[]}')
  _atomic_json_write "$TXN_STATE" "$journal"
  log "▶ preparing Indexer LB $NODE $shared_label pubkey=$POOL_PUBKEY"
  if prepare_out="$(_control_post /prepare "$prepare_payload" 2>&1)"; then
    prepare_rc=0
  else
    prepare_rc=$?
  fi
  if [ "$prepare_rc" -ne 0 ]; then
    if [ "$(_operation_status "$operation_id" 2>/dev/null || true)" = prepared ]; then
      if _control_post /abort "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" >/dev/null 2>&1; then
        _txn_clear
      fi
    fi
    die "LB prepare failed or response was uncertain (rc=$prepare_rc); journal retained unless exact abort succeeded: $prepare_out"
  fi
  [ "$(echo "$prepare_out" | jq -r '.operation_id // empty')" = "$operation_id" ] \
    || die "LB prepare returned the wrong operation_id; recover using $TXN_STATE"
  authoritative_previous=$(echo "$prepare_out" | jq -ceS '.prepared.previous') \
    || die "LB prepare response omitted authoritative previous state"
  if [ "$authoritative_previous" != "$controller_snapshot" ]; then
    _txn_update_phase controller-snapshot-mismatch
    if _control_post /abort "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" >/dev/null 2>&1; then
      _txn_clear
    fi
    die "controller active generation changed between snapshot and prepare; stale coordinator aborted"
  fi
  restore_payload="$(_restore_payload_from_previous "$operation_id" "$authoritative_previous")"
  _journal_authoritative_prepare "$authoritative_previous" "$restore_payload" "$prepare_out"
  _record_private_lb_response prepare "$prepare_out"

  before_intent="$(_registry_snapshot)" \
    || die "could not re-read registry before durable intent; prepared generation remains paused"
  if [ "$before_intent" != "$before" ]; then
    _txn_update_phase registry-snapshot-mismatch
    if _control_post /abort "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" >/dev/null 2>&1; then
      _txn_clear
    fi
    die "IndexerRegistry changed after snapshot; stale coordinator aborted before intent"
  fi
  if ! _control_post /intent "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" >/dev/null; then
    die "could not durably mark registry intent; LB remains paused and recovery journal is $TXN_STATE"
  fi
  _txn_update_phase controller-intent
  log "▶ signing forward IndexerRegistry transaction before publication"
  if tx_hash="$(_build_registry_tx forward "$stable" "$POOL_CODE_ID" "$POOL_PUBKEY")"; then
    log "✔ journaled signed forward registry tx=$tx_hash before broadcast"
  else
    value="$(_registry_snapshot 2>/dev/null || true)"
    if [ "$value" = "$before" ] && _restore_previous_and_clear; then
      die "registry transaction construction failed before publication; previous pool safely restored"
    fi
    die "registry transaction construction failed before publication; state changed or restore failed, journal retained"
  fi
  _publish_active_registry_tx
  recover_transaction
}

_finalize_recovered_transaction() {
  local current first_backend first_target
  current="$(_txn_read)"
  first_backend=$(jq -r '.pool.backends[0]' <<<"$current")
  first_target=$(jq -r '.pool.targets[0]' <<<"$current")
  ACTIVE_BACKEND="$first_backend"
  ACTIVE_BACKEND_NODE="$first_target"
  ACTIVE_BACKENDS=$(jq -r '.pool.backends | join(",")' <<<"$current")
  ACTIVE_BACKEND_NODES=$(jq -r '.pool.targets | join(",")' <<<"$current")
  ACTIVE_PUBKEY=$(jq -r '.pool.pubkey' <<<"$current")
  ACTIVE_CODE_ID=$(jq -r '.pool.code_id' <<<"$current")
  ACTIVE_INDEXER_CLUSTER=$(jq -r '.pool.cluster' <<<"$current")
  ACTIVE_MEMBER_IDS=$(jq -r '.pool.members | join(",")' <<<"$current")
  STABLE_ENDPOINT=$(jq -r '.stable_endpoint' <<<"$current")
  _save_lb
  verify_lb
  _txn_clear
  log "✔ recovered and finalized committed Indexer LB transaction"
}

recover_transaction() {
  local operation_id status registry finalized_registry kind finalized_kind phase purpose tx_state classification wait_rc
  local txn current_exact old_exact commit_out previous
  [ -e "$TXN_STATE" ] || [ -L "$TXN_STATE" ] \
    || die "no unfinished Indexer LB transaction journal: $TXN_STATE"
  txn="$(_txn_read)"
  _load_generic
  _default_cluster_env
  _ensure_secrets
  _discover_mesh_ip || die "LB mesh IP unavailable"
  _registry_owner_preflight
  [ "$(jq -r '.schema // 0' <<<"$txn")" = 2 ] \
    || die "unsupported or legacy transaction journal schema: $TXN_STATE"
  operation_id=$(jq -er '.operation_id' <<<"$txn") \
    || die "transaction journal has no operation_id: $TXN_STATE"
  phase=$(jq -r '.phase' <<<"$txn")
  previous=$(jq -ce '.controller_previous // .controller_snapshot' <<<"$txn") \
    || die "journal has no controller snapshot"
  old_exact=$(jq -ce '.old_registry_snapshot' <<<"$txn") \
    || die "journal has no exact prior registry snapshot"

  if ! jq -e '(.active_registry_tx | type) == "object"' <<<"$txn" >/dev/null; then
    status="$(_operation_status "$operation_id")" \
      || die "controller status unavailable; no registry transaction was signed and journal remains"
    current_exact="$(_registry_snapshot)" \
      || die "registry unavailable; no mutation performed"
    if [ "$status" = prepared ]; then
      _control_post /abort "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" >/dev/null \
        || die "controller abort failed; journal retained"
      _controller_matches_previous "$previous" \
        || die "controller did not restore authoritative previous generation"
      _txn_clear
      log "✔ recovered pre-intent prepare without publishing a registry transaction"
      return 0
    fi
    if [ "$status" = unknown ] && [ "$current_exact" = "$old_exact" ] \
      && _controller_matches_previous "$previous"; then
      _txn_clear
      log "✔ cleared a transaction that never published or changed controller state"
      return 0
    fi
    case "$status" in
      registry-intent|commit-started)
        [ "$current_exact" = "$old_exact" ] \
          || die "registry changed before any journaled tx; refusing stale recovery"
        _build_registry_tx forward \
          "$(jq -r '.new_registry.endpoint' <<<"$txn")" \
          "$(jq -r '.new_registry.code_id' <<<"$txn")" \
          "$(jq -r '.new_registry.pubkey' <<<"$txn")" >/dev/null \
          || die "forward recovery transaction construction failed before publication"
        _publish_active_registry_tx
        ;;
      *) die "controller state=$status has no journaled registry transaction; refusing mutation" ;;
    esac
  fi

  txn="$(_txn_read)"
  tx_state=$(jq -er '.active_registry_tx.state' <<<"$txn") \
    || die "journal has invalid active registry transaction"
  case "$tx_state" in
    signed-not-published|publish-attempting|publish-uncertain|publish-unseen)
      _publish_active_registry_tx
      ;;
  esac
  if classification="$(_wait_active_registry_tx)"; then
    wait_rc=0
  else
    wait_rc=$?
  fi
  if [ "$wait_rc" -ne 0 ]; then
    die "registry transaction remains $classification; exact signed tx can still mine, controller stays paused"
  fi
  txn="$(_txn_read)"
  purpose=$(jq -er '.active_registry_tx.purpose' <<<"$txn") \
    || die "journal registry transaction has no purpose"
  registry="$(_registry_snapshot)" \
    || die "registry unavailable after transaction resolution; no data-plane mutation performed"
  finalized_registry="$(_registry_snapshot finalized)" \
    || die "finalized registry snapshot unavailable after transaction resolution; controller stays paused"
  kind="$(_registry_tuple_kind "$registry")"
  finalized_kind="$(_registry_tuple_kind "$finalized_registry")"
  [ "$kind" = "$finalized_kind" ] \
    || die "latest registry tuple=$kind disagrees with finalized tuple=$finalized_kind; controller stays paused"
  kind="$finalized_kind"

  if [ "$purpose" = forward ]; then
    case "$classification:$kind" in
      success:new|success:both|replaced:new)
        status="$(_operation_status "$operation_id")" \
          || die "controller operation status unavailable after final forward tx"
        if [ "$status" = committed ]; then
          _txn_update_phase committed
          _finalize_recovered_transaction
          return 0
        fi
        if [ "$status" = unknown ]; then
          if _controller_matches_previous "$previous"; then
            _begin_registry_rollback
            return
          fi
          die "forward tx finalized but controller generation is unknown and not previous"
        fi
        if commit_out="$(_control_post /commit "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" 2>&1)"; then
          _record_private_lb_response commit "$commit_out"
          _txn_update_phase committed
          _finalize_recovered_transaction
          return 0
        fi
        status="$(_operation_status "$operation_id")" \
          || die "commit response was lost and serialized controller status is unavailable; journal retained"
        if [ "$status" = committed ]; then
          _txn_update_phase committed
          _finalize_recovered_transaction
          return 0
        fi
        case "$status" in
          registry-intent|commit-started)
            # /operation takes MUTATION_LOCK, so a successful response proves
            # the failed commit handler is no longer running. Only then may a
            # rollback race neither an in-flight commit nor a late reopen.
            log "forward registry tx finalized but quiescent controller commit failed; starting signed rollback"
            _begin_registry_rollback
            return
            ;;
          *)
            die "commit response was lost and quiescent controller state=$status is not rollback-safe; journal retained"
            ;;
        esac
        ;;
      revert:old|revert:both|replaced:old|replaced:both)
        _restore_previous_and_clear \
          || die "forward tx cannot mine but previous controller restore failed; journal retained"
        return 0
        ;;
      *) die "resolved forward tx=$classification but registry tuple=$kind; refusing mutation" ;;
    esac
  fi

  case "$purpose" in
    rollback|recovery-rollback-*) ;;
    *) die "unknown registry transaction purpose: $purpose" ;;
  esac
  case "$classification:$kind" in
    success:old|success:both|revert:old|revert:both|replaced:old|replaced:both)
      _restore_previous_and_clear \
        || die "rollback is resolved but previous controller restore failed; journal retained"
      return 0
      ;;
    revert:new|replaced:new)
      log "rollback did not publish the old tuple; signing a new recovery rollback"
      _begin_registry_rollback
      return
      ;;
    *) die "resolved rollback tx=$classification but registry tuple=$kind; refusing mutation" ;;
  esac
}

verify_sidecar_health() {
  _load_generic
  local bridge i body
  for i in $(seq 1 40); do
    bridge="$(_bridge_ip_for_vm "$VM_ID" 2>/dev/null || true)"
    if [ -z "$bridge" ]; then
      log "… waiting for Indexer LB bridge neighbor after VM start ($i/40)"
      sleep 10
      continue
    fi
    # A long-lived v1 cluster can contain permanently registered, dead test
    # members. A brand-new member may therefore never latch full convergence even
    # though its mesh route and CSK are ready. The authenticated control request in
    # `switch_backend` is the end-to-end mesh proof; here require its prerequisites
    # without treating an intentional HTTP 503 as an absent response.
    body=$(ssh_box "curl -sS --noproxy '*' --max-time 8 'http://$bridge:9091/healthz'" 2>/dev/null || true)
    if echo "$body" | jq -e '.csk_acquired == true and (.live_peers // 0) > 0' >/dev/null 2>&1; then
      log "✔ Indexer LB sidecar mesh-ready (health remains convergence+CSK): $body"
      return 0
    fi
    log "… Indexer LB sidecar mesh prerequisites unavailable ($i/40)"
    sleep 10
  done
  die "Indexer LB sidecar never acquired its CSK and a live mesh peer"
}

verify_lb() {
  _load_generic
  _load_lb
  _default_cluster_env
  _discover_mesh_ip || die "LB mesh IP unavailable"
  local control stable status registry_value endpoint registry_pub registry_code active_pub active_code active_cluster
  local backend_status expected_member unique_members i probe_dir rc healthy=0
  local -a active_backends=() active_members=() probe_pids=()
  control="$(_control_get /active)" || die "LB control API unavailable"
  echo "$control" | jq -e '.haproxy_socket == true' >/dev/null \
    || die "LB control response does not report HAProxy socket: $control"
  mapfile -t active_backends < <(echo "$control" | jq -r '.active_backends[]? // empty')
  if [ "${#active_backends[@]}" -eq 0 ]; then
    mapfile -t active_backends < <(echo "$control" | jq -r \
      '(.active_backend // "") | split(",")[] | select(length > 0)')
  fi
  mapfile -t active_members < <(echo "$control" | jq -r '.active_members[]? // empty' | tr 'A-F' 'a-f')
  active_pub=$(echo "$control" | jq -r '.active_pubkey // empty' | tr 'A-F' 'a-f')
  active_code=$(echo "$control" | jq -r '.active_code_id // empty' | tr 'A-F' 'a-f')
  active_cluster=$(echo "$control" | jq -r '.active_cluster // empty' | tr 'A-F' 'a-f')
  if [ -n "$active_pub" ]; then
    stable="$(_stable_endpoint)"
    status=$(curl -fsS --noproxy '*' --max-time 15 --max-filesize 65536 --location --max-redirs 0 \
      --proto '=https' "${stable/-50052./-9090.}/status") \
      || die "stable Indexer HTTP endpoint unavailable"
    [ "$(echo "$status" | jq -r '.pubKey // empty' | tr 'A-F' 'a-f')" = "${active_pub,,}" ] \
      || die "stable endpoint pubkey does not match LB active pool"
    registry_value="$(_registry_snapshot)" || die "could not read IndexerRegistry.current()"
    endpoint=$(echo "$registry_value" | jq -r '.[0]')
    registry_code=$(echo "$registry_value" | jq -r '.[1]' | tr 'A-F' 'a-f')
    registry_pub=$(echo "$registry_value" | jq -r '.[2]' | tr 'A-F' 'a-f')
    [ "$endpoint" = "$stable" ] || die "registry endpoint=$endpoint, expected stable LB endpoint=$stable"
    [ "$registry_pub" = "${active_pub,,}" ] || die "registry pubkey does not match LB active pool"
    if [ -n "$active_code" ]; then
      [ "$registry_code" = "$active_code" ] \
        || die "registry codeId does not match LB active pool"
    fi
  fi

  if [ "${#active_backends[@]}" -ge 2 ]; then
    echo "$active_pub" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "shared active pool has an invalid pubkey"
    echo "$active_code" | grep -Eq '^0x[0-9a-f]{64}$' \
      || die "shared active pool has an invalid codeId"
    [ "$active_code" != "$ZERO32" ] || die "shared active pool has a zero codeId"
    echo "$active_cluster" | grep -Eq '^0x[0-9a-f]{40}$' \
      || die "shared active pool has an invalid dedicated cluster"
    [ "$active_cluster" != "$ZERO_ADDRESS" ] || die "shared active pool has a zero cluster"
    [ "$active_cluster" != "${CLUSTER,,}" ] \
      || die "shared active pool is using the LB/C3 cluster instead of a dedicated Indexer cluster"
    [ "${#active_members[@]}" -eq "${#active_backends[@]}" ] \
      || die "shared active pool member IDs do not match its backend count"
    unique_members=$(printf '%s\n' "${active_members[@]}" | sort -u | wc -l | tr -d ' ')
    [ "$unique_members" -eq "${#active_members[@]}" ] \
      || die "shared active pool contains duplicate serving member IDs"

    probe_dir=$(mktemp -d "$LB_PRIVATE_STATE_DIR/indexer-lb-verify.XXXXXX") \
      || die "could not create bounded backend verification workspace"
    for i in "${!active_backends[@]}"; do
      (
        if backend_status="$(_backend_http "${active_backends[$i]}" /status)"; then
          printf '%s\n' "$backend_status" >"$probe_dir/$i.body"
          printf '0\n' >"$probe_dir/$i.rc"
        else
          printf '%s\n' "$?" >"$probe_dir/$i.rc"
        fi
      ) &
      probe_pids+=("$!")
    done
    for i in "${!probe_pids[@]}"; do
      wait "${probe_pids[$i]}" || true
    done
    for i in "${!active_backends[@]}"; do
      expected_member="${active_members[$i]}"
      rc=$(<"$probe_dir/$i.rc")
      if [ "$rc" = 75 ]; then
        log "… shared active backend ${active_backends[$i]} is unreachable; retaining it disabled until identity refresh"
        continue
      fi
      if [ "$rc" != 0 ]; then
        rm -rf "$probe_dir"
        die "reachable shared backend ${active_backends[$i]} returned an invalid, redirected, or oversized /status response"
      fi
      backend_status=$(<"$probe_dir/$i.body")
      echo "$backend_status" | jq -e \
        --arg pubkey "$active_pub" \
        --arg code "$active_code" \
        --arg cluster "$active_cluster" \
        --arg member "$expected_member" '
          .identityMode == "cluster-shared"
          and ((.pubKey // "") | ascii_downcase) == $pubkey
          and ((.codeId // "") | ascii_downcase) == $code
          and ((.indexerCluster // "") | ascii_downcase) == $cluster
          and ((.servingMemberId // "") | ascii_downcase) == $member
        ' >/dev/null \
        || {
          rm -rf "$probe_dir"
          die "reachable shared active backend ${active_backends[$i]} identity diverged: $backend_status"
        }
      if echo "$backend_status" | jq -e '
          .health.ok == true
          and .health.grpcAccepting == true
          and .health.rpcReachable == true
          and (.health.chainHeadLagBlocks | type == "number")
          and .health.chainHeadLagBlocks >= 0
          and .health.chainHeadLagBlocks < 10
          and (.readModel.atBlock | type == "number")
          and (.readModel.clusterCount | type == "number")
          and .readModel.clusterCount >= 1
        ' >/dev/null; then
        healthy=$((healthy + 1))
      else
        log "… identity-matching shared backend ${active_backends[$i]} is unhealthy; HAProxy keeps it out of service"
      fi
    done
    rm -rf "$probe_dir"
    [ "$healthy" -ge 1 ] \
      || die "shared active pool has no healthy identity-matching backend"
  fi
  echo "$control" | jq \
    '{active_backend,active_backends,active_pubkey,active_code_id,active_cluster,active_members,prepared,haproxy_socket}'
  log "✔ Indexer LB control=$MESH_IP:50053 grpc=$(_stable_endpoint)"
}

active() { _control_get /active | jq; }

drain_reservations() {
  local response
  response="$(_control_get /drain-reservations)" \
    || die "could not list drain reservations"
  echo "$response" | jq -e '
    type == "object"
    and (.reservations | type == "array")
    and all(.reservations[];
      (.backend | type == "string")
      and (.reserved_at_operation_id | type == "string" and test("^[0-9a-f]{64}$"))
      and (.reservation_id | type == "string" and test("^[0-9a-f]{64}$"))
      and (.release_token | type == "string" and test("^[0-9a-f]{64}$")))
  ' >/dev/null || die "LB returned an invalid drain reservation list"
  echo "$response" | jq
}

assert_drained() {
  local target="${1:-}" backend control operation_id pinned_operation payload proof
  local expected_state_hash="${INDEXER_EXPECTED_BACKEND_STATE_SHA256:-}" state_snapshot="" state_json=""
  [ -n "$target" ] \
    || die "usage: $0 $NODE assert-drained <indexer-worker-node-or-private-ip>"
  [[ "$target" != *,* ]] || die "assert-drained accepts exactly one worker"
  if [ -n "$expected_state_hash" ]; then
    [[ "$expected_state_hash" =~ ^[0-9a-f]{64}$ ]] \
      || die "INDEXER_EXPECTED_BACKEND_STATE_SHA256 must be 64 lowercase hex characters"
    [[ ! "$target" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] \
      || die "a pinned backend-state SHA256 requires a named worker, not a literal IP"
    state_snapshot="$(_snapshot_expected_backend_state "$target" "$expected_state_hash")" \
      || die "named worker state did not match INDEXER_EXPECTED_BACKEND_STATE_SHA256"
    _validate_backend_state_snapshot "$state_snapshot" "$expected_state_hash" \
      || die "verified named worker state snapshot changed before resolution"
    state_json="$(_backend_state_json "$target" "$state_snapshot")" \
      || die "verified named worker state snapshot is invalid"
  fi
  backend="$(_backend_ip "$target" 1 "$state_json")" \
    || die "could not resolve worker to assert drained: $target"
  [ -z "$state_snapshot" ] || rm -f "$state_snapshot"
  control="$(_control_get /active)" || die "could not read active LB generation"
  operation_id=$(echo "$control" | jq -er '.active_operation_id | select(type == "string" and test("^[0-9a-f]{64}$"))') \
    || die "LB has no tokenized active generation; perform a successful switch before stopping workers"
  pinned_operation="${INDEXER_LB_ACTIVE_OPERATION_ID:-}"
  if [ -n "$pinned_operation" ]; then
    pinned_operation="${pinned_operation,,}"
    [[ "$pinned_operation" =~ ^[0-9a-f]{64}$ ]] \
      || die "INDEXER_LB_ACTIVE_OPERATION_ID must be 32 bytes of lowercase hex"
    [ "$pinned_operation" = "$operation_id" ] \
      || die "active LB operation changed: expected $pinned_operation, got $operation_id"
  fi
  payload=$(jq -nc \
    --arg operation "$operation_id" \
    --arg backend "$backend" \
    '{operation_id:$operation,backend:$backend}')
  proof="$(_control_post /assert-drained "$payload")" \
    || die "LB did not return a drain proof for $backend"
  echo "$proof" | jq -e \
    --arg backend "$backend" \
    --arg operation "$operation_id" '
      type == "object"
      and .drained == true
      and .backend == $backend
      and .operation_id == $operation
      and (.reserved_at_operation_id | type == "string" and test("^[0-9a-f]{64}$"))
      and (.reservation_id | type == "string" and test("^[0-9a-f]{64}$"))
      and (.release_token | type == "string" and test("^[0-9a-f]{64}$"))
      and (.active_backends | type == "array")
      and ((.active_backends | index($backend)) == null)
      and (.idempotent | type == "boolean")
    ' >/dev/null \
    || die "LB returned a drain proof that is not bound to requested backend=$backend operation=$operation_id"
  echo "$proof" | jq
}

release_drain() {
  local target="${1:-}" reservation_id="${2:-}" release_token="${3:-}"
  local backend control operation_id payload response
  [ -n "$target" ] && [ -n "$reservation_id" ] && [ -n "$release_token" ] \
    || die "usage: $0 $NODE release-drain <worker-node-or-private-ip> <reservation-id> <release-token>"
  [[ "$target" != *,* ]] || die "release-drain accepts exactly one worker"
  reservation_id="${reservation_id,,}"
  release_token="${release_token,,}"
  [[ "$reservation_id" =~ ^[0-9a-f]{64}$ ]] \
    || die "reservation-id must be 32 bytes of lowercase hex"
  [[ "$release_token" =~ ^[0-9a-f]{64}$ ]] \
    || die "release-token must be 32 bytes of lowercase hex"
  backend="$(_backend_ip "$target" 1)" \
    || die "could not resolve worker drain reservation: $target"
  control="$(_control_get /active)" || die "could not read active LB generation"
  operation_id=$(echo "$control" | jq -er \
    '.active_operation_id | select(type == "string" and test("^[0-9a-f]{64}$"))') \
    || die "LB has no tokenized active generation"
  payload=$(jq -nc \
    --arg operation "$operation_id" \
    --arg backend "$backend" \
    --arg reservation "$reservation_id" \
    --arg token "$release_token" \
    '{operation_id:$operation,backend:$backend,
      reservation_id:$reservation,release_token:$token}')
  response="$(_control_post /release-drain "$payload")" \
    || die "LB did not confirm drain release for $backend"
  echo "$response" | jq -e \
    --arg backend "$backend" \
    --arg operation "$operation_id" \
    --arg reservation "$reservation_id" '
      type == "object"
      and .released == true
      and .backend == $backend
      and .operation_id == $operation
      and .reservation_id == $reservation
      and (.idempotent | type == "boolean")
    ' >/dev/null \
    || die "LB returned an invalid drain release confirmation"
  echo "$response" | jq
}

abort_prepare() {
  local operation_id="${1:-}" journal_operation="" txn=""
  if [ -e "$TXN_STATE" ] || [ -L "$TXN_STATE" ]; then
    txn="$(_txn_read)"
    journal_operation=$(jq -er '.operation_id | select(type == "string" and test("^[0-9a-f]{64}$"))' <<<"$txn") \
      || die "transaction journal contains an invalid operation_id: $TXN_STATE"
  fi
  if [ -z "$operation_id" ]; then
    operation_id="$journal_operation"
  elif [ -n "$journal_operation" ] && [ "${operation_id,,}" != "$journal_operation" ]; then
    die "refusing to abort operation $operation_id while local journal tracks $journal_operation"
  fi
  operation_id="${operation_id,,}"
  [ -n "$operation_id" ] \
    || die "abort requires an operation_id or local transaction journal"
  [[ "$operation_id" =~ ^[0-9a-f]{64}$ ]] \
    || die "abort operation_id must be 32 bytes of lowercase hex"
  _control_post /abort "$(jq -nc --arg operation "$operation_id" '{operation_id:$operation}')" | jq
  if [ -e "$TXN_STATE" ] || [ -L "$TXN_STATE" ]; then
    _txn_clear
  fi
}

_ensure_lb_state_dir
MIGRATION_LOCK_HELD=0
if _legacy_migration_needed; then
  _acquire_cutover_lock
  MIGRATION_LOCK_HELD=1
fi
_migrate_legacy_lb_state
case "$ACTION" in
  setup|preflight|deploy|prime|bind|start|register-direct|assert-drained|release-drain|abort|recover|switch|update|stop|all)
    [ "$MIGRATION_LOCK_HELD" = 1 ] || _acquire_cutover_lock
    ;;
esac

log "=== AttestMesh Indexer LB node: $NODE ==="
case "$ACTION" in
  setup) _ensure_secrets ;;
  preflight|deploy|prime|bind|start|register-direct|verify) generic "$ACTION" ;;
  verify-health) verify_sidecar_health ;;
  verify-lb) verify_lb ;;
  active) active ;;
  drain-reservations) drain_reservations ;;
  assert-drained) assert_drained "$TARGET" ;;
  release-drain) release_drain "$TARGET" "$ARG4" "$ARG5" ;;
  abort) abort_prepare "$TARGET" ;;
  recover) recover_transaction ;;
  switch) switch_backend "$TARGET" ;;
  update) generic update; verify_sidecar_health; verify_lb ;;
  stop) generic stop ;;
  all)
    _ensure_secrets
    generic all
    verify_sidecar_health
    _discover_mesh_ip || die "LB failed to acquire a mesh IP"
    switch_backend "${TARGET:-${INDEXER_LB_INITIAL_INDEXER:-attestmesh-indexer-c3-green}}"
    ;;
  *) die "usage: $0 <node-name> [setup|preflight|deploy|prime|bind|start|register-direct|verify|verify-health|verify-lb|active|drain-reservations|assert-drained|release-drain|abort|recover|switch|update|stop|all] [target-or-operation-id] [reservation-id] [release-token]" ;;
esac
