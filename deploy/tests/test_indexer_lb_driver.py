#!/usr/bin/env python3
"""Host-side fault tests for the Indexer LB shell transaction coordinator."""

import json
import hashlib
import os
import shlex
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "deploy" / "indexer-lb-node.sh"
SOURCE = DRIVER.read_text(encoding="utf-8")
HASH = "0x" + ("a" * 64)
CODE = "0x" + ("b" * 64)
PUBKEY = "0x" + ("c" * 64)
OPERATION = "1" * 64
BLOCK_HASH = "0x" + ("d" * 64)
OTHER_BLOCK_HASH = "0x" + ("e" * 64)


def between(start: str, end: str) -> str:
    offset = SOURCE.index(start)
    return SOURCE[offset : SOURCE.index(end, offset)]


PRIVATE_JSON_READER = between("_read_private_json() {", "_read_private_secret() {")
PRIVATE_STATE_FUNCTIONS = between("_ensure_lb_state_dir() {", "_state_value() {")
LOAD_GENERIC = between("_load_generic() {", "_load_lb() {")
LOAD_LB = between("_load_lb() {", "_save_lb() {")
ENSURE_SECRETS = between("_ensure_secrets() {", "_acquire_cutover_lock() {")
DISCOVER_MESH_IP = between("_discover_mesh_ip() {", "_bridge_ip_for_vm() {")
CONTROL_FUNCTIONS = between("_control_request_to() {", "_registry_owner_preflight() {")
TX_FUNCTIONS = PRIVATE_JSON_READER + between(
    "_atomic_json_write() {", "_operation_status() {"
)
TXN_READER = PRIVATE_JSON_READER + between("_txn_read() {", "_txn_update_phase() {")
RECOVER_FUNCTION = TXN_READER + between(
    "recover_transaction() {", "verify_sidecar_health() {"
)
LOCK_FUNCTION = between("_prepare_cutover_lock() {", "_state_value() {") + between(
    "_acquire_cutover_lock() {", "_require_protocol_v3_fleet_confirmation() {"
)
VALIDATORS = between("_validate_vm_id() {", "_trim() {")
STATE_SNAPSHOT = between("_backend_state() {", "_validate_vm_id() {")
ASSERT_DRAINED = between("assert_drained() {", "release_drain() {")
GENERIC_MIGRATION_BINDING = between(
    "_trusted_lb_app_for_migration() {", "_legacy_migration_needed() {"
)
MIGRATE_LB_STATE = between(
    "_migrate_legacy_lb_state() {", "_ingest_legacy_candidate_state() {"
)
INGEST_LEGACY_CANDIDATE = between(
    "_ingest_legacy_candidate_state() {", "_named_backend_state_json() {"
)
RESOLVE_SWITCH_POOL = between(
    "_resolve_switch_pool() {", "_control_request_to() {"
)


