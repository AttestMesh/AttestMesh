#!/bin/sh
# Encrypting S3 gateway entrypoint: derive the crypt key from the CLUSTER SHARED KEY, then serve.
#
#   1. Wait for the sidecar to acquire the CSK (GetMeshStatus over the agent UDS).
#   2. Fetch the CSK (GetClusterSharedKey) and HKDF-SHA256 two outputs in-process:
#        password  = HKDF(CSK, "attestmesh.r2host.crypt.v1")
#        password2 = HKDF(CSK, "attestmesh.r2host.crypt.salt.v1")
#      The CSK is app_id-bound + on-chain-committed, so a fresh CVM of the same app_id derives the
#      SAME crypt key and can read every object in R2 (recovery from total CVM loss).
#   3. exec rclone serve s3 on a crypt: remote layered over the R2 bucket. Key material lives only
#      in this process env (rclone-obscured); no rclone.conf is ever written, nothing keyed touches
#      disk or the status file.
#
# Progress/errors go to the status file (the TEE blocks container logs) — walg-csk-key.sh precedent.
set -u

SOCK="${AGENT_GRPC_SOCKET:-/var/run/attestmesh/agent.sock}"
SVC="attestmesh.agent.v1.Agent"
# grpcurl over the UDS: use the unix:// SCHEME (the -unix flag dials TCP in grpcurl 1.9.x) and an
# IMPORT-PATH (grpcurl rejects an absolute -proto path: "must specify at least one import path").
GRPC="grpcurl -plaintext -import-path /etc/r2host -proto agent.proto"
ADDR="unix://$SOCK"
STAT="${S3GW_STATUS_FILE:-/s3gw-status/state}"; mkdir -p "$(dirname "$STAT")" 2>/dev/null || true
_st() { echo "$(date -u +%FT%TZ) s3gw: $*" >> "$STAT" 2>/dev/null || true; }
# Observable failure, no crash-loop storm: report, linger, then let restart policy retry.
_die() { _st "ERROR: $*"; sleep 30; exit 1; }

for v in R2_ENDPOINT R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY S3GW_ACCESS_KEY_ID S3GW_SECRET_ACCESS_KEY; do
  eval "val=\${$v:-}"
  [ -n "$val" ] || _die "missing required env $v"
done

_st "boot: waiting for sidecar CSK (sock=$SOCK)"
i=0
while :; do
  resp="$($GRPC -d '{}' "$ADDR" "$SVC/GetMeshStatus" 2>&1)"
  acq="$(printf '%s' "$resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin); print("yes" if d.get("cskAcquired") or d.get("csk_acquired") else "no")
except Exception:
    print("no")' 2>/dev/null || echo no)"
  [ "$acq" = "yes" ] && break
  i=$((i + 1))
  [ $((i % 6)) -eq 0 ] && _st "waiting for CSK ($i); GetMeshStatus -> $(printf '%s' "$resp" | tr '\n' ' ' | head -c 200)"
  sleep 10
done

_st "CSK acquired; deriving crypt key"
csk="$($GRPC -d '{}' "$ADDR" "$SVC/GetClusterSharedKey" 2>&1 \
      | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["key"])
except Exception: print("")' 2>/dev/null)"
[ -n "$csk" ] || _die "GetClusterSharedKey returned no key"

# CSK via argv (NOT stdin — the heredoc owns stdin here). Line 1 = password, line 2 = password2.
keys="$(python3 - "$csk" <<'PY'
import sys, base64, hashlib, hmac
csk = base64.b64decode(sys.argv[1])
assert len(csk) == 32, f"CSK length {len(csk)} != 32"
prk = hmac.new(b"\x00" * 32, csk, hashlib.sha256).digest()
print(hmac.new(prk, b"attestmesh.r2host.crypt.v1\x01",      hashlib.sha256).hexdigest())
print(hmac.new(prk, b"attestmesh.r2host.crypt.salt.v1\x01", hashlib.sha256).hexdigest())
PY
)" || _die "HKDF derivation failed"
pw_hex="$(printf '%s\n' "$keys" | sed -n 1p)"
salt_hex="$(printf '%s\n' "$keys" | sed -n 2p)"
unset csk keys
[ -n "$pw_hex" ] && [ -n "$salt_hex" ] || _die "derived key material empty"

# Remote config entirely via env vars — no rclone.conf on disk, ever.
export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="$R2_ENDPOINT"
export RCLONE_CONFIG_R2_REGION="${R2_REGION:-auto}"
export RCLONE_CONFIG_R2_FORCE_PATH_STYLE=true
# Bucket pre-exists; a bucket-scoped R2 token cannot CreateBucket, so never try.
export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
export RCLONE_CONFIG_CRYPT_TYPE=crypt
export RCLONE_CONFIG_CRYPT_REMOTE="r2:${R2_BUCKET}"
export RCLONE_CONFIG_CRYPT_FILENAME_ENCRYPTION=standard
export RCLONE_CONFIG_CRYPT_PASSWORD="$(rclone obscure "$pw_hex")"
export RCLONE_CONFIG_CRYPT_PASSWORD2="$(rclone obscure "$salt_hex")"
unset pw_hex salt_hex

# rclone serve-s3 maps S3 buckets to directories at the served remote root.
# WAL-G correctly assumes its bucket already exists, so create the encrypted
# directory before accepting requests. This is idempotent and the name is
# encrypted by crypt before it reaches upstream R2.
if [ -n "${S3GW_DEFAULT_BUCKET:-}" ]; then
  _st "ensuring encrypted S3 bucket ${S3GW_DEFAULT_BUCKET}"
  # S3 has no durable empty directories: mkdir alone is a no-op. A harmless
  # encrypted marker makes the top-level directory discoverable as a bucket by
  # rclone serve-s3 on every subsequent restart.
  printf 'attestmesh encrypted bucket\n' \
    | rclone rcat "crypt:${S3GW_DEFAULT_BUCKET}/.attestmesh-bucket" \
    || _die "could not create encrypted S3 bucket ${S3GW_DEFAULT_BUCKET}"
fi

_st "ready: serving S3 on :${S3GW_LISTEN_PORT:-19000} -> crypt over r2:${R2_BUCKET}"
# 0.0.0.0 is safe: the port is never compose-published; the only route in is the
# mesh-IP-bound socat proxy in the sidecar netns. NOTICE-level log to the status
# volume for post-mortems (TEE blocks stdout).
exec rclone serve s3 crypt: \
  --auth-key "${S3GW_ACCESS_KEY_ID},${S3GW_SECRET_ACCESS_KEY}" \
  --addr "0.0.0.0:${S3GW_LISTEN_PORT:-19000}" \
  --vfs-cache-mode "${S3GW_VFS_CACHE_MODE:-writes}" \
  --vfs-cache-max-size "${S3GW_VFS_CACHE_MAX_SIZE:-40G}" \
  --cache-dir /vfs-cache \
  --log-level NOTICE \
  --log-file "${S3GW_RCLONE_LOG:-/s3gw-status/rclone.log}"
