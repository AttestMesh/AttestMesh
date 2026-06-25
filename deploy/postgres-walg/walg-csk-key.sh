#!/bin/sh
# Derive the wal-g encryption key from the CLUSTER SHARED KEY and write it (hex) to the runtime key file.
# The CSK comes from the sidecar's app-facing gRPC (GetClusterSharedKey) over the agent UDS. The CSK is
# app_id-bound + on-chain-committed, so a fresh CVM of the same app_id re-derives the SAME key and can
# decrypt the backups (recovery from total CVM loss). Writes once then exits. Progress/errors go to the
# status file (served by nginx at /_backup/status) since the TEE blocks container logs.
set -u
SOCK="${AGENT_GRPC_SOCKET:-/var/run/attestmesh/agent.sock}"
SVC="attestmesh.agent.v1.Agent"
KEY_FILE="${WALG_KEY_FILE:-/run/walg/key}"
# grpcurl over the UDS: use the unix:// SCHEME (the -unix flag dials TCP in grpcurl 1.9.x) and an
# IMPORT-PATH (grpcurl rejects an absolute -proto path: "must specify at least one import path").
GRPC="grpcurl -plaintext -import-path /etc/walg -proto agent.proto"
ADDR="unix://$SOCK"
STAT="${WALG_STATUS_FILE:-/walg-status/state}"; mkdir -p "$(dirname "$STAT")" "$(dirname "$KEY_FILE")" 2>/dev/null || true
_st() { echo "$(date -u +%FT%TZ) csk: $*" >> "$STAT" 2>/dev/null || true; }

if [ -s "$KEY_FILE" ]; then _st "key already present"; exit 0; fi
_st "waiting for sidecar to acquire the CSK (sock=$SOCK)"
i=0
while :; do
  resp="$($GRPC -d '{}' "$ADDR" "$SVC/GetMeshStatus" 2>&1)"
  acq="$(printf '%s' "$resp" | python3 -c '
import sys,json
try:
    d=json.load(sys.stdin); print("yes" if d.get("cskAcquired") or d.get("csk_acquired") else "no")
except Exception:
    print("no")' 2>/dev/null || echo no)"
  [ "$acq" = "yes" ] && break
  i=$((i + 1))
  [ $((i % 3)) -eq 0 ] && _st "waiting ($i); GetMeshStatus -> $(printf '%s' "$resp" | tr '\n' ' ' | head -c 220)"
  sleep 10
done
_st "CSK acquired; fetching"
csk="$($GRPC -d '{}' "$ADDR" "$SVC/GetClusterSharedKey" 2>&1 \
      | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["key"])
except Exception as e: print("")' 2>/dev/null)"
if [ -z "$csk" ]; then _st "ERROR: GetClusterSharedKey returned no key"; exit 1; fi
python3 - "$csk" "$KEY_FILE" <<'PY'
import sys, base64, hashlib, hmac
csk = base64.b64decode(sys.argv[1])   # CSK via argv (NOT stdin — the heredoc owns stdin here)
assert len(csk) == 32, f"CSK length {len(csk)} != 32"
prk = hmac.new(b"\x00" * 32, csk, hashlib.sha256).digest()
okm = hmac.new(prk, b"attestmesh.matrix.walg.v1\x01", hashlib.sha256).digest()
open(sys.argv[2], "w").write(okm.hex())
PY
chmod 600 "$KEY_FILE" 2>/dev/null || true
[ -s "$KEY_FILE" ] && _st "KEY WRITTEN ($KEY_FILE)" || _st "ERROR: key file empty after HKDF"
