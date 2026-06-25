#!/bin/sh
# Derive the wal-g encryption key from the CLUSTER SHARED KEY and write it (hex) to the runtime key file.
# The CSK comes from the sidecar's app-facing gRPC (GetClusterSharedKey) over the agent UDS — the same
# socket the matrix-admin-agent uses. The CSK is app_id-bound + on-chain-committed, so a fresh CVM of the
# same app_id re-derives the SAME key and can decrypt the backups (disaster recovery from total CVM loss).
# Writes once then exits; the archive_command and base loop just read the file.
set -u
SOCK="${AGENT_GRPC_SOCKET:-/var/run/attestmesh/agent.sock}"
PROTO=/etc/walg/agent.proto
SVC="attestmesh.agent.v1.Agent"
KEY_FILE="${WALG_KEY_FILE:-/run/walg/key}"
mkdir -p "$(dirname "$KEY_FILE")"
if [ -s "$KEY_FILE" ]; then echo "walg-csk-key: key already present"; exit 0; fi

echo "walg-csk-key: waiting for the sidecar to acquire the CSK…"
i=0
while :; do
  acq="$(grpcurl -plaintext -unix -proto "$PROTO" -d '{}' "$SOCK" "$SVC/GetMeshStatus" 2>/dev/null \
        | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print("yes" if d.get("cskAcquired") or d.get("csk_acquired") else "no")
except Exception:
    print("no")' 2>/dev/null || echo no)"
  [ "$acq" = "yes" ] && break
  i=$((i + 1)); [ $((i % 6)) -eq 0 ] && echo "walg-csk-key: still waiting for CSK ($i)"
  sleep 10
done

csk="$(grpcurl -plaintext -unix -proto "$PROTO" -d '{}' "$SOCK" "$SVC/GetClusterSharedKey" 2>/dev/null \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')"
python3 - "$csk" "$KEY_FILE" <<'PY'
import sys, base64, hashlib, hmac
csk = base64.b64decode(sys.argv[1])
assert len(csk) == 32, f"CSK length {len(csk)} != 32"
# HKDF-SHA256 (RFC 5869): one 32-byte block, salt = zeros, distinct info tag → a backup subkey (never
# reuse the raw CSK across purposes). wal-g consumes this as WALG_LIBSODIUM_KEY (hex transform).
prk = hmac.new(b"\x00" * 32, csk, hashlib.sha256).digest()
okm = hmac.new(prk, b"attestmesh.matrix.walg.v1\x01", hashlib.sha256).digest()
with open(sys.argv[2], "w") as f:
    f.write(okm.hex())
PY
chmod 600 "$KEY_FILE"
echo "walg-csk-key: WALG_LIBSODIUM_KEY written to $KEY_FILE"
