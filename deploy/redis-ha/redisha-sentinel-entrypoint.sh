#!/usr/bin/env bash
# Redis Sentinel entrypoint. Runs in the SIDECAR netns on :26379; monitors master name
# "redisha" with quorum 2 and owns failover thereafter. Sentinel REWRITES its config file
# at runtime (known replicas/sentinels, epoch), so the conf lives on the persistent
# /sentinel-data volume: an existing conf is reused verbatim (plain restart keeps sentinel
# state), a fresh one is rendered pointing at the CURRENT master (probed via INFO replication
# so a post-failover fresh disk doesn't monitor a replica). Sentinel's own requirepass is the
# sealed REDISHA_VERIFY_PASSWORD so the driver can query SENTINEL masters from the mesh shell
# without the CSK (redis ≥6.2 also uses requirepass to authenticate sentinel-to-sentinel).
set -u
REDISHA_LOG_TAG=sentinel
source /usr/local/bin/redisha-common.sh

NODE="${REDISHA_NODE_NAME:?REDISHA_NODE_NAME not set}"
SDATA="${SENTINEL_DATA_DIR:-/sentinel-data}"
CFG="$SDATA/sentinel.conf"
mkdir -p "$SDATA"
chown redis:redis "$SDATA" 2>/dev/null || true

_st "boot: waiting for attestmesh0"
MY_IP="$(wait_for_mesh_ip)"
assert_self_ip "$MY_IP"
_st "mesh ip $MY_IP confirmed for $NODE; deriving redis auth credential"
AUTH_PW="$(csk_derive attestmesh.redisha.auth.v1)" || exit 1
VERIFY_PW="${REDISHA_VERIFY_PASSWORD:?REDISHA_VERIFY_PASSWORD not set}"

if [ -s "$CFG" ]; then
  _st "existing sentinel.conf -> plain restart with preserved sentinel state"
else
  # Fresh sentinel state: find the current master. Prefer any peer whose INFO replication
  # says role:master; fall back to the first peer (cold cluster bootstrap — nothing up yet).
  MASTER_IP=""
  for n in $(peer_names); do
    ip="$(peer_ip "$n")"
    role="$(redis-cli -h "$ip" -p 6380 -a "$AUTH_PW" --no-auth-warning INFO replication 2>/dev/null \
      | grep -o 'role:master' || true)"
    [ -n "$role" ] && { MASTER_IP="$ip"; break; }
  done
  if [ -z "$MASTER_IP" ]; then
    MASTER_IP="$(peer_ip "$(peer_names | head -n1)")"
    _st "no live master found; defaulting monitor target to first peer $MASTER_IP"
  else
    _st "current master probed at $MASTER_IP"
  fi
  umask 077
  cat > "$CFG" <<CONF
port 26379
bind 0.0.0.0
protected-mode no
requirepass $VERIFY_PW
dir $SDATA
sentinel announce-ip $MY_IP
sentinel announce-port 26379
sentinel monitor redisha $MASTER_IP 6380 2
sentinel auth-pass redisha $AUTH_PW
sentinel down-after-milliseconds redisha 5000
sentinel failover-timeout redisha 60000
sentinel parallel-syncs redisha 1
CONF
  chown redis:redis "$CFG" 2>/dev/null || true
  _st "rendered fresh sentinel.conf (monitor redisha $MASTER_IP:6380 quorum 2)"
fi

_st "starting redis-sentinel on :26379 (announce $MY_IP)"
exec redis-server "$CFG" --sentinel
