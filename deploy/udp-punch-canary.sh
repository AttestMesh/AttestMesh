#!/usr/bin/env bash
# Issue #36 direct-UDP canary helper. Run inside the sidecar network namespace
# (for example from ssh-node's sshd-mesh service). This script intentionally
# never calls `wg show ... dump`, whose interface row contains the private key.
set -euo pipefail

HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:9090}
WG_INTERFACE=${WG_INTERFACE:-attestmesh0}
WG_LISTEN_PORT=${WG_LISTEN_PORT:-51821}
FAULT_STATE=${FAULT_STATE:-/run/attestmesh-udp-canary.rule}

usage() {
  echo "usage:" >&2
  echo "  $0 snapshot" >&2
  echo "  $0 wait-udp MEMBER_ID [TIMEOUT_SECONDS]" >&2
  echo "  $0 soak MEMBER_ID [DURATION_SECONDS] [INTERVAL_SECONDS] [-- WORKLOAD ...]" >&2
  echo "  $0 fault-add WG_PUBLIC_KEY" >&2
  echo "  $0 fault-remove" >&2
  exit 2
}

require_tools() {
  local tool
  for tool in curl jq wg date; do
    command -v "$tool" >/dev/null || {
      echo "missing required tool: $tool" >&2
      exit 1
    }
  done
}

health() {
  curl --fail --silent --show-error "$HEALTH_URL/healthz"
}

metrics() {
  curl --fail --silent --show-error "$HEALTH_URL/metrics"
}

metric_value() {
  local name=$1
  metrics | awk -v name="$name" '$1 == name { print $2; found=1 } END { if (!found) print 0 }'
}

snapshot() {
  local health_json metrics_text endpoints handshakes transfers
  health_json=$(health)
  metrics_text=$(metrics | grep -E '^attestmesh_(punch_(attempts|success)_total|udp_reverts_total|peer_(transport|punch_status))' || true)
  endpoints=$(wg show "$WG_INTERFACE" endpoints)
  handshakes=$(wg show "$WG_INTERFACE" latest-handshakes)
  transfers=$(wg show "$WG_INTERFACE" transfer)
  jq -n \
    --arg captured_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --argjson health "$health_json" \
    --arg metrics "$metrics_text" \
    --arg wg_endpoints "$endpoints" \
    --arg wg_latest_handshakes "$handshakes" \
    --arg wg_transfer "$transfers" \
    '{captured_at: $captured_at, health: $health, metrics: $metrics,
      wg_endpoints: $wg_endpoints, wg_latest_handshakes: $wg_latest_handshakes,
      wg_transfer: $wg_transfer}'
}

peer_transport() {
  local member_id=$1
  health | jq -r --arg member_id "$member_id" '.transports[$member_id] // "missing"'
}

wait_udp() {
  local member_id=$1 timeout=${2:-70} start
  start=$(date +%s)
  while (( $(date +%s) - start < timeout )); do
    if [[ $(peer_transport "$member_id") == udp ]]; then
      snapshot
      return 0
    fi
    sleep 2
  done
  echo "peer $member_id did not reach udp within ${timeout}s" >&2
  snapshot >&2
  return 1
}

soak() {
  local member_id=$1 duration=${2:-86400} interval=${3:-30}
  shift $(( $# >= 3 ? 3 : $# ))
  if [[ ${1:-} == -- ]]; then
    shift
  fi
  local -a workload=("$@")
  local started deadline baseline_reverts now health_json transport status success reverts workload_ok
  started=$(date +%s)
  deadline=$((started + duration))
  baseline_reverts=$(metric_value attestmesh_udp_reverts_total)

  while (( $(date +%s) < deadline )); do
    now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    health_json=$(health)
    transport=$(jq -r --arg member_id "$member_id" '.transports[$member_id] // "missing"' <<<"$health_json")
    status=$(jq -r --arg member_id "$member_id" '.punch_peer_status[$member_id] // "missing"' <<<"$health_json")
    success=$(jq -r '.punch.punch_success_total' <<<"$health_json")
    reverts=$(jq -r '.punch.udp_reverts_total' <<<"$health_json")
    workload_ok=true
    if (( ${#workload[@]} > 0 )) && ! "${workload[@]}" >/dev/null; then
      workload_ok=false
    fi

    jq -cn \
      --arg captured_at "$now" \
      --arg member_id "$member_id" \
      --arg transport "$transport" \
      --arg status "$status" \
      --argjson punch_success_total "$success" \
      --argjson udp_reverts_total "$reverts" \
      --argjson workload_ok "$workload_ok" \
      '{captured_at: $captured_at, member_id: $member_id, transport: $transport,
        punch_status: $status, punch_success_total: $punch_success_total,
        udp_reverts_total: $udp_reverts_total, workload_ok: $workload_ok}'

    [[ $transport == udp && $status == udp && $success -ge 1 ]] || {
      echo "continuous UDP soak failed for peer $member_id" >&2
      return 1
    }
    [[ $reverts == "$baseline_reverts" ]] || {
      echo "udp_reverts_total changed during the pre-fault soak" >&2
      return 1
    }
    [[ $workload_ok == true ]] || {
      echo "canary workload failed" >&2
      return 1
    }
    sleep "$interval"
  done
}

fault_add() {
  local public_key=$1 endpoint ip port
  command -v iptables >/dev/null || {
    echo "iptables is required for fault injection" >&2
    exit 1
  }
  endpoint=$(wg show "$WG_INTERFACE" endpoints | awk -v key="$public_key" '$1 == key { print $2 }')
  [[ $endpoint =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}:[0-9]+$ ]] || {
    echo "peer endpoint is missing, IPv6, or not an IPv4 UDP endpoint: $endpoint" >&2
    exit 1
  }
  ip=${endpoint%:*}
  port=${endpoint##*:}
  [[ $ip != 127.* && $ip != 0.0.0.0 ]] || {
    echo "refusing to block loopback/TCP-bridge endpoint: $endpoint" >&2
    exit 1
  }
  [[ ! -e $FAULT_STATE ]] || {
    echo "fault rule state already exists at $FAULT_STATE; remove it first" >&2
    exit 1
  }
  iptables -I OUTPUT 1 -p udp --sport "$WG_LISTEN_PORT" -d "$ip" --dport "$port" \
    -m comment --comment attestmesh-udp-canary -j DROP
  printf '%s %s\n' "$ip" "$port" >"$FAULT_STATE"
  echo "dropping only $ip:$port/udp; gateway TCP remains available"
}

fault_remove() {
  local ip port
  [[ -r $FAULT_STATE ]] || {
    echo "no fault rule state at $FAULT_STATE" >&2
    exit 1
  }
  read -r ip port <"$FAULT_STATE"
  iptables -D OUTPUT -p udp --sport "$WG_LISTEN_PORT" -d "$ip" --dport "$port" \
    -m comment --comment attestmesh-udp-canary -j DROP
  rm -f "$FAULT_STATE"
  echo "removed direct-UDP fault rule for $ip:$port"
}

require_tools
case ${1:-} in
  snapshot) snapshot ;;
  wait-udp) [[ $# -ge 2 ]] || usage; wait_udp "$2" "${3:-70}" ;;
  soak) [[ $# -ge 2 ]] || usage; shift; soak "$@" ;;
  fault-add) [[ $# -eq 2 ]] || usage; fault_add "$2" ;;
  fault-remove) [[ $# -eq 1 ]] || usage; fault_remove ;;
  *) usage ;;
esac
