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
PG_STATE="$LOGDIR/pg-ha-pg-ha.state"
AGENT_STATE="$LOGDIR/agent-session-mcp-node-agent-session-mcp.state"
POCKET_STATE="$LOGDIR/pocket-mcp-node-pocket-mcp.state"
TELEGRAM_STATE="$LOGDIR/telegram-sync-node-telegram-sync-node.state"
HINDSIGHT_STATE="$LOGDIR/hindsight-node-hindsight-node.state"
FUGU_LB_STATE="$LOGDIR/fugu-router-lb-node-fugu-router-lb.state"
FUGU_BLUE_STATE="$LOGDIR/fugu-router-node-fugu-router-blue.state"
FUGU_GREEN_STATE="$LOGDIR/fugu-router-node-fugu-router-green.state"
FUGU_SECRETS="${FUGU_SECRETS:-$HOME/.attestmesh/fugu-router.env}"
TUNNEL_PIDS=()
probe_pid=""

cleanup() {
  [ -z "$probe_pid" ] || kill "$probe_pid" 2>/dev/null || true
  local pid
  for pid in "${TUNNEL_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
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
  # The mesh gateway can reset a long-lived TLS/SSH session during an unrelated
  # CVM restart. Keep each forwarding process supervised so one transport reset
  # cannot masquerade as a 40-second application outage.
  (
    trap - EXIT
    child=""
    trap '[ -z "$child" ] || { kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; }; exit 0' TERM INT HUP
    while :; do
      ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
        -L "127.0.0.1:${local_port}:${mesh_ip}:${remote_port}" "$MESH_SSH_HOST" &
      child=$!
      wait "$child" 2>/dev/null || true
      child=""
      sleep 0.25
    done
  ) &
  pid=$!
  TUNNEL_PIDS+=("$pid")
  for _ in $(seq 1 20); do
    (: </dev/tcp/127.0.0.1/"$local_port") >/dev/null 2>&1 && return 0
    kill -0 "$pid" 2>/dev/null || die "SSH tunnel to ${mesh_ip}:${remote_port} exited"
    sleep 0.25
  done
  die "SSH tunnel to ${mesh_ip}:${remote_port} did not open"
}

baseline_rounds() {
  local consecutive=0 attempts=0
  while [ "$consecutive" -lt 3 ] && [ "$attempts" -lt 12 ]; do
    attempts=$((attempts + 1))
    if run_probes 0; then
      consecutive=$((consecutive + 1))
    else
      consecutive=0
      log "baseline probe failure reset the consecutive-round counter ($attempts/12)"
    fi
  done
  [ "$consecutive" -eq 3 ] || die "could not establish three consecutive clean consumer rounds"
}

