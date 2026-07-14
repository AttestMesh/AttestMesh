#!/usr/bin/env bash
# etcd member entrypoint (Patroni DCS). Runs in the SIDECAR netns so it can bind the mesh IP and
# reach peers over attestmesh0. Bootstrap state machine (docs/specs/pg-ha.md §3.1):
#   data dir present            -> plain restart (etcd ignores initial-* after init)
#   empty + PGHA_BOOTSTRAP=new  -> static bootstrap from the precomputed PGHA_PEERS
#   empty + PGHA_BOOTSTRAP=join -> remove any stale member holding my peer URL, member add, join
# Client API is password-gated (root password HKDF-derived from the CSK); peer traffic rides the
# attested wireguard mesh unauthenticated (accepted v1 deviation, spec §8).
set -u
PGHA_LOG_TAG=etcd
source /usr/local/bin/pgha-common.sh

NODE="${PGHA_NODE_NAME:?PGHA_NODE_NAME not set}"
DATA="${ETCD_DATA_DIR:-/var/lib/etcd}"
mkdir -p "$DATA"

_st "boot: waiting for attestmesh0"
MY_IP="$(wait_for_mesh_ip)"
assert_self_ip "$MY_IP"
_st "mesh ip $MY_IP confirmed for $NODE; deriving etcd root credential"
ROOT_PW="$(csk_derive attestmesh.pgha.etcd.v1)" || exit 1

# etcdctl wrapper: try without creds first (pre-auth cluster), fall back to root creds.
_ectl() {
  local out
  out="$(etcdctl "$@" 2>&1)" && { printf '%s\n' "$out"; return 0; }
  if printf '%s' "$out" | grep -qiE 'user name is empty|permission denied|invalid auth'; then
    etcdctl --user "root:$ROOT_PW" "$@" 2>&1
    return $?
  fi
  printf '%s\n' "$out" >&2
  return 1
}

COMMON_ARGS=(
  --name "$NODE"
  --data-dir "$DATA"
  --listen-peer-urls "http://$MY_IP:2380"
  --initial-advertise-peer-urls "http://$MY_IP:2380"
  --listen-client-urls "http://$MY_IP:2379,http://127.0.0.1:2379"
  --advertise-client-urls "http://$MY_IP:2379"
)

if [ -d "$DATA/member" ]; then
  _st "existing data dir -> plain restart"
  etcd "${COMMON_ARGS[@]}" &
elif [ "${PGHA_BOOTSTRAP:-new}" = "new" ]; then
  initial=""
  for n in $(peer_names); do
    initial="${initial:+$initial,}$n=http://$(peer_ip "$n"):2380"
  done
  _st "empty data dir + bootstrap=new -> static bootstrap: $initial"
  etcd "${COMMON_ARGS[@]}" \
    --initial-cluster "$initial" \
    --initial-cluster-state new \
    --initial-cluster-token attestmesh-pg-ha &
else
  # Fresh disk on an established cluster (re-provision or scale-out). Same app_id ⇒ same mesh
  # IP, so a stale member may still hold my peer URL — remove it, then add myself.
  _st "empty data dir + bootstrap=join -> runtime member add via peers"
  ep=""
  while [ -z "$ep" ]; do
    for n in $(peer_names); do
      [ "$n" = "$NODE" ] && continue
      ip="$(peer_ip "$n")"
      if curl -fsS --max-time 3 "http://$ip:2379/health" 2>/dev/null | grep -q '"true"'; then
        ep="http://$ip:2379"; break
      fi
    done
    [ -z "$ep" ] && { _st "no healthy peer client endpoint yet; retrying"; sleep 5; }
  done
  _st "using peer endpoint $ep"
  # Parse the SIMPLE (CSV) output, not JSON: etcd member IDs are uint64 and jq (IEEE-754
  # double) mangles IDs > 2^53, while `printf '%x'` overflows bash's signed 64-bit ints for
  # IDs >= 2^63. Simple format already gives the hex ID in field 1, and member remove takes
  # that hex verbatim. Fields: <hexID>, <status>, <name>, <peerURLs>, <clientURLs>, <isLearner>.
  stale_hex="$(_ectl --endpoints="$ep" member list -w simple 2>/dev/null \
    | awk -F', *' -v url="http://$MY_IP:2380" '$4 == url {print $1; exit}')"
  if [ -n "$stale_hex" ]; then
    _st "removing stale member $stale_hex (held my peer URL)"
    _ectl --endpoints="$ep" member remove "$stale_hex" || _die "stale member remove failed"
  fi
  _ectl --endpoints="$ep" member add "$NODE" --peer-urls="http://$MY_IP:2380" >/dev/null \
    || _die "member add failed"
  # Initial cluster = every started member (name=peerURL, from simple output) + self.
  initial=""
  while IFS=',' read -r _id _status _name _peer _rest; do
    _name="$(printf '%s' "$_name" | tr -d ' ')"; _peer="$(printf '%s' "$_peer" | tr -d ' ')"
    [ -n "$_name" ] && [ "$_name" != "$NODE" ] && initial="${initial:+$initial,}$_name=$_peer"
  done < <(_ectl --endpoints="$ep" member list -w simple 2>/dev/null)
  initial="${initial:+$initial,}$NODE=http://$MY_IP:2380"
  _st "joining with initial-cluster=$initial"
  etcd "${COMMON_ARGS[@]}" \
    --initial-cluster "$initial" \
    --initial-cluster-state existing &
fi
ETCD_PID=$!

# Converge client auth in the BACKGROUND: keep retrying until `auth status` reports enabled.
# Quorum can take longer than any fixed window to form (peers register on-chain at their own
# pace), and auth enable needs a quorum — a one-shot attempt could silently leave the client
# API unauthenticated forever. This loop is idempotent and safe to run on every node (the
# first to reach quorum enables it; the rest observe "already enabled" and stop).
(
  while :; do
    if _ectl --endpoints=http://127.0.0.1:2379 auth status 2>/dev/null | grep -qi 'Authentication Status: true'; then
      _st "client auth enabled"; break
    fi
    if curl -fsS --max-time 2 http://127.0.0.1:2379/health 2>/dev/null | grep -q '"true"'; then
      _ectl --endpoints=http://127.0.0.1:2379 user add "root:$ROOT_PW" >/dev/null 2>&1 || true
      _ectl --endpoints=http://127.0.0.1:2379 user grant-role root root >/dev/null 2>&1 || true
      _ectl --endpoints=http://127.0.0.1:2379 auth enable >/dev/null 2>&1 \
        && { _st "client auth enabled"; break; }
    fi
    sleep 10
  done
) &

wait "$ETCD_PID"
rc=$?
_st "etcd exited rc=$rc"
exit $rc
