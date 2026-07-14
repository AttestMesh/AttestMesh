#!/usr/bin/env bash
set -u

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export LOGDIR="$TMP/logs"
mkdir -p "$LOGDIR"
source "$ROOT/deploy/lib.sh"

export RPC_URL=https://rpc.invalid PRIVATE_KEY=secret DEPLOYER_ADDR=0x1234
export RECEIPT_WAITER="$TMP/waiter.mjs"
HASH="0x$(printf 'ab%.0s' {1..32})"
COUNT="$TMP/send-count"
printf '0\n' >"$COUNT"

sleep() { :; }
cast() {
  case "$1" in
    nonce) printf '7\n' ;;
    send)
      local count
      count=$(<"$COUNT"); count=$((count + 1)); printf '%s\n' "$count" >"$COUNT"
      if [ -f "$TMP/hash-then-error" ]; then
        printf '%s\n' "$HASH"
        return 1
      fi
      if [ -f "$TMP/fail-first" ] && [ "$count" -eq 1 ]; then
        printf 'submission failed\n' >&2
        return 1
      fi
      printf '%s\n' "$HASH"
      ;;
    *) return 2 ;;
  esac
}
node() {
  [ ! -f "$TMP/receipt-fails" ]
}

touch "$TMP/fail-first"
send_with_nonce_retry retry-test 0xdead 'write()'
[ "$(<"$COUNT")" -eq 2 ] || { echo "expected one pre-hash retry"; exit 1; }

rm -f "$TMP/fail-first"
touch "$TMP/hash-then-error"
printf '0\n' >"$COUNT"
send_with_nonce_retry hash-then-error-test 0xdead 'write()'
[ "$(<"$COUNT")" -eq 1 ] || { echo "nonzero submission with a hash was resubmitted"; exit 1; }

rm -f "$TMP/hash-then-error"
touch "$TMP/receipt-fails"
printf '0\n' >"$COUNT"
if send_with_nonce_retry no-resubmit-test 0xdead 'write()'; then
  echo "receipt failure unexpectedly succeeded"
  exit 1
else
  rc=$?
fi
[ "$rc" -eq 75 ] || { echo "expected ambiguous-transaction rc=75, got $rc"; exit 1; }
[ "$(<"$COUNT")" -eq 1 ] || { echo "receipt failure resubmitted the transaction"; exit 1; }

echo "transaction confirmation shell tests passed"