prepare_probe_env() {
  local agent_ip pocket_ip telegram_ip hindsight_ip fugu_lb_ip fugu_blue_ip fugu_green_ip
  agent_ip="$(mesh_ip_for_state "$AGENT_STATE")"
  pocket_ip="$(mesh_ip_for_state "$POCKET_STATE")"
  telegram_ip="$(state_value "$TELEGRAM_STATE" MESH_IP)"
  hindsight_ip="$(state_value "$HINDSIGHT_STATE" MESH_IP)"
  fugu_lb_ip="$(state_value "$FUGU_LB_STATE" MESH_IP)"
  fugu_blue_ip="$(state_value "$FUGU_BLUE_STATE" MESH_IP)"
  fugu_green_ip="$(state_value "$FUGU_GREEN_STATE" MESH_IP)"
  [ -n "$fugu_lb_ip" ] && [ -n "$fugu_blue_ip" ] && [ -n "$fugu_green_ip" ] \
    || die "Fugu LB/blue/green state is incomplete"
  source "$FUGU_SECRETS"
  [ -n "${LITELLM_MASTER_KEY:-}" ] || die "LITELLM_MASTER_KEY is missing from $FUGU_SECRETS"

  open_tunnel 28081 "$agent_ip" 8081
  open_tunnel 28085 "$telegram_ip" 18085
  open_tunnel 28088 "$hindsight_ip" 18888
  open_tunnel 28410 "$fugu_lb_ip" 18410
  open_tunnel 28411 "$fugu_blue_ip" 18410
  open_tunnel 28412 "$fugu_green_ip" 18410
  open_tunnel 28800 "$pocket_ip" 20800

  export PROBE_AGENT_MCP_URL=http://127.0.0.1:28081/mcp
  export PROBE_TELEGRAM_MCP_URL=http://127.0.0.1:28085/mcp
  export PROBE_HINDSIGHT_URL=http://127.0.0.1:28088
  export PROBE_HINDSIGHT_TOKEN="$(state_value "$HINDSIGHT_STATE" TAK)"
  export PROBE_FUGU_LB_URL=http://127.0.0.1:28410
  export PROBE_FUGU_BLUE_URL=http://127.0.0.1:28411
  export PROBE_FUGU_GREEN_URL=http://127.0.0.1:28412
  export PROBE_FUGU_API_KEY="$LITELLM_MASTER_KEY"
  export PROBE_POCKET_MCP_URL=http://127.0.0.1:28800/sse
  export PROBE_SYNCLAVE_URL=https://console.attestmesh.xyz
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

zero_lag_replica() {
  local first_ip candidate i topology
  first_ip="$(state_value "$PG_STATE" PGHA_PEERS)"
  first_ip="${first_ip#*=}"; first_ip="${first_ip%%,*}"
  for i in $(seq 1 30); do
    topology="$(ssh -o BatchMode=yes "$MESH_SSH_HOST" \
      "curl -fsS --max-time 5 http://${first_ip}:8008/cluster" 2>/dev/null || true)"
    candidate="$(jq -r '.members[]?
        | select(.role == "replica")
        | select(.state == "streaming" or .state == "running")
        | select((.lag // 0) == 0)
        | .name' <<<"$topology" \
      | sort \
      | head -1)"
    if [ -n "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
    log "… waiting for a zero-lag failover candidate ($i/30)" >&2
    sleep 1
  done
  return 0
}

case "$ACTION" in
  probe-once)
    prepare_probe_env
    baseline_rounds
    ;;
  soak)
    prepare_probe_env
    baseline_rounds
    log_file="$LOGDIR/pg-ha-client-soak.$(ts).jsonl"
    if ! run_probes "${PROBE_SOAK_SECONDS:-150}" >"$log_file" 2>&1; then
      die "client soak violated the recovery SLO; probe log: $log_file"
    fi
    log "✔ client soak passed; probe log: $log_file"
    ;;
  gate)
    prepare_probe_env
    baseline_rounds
    # Baseline probes can generate WAL. Select immediately before the mutation
    # rather than carrying a replica decision made before those writes.
    CANDIDATE="${CANDIDATE:-$(zero_lag_replica)}"
    [ -n "$CANDIDATE" ] || die "no zero-lag replica is eligible for the failover gate"
    former_leader="$(current_leader)"
    log_file="$LOGDIR/pg-ha-client-gate.$(ts).jsonl"
    run_probes 150 >"$log_file" 2>&1 &
    probe_pid=$!
    sleep 5
    if ! "$HERE/pg-ha-node.sh" pg-ha switchover "$CANDIDATE"; then
      die "controlled switchover did not reach the requested topology"
    fi
    if ! "$HERE/pg-ha-node.sh" pg-ha cycle-replica "$former_leader"; then
      die "former leader did not complete its guarded replica cycle"
    fi
    if ! wait "$probe_pid"; then
      probe_pid=""
      die "client failover gate violated the recovery SLO; probe log: $log_file"
    fi
    probe_pid=""
    log "✔ client failover gate passed; probe log: $log_file"
    ;;
  *)
    die "usage: $0 [probe-once|soak|gate [pgN]]"
    ;;
esac
