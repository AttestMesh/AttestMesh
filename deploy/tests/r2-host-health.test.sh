#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
STATE="$ROOT/deploy/logs/r2-host-node-health-test.state"
trap 'rm -rf "$TMP" "$STATE"' EXIT
mkdir -p "$ROOT/deploy/logs" "$TMP/bin"
printf 'VM_ID=test-vm\n' > "$STATE"

cat > "$TMP/bin/seq" <<'EOF'
#!/usr/bin/env bash
echo 1
EOF
cat > "$TMP/bin/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$TMP/bin/ssh" <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
case "${MOCK_HEALTH_MODE:?}" in
  healthy)
    printf '%s\n' '{"phase":"healthy","first_converged":true,"csk_acquired":true}'
    exit 0
    ;;
  503)
    printf '%s\n' '{"phase":"wg-configuring","first_converged":false,"csk_acquired":true}'
    exit 22
    ;;
  false-gates)
    printf '%s\n' '{"phase":"healthy","first_converged":false,"csk_acquired":true}'
    exit 0
    ;;
esac
EOF
chmod +x "$TMP/bin/seq" "$TMP/bin/sleep" "$TMP/bin/ssh"

run_health() {
  PATH="$TMP/bin:$PATH" \
  RPC_URL=https://rpc.invalid CHAIN_ID=8453 PRIVATE_KEY=0x01 DEPLOYER_ADDR=0x0000000000000000000000000000000000000001 \
  MOCK_HEALTH_MODE="$1" \
    bash "$ROOT/deploy/r2-host-node.sh" health-test verify-health >/dev/null 2>&1
}

run_health healthy
if run_health 503; then
  echo "HTTP 503 health response was incorrectly accepted" >&2
  exit 1
fi
if run_health false-gates; then
  echo "HTTP 200 with closed convergence gate was incorrectly accepted" >&2
  exit 1
fi
echo "r2-host verify-health regression tests passed"
