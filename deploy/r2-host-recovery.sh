#!/usr/bin/env bash
# Durable recovery routines for an r2-host whose dstack KMS signer was not
# allowlisted on its cluster. Designed to be sequenced by Smithers.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"
: "${RPC_URL:?source deploy/env.sh first}"
require PRIVATE_KEY RPC_URL DEPLOYER_ADDR

NODE="${1:?usage: r2-host-recovery.sh <node> <capture-proof|verify-proof|allow-root|simulate-register|submit-register|clean-roll|verify|health|storage|isolation>}"
ACTION="${2:?missing recovery action}"
STATE="$LOGDIR/r2-host-node-${NODE}.state"
PAYLOAD_FILE="${REGISTRATION_PAYLOAD_FILE:-$LOGDIR/r2-host-registration-${NODE}.json}"
EXPECTED_KMS_ROOT="${EXPECTED_KMS_ROOT:-0x7FA63D99495bE2129cf28eeE54e2ef2724E3aA2e}"
BOX_HOST="${BOX_HOST:-ubuntu@173.231.234.133}"
BOX_PY="${BOX_PY:-/opt/dstack-mcp/venv/bin/python}"
REGISTER_SIG='dstack_register((bytes32,bytes32,bytes,bytes,bytes,bytes,bytes,string),address,bytes32,bytes32)'
ZERO32=0x0000000000000000000000000000000000000000000000000000000000000000

[ -f "$STATE" ] || die "missing node state: $STATE"
# shellcheck disable=SC1090
source "$STATE"
[ -n "${X:-}" ] && [ -n "${CLUSTER:-}" ] || die "state lacks X/CLUSTER: $STATE"

payload_member() { jq -er .member "$PAYLOAD_FILE"; }
payload_calldata() { jq -er .calldata "$PAYLOAD_FILE"; }

capture_proof() {
  if [ -s "$PAYLOAD_FILE" ] && jq -e '.member and .calldata' "$PAYLOAD_FILE" >/dev/null 2>&1; then
    log "registration proof already captured: $PAYLOAD_FILE"
    return 0
  fi
  [ -n "${VM_ID:-}" ] || die "state lacks VM_ID"
  mkdir -p "$(dirname "$PAYLOAD_FILE")"
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" \
    "sudo VM_ID='$VM_ID' '$BOX_PY' - <<'PY'
import os, sys
sys.path.insert(0, '/opt/dstack-mcp')
import mcp_dstack as m
try:
    m.vmm('StartVm', {'id': os.environ['VM_ID']})
except Exception:
    pass
PY"
  local payload tmp
  tmp="${PAYLOAD_FILE}.tmp"
  payload=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$BOX_HOST" "sudo bash -s" <<SCRIPT
set -u
VMID='$VM_ID'
for n in \$(seq 1 60); do
  MAC=\$(ps -eo args | grep -F "\$VMID" | grep -v grep | grep -oE 'mac=[0-9a-f:]+' | head -1 | cut -d= -f2 || true)
  if [ -n "\$MAC" ]; then
    for i in \$(seq 2 254); do ping -c1 -W1 10.0.2.\$i >/dev/null 2>&1 & done
    wait
    IP=\$(ip neigh show dev dstack-br0 | grep -i "\$MAC" | grep -oE '^10\\.0\\.[0-9]+\\.[0-9]+' | head -1 || true)
    if [ -n "\$IP" ]; then
      P=\$(curl -sfm 5 "http://\$IP:9092/registration-calldata" || true)
      if printf '%s' "\$P" | jq -e '.member and .calldata' >/dev/null 2>&1; then
        printf '%s\n' "\$P"; exit 0
      fi
    fi
  fi
  sleep 5
done
exit 1
SCRIPT
  ) || die "registration helper did not produce calldata"
  printf '%s\n' "$payload" > "$tmp"
  jq -e '.member and .calldata' "$tmp" >/dev/null || die "captured payload is malformed"
  mv "$tmp" "$PAYLOAD_FILE"
  chmod 600 "$PAYLOAD_FILE"
  log "✔ captured registration proof -> $PAYLOAD_FILE"
}

