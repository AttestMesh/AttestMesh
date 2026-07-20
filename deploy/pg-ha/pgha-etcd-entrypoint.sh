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

# Recovery for a member that was explicitly removed from quorum while its CVM disk survived.
# This deletes only the node-local etcd state; PostgreSQL data is on a separate volume.
if [ "${PGHA_ETCD_FORCE_REJOIN:-false}" = true ] && [ -d "$DATA/member" ]; then
  _st "forced etcd rejoin requested; clearing retired local etcd member state"
  rm -rf "$DATA/member"
fi

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

if [ -d "$DATA/member" ] && { [ "${PGHA_ETCD_FORCE_NEW_CLUSTER:-false}" = true ] || [ "${PGHA_RECOVERY_CANDIDATE:-}" = "$NODE" ] || [ "${PGHA_IMAGE_FORCE_NEW_NODE:-}" = "$NODE" ]; }; then
  _st "disaster recovery requested; preserving DCS data and forcing a one-member cluster"
  etcd "${COMMON_ARGS[@]}" --force-new-cluster >>"$STAT" 2>&1 &
elif [ -d "$DATA/member" ]; then
  _st "existing data dir -> plain restart"
  etcd "${COMMON_ARGS[@]}" >>"$STAT" 2>&1 &
elif [ "${PGHA_BOOTSTRAP:-new}" = "new" ]; then
  initial=""
  for n in $(peer_names); do
    initial="${initial:+$initial,}$n=http://$(peer_ip "$n"):2380"
  done
  _st "empty data dir + bootstrap=new -> static bootstrap: $initial"
  etcd "${COMMON_ARGS[@]}" \
    --initial-cluster "$initial" \
    --initial-cluster-state new \
    --initial-cluster-token attestmesh-pg-ha >>"$STAT" 2>&1 &
else
  # Fresh disk on an established cluster (re-provision or scale-out). Same app_id ⇒ same mesh
  # IP, so a stale member may still hold my peer URL — remove it, then add myself.
  _st "empty data dir + bootstrap=join -> runtime member add via peers"
  # A replacement must never fall through to an arbitrary healthy peer. During a
  # partition, a retired node can still answer /health from a different etcd
  # cluster and would then admit this node into the stale partition. The first
  # non-self entry is the operator-selected authoritative seed; retry it until it
  # is healthy. Peer-map ordering is therefore a recovery safety boundary.
  ep="" seed_ip=""
  for n in $(peer_names); do
    [ "$n" = "$NODE" ] && continue
    seed_ip="$(peer_ip "$n")"
    break
  done
  [ -n "$seed_ip" ] || _die "join requires at least one authoritative non-self peer"
  while [ -z "$ep" ]; do
    if curl -fsS --max-time 3 "http://$seed_ip:2379/health" 2>/dev/null | grep -q '"true"'; then
      ep="http://$seed_ip:2379"
    else
      _st "authoritative seed $seed_ip is not healthy yet; refusing stale-peer fallback"
      sleep 5
    fi
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
  # Snapshot membership BEFORE adding the voter. Adding a voter to a one-member cluster
  # immediately requires two votes, so any member-list read after the add deadlocks on the
  # quorum that this process is responsible for restoring.
  pre_add_members="$(_ectl --endpoints="$ep" member list -w simple 2>/dev/null)" \
    || _die "pre-add member snapshot failed"
  _ectl --endpoints="$ep" member add "$NODE" --peer-urls="http://$MY_IP:2380" >/dev/null \
    || _die "member add failed"
  # Initial cluster = every pre-existing member (name=peerURL) + self.
  initial=""
  while IFS=',' read -r _id _status _name _peer _rest; do
    _name="$(printf '%s' "$_name" | tr -d ' ')"; _peer="$(printf '%s' "$_peer" | tr -d ' ')"
    [ -n "$_name" ] && [ "$_name" != "$NODE" ] && initial="${initial:+$initial,}$_name=$_peer"
  done <<<"$pre_add_members"
  initial="${initial:+$initial,}$NODE=http://$MY_IP:2380"
  _st "joining with initial-cluster=$initial"
  etcd "${COMMON_ARGS[@]}" \
    --initial-cluster "$initial" \
    --initial-cluster-state existing >>"$STAT" 2>&1 &
fi
ETCD_PID=$!

# Reconcile retired voters after the local member is healthy. Initial-cluster is ignored when a
# data directory already exists, so a scale-in expressed by a smaller sealed PGHA_PEERS map would
# otherwise leave a permanently dead etcd voter behind. Removal is idempotent across members and
# deliberately never removes this node.
(
  while ! curl -fsS --max-time 2 http://127.0.0.1:2379/health 2>/dev/null | grep -q '"true"'; do
    sleep 5
  done
  expected=" $(peer_names | tr '\n' ' ') "
  while IFS=',' read -r member_id _status member_name _peer _rest; do
    member_name="$(printf '%s' "$member_name" | tr -d ' ')"
    [ -n "$member_name" ] || continue
    [ "$member_name" = "$NODE" ] && continue
    case "$expected" in *" $member_name "*) continue;; esac
    _st "removing retired member $member_name ($member_id); absent from sealed peer map"
    _ectl --endpoints=http://127.0.0.1:2379 member remove "$member_id" >/dev/null 2>&1 || true
  done < <(_ectl --endpoints=http://127.0.0.1:2379 member list -w simple 2>/dev/null || true)
) &

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
