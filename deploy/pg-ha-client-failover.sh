#!/usr/bin/env bash
# Live pg-ha consumer gate: probe every application through a controlled
# switchover, then cycle the former leader after it has become a replica.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/env.sh"
source "$HERE/lib.sh"

ACTION="${1:-probe-once}"
CANDIDATE="${2:-}"
MESH_SSH_HOST="${MESH_SSH_HOST:-attestmesh-mesh-node}"
PG_STATE="$ROOT/deploy/logs/pg-ha-pg-ha.state"
AGENT_STATE="$ROOT/deploy/logs/agent-session-mcp-node-agent-session-mcp.state"
POCKET_STATE="$ROOT/deploy/logs/pocket-mcp-node-pocket-mcp.state"
TELEGRAM_STATE="$ROOT/deploy/logs/telegram-sync-node-telegram-sync-node.state"
HINDSIGHT_STATE="$ROOT/deploy/logs/hindsight-node-hindsight-node.state"
LANGFUSE_STATE="$ROOT/deploy/logs/langfuse-node-langfuse-node.state"
LANGFUSE_SECRETS="${LANGFUSE_SECRETS:-$HOME/.attestmesh/langfuse-node.env}"
TUNNEL_PIDS=()
probe_pid=""

cleanup() {
  [ -z "$probe_pid" ] || kill "$probe_pid" 2>/dev/null || true
  local pid
  for pid in "${TUNNEL_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

state_value() { sed -n "s/^$2=//p" "$1" | head -1; }

ipv4_from_u32() {
  local n="$1"
  printf '%d.%d.%d.%d' "$(( (n >> 24) & 255 ))" "$(( (n >> 16) & 255 ))" "$(( (n >> 8) & 255 ))" "$(( n & 255 ))"
}

mesh_ip_for_state() {
  local state="$1" x cluster member raw
  x="$(state_value "$state" X)"
  cluster="$(state_value "$PG_STATE" CLUSTER)"
  member="$(cast call "$cluster" 'memberIdOf(address)(bytes32)' "$x" --rpc-url "$RPC_URL")"
  raw="$(cast call "$cluster" 'meshIpOf(bytes32)(uint32)' "$member" --rpc-url "$RPC_URL" | awk '{print int($1)}')"
  ipv4_from_u32 "$raw"
}

open_tunnel() {
  local local_port="$1" mesh_ip="$2" remote_port="$3" pid
  ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -L "127.0.0.1:${local_port}:${mesh_ip}:${remote_port}" "$MESH_SSH_HOST" &
  pid=$!
  TUNNEL_PIDS+=("$pid")
  for _ in $(seq 1 20); do
    (: </dev/tcp/127.0.0.1/"$local_port") >/dev/null 2>&1 && return 0
    kill -0 "$pid" 2>/dev/null || die "SSH tunnel to ${mesh_ip}:${remote_port} exited"
    sleep 0.25
  done
  die "SSH tunnel to ${mesh_ip}:${remote_port} did not open"
}

prepare_probe_env() {
  local agent_ip pocket_ip telegram_ip hindsight_ip langfuse_ip
  agent_ip="$(mesh_ip_for_state "$AGENT_STATE")"
  pocket_ip="$(mesh_ip_for_state "$POCKET_STATE")"
  telegram_ip="$(state_value "$TELEGRAM_STATE" MESH_IP)"
  hindsight_ip="$(state_value "$HINDSIGHT_STATE" MESH_IP)"
  langfuse_ip="$(state_value "$LANGFUSE_STATE" MESH_IP)"
  source "$LANGFUSE_SECRETS"

  open_tunnel 28081 "$agent_ip" 8081
  open_tunnel 28085 "$telegram_ip" 18085
  open_tunnel 28088 "$hindsight_ip" 18888
  open_tunnel 28420 "$langfuse_ip" 18420
  open_tunnel 28800 "$pocket_ip" 20800

  export PROBE_AGENT_MCP_URL=http://127.0.0.1:28081/mcp
  export PROBE_TELEGRAM_MCP_URL=http://127.0.0.1:28085/mcp
  export PROBE_HINDSIGHT_URL=http://127.0.0.1:28088
  export PROBE_HINDSIGHT_TOKEN="$(state_value "$HINDSIGHT_STATE" TAK)"
  export PROBE_LANGFUSE_URL=http://127.0.0.1:28420
  export PROBE_LANGFUSE_PUBLIC_KEY="$LANGFUSE_INIT_PROJECT_PUBLIC_KEY"
  export PROBE_LANGFUSE_SECRET_KEY="$LANGFUSE_INIT_PROJECT_SECRET_KEY"
  export PROBE_POCKET_MCP_URL=http://127.0.0.1:28800/sse
  export PROBE_SYNCLAVE_URL=https://synclave.net
}

run_probes() {
  PROBE_DURATION="$1" PROBE_INTERVAL=1 PROBE_MAX_RECOVERY=10 \
    uv run --with 'mcp>=1.28.1' python "$HERE/pg-ha-client-probes.py"
}

current_leader() {
  local first_ip
  first_ip="$(state_value "$PG_STATE" PGHA_PEERS)"
  first_ip="${first_ip#*=}"; first_ip="${first_ip%%,*}"
  ssh -o BatchMode=yes "$MESH_SSH_HOST" \
    "curl -fsS --max-time 5 http://${first_ip}:8008/cluster" \
    | jq -r '.members[] | select(.role == "leader") | .name'
}

case "$ACTION" in
  probe-once)
    prepare_probe_env
    run_probes 0
    ;;
  gate)
    [ -n "$CANDIDATE" ] || die "usage: $0 gate <pgN>"
    prepare_probe_env
    run_probes 0
    former_leader="$(current_leader)"
    log_file="$LOGDIR/pg-ha-client-gate.$(ts).jsonl"
    run_probes 150 >"$log_file" 2>&1 &
    probe_pid=$!
    sleep 5
    "$HERE/pg-ha-node.sh" pg-ha switchover "$CANDIDATE"
    "$HERE/pg-ha-node.sh" pg-ha cycle-replica "$former_leader"
    wait "$probe_pid"
    probe_pid=""
    log "✔ client failover gate passed; probe log: $log_file"
    ;;
  *)
    die "usage: $0 [probe-once|gate <pgN>]"
    ;;
esac
