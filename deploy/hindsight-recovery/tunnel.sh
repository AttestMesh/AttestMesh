#!/usr/bin/env bash
set -euo pipefail

CONFIG=${RECOVERY_CONFIG:-$HOME/.config/hindsight-recovery/recovery.env}
[ -r "$CONFIG" ] || { echo "missing recovery config: $CONFIG" >&2; exit 2; }
set -a
# Operator-owned assignment-only configuration.
# shellcheck disable=SC1090
source "$CONFIG"
set +a

IFS=, read -r pg_port_1 pg_port_2 pg_port_3 <<<"${PG_LOCAL_PORTS:-55439,55440,55441}"
IFS=, read -r pg_host_1 pg_host_2 pg_host_3 <<<"${PG_REMOTE_HOSTS:-10.18.147.86,10.18.251.71,10.18.172.186}"
IFS=, read -r patroni_port_1 patroni_port_2 patroni_port_3 <<<"${PATRONI_LOCAL_PORTS:-18081,18082,18083}"

exec ssh \
  -o BatchMode=yes \
  -o ConnectTimeout=8 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=5 \
  -o ServerAliveCountMax=2 \
  -o TCPKeepAlive=yes \
  -N \
  -L "127.0.0.1:${pg_port_1}:${pg_host_1}:5432" \
  -L "127.0.0.1:${pg_port_2}:${pg_host_2}:5432" \
  -L "127.0.0.1:${pg_port_3}:${pg_host_3}:5432" \
  -L "127.0.0.1:${HINDSIGHT_LOCAL_PORT:-18898}:10.18.78.76:18888" \
  -L "127.0.0.1:${BUDGET_LOCAL_PORT:-18899}:10.18.78.76:18889" \
  -L "127.0.0.1:${FUGU_LOCAL_PORT:-18419}:10.18.133.81:18410" \
  -L "127.0.0.1:${patroni_port_1}:${pg_host_1}:8008" \
  -L "127.0.0.1:${patroni_port_2}:${pg_host_2}:8008" \
  -L "127.0.0.1:${patroni_port_3}:${pg_host_3}:8008" \
  "${RECOVERY_SSH_TARGET:-attestmesh-mesh-node}"