def run_bash(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script, "test", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def private_file(tmp: str, name: str) -> Path:
    ancestor = Path(tmp) / "owner-private"
    state_dir = ancestor / "state"
    ancestor.mkdir(mode=0o700)
    state_dir.mkdir(mode=0o700)
    return state_dir / name


def journal(path: Path, *, purpose: str = "forward", state: str = "published") -> None:
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "operation_id": OPERATION,
                "phase": "registry-published",
                "controller_previous": {
                    "backends": ["10.0.0.2"],
                    "pubkey": PUBKEY,
                    "code_id": CODE,
                    "cluster": "",
                    "members": [],
                    "operation_id": "",
                },
                "old_registry_snapshot": ["old", CODE, PUBKEY, "1"],
                "old_registry": {"endpoint": "old", "code_id": CODE, "pubkey": PUBKEY},
                "new_registry": {"endpoint": "new", "code_id": CODE, "pubkey": PUBKEY},
                "active_registry_tx": {
                    "purpose": purpose,
                    "nonce": 5,
                    "hash": HASH,
                    "raw_tx": "0x12",
                    "required_confirmations": 2,
                    "publish_attempts": 1,
                    "state": state,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


class DriverTransactionTests(unittest.TestCase):
    def test_pre_publish_persistence_failure_never_calls_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = private_file(tmp, "transaction.json")
            marker = Path(tmp) / "published"
            journal(state, state="signed-not-published")
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 91; }}
                log() {{ :; }}
                TXN_STATE=$1
                MARKER=$2
                {TX_FUNCTIONS}
                _txn_patch_active_registry_tx() {{ return 44; }}
                cast() {{
                  if [ "$1" = keccak ]; then printf '%s\n' {HASH}; return 0; fi
                  if [ "$1" = publish ]; then : >"$MARKER"; return 0; fi
                  return 1
                }}
                _publish_active_registry_tx || true
                printf 'UNREACHABLE\n'
                """
            )
            result = run_bash(script, str(state), str(marker))
            self.assertEqual(result.returncode, 91, result.stderr)
            self.assertFalse(marker.exists())
            self.assertNotIn("UNREACHABLE", result.stdout)

    def test_signed_transaction_install_failure_cannot_report_success(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            die() {{ printf '%s\n' "$*" >&2; exit 92; }}
            log() {{ :; }}
            TXN_STATE=/unused
            INDEXER_LB_TX_CONFIRMATIONS=2
            DEPLOYER_ADDR=0x{'1' * 40}
            REGISTRY=0x{'2' * 40}
            RPC_URL=http://rpc.invalid
            PRIVATE_KEY=0x{'3' * 64}
            CHAIN_ID=1
            {TX_FUNCTIONS}
            _registry_owner_preflight() {{ :; }}
            _txn_install_registry_tx() {{ return 45; }}
            cast() {{
              case "$1" in
                nonce) printf '5\n' ;;
                block-number) printf '100\n' ;;
                mktx) printf '0x12\n' ;;
                keccak) printf '%s\n' {HASH} ;;
                *) return 1 ;;
              esac
            }}
            _build_registry_tx forward endpoint {CODE} {PUBKEY} || true
            printf 'UNREACHABLE\n'
            """
        )
        result = run_bash(script)
        self.assertEqual(result.returncode, 92, result.stderr)
        self.assertNotIn("UNREACHABLE", result.stdout)

    def test_replacement_depth_and_reorg_reset_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = private_file(tmp, "transaction.json")
            journal(state)
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 93; }}
                log() {{ :; }}
                TXN_STATE=$1
                RPC_URL=http://rpc.invalid
                DEPLOYER_ADDR=0x{'1' * 40}
                HEAD=100 FINALIZED=5 LATEST=6 PENDING=6
                {TX_FUNCTIONS}
                cast() {{
                  local command=$1 block=""; shift
                  case "$command" in
                    receipt|tx) return 1 ;;
                    block-number) printf '%s\n' "$HEAD" ;;
                    nonce)
                      while [ "$#" -gt 0 ]; do
                        if [ "$1" = --block ]; then block=$2; break; fi
                        shift
                      done
                      case "$block" in
                        finalized) printf '%s\n' "$FINALIZED" ;;
                        latest) printf '%s\n' "$LATEST" ;;
                        pending) printf '%s\n' "$PENDING" ;;
                        *) return 1 ;;
                      esac ;;
                    *) return 1 ;;
                  esac
                }}
                [ "$(_classify_active_registry_tx)" = pending-finality ]
                [ "$(jq -r .active_registry_tx.replacement_observed_block "$TXN_STATE")" = 100 ]
                LATEST=5 PENDING=5
                [ "$(_classify_active_registry_tx)" = unseen ]
                [ "$(jq -r .active_registry_tx.replacement_observed_block "$TXN_STATE")" = null ]
                HEAD=105 FINALIZED=6 LATEST=6 PENDING=6
                [ "$(_classify_active_registry_tx)" = pending-finality ]
                [ "$(jq -r .active_registry_tx.replacement_observed_block "$TXN_STATE")" = 105 ]
                HEAD=106
                [ "$(_classify_active_registry_tx)" = replaced ]
                """
            )
            result = run_bash(script, str(state))
            self.assertEqual(result.returncode, 0, result.stderr)
            resolved = json.loads(state.read_text(encoding="utf-8"))["active_registry_tx"]
            self.assertEqual(resolved["resolution"], "replaced")
            self.assertEqual(resolved["replacement_confirmations"], 2)

    def test_lost_publish_response_republishes_exact_raw_then_resolves_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = private_file(tmp, "transaction.json")
            count = Path(tmp) / "publish-count"
            journal(state, state="signed-not-published")
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 94; }}
                log() {{ :; }}
                TXN_STATE=$1 PUBLISH_COUNT=$2
                RPC_URL=http://rpc.invalid
                DEPLOYER_ADDR=0x{'1' * 40}
                MODE=lost
                {TX_FUNCTIONS}
                cast() {{
                  case "$1" in
                    keccak) printf '%s\n' {HASH} ;;
                    publish)
                      printf x >>"$PUBLISH_COUNT"
                      if [ "$MODE" = lost ]; then return 28; fi
                      printf '%s\n' {HASH} ;;
                    receipt)
                      [ "$MODE" = receipt ] || return 1
                      printf '{{"blockNumber":"0x64","blockHash":"{BLOCK_HASH}","status":"0x1"}}\n' ;;
                    block-number) printf '101\n' ;;
                    block)
                      if [ "$2" = finalized ]; then
                        printf '{{"number":"0x64"}}\n'
                      else
                        printf '{{"hash":"{BLOCK_HASH}"}}\n'
                      fi ;;
                    tx) return 1 ;;
                    *) return 1 ;;
                  esac
                }}
                _rpc_publish_raw() {{
                  printf x >>"$PUBLISH_COUNT"
                  if [ "$MODE" = lost ]; then return 28; fi
                  printf '%s\n' {HASH}
                }}
                _publish_active_registry_tx
                [ "$(jq -r .active_registry_tx.state "$TXN_STATE")" = publish-uncertain ]
                MODE=republish
                _publish_active_registry_tx
                [ "$(wc -c <"$PUBLISH_COUNT")" = 2 ]
                MODE=receipt
                [ "$(_classify_active_registry_tx)" = success ]
                """
            )
            result = run_bash(script, str(state), str(count))
            self.assertEqual(result.returncode, 0, result.stderr)
            resolved = json.loads(state.read_text(encoding="utf-8"))["active_registry_tx"]
            self.assertEqual(resolved["resolution"], "success")

    def test_exact_receipt_waits_for_finalized_head_and_rpc_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = private_file(tmp, "transaction.json")
            journal(state)
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 98; }}
                log() {{ :; }}
                TXN_STATE=$1 RPC_URL=http://rpc.invalid
                DEPLOYER_ADDR=0x{'1' * 40}
                FINAL_MODE=unsupported FINAL_BLOCK=99 STATUS=1
                CANONICAL_HASH={OTHER_BLOCK_HASH}
                {TX_FUNCTIONS}
                cast() {{
                  case "$1" in
                    receipt) printf '{{"blockNumber":"0x64","blockHash":"{BLOCK_HASH}","status":"0x%s"}}\n' "$STATUS" ;;
                    block-number) printf '105\n' ;;
                    block)
                      [ "$FINAL_MODE" = supported ] || return 1
                      if [ "$2" = finalized ]; then
                        printf '{{"number":"0x%x"}}\n' "$FINAL_BLOCK"
                      else
                        printf '{{"hash":"%s"}}\n' "$CANONICAL_HASH"
                      fi ;;
                    *) return 1 ;;
                  esac
                }}
                [ "$(_classify_active_registry_tx)" = rpc-unavailable ]
                FINAL_MODE=supported
                [ "$(_classify_active_registry_tx)" = pending-finality ]
                [ "$(jq -r .active_registry_tx.observed_finalized_block "$TXN_STATE")" = 99 ]
                FINAL_BLOCK=100
                [ "$(_classify_active_registry_tx)" = receipt-noncanonical ]
                CANONICAL_HASH={BLOCK_HASH}
                [ "$(_classify_active_registry_tx)" = success ]
                STATUS=0
                [ "$(_classify_active_registry_tx)" = revert ]
                """
            )
            result = run_bash(script, str(state))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_pending_timeout_does_not_resolve_from_current_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = private_file(tmp, "transaction.json")
            journal(state)
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ exit 95; }}
                log() {{ :; }}
                TXN_STATE=$1 RPC_URL=http://rpc.invalid
                DEPLOYER_ADDR=0x{'1' * 40}
                INDEXER_LB_TX_WAIT_SECONDS=0
                {TX_FUNCTIONS}
                cast() {{
                  case "$1" in
                    receipt) return 1 ;;
                    tx) printf '{{}}\n' ;;
                    *) return 1 ;;
                  esac
                }}
                set +e
                outcome=$(_wait_active_registry_tx)
                rc=$?
                set -e
                [ "$rc" = 75 ]
                [ "$outcome" = pending ]
                """
            )
            result = run_bash(script, str(state))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_rollback_purpose_resolves_only_to_authoritative_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = private_file(tmp, "transaction.json")
            restored = Path(tmp) / "restored"
            journal(state, purpose="rollback", state="confirmed-success")
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 96; }}
                log() {{ :; }}
                TXN_STATE=$1 RESTORED=$2
                {RECOVER_FUNCTION}
                _load_generic() {{ :; }}; _default_cluster_env() {{ :; }}
                _ensure_secrets() {{ :; }}; _discover_mesh_ip() {{ return 0; }}
                _registry_owner_preflight() {{ :; }}
                _wait_active_registry_tx() {{ printf 'success\n'; }}
                _registry_snapshot() {{ printf '["old","{CODE}","{PUBKEY}","9"]\n'; }}
                _registry_tuple_kind() {{ printf 'old\n'; }}
                _restore_previous_and_clear() {{ : >"$RESTORED"; }}
                recover_transaction
                """
            )
            result = run_bash(script, str(state), str(restored))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(restored.exists())

    def test_same_semantic_tuple_follows_exact_forward_resolution(self) -> None:
        for classification, expected in (
            ("success", "commit"),
            ("revert", "restore"),
            ("replaced", "restore"),
        ):
            with self.subTest(classification=classification), tempfile.TemporaryDirectory() as tmp:
                state = private_file(tmp, "transaction.json")
                committed = Path(tmp) / "committed"
                restored = Path(tmp) / "restored"
                journal(state, purpose="forward", state=f"confirmed-{classification}")
                script = textwrap.dedent(
                    f"""
                    set -euo pipefail
                    die() {{ printf '%s\n' "$*" >&2; exit 99; }}
                    log() {{ :; }}
                    TXN_STATE=$1 COMMITTED=$2 RESTORED=$3
                    {RECOVER_FUNCTION}
                    _load_generic() {{ :; }}; _default_cluster_env() {{ :; }}
                    _ensure_secrets() {{ :; }}; _discover_mesh_ip() {{ return 0; }}
                    _registry_owner_preflight() {{ :; }}
                    _wait_active_registry_tx() {{ printf '%s\n' {classification}; }}
                    _registry_snapshot() {{ printf '["same","{CODE}","{PUBKEY}","9"]\n'; }}
                    _registry_tuple_kind() {{ printf 'both\n'; }}
                    _operation_status() {{ printf 'committed\n'; }}
                    _txn_update_phase() {{ :; }}
                    _finalize_recovered_transaction() {{ : >"$COMMITTED"; }}
                    _restore_previous_and_clear() {{ : >"$RESTORED"; }}
                    recover_transaction
                    """
                )
                result = run_bash(
                    script, str(state), str(committed), str(restored)
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(committed.exists(), expected == "commit")
                self.assertEqual(restored.exists(), expected == "restore")

    def test_lost_commit_response_with_unavailable_status_never_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = private_file(tmp, "transaction.json")
            rolled_back = Path(tmp) / "rolled-back"
            calls = Path(tmp) / "status-calls"
            journal(state, purpose="forward", state="confirmed-success")
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 97; }}
                log() {{ :; }}
                TXN_STATE=$1 ROLLED_BACK=$2 STATUS_CALLS=$3
                LOGDIR=$(dirname "$TXN_STATE") NODE=test
                {RECOVER_FUNCTION}
                _load_generic() {{ :; }}; _default_cluster_env() {{ :; }}
                _ensure_secrets() {{ :; }}; _discover_mesh_ip() {{ return 0; }}
                _registry_owner_preflight() {{ :; }}
                _wait_active_registry_tx() {{ printf 'success\n'; }}
                _registry_snapshot() {{ printf '["new","{CODE}","{PUBKEY}","9"]\n'; }}
                _registry_tuple_kind() {{ printf 'new\n'; }}
                _operation_status() {{
                  local n=0
                  [ ! -f "$STATUS_CALLS" ] || n=$(cat "$STATUS_CALLS")
                  n=$((n + 1)); printf '%s\n' "$n" >"$STATUS_CALLS"
                  [ "$n" = 1 ] && {{ printf 'registry-intent\n'; return 0; }}
                  return 1
                }}
                _control_post() {{ return 28; }}
                _begin_registry_rollback() {{ : >"$ROLLED_BACK"; }}
                recover_transaction
                """
            )
            result = run_bash(script, str(state), str(rolled_back), str(calls))
            self.assertEqual(result.returncode, 97, result.stderr)
            self.assertFalse(rolled_back.exists())

    def test_unfinalized_registry_tuple_cannot_resolve_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = private_file(tmp, "transaction.json")
            mutated = Path(tmp) / "data-plane-mutated"
            journal(state, purpose="forward", state="confirmed-replaced")
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 70; }}
                log() {{ :; }}
                TXN_STATE=$1 MUTATED=$2
                {RECOVER_FUNCTION}
                _load_generic() {{ :; }}; _default_cluster_env() {{ :; }}
                _ensure_secrets() {{ :; }}; _discover_mesh_ip() {{ return 0; }}
                _registry_owner_preflight() {{ :; }}
                _wait_active_registry_tx() {{ printf 'replaced\n'; }}
                _registry_snapshot() {{
                  if [ "${{1:-}}" = finalized ]; then printf '["old"]\n';
                  else printf '["new"]\n'; fi
                }}
                _registry_tuple_kind() {{
                  case "$1" in *new*) printf 'new\n' ;; *) printf 'old\n' ;; esac
                }}
                _finalize_recovered_transaction() {{ : >"$MUTATED"; }}
                _restore_previous_and_clear() {{ : >"$MUTATED"; }}
                _begin_registry_rollback() {{ : >"$MUTATED"; }}
                recover_transaction
                """
            )
            result = run_bash(script, str(state), str(mutated))
            self.assertEqual(result.returncode, 70, result.stderr)
            self.assertFalse(mutated.exists())
            self.assertIn("disagrees with finalized", result.stderr)

    def test_nonblocking_process_lock_excludes_concurrent_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = private_file(tmp, "cutover.lock")
            harness = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ exit 73; }}
                CUTOVER_LOCK=$1
                {LOCK_FUNCTION}
                _acquire_cutover_lock
                """
            )
            holder = subprocess.Popen(
                ["bash", "-c", harness + "\nprintf 'ready\\n'; read -r _", "test", str(lock)],
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                contender = run_bash(harness, str(lock))
                self.assertEqual(contender.returncode, 73)
            finally:
                assert holder.stdin is not None
                holder.stdin.write("done\n")
                holder.stdin.flush()
                holder.wait(timeout=5)

    def test_driver_rfc1918_validator_and_bounded_curl_policy(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            die() {{ exit 71; }}
            ZERO_ADDRESS=0x{'0' * 40}
            ZERO32=0x{'0' * 64}
            {VALIDATORS}
            _validate_private_ipv4 10.0.0.2 >/dev/null
            ! _validate_private_ipv4 127.0.0.1 >/dev/null 2>&1
            ! _validate_private_ipv4 0.0.0.0 >/dev/null 2>&1
            ! _validate_private_ipv4 169.254.1.1 >/dev/null 2>&1
            """
        )
        result = run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        for required in (
            "--max-filesize 65536",
            "--location --max-redirs 0",
            "--proto '=http'",
        ):
            self.assertIn(required, between("_backend_http() {", "_current_registry_cluster_count() {"))
        dispatch = SOURCE[SOURCE.rindex('case "$ACTION" in') :]
        for action in ("switch", "recover", "abort", "update", "stop", "all"):
            self.assertIn(action, dispatch)

    def test_assert_drained_rejects_proof_for_another_backend(self) -> None:
        wrong_proof = json.dumps(
            {
                "drained": True,
                "backend": "10.0.0.4",
                "operation_id": OPERATION,
                "reserved_at_operation_id": OPERATION,
                "reservation_id": "2" * 64,
                "release_token": "3" * 64,
                "active_backends": ["10.0.0.2"],
                "idempotent": False,
            }
        )
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            die() {{ printf '%s\n' "$*" >&2; exit 72; }}
            NODE=lb
            INDEXER_LB_ACTIVE_OPERATION_ID=
            {ASSERT_DRAINED}
            _backend_ip() {{ printf '10.0.0.3\n'; }}
            _control_get() {{ printf '{{"active_operation_id":"{OPERATION}"}}\n'; }}
            _control_post() {{ printf '%s\n' {shlex.quote(wrong_proof)}; }}
            assert_drained 10.0.0.3
            """
        )
        result = run_bash(script)
        self.assertEqual(result.returncode, 72)
        self.assertIn("not bound to requested backend", result.stderr)

    def test_named_backend_state_hash_snapshot_prevents_swap_and_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "private"
            lb_state_dir = Path(tmp) / "lb-private"
            log_dir = Path(tmp) / "logs"
            state_dir.mkdir(mode=0o700)
            lb_state_dir.mkdir(mode=0o700)
            log_dir.mkdir(mode=0o775)
            state = state_dir / "generic-node-worker-1.state"
            original = b"VM_ID=vm-one\nX=0x" + (b"1" * 40) + b"\n"
            state.write_bytes(original)
            state.chmod(0o600)
            expected = hashlib.sha256(original).hexdigest()
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 74; }}
                LOGDIR=$1 INDEXER_BACKEND_STATE_DIR=$2 LB_PRIVATE_STATE_DIR=$3
                {STATE_SNAPSHOT}
                snapshot=$(_snapshot_expected_backend_state worker-1 {expected})
                [ "$(dirname "$snapshot")" = "$LB_PRIVATE_STATE_DIR" ]
                _validate_backend_state_snapshot "$snapshot" {expected}
                printf 'SWAPPED\n' >"$INDEXER_BACKEND_STATE_DIR/generic-node-worker-1.state"
                [ "$(sha256sum "$snapshot" | cut -d' ' -f1)" = {expected} ]
                [ "$(_backend_state worker-1)" = "$INDEXER_BACKEND_STATE_DIR/generic-node-worker-1.state" ]
                rm -f "$snapshot"
                """
            )
            result = run_bash(script, str(log_dir), str(state_dir), str(lb_state_dir))
            self.assertEqual(result.returncode, 0, result.stderr)

            mismatch = run_bash(
                textwrap.dedent(
                    f"""
                    set -euo pipefail
                    die() {{ exit 74; }}
                    LOGDIR=$1 INDEXER_BACKEND_STATE_DIR=$2 LB_PRIVATE_STATE_DIR=$3
                    {STATE_SNAPSHOT}
                    _snapshot_expected_backend_state worker-1 {'0' * 64}
                    """
                ),
                str(log_dir),
                str(state_dir),
                str(lb_state_dir),
            )
            self.assertNotEqual(mismatch.returncode, 0)

    def test_group_writable_logs_cannot_supply_lb_state_or_redirect_control_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "lb-private"
            logs = root / "logs"
            private.mkdir(mode=0o700)
            logs.mkdir(mode=0o775)
            fake = logs / "indexer-lb-node-lb.state"
            fake.write_text("MESH_IP=10.0.0.99\n", encoding="utf-8")
            fake.chmod(0o600)
            lb_state = private / "indexer-lb-node-lb.state"
            marker = root / "secret-sent"
            common = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 75; }}
                log() {{ :; }}
                LB_STATE=$1 LOGDIR=$2 MARKER=$3
                MAX_INDEXER_BACKENDS=8
                ZERO_ADDRESS=0x{'0' * 40}
                ZERO32=0x{'0' * 64}
                {PRIVATE_STATE_FUNCTIONS}
                {VALIDATORS}
                {LOAD_LB}
                """
            )
            ignored = run_bash(
                common
                + "\n_load_lb\n[ -z \"${MESH_IP:-}\" ]\n"
                + "[ -f \"$LOGDIR/indexer-lb-node-lb.state\" ]\n",
                str(lb_state),
                str(logs),
                str(marker),
            )
            self.assertEqual(ignored.returncode, 0, ignored.stderr)

            lb_state.write_text(
                "\n".join(
                    (
                        "STATE_SCHEMA=1",
                        "UPDATED_AT=20260715T000000Z",
                        "MESH_IP=10.0.0.99",
                        "ACTIVE_BACKEND=",
                        "ACTIVE_BACKEND_NODE=",
                        "ACTIVE_BACKENDS=",
                        "ACTIVE_BACKEND_NODES=",
                        "ACTIVE_PUBKEY=",
                        "ACTIVE_CODE_ID=",
                        "ACTIVE_INDEXER_CLUSTER=",
                        "ACTIVE_MEMBER_IDS=",
                        "STABLE_ENDPOINT=",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            lb_state.chmod(0o600)
            redirected = run_bash(
                common
                + textwrap.dedent(
                    f"""
                    {DISCOVER_MESH_IP}
                    {CONTROL_FUNCTIONS}
                    _ensure_secrets() {{ INDEXER_LB_ADMIN_KEY=ilb_{'a' * 64}; }}
                    _load_generic() {{ X=0x{'1' * 40}; CLUSTER=0x{'2' * 40}; }}
                    _default_cluster_env() {{ :; }}
                    _member_mesh_ip() {{ printf '10.0.0.2\n'; }}
                    _save_lb() {{ :; }}
                    ssh_mesh() {{ : >"$MARKER"; }}
                    _control_get /active
                    """
                ),
                str(lb_state),
                str(logs),
                str(marker),
            )
            self.assertEqual(redirected.returncode, 75, redirected.stderr)
            self.assertIn("differs from its on-chain cluster binding", redirected.stderr)
            self.assertFalse(marker.exists(), "control secret reached substituted MESH_IP")

    def test_generic_state_is_single_read_from_private_directory_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            logs = root / "logs"
            private.mkdir(mode=0o700)
            logs.mkdir(mode=0o775)
            state = private / "generic-node-lb.state"
            fake = logs / "generic-node-lb.state"

            def generic_state(app: str) -> str:
                return "\n".join(
                    (
                        "STATE_SCHEMA=2",
                        "UPDATED_AT=20260715T000000Z",
                        "STATE_PHASE=registered",
                        f"X={app}",
                        f"H=0x{'3' * 64}",
                        "VM_ID=vm-lb",
                        f"CLUSTER=0x{'4' * 40}",
                        f"MEMBER_IMPL=0x{'5' * 40}",
                        f"KMS_ROOT=0x{'6' * 40}",
                        "GATEWAY_DOMAIN=gateway.attestmesh.xyz",
                        f"GUEST_CONFIG_SHA256={'7' * 64}",
                    )
                ) + "\n"

            state.write_text(generic_state("0x" + ("1" * 40)), encoding="utf-8")
            fake.write_text(generic_state("0x" + ("9" * 40)), encoding="utf-8")
            state.chmod(0o600)
            fake.chmod(0o600)
            harness = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 78; }}
                GENERIC_STATE=$1
                GATEWAY_DOMAIN=gateway.attestmesh.xyz
                CLUSTER=0x{'4' * 40}
                MEMBER_IMPL=0x{'5' * 40}
                KMS_ROOT=0x{'6' * 40}
                ZERO_ADDRESS=0x{'0' * 40}
                ZERO32=0x{'0' * 64}
                {PRIVATE_STATE_FUNCTIONS}
                {VALIDATORS}
                {LOAD_GENERIC}
                _load_generic
                [ "$X" = 0x{'1' * 40} ]
                """
            )
            loaded = run_bash(harness, str(state))
            self.assertEqual(loaded.returncode, 0, loaded.stderr)

            state.unlink()
            state.symlink_to(fake)
            replaced = run_bash(harness, str(state))
            self.assertEqual(replaced.returncode, 78, replaced.stderr)
            self.assertIn("could not safely read LB node state", replaced.stderr)

    def test_legacy_control_key_migrates_atomically_without_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "lb-private"
            legacy_dir = root / "legacy-private"
            state_dir.mkdir(mode=0o700)
            legacy_dir.mkdir(mode=0o700)
            secret = "ilb_" + ("b" * 64)
            legacy = legacy_dir / "indexer-lb.env"
            current = state_dir / "control.env"
            legacy.write_text(
                "# legacy controller key\nINDEXER_LB_ADMIN_KEY=" + secret + "\n",
                encoding="utf-8",
            )
            legacy.chmod(0o600)
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 79; }}
                log() {{ :; }}
                LB_PRIVATE_STATE_DIR=$1 SECRETS_FILE=$2 LEGACY_SECRETS_FILE=$3
                SECRETS_FILE_IS_DEFAULT=1
                {PRIVATE_STATE_FUNCTIONS}
                {ENSURE_SECRETS}
                _ensure_secrets
                [ "$INDEXER_LB_ADMIN_KEY" = {secret} ]
                [ -f "$LEGACY_SECRETS_FILE" ]
                [ "$(stat -c %a "$SECRETS_FILE")" = 600 ]
                """
            )
            migrated = run_bash(script, str(state_dir), str(current), str(legacy))
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            self.assertEqual(
                current.read_text(encoding="utf-8"),
                f"INDEXER_LB_ADMIN_KEY={secret}\n",
            )

    def test_transaction_journal_rejects_symlink_mode_and_missing_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            logs = root / "logs"
            private.mkdir(mode=0o700)
            logs.mkdir(mode=0o775)
            journal_path = private / "transaction.json"
            outside = logs / "attacker.json"
            outside.write_text('{"schema":2,"operation_id":"bad"}\n', encoding="utf-8")
            outside.chmod(0o600)
            journal_path.symlink_to(outside)
            reader = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 80; }}
                TXN_STATE=$1
                {TX_FUNCTIONS}
                _txn_read
                """
            )
            symlinked = run_bash(reader, str(journal_path))
            self.assertEqual(symlinked.returncode, 80, symlinked.stderr)
            before = outside.read_bytes()
            atomic = run_bash(
                reader.replace("_txn_read\n", "_atomic_json_write \"$TXN_STATE\" '{\"schema\":2}'\n"),
                str(journal_path),
            )
            self.assertEqual(atomic.returncode, 80, atomic.stderr)
            self.assertEqual(outside.read_bytes(), before)

            journal_path.unlink()
            journal_path.write_text('{"schema":2}\n', encoding="utf-8")
            journal_path.chmod(0o644)
            bad_mode = run_bash(reader, str(journal_path))
            self.assertEqual(bad_mode.returncode, 80, bad_mode.stderr)

            journal_path.unlink()
            marker = root / "mutated"
            missing = run_bash(
                textwrap.dedent(
                    f"""
                    set -euo pipefail
                    die() {{ printf '%s\n' "$*" >&2; exit 81; }}
                    TXN_STATE=$1 MUTATED=$2
                    {RECOVER_FUNCTION}
                    _registry_snapshot() {{ : >"$MUTATED"; }}
                    recover_transaction
                    """
                ),
                str(journal_path),
                str(marker),
            )
            self.assertEqual(missing.returncode, 81, missing.stderr)
            self.assertFalse(marker.exists())

    def test_cutover_lock_rejects_symlink_outside_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            logs = root / "logs"
            private.mkdir(mode=0o700)
            logs.mkdir(mode=0o775)
            target = logs / "attacker-lock"
            target.write_text("unchanged", encoding="utf-8")
            lock = private / "cutover.lock"
            lock.symlink_to(target)
            harness = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 82; }}
                CUTOVER_LOCK=$1
                {LOCK_FUNCTION}
                _acquire_cutover_lock
                """
            )
            rejected = run_bash(harness, str(lock))
            self.assertEqual(rejected.returncode, 82, rejected.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
            self.assertIn(
                'CUTOVER_LOCK="$LB_PRIVATE_STATE_DIR/indexer-lb-${NODE}.lock"', SOURCE
            )
            self.assertNotIn(
                'INDEXER_BACKEND_STATE_DIR:-$LOGDIR', SOURCE
            )

    def test_legacy_lb_migration_requires_independent_app_binding(self) -> None:
        trusted_app = "0x" + ("1" * 40)
        swapped_app = "0x" + ("9" * 40)

        def value(app: str) -> str:
            return json.dumps(
                {
                    "UPDATED_AT": "20260715T000000Z",
                    "STATE_PHASE": "registered",
                    "X": app,
                    "H": "0x" + ("3" * 64),
                    "VM_ID": "vm-lb",
                    "CLUSTER": "0x" + ("4" * 40),
                    "MEMBER_IMPL": "0x" + ("5" * 40),
                    "KMS_ROOT": "0x" + ("6" * 40),
                    "GATEWAY_DOMAIN": "gateway.attestmesh.xyz",
                },
                separators=(",", ":"),
            )

        harness = textwrap.dedent(
            f"""
            set -euo pipefail
            die() {{ printf '%s\n' "$*" >&2; exit 83; }}
            ZERO_ADDRESS=0x{'0' * 40}
            ZERO32=0x{'0' * 64}
            CLUSTER=0x{'4' * 40}
            MEMBER_IMPL=0x{'5' * 40}
            KMS_ROOT=0x{'6' * 40}
            GATEWAY_DOMAIN=gateway.attestmesh.xyz
            INDEXER_LB_EXPECTED_APP_ID={trusted_app}
            REGISTRY=0x{'7' * 40} RPC_URL=http://rpc.invalid
            VALUE=$1
            {VALIDATORS}
            {GENERIC_MIGRATION_BINDING}
            _normalize_generic_state_json "$VALUE" 1 >/dev/null
            """
        )
        accepted = run_bash(harness, value(trusted_app))
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        swapped = run_bash(harness, value(swapped_app))
        self.assertEqual(swapped.returncode, 83, swapped.stderr)
        self.assertIn("independently pinned LB app", swapped.stderr)

    def test_legacy_candidate_ingestion_requires_independent_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            private = root / "private"
            logs.mkdir(mode=0o775)
            private.mkdir(mode=0o700)
            state = logs / "generic-node-blue.state"
            raw = (
                "UPDATED_AT=20260715T000000Z\n"
                "STATE_PHASE=registered\n"
                f"X=0x{'1' * 40}\n"
                f"H=0x{'3' * 64}\n"
                "VM_ID=vm-blue\n"
                f"CLUSTER=0x{'4' * 40}\n"
                f"MEMBER_IMPL=0x{'5' * 40}\n"
                f"KMS_ROOT=0x{'6' * 40}\n"
                "GATEWAY_DOMAIN=gateway.attestmesh.xyz\n"
            ).encode()
            state.write_bytes(raw)
            state.chmod(0o600)
            digest = hashlib.sha256(raw).hexdigest()
            harness = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 84; }}
                LOGDIR=$1 LB_PRIVATE_STATE_DIR=$2
                GATEWAY_DOMAIN=gateway.attestmesh.xyz
                ZERO_ADDRESS=0x{'0' * 40}
                ZERO32=0x{'0' * 64}
                {PRIVATE_STATE_FUNCTIONS}
                {VALIDATORS}
                {GENERIC_MIGRATION_BINDING}
                {INGEST_LEGACY_CANDIDATE}
                _ingest_legacy_candidate_state blue >/dev/null
                """
            )
            unpinned = run_bash(harness, str(logs), str(private))
            self.assertEqual(unpinned.returncode, 84, unpinned.stderr)
            self.assertIn("requires independent", unpinned.stderr)
            mismatched = run_bash(
                "INDEXER_EXPECTED_BACKEND_STATE_SHA256=" + ("0" * 64) + "\n" + harness,
                str(logs),
                str(private),
            )
            self.assertEqual(mismatched.returncode, 84, mismatched.stderr)
            self.assertIn("SHA256 mismatch", mismatched.stderr)
            accepted = run_bash(
                "INDEXER_EXPECTED_BACKEND_STATE_SHA256=" + digest + "\n" + harness,
                str(logs),
                str(private),
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            snapshot = private / "legacy-candidate-blue.state"
            self.assertTrue(snapshot.is_file())
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)

    def test_legacy_lb_routing_migration_requires_authenticated_active_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generic = root / "generic.state"
            legacy_lb = root / "legacy-lb.state"
            lb_state = root / "private-lb.state"
            marker = root / "adopted"
            generic.touch()
            legacy_lb.touch()
            app = "0x" + ("1" * 40)
            pub = "0x" + ("2" * 64)
            code = "0x" + ("3" * 64)
            legacy = json.dumps(
                {
                    "UPDATED_AT": "20260715T000000Z",
                    "MESH_IP": "10.0.0.2",
                    "ACTIVE_BACKEND": "10.0.0.3",
                    "ACTIVE_BACKEND_NODE": "blue",
                    "ACTIVE_BACKENDS": "10.0.0.3",
                    "ACTIVE_BACKEND_NODES": "blue",
                    "ACTIVE_PUBKEY": pub,
                    "ACTIVE_CODE_ID": code,
                    "ACTIVE_INDEXER_CLUSTER": "",
                    "ACTIVE_MEMBER_IDS": "",
                    "STABLE_ENDPOINT": f"https://{app[2:]}-50052.gateway.attestmesh.xyz",
                },
                separators=(",", ":"),
            )
            base_control = {
                "prepared": {},
                "active_backends": ["10.0.0.3"],
                "active_pubkey": pub,
                "active_code_id": code,
                "active_cluster": "",
                "active_members": [],
                "active_operation_id": OPERATION,
            }
            harness = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 85; }}
                log() {{ :; }}
                GENERIC_STATE=$1 LB_STATE=$2 LEGACY_LB_STATE=$3 MARKER=$4
                LEGACY_TXN_STATE=$5 TXN_STATE=$6 LEGACY_GENERIC_STATE=$7
                LEGACY_JSON=$8 CONTROL_JSON=$9
                GATEWAY_DOMAIN=gateway.attestmesh.xyz MAX_INDEXER_BACKENDS=8
                ZERO_ADDRESS=0x{'0' * 40} ZERO32=0x{'0' * 64}
                CLUSTER=0x{'4' * 40} MEMBER_IMPL=0x{'5' * 40} KMS_ROOT=0x{'6' * 40}
                {VALIDATORS}
                {MIGRATE_LB_STATE}
                _read_legacy_owned_kv_json() {{ printf '%s\n' "$LEGACY_JSON"; }}
                _read_legacy_owned_json() {{ return 1; }}
                _load_generic() {{ X={app}; CLUSTER=0x{'4' * 40}; }}
                _default_cluster_env() {{ :; }}
                _member_mesh_ip() {{ printf '10.0.0.2\n'; }}
                _control_request_to() {{ printf '%s\n' "$CONTROL_JSON"; }}
                _controller_snapshot() {{ jq -c '{{backends:.active_backends,pubkey:.active_pubkey,code_id:.active_code_id,cluster:.active_cluster,members:.active_members,operation_id:.active_operation_id}}'; }}
                _durable_private_text_write() {{ : >"$MARKER"; }}
                _load_lb() {{ :; }}
                _migrate_legacy_lb_state
                """
            )
            args = (
                str(generic),
                str(lb_state),
                str(legacy_lb),
                str(marker),
                str(root / "no-legacy-txn"),
                str(root / "no-private-txn"),
                str(root / "no-legacy-generic"),
                legacy,
            )
            mismatched_control = json.dumps(
                {**base_control, "active_backends": ["10.0.0.99"]},
                separators=(",", ":"),
            )
            mismatched = run_bash(harness, *args, mismatched_control)
            self.assertEqual(mismatched.returncode, 85, mismatched.stderr)
            self.assertIn("authenticated controller", mismatched.stderr)
            self.assertFalse(marker.exists())
            matching = run_bash(
                harness,
                *args,
                json.dumps(base_control, separators=(",", ":")),
            )
            self.assertEqual(matching.returncode, 0, matching.stderr)
            self.assertTrue(marker.exists())

    def test_switch_rejects_lb_own_bridge_address_before_candidate_probe(self) -> None:
        marker = tempfile.NamedTemporaryFile(delete=False)
        marker.close()
        os.unlink(marker.name)
        try:
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                die() {{ printf '%s\n' "$*" >&2; exit 86; }}
                VM_ID=lb-vm MESH_IP=10.0.0.9 MAX_INDEXER_BACKENDS=8
                ZERO_ADDRESS=0x{'0' * 40} ZERO32=0x{'0' * 64}
                MARKER=$1
                {RESOLVE_SWITCH_POOL}
                _bridge_ip_for_vm() {{ printf '10.0.0.2\n'; }}
                _validate_private_ipv4() {{ printf '%s\n' "$1"; }}
                _trim() {{ printf '%s\n' "$1"; }}
                _backend_ip() {{ printf '%s\n' "$1"; }}
                _backend_metadata() {{ : >"$MARKER"; }}
                _resolve_switch_pool 10.0.0.2
                """
            )
            rejected = run_bash(script, marker.name)
            self.assertEqual(rejected.returncode, 86, rejected.stderr)
            self.assertIn("own bridge address", rejected.stderr)
            self.assertFalse(Path(marker.name).exists())
        finally:
            Path(marker.name).unlink(missing_ok=True)

    def test_sensitive_control_and_raw_transaction_values_are_not_subprocess_argv(self) -> None:
        for forbidden in (
            'python3 - "$path" "$value"',
            'cast keccak "$raw"',
            '--arg raw_tx "$raw"',
            'cast publish --async "$raw"',
            'INDEXER_LB_ADMIN_KEY=$(printf',
            '-H "Authorization: Bearer $INDEXER_LB_ADMIN_KEY"',
        ):
            self.assertNotIn(forbidden, SOURCE)
        self.assertIn("printf '%s' \"$raw\" | cast keccak", SOURCE)
        self.assertIn("_rpc_publish_raw", SOURCE)


if __name__ == "__main__":
    unittest.main()
