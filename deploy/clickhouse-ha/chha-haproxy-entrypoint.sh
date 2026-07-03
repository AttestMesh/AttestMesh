#!/usr/bin/env bash
# HAProxy entrypoint: owns <mesh_ip>:8123 (HTTP) and :9000 (native) in the sidecar netns.
# Every replica of the 1-shard × N-replica cluster is writable, so both listeners roundrobin
# across all healthy backends; health = GET /ping on each node's :18123 (also used as the
# check for the native :19001 backends). Backends are rendered from the precomputed CH_PEERS
# (pg-ha pattern).
set -u
CHHA_LOG_TAG=haproxy
source /usr/local/bin/chha-common.sh

_st "boot: waiting for attestmesh0"
MY_IP="$(wait_for_mesh_ip)"
assert_self_ip "$MY_IP"

CFG=/run/chha-haproxy.cfg
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

listen http
  bind $MY_IP:8123
  option httpchk GET /ping
  http-check expect status 200
  balance roundrobin
  default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
CFG
  for n in $(peer_names); do
    echo "  server $n $(peer_ip "$n"):18123 check port 18123"
  done
  cat <<CFG

listen native
  bind $MY_IP:9000
  option httpchk GET /ping
  http-check expect status 200
  balance roundrobin
  default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
CFG
  for n in $(peer_names); do
    echo "  server $n $(peer_ip "$n"):19001 check port 18123"
  done
} > "$CFG"
chmod 600 "$CFG"

_st "starting haproxy on $MY_IP:8123/:9000 (backends: ${CH_PEERS})"
exec haproxy -f "$CFG" -db
