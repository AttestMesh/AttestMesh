#!/usr/bin/env bash
set -euo pipefail

action="${1:-add}"
gateway="192.168.96.1"
subnet="192.168.96.0/20"
iface="$(ip -o -4 addr show | awk -v gateway="$gateway" '$4 ~ ("^" gateway "/") {print $2; exit}')"
[ -n "$iface" ] || { echo "frontier bridge is not present" >&2; exit 1; }

rule=(
  -i "$iface"
  -s "$subnet"
  -d "$gateway"
  -p tcp
  -m multiport --dports 28082,28083
  -m comment --comment hindsight-frontier-canary-proxies
  -j ACCEPT
)

case "$action" in
  add)
    sudo iptables -C INPUT "${rule[@]}" 2>/dev/null ||
      sudo iptables -I INPUT 1 "${rule[@]}"
    ;;
  remove)
    while sudo iptables -C INPUT "${rule[@]}" 2>/dev/null; do
      sudo iptables -D INPUT "${rule[@]}"
    done
    ;;
  *)
    echo "usage: $0 [add|remove]" >&2
    exit 2
    ;;
esac
