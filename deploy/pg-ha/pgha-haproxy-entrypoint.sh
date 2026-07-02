#!/usr/bin/env bash
# HAProxy entrypoint: owns <mesh_ip>:5432 (current primary) and :5433 (replicas) in the sidecar
# netns. Routing decisions come from Patroni's public health GETs on each node's :8008 —
# /primary and /replica return 200 only for that role, so failover re-routes within one check
# window. Backends are rendered from the precomputed PGHA_PEERS (docs/specs/pg-ha.md §5).
set -u
PGHA_LOG_TAG=haproxy
source /usr/local/bin/pgha-common.sh

_st "boot: waiting for attestmesh0"
MY_IP="$(wait_for_mesh_ip)"
assert_self_ip "$MY_IP"

CFG=/run/pgha-haproxy.cfg
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
  bind $MY_IP:5432
  option httpchk GET /primary
  http-check expect status 200
  default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
CFG
  for n in $(peer_names); do
    echo "  server $n $(peer_ip "$n"):5434 check port 8008"
  done
  cat <<CFG

listen replicas
  bind $MY_IP:5433
  option httpchk GET /replica
  http-check expect status 200
  balance roundrobin
  default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
CFG
  for n in $(peer_names); do
    echo "  server $n $(peer_ip "$n"):5434 check port 8008"
  done
} > "$CFG"

_st "starting haproxy on $MY_IP:5432/:5433 (backends: ${PGHA_PEERS})"
exec haproxy -f "$CFG" -db