verify_proof() {
  [ -s "$PAYLOAD_FILE" ] || die "missing captured registration payload: $PAYLOAD_FILE"
  local calldata decoded code_id code_member kms_sig app_pub purpose member prefix preimage digest
  local sig_r sig_s sig_v v_word recover_input recover_output recovered
  calldata=$(payload_calldata) || die "payload has no calldata"
  member=$(payload_member) || die "payload has no member"
  [ "${member,,}" = "${X,,}" ] || die "payload member $member != state member $X"
  decoded=$(cast calldata-decode "$REGISTER_SIG" "$calldata" --json) || die "cannot decode registration calldata"
  code_id=$(jq -er '.[0][0]' <<<"$decoded")
  kms_sig=$(jq -er '.[0][4]' <<<"$decoded")
  app_pub=$(jq -er '.[0][6]' <<<"$decoded")
  purpose=$(jq -er '.[0][7]' <<<"$decoded")
  [ "$purpose" = ethereum ] || die "unexpected proof purpose: $purpose"
  code_member=${code_id:0:42}
  [ "${code_member,,}" = "${X,,}" ] || die "codeId is not member app_id: $code_id"
  [ "${code_id:42}" = 000000000000000000000000 ] || die "codeId padding is non-zero: $code_id"
  prefix=$(cast from-ascii 'dstack-kms-issued:')
  preimage=$(cast concat-hex "$prefix" "$code_member" "$app_pub")
  digest=$(cast keccak "$preimage")
  [ ${#kms_sig} -eq 132 ] || die "KMS signature is not 65 bytes"
  sig_r="0x${kms_sig:2:64}"
  sig_s="0x${kms_sig:66:64}"
  sig_v=${kms_sig:130:2}
  v_word=$(printf '%064x' "$((16#$sig_v))")
  recover_input=$(cast concat-hex "$digest" "0x$v_word" "$sig_r" "$sig_s")
  recover_output=$(cast call 0x0000000000000000000000000000000000000001 \
    --data "$recover_input" --rpc-url "$RPC_URL")
  [ ${#recover_output} -eq 66 ] || die "ecrecover precompile returned malformed output"
  recovered="0x${recover_output: -40}"
  [ "${recovered,,}" = "${EXPECTED_KMS_ROOT,,}" ] \
    || die "KMS signer $recovered != expected $EXPECTED_KMS_ROOT; refusing to allowlist"
  log "✔ proof gate: member=$member purpose=$purpose kmsSigner=$recovered digest=$digest"
}

allow_root() {
  verify_proof
  local allowed
  allowed=$(cast call "$CLUSTER" 'allowedKmsRoots(address)(bool)' "$EXPECTED_KMS_ROOT" --rpc-url "$RPC_URL")
  if [ "$allowed" = true ]; then
    log "KMS root already allowlisted: $EXPECTED_KMS_ROOT"
  else
    send_with_nonce_retry "r2host-addKmsRoot-${NODE}" "$CLUSTER" \
      'addAllowedKmsRoot(address)' "$EXPECTED_KMS_ROOT"
  fi
  [ "$(cast call "$CLUSTER" 'allowedKmsRoots(address)(bool)' "$EXPECTED_KMS_ROOT" --rpc-url "$RPC_URL")" = true ] \
    || die "KMS root allowlist write did not stick"
}

simulate_register() {
  verify_proof
  [ "$(cast call "$CLUSTER" 'allowedKmsRoots(address)(bool)' "$EXPECTED_KMS_ROOT" --rpc-url "$RPC_URL")" = true ] \
    || die "expected KMS root is not allowlisted"
  local calldata out
  calldata=$(payload_calldata)
  out=$(cast call "$CLUSTER" --data "$calldata" --from "$DEPLOYER_ADDR" --rpc-url "$RPC_URL") \
    || die "registration eth_call reverted"
  [ -n "$out" ] && [ "$out" != 0x ] || die "registration eth_call returned empty output"
  log "✔ registration simulation succeeded: $out"
}

submit_register() {
  local id calldata
  id=$(cast call "$CLUSTER" 'memberIdOf(address)(bytes32)' "$X" --rpc-url "$RPC_URL")
  if [ "$id" != "$ZERO32" ]; then
    log "member already registered: $id"
    return 0
  fi
  simulate_register
  calldata=$(payload_calldata)
  send_with_nonce_retry "r2host-direct-register-${NODE}" "$CLUSTER" --data "$calldata"
  id=$(cast call "$CLUSTER" 'memberIdOf(address)(bytes32)' "$X" --rpc-url "$RPC_URL")
  [ "$id" != "$ZERO32" ] || die "registration transaction landed but memberId is still zero"
  log "✔ member registered: $id"
}

run_node() { "$HERE/r2-host-node.sh" "$NODE" "$1"; }

case "$ACTION" in
  capture-proof) capture_proof ;;
  verify-proof) verify_proof ;;
  allow-root) allow_root ;;
  simulate-register) simulate_register ;;
  submit-register) submit_register ;;
  clean-roll) run_node update ;;
  verify) run_node verify ;;
  health) run_node verify-health ;;
  storage) run_node verify-s3; run_node verify-r2 ;;
  isolation) run_node verify-isolation ;;
  *) die "unknown recovery action: $ACTION" ;;
esac
