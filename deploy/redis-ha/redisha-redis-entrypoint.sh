#!/usr/bin/env bash
# redis-server entrypoint. Runs in the SIDECAR netns; Redis listens on 6380 (HAProxy owns the
# mesh IP's 6379/6381 in the same netns). Auth (requirepass + masterauth) is HKDF-derived from
# the CSK — every node computes the identical value with no secret exchange (pg-ha pattern).
# Bootstrap state machine (mirrors pgha-etcd-entrypoint.sh):
#   data present                            -> plain restart, no forced role; the sentinels
#                                              reconcile a stale ex-master back to replica
#   empty + REDISHA_BOOTSTRAP=new + first   -> start as master
#   empty otherwise                         -> REPLICAOF <first-peer-ip> 6380 (sentinels may
#                                              re-point it if the master has since moved)
set -u
REDISHA_LOG_TAG=redis
source /usr/local/bin/redisha-common.sh

NODE="${REDISHA_NODE_NAME:?REDISHA_NODE_NAME not set}"
DATA="${REDIS_DATA_DIR:-/data}"
mkdir -p "$DATA"
chown redis:redis "$DATA" 2>/dev/null || true

_st "boot: waiting for attestmesh0"
MY_IP="$(wait_for_mesh_ip)"
assert_self_ip "$MY_IP"
_st "mesh ip $MY_IP confirmed for $NODE; deriving redis auth credential"
AUTH_PW="$(csk_derive attestmesh.redisha.auth.v1)" || exit 1
# Driver-side verification credential (sealed env, NOT the CSK): the mesh shell runs
# verify-ha/verify-failover as the `meshverify` ACL user, restricted to the verify:*
# key namespace + INFO/PING (deploy/redis-ha-node.sh).
VERIFY_PW="${REDISHA_VERIFY_PASSWORD:?REDISHA_VERIFY_PASSWORD not set}"

FIRST="$(peer_names | head -n1)"
FIRST_IP="$(peer_ip "$FIRST")"

CFG=/run/redisha-redis.conf
umask 077
cat > "$CFG" <<CONF
port 6380
bind 0.0.0.0
protected-mode no
requirepass $AUTH_PW
masterauth $AUTH_PW
user meshverify on >$VERIFY_PW ~verify:* +ping +info +get +set +del +client|setinfo
appendonly yes
dir $DATA
replica-announce-ip $MY_IP
replica-announce-port 6380
CONF

# ls -A: appendonlydir/ (AOF) or dump.rdb — either means this node has prior state.
# A replica target must never be this node itself: with bootstrap=join and an empty
# disk, the first peer would otherwise REPLICAOF itself and the cluster has no master
# (hit live 2026-07-03). Empty + no other live master -> the first peer becomes master
# regardless of bootstrap mode; a live master found on another peer wins.
find_live_master() {
  local n ip
  for n in $(peer_names); do
    [ "$n" = "$NODE" ] && continue
    ip="$(peer_ip "$n")"
    if timeout 3 redis-cli -h "$ip" -p 6380 -a "$AUTH_PW" --no-auth-warning info replication 2>/dev/null \
        | grep -q '^role:master'; then
      echo "$ip"; return 0
    fi
  done
  return 1
}
if [ -n "$(ls -A "$DATA" 2>/dev/null)" ]; then
  _st "existing data in $DATA -> plain restart (role reconciled by sentinel)"
elif MASTER_IP="$(find_live_master)"; then
  _st "empty data -> bootstrapping as replica of live master $MASTER_IP:6380"
  echo "replicaof $MASTER_IP 6380" >> "$CFG"
elif [ "$NODE" = "$FIRST" ]; then
  _st "empty data + no live master + first peer -> starting as master"
else
  _st "empty data -> bootstrapping as replica of $FIRST ($FIRST_IP:6380)"
  echo "replicaof $FIRST_IP 6380" >> "$CFG"
fi

_st "starting redis-server on :6380 (announce $MY_IP)"
exec redis-server "$CFG"
