#!/usr/bin/env bash
# Shared helpers for the pg-ha entrypoints (etcd / patroni / haproxy). Sourced, not executed.
# The TEE blocks container logs, so everything self-reports to the shared status volume.
# Peer identity comes from PGHA_PEERS ("pg1=10.18.a.b,pg2=…"), precomputed by the driver from
# on-chain state (docs/specs/pg-ha.md §3); the mesh IP guard below hard-fails on drift.

STAT="${PGHA_STATUS_FILE:-/pgha-status/state}"
mkdir -p "$(dirname "$STAT")" 2>/dev/null || true
_st() { echo "$(date -u +%FT%TZ) ${PGHA_LOG_TAG:-pgha}: $*" >> "$STAT" 2>/dev/null || true; }
_die() { _st "FATAL: $*"; echo "pgha: FATAL: $*" >&2; exit 1; }

# Block until the sidecar's wireguard interface exists, then print its IPv4.
wait_for_mesh_ip() {
  local ip i=0
  while :; do
    ip="$(ip -4 -o addr show dev attestmesh0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
    [ -n "$ip" ] && { printf '%s\n' "$ip"; return 0; }
    i=$((i + 1)); [ $((i % 30)) -eq 0 ] && _st "still waiting for attestmesh0 (${i}s)"
    sleep 1
  done
}

# PGHA_PEERS accessors. peer_names → one name per line (order preserved); peer_ip <name> → its IP.
peer_names() { printf '%s\n' "${PGHA_PEERS:?PGHA_PEERS not set}" | tr ',' '\n' | cut -d= -f1; }
peer_ip() {
  printf '%s\n' "${PGHA_PEERS:?}" | tr ',' '\n' | awk -F= -v n="$1" '$1 == n {print $2; exit}'
}

# The driver precomputed my mesh IP from chain math; the interface is ground truth. Any
# mismatch means the off-chain math drifted — stop before writing quorum or database state.
assert_self_ip() {
  local my_ip="$1" want
  want="$(peer_ip "${PGHA_NODE_NAME:?PGHA_NODE_NAME not set}")"
  [ -n "$want" ] || _die "PGHA_NODE_NAME=$PGHA_NODE_NAME not present in PGHA_PEERS=$PGHA_PEERS"
  [ "$my_ip" = "$want" ] || _die "mesh IP mismatch: attestmesh0=$my_ip but PGHA_PEERS says $want — precompute drift"
}

# HKDF a named secret from the cluster shared key (same recipe as walg-csk-key.sh: HKDF-Extract
# with a zero salt, HKDF-Expand with the versioned label). Prints 32 bytes hex on stdout.
# Blocks until the sidecar has acquired the CSK.
GRPC="grpcurl -plaintext -import-path /etc/walg -proto agent.proto"
AGENT_ADDR="unix://${AGENT_GRPC_SOCKET:-/var/run/attestmesh/agent.sock}"
AGENT_SVC="attestmesh.agent.v1.Agent"

csk_derive() {
  local label="$1" i=0 resp acq csk
  while :; do
    resp="$($GRPC -d '{}' "$AGENT_ADDR" "$AGENT_SVC/GetMeshStatus" 2>&1)"
    acq="$(printf '%s' "$resp" | jq -r 'if .cskAcquired or .csk_acquired then "yes" else "no" end' 2>/dev/null || echo no)"
    [ "$acq" = "yes" ] && break
    i=$((i + 1)); [ $((i % 6)) -eq 0 ] && _st "waiting for CSK ($((i * 5))s); GetMeshStatus -> $(printf '%s' "$resp" | tr '\n' ' ' | head -c 200)"
    sleep 5
  done
  csk="$($GRPC -d '{}' "$AGENT_ADDR" "$AGENT_SVC/GetClusterSharedKey" 2>/dev/null | jq -r .key 2>/dev/null)"
  [ -n "$csk" ] && [ "$csk" != null ] || _die "GetClusterSharedKey returned no key"
  python3 - "$csk" "$label" <<'PY'
import sys, base64, hashlib, hmac
csk = base64.b64decode(sys.argv[1])
assert len(csk) == 32, f"CSK length {len(csk)} != 32"
prk = hmac.new(b"\x00" * 32, csk, hashlib.sha256).digest()
okm = hmac.new(prk, sys.argv[2].encode() + b"\x01", hashlib.sha256).digest()
print(okm.hex())
PY
}
