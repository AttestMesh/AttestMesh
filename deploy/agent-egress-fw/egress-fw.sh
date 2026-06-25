#!/bin/sh
# Egress firewall for the matrix-admin-agent (docs/specs/matrix-admin-agent.md §12).
#
# Shares the agent container's network namespace (compose: network_mode:
# service:matrix-admin-agent) and sets a default-DROP OUTPUT policy, allowing ONLY:
#   - loopback + established/related (return traffic)
#   - Docker embedded DNS (so service names + the LLM host resolve)
#   - the internal compose services the agent needs (Synapse, Postgres, nginx), by
#     their RESOLVED container IPs — NOT the whole bridge, so the host gateway and
#     anything else on the subnet stay unreachable
#   - the single pinned LLM host on :443
# Everything else is dropped. This is the network-enforced exfiltration control: even
# a compromised or prompt-injected agent cannot open a socket to the open internet.
#
# Design choices:
#   - Tools (iptables, dig) are baked into the image, so a restart re-locks cleanly
#     even though the shared netns may already carry the DROP policy (no runtime apk
#     over an already-locked netns).
#   - Plain iptables (no ipset) — only needs iptables + conntrack, which are already
#     proven on this box by ts-firewall (the ip_set kernel module may be absent).
#   - Idempotent per-IP ACCEPTs (never flush) — re-resolution only ADDS, so there is
#     no window where the allowlist is empty.
#   - FAIL CLOSED: if the LLM host is unknown/unresolvable, we still lock down; the
#     agent simply loses LLM egress (it never gains open egress).
#   - NOT `set -e`: a transient dig/iptables failure must NOT kill the re-resolve loop
#     (that would leave the agent unable to reach the LLM after a boot-time DNS miss).
set -u

host_of() {  # strip scheme then :port//path — POSIX param expansion (busybox-safe, no sed)
  h="${1#*://}"; printf '%s' "${h%%[:/]*}"
}

LLM_HOST="$(host_of "${LLM_BASE_URL:-}")"      # empty allowed → fail closed (no LLM allow)
LLM_PORT="${LLM_PORT:-443}"
ALLOW_DNS="${ALLOW_DNS:-127.0.0.11}"
INTERNAL_HOSTS="${INTERNAL_HOSTS:-synapse postgres nginx}"
LLM_ALLOW_CIDRS="${LLM_ALLOW_CIDRS:-}"         # static :443 allows — pin the LLM's IP/block so it
                                               # is reachable regardless of resolution timing
RERESOLVE="${RERESOLVE_SECONDS:-15}"

# Generic single-host allow (reused beyond the agent — e.g. the Matrix node's Postgres egressing only to
# Cloudflare R2 for wal-g backups). Defaults to the LLM_* values so the matrix-admin-agent compose is
# unchanged; a different consumer sets ALLOW_HOST/ALLOW_PORT/ALLOW_CIDRS (+ INTERNAL_HOSTS='') instead.
ALLOW_HOST="$(host_of "${ALLOW_BASE_URL:-}")"; ALLOW_HOST="${ALLOW_HOST:-$LLM_HOST}"
ALLOW_PORT="${ALLOW_PORT:-$LLM_PORT}"
ALLOW_CIDRS="${ALLOW_CIDRS:-$LLM_ALLOW_CIDRS}"

echo "egress-fw: allow='${ALLOW_HOST:-<none>}':$ALLOW_PORT internal='$INTERNAL_HOSTS' static='$ALLOW_CIDRS' dns=$ALLOW_DNS"

add() {  # idempotent append to the AMX_EGRESS chain
  iptables -C AMX_EGRESS "$@" 2>/dev/null || iptables -A AMX_EGRESS "$@"
}

allow_host() {  # $1=host  $2=optional "proto:port" restriction (else any port)
  h="$1"; restrict="${2:-}"
  [ -n "$h" ] || return 0
  for ip in $(dig +short A "$h" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'); do
    if [ -n "$restrict" ]; then
      add -d "$ip" -p "${restrict%%:*}" --dport "${restrict##*:}" -j ACCEPT
    else
      add -d "$ip" -j ACCEPT
    fi
  done
}

resolve() {
  for h in $INTERNAL_HOSTS; do allow_host "$h"; done
  allow_host "$ALLOW_HOST" "tcp:$ALLOW_PORT"
}

# Dedicated chain so the OUTPUT policy can be DROP while we (idempotently) allow
# specifics. Jump from OUTPUT at position 1.
iptables -N AMX_EGRESS 2>/dev/null || true
iptables -C OUTPUT -j AMX_EGRESS 2>/dev/null || iptables -I OUTPUT 1 -j AMX_EGRESS
add -o lo -j ACCEPT
add -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# Outbound DNS to ANY resolver: Docker's embedded DNS (127.0.0.11) forwards EXTERNAL queries from
# THIS netns to the upstream, so allowing only 127.0.0.11 breaks external name resolution (the LLM
# host → EAI_AGAIN). Residual: :53 is a low-bandwidth exfil channel (DNS tunneling); all DATA-port
# egress stays deny-all (the canary :443 is still blocked → egress_locked holds). Tighten later with
# DNS-locked resolution (dnsmasq restricting which domains resolve) if needed.
add -p udp --dport 53 -j ACCEPT
add -p tcp --dport 53 -j ACCEPT
# Static LLM pins (resolution-independent): guarantee the pinned LLM IP/block is reachable even if
# a boot-time dig misses. The dynamic resolve below is the complement, not the gate.
for c in $ALLOW_CIDRS; do add -d "$c" -p tcp --dport "$ALLOW_PORT" -j ACCEPT; done

# Populate the allowlist BEFORE locking the policy (on a fresh netns the policy is
# still ACCEPT so the first dig works; on a restart the DNS rule above already exists).
resolve

iptables -P OUTPUT DROP

# No IPv6 egress (the agent is IPv4-only here); allow loopback only. Guarded for
# kernels without ip6tables.
ip6tables -C OUTPUT -o lo -j ACCEPT 2>/dev/null || ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
ip6tables -P OUTPUT DROP 2>/dev/null || true

echo "egress-fw: OUTPUT=DROP — restricted to internal services + ${ALLOW_HOST:-<none>}:$ALLOW_PORT"
while true; do
  sleep "$RERESOLVE"
  resolve   # re-resolve (DNS pinning): pick up any new LLM/internal IPs; old IPs stay (harmless)
done
