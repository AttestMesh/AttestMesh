#!/usr/bin/env bash
# HAProxy entrypoint: owns <mesh_ip>:6379 (current master) and :6381 (replicas) in the sidecar
# netns. Redis has no HTTP role endpoint, so routing uses a tcp-check dialogue against each
# backend's :6380 — AUTH with the CSK-derived password, then INFO replication, expecting
# role:master (:6379) or role:slave (:6381). Failover re-routes within one check window.
# Backends are rendered from the precomputed REDIS_PEERS (pg-ha pattern). The rendered config
# embeds the derived password, so it is written 0600 under /run.
set -u
REDISHA_LOG_TAG=haproxy
source /usr/local/bin/redisha-common.sh

_st "boot: waiting for attestmesh0"
MY_IP="$(wait_for_mesh_ip)"
assert_self_ip "$MY_IP"
_st "deriving redis auth credential for health checks"
AUTH_PW="$(csk_derive attestmesh.redisha.auth.v1)" || exit 1

CFG=/run/redisha-haproxy.cfg
umask 077
{
  cat <<CFG
global
  maxconn 400

defaults
  mode tcp
  timeout connect 5s
  timeout client 30m
  timeout server 30m
  timeout check 5s

listen stats
  bind 127.0.0.1:7000
  mode http
  stats enable
  stats uri /

listen primary
  bind $MY_IP:6379
  option tcp-check
  tcp-check connect
  tcp-check send AUTH\\ $AUTH_PW\\r\\n
  tcp-check expect string +OK
  tcp-check send INFO\\ replication\\r\\n
  tcp-check expect string role:master
  tcp-check send QUIT\\r\\n
  tcp-check expect string +OK
  default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
CFG
  for n in $(peer_names); do
    echo "  server $n $(peer_ip "$n"):6380 check"
  done
  cat <<CFG

listen replicas
  bind $MY_IP:6381
  option tcp-check
  tcp-check connect
  tcp-check send AUTH\\ $AUTH_PW\\r\\n
  tcp-check expect string +OK
  tcp-check send INFO\\ replication\\r\\n
  tcp-check expect string role:slave
  tcp-check send QUIT\\r\\n
  tcp-check expect string +OK
  balance roundrobin
  default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
CFG
  for n in $(peer_names); do
    echo "  server $n $(peer_ip "$n"):6380 check"
  done
} > "$CFG"
chmod 600 "$CFG"

_st "starting haproxy on $MY_IP:6379/:6381 (backends: ${REDIS_PEERS})"
exec haproxy -f "$CFG" -db
