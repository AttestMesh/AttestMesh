from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
import re
import stat
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest


def _state_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _load_node_box(monkeypatch: pytest.MonkeyPatch, vmm) -> types.ModuleType:
    root = Path(__file__).resolve().parents[3]
    fake_mcp = types.ModuleType("mcp_dstack")
    fake_mcp.KMS_URLS = []
    fake_mcp.vmm = vmm
    monkeypatch.setitem(sys.modules, "mcp_dstack", fake_mcp)
    path = root / "deploy/hindsight-node-box.py"
    spec = importlib.util.spec_from_file_location(
        f"hindsight_node_box_test_{id(vmm)}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_budget_admin_token_is_durable_before_update_preflight(
    tmp_path: Path,
) -> None:
    """Credentials and the exact logical roll survive every update crash edge."""
    root = Path(__file__).resolve().parents[3]
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    state = log_dir / "hindsight-node-crash-boundary.state"
    original = {
        "X": "0x1111111111111111111111111111111111111111",
        "H": "a" * 64,
        "VM_ID": "11111111-2222-3333-4444-555555555555",
        "UPDATED_AT": "2026-07-14T00:00:00Z",
        "UPDATE_SEQUENCE": "7",
        "CLUSTER": "0x2222222222222222222222222222222222222222",
        "MEMBER_IMPL": "0x3333333333333333333333333333333333333333",
        "GATEWAY_DOMAIN": "gateway.attestmesh.xyz",
        "TAK": "tenant-token",
        "CPK": "control-plane-token",
        "BPT": "proxy-token",
        "BAT": "",
        "MESH_IP": "10.9.0.42",
        "DB_MODE": "pg0",
        "DB_PASSWORD": "",
    }
    state.write_text("".join(f"{key}={value}\n" for key, value in original.items()))
    state.chmod(0o600)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LOGDIR": str(log_dir),
            "RPC_URL": "http://127.0.0.1:1",
            "PRIVATE_KEY": "test-only",
            "CHAIN_ID": "8453",
            "DEPLOYER_ADDR": "0x4444444444444444444444444444444444444444",
            "HINDSIGHT_CVM_RPC_URL": "http://127.0.0.1:2",
            "HINDSIGHT_ROUTER_MESH_IP": "10.9.0.8",
            "HINDSIGHT_ROUTER_API_KEY": "provider-token",
            "HINDSIGHT_RECONCILE_AMBIGUOUS_ENABLED": "1",
            "HINDSIGHT_RECONCILE_MANIFEST_SHA256": "b" * 64,
            "HINDSIGHT_RESET_PROVIDER_AUTH_CIRCUIT_ENABLED": "0",
            "HINDSIGHT_PROVIDER_AUTH_RESET_TOKEN": "",
            # Ensure the update reaches _box_run and then fails before any
            # remote/hash mutation because this isolated HOME has no GHCR key.
            "PRODUCTION_ENV_FILE": str(tmp_path / "absent-production.env"),
            "HINDSIGHT_ROUTER_API_KEY_FILE": str(tmp_path / "absent-router.json"),
        }
    )
    script = root / "deploy/hindsight-node.sh"

    def run(*args: str, run_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), "crash-boundary", *args],
            cwd=root,
            env=run_env or env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    result = run("update")

    assert result.returncode != 0
    assert "could not compute new compose_hash" in result.stderr
    persisted = _state_values(state)
    recovery_fields = {
        "RECOVERY_CONTROL_SEQUENCE",
        "RECOVERY_CONTROL_STATE_VERSION",
        "RECOVERY_CONTROL_NONCE",
        "RECOVERY_CONTROL_ROLL_SHA256",
        "RECOVERY_CONTROL_COMPOSE_HASH",
        "RECOVERY_CONTROL_VM_ID",
        "RECOVERY_CONTROL_PENDING_NONCE",
        "RECOVERY_CONTROL_PENDING_ROLL_SHA256",
        "RECOVERY_CONTROL_PENDING_COMPOSE_HASH",
        "RECOVERY_CONTROL_PENDING_VM_ID",
    }
    assert set(persisted) == set(original) | recovery_fields
    assert re.fullmatch(r"[0-9a-f]{64}", persisted["BAT"])
    for key, value in original.items():
        if key != "BAT":
            assert persisted[key] == value
    assert persisted["RECOVERY_CONTROL_SEQUENCE"] == "0"
    assert persisted["RECOVERY_CONTROL_STATE_VERSION"] == "2"
    assert persisted["RECOVERY_CONTROL_NONCE"] == ""
    assert persisted["RECOVERY_CONTROL_ROLL_SHA256"] == ""
    assert persisted["RECOVERY_CONTROL_COMPOSE_HASH"] == ""
    assert persisted["RECOVERY_CONTROL_VM_ID"] == ""
    assert re.fullmatch(
        r"[0-9a-f]{32}", persisted["RECOVERY_CONTROL_PENDING_NONCE"]
    )
    assert re.fullmatch(
        r"[0-9a-f]{64}", persisted["RECOVERY_CONTROL_PENDING_ROLL_SHA256"]
    )
    pending_nonce = persisted["RECOVERY_CONTROL_PENDING_NONCE"]
    pending_roll = persisted["RECOVERY_CONTROL_PENDING_ROLL_SHA256"]
    assert persisted["RECOVERY_CONTROL_PENDING_COMPOSE_HASH"] == ""
    assert persisted["RECOVERY_CONTROL_PENDING_VM_ID"] == ""
    state_text = state.read_text()
    assert "b" * 64 not in state_text
    assert "HINDSIGHT_RECONCILE_AMBIGUOUS_ENABLED" not in state_text
    assert "HINDSIGHT_RECONCILE_MANIFEST_SHA256" not in state_text
    assert "HINDSIGHT_PROVIDER_AUTH_RESET_TOKEN" not in state_text
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert not list(log_dir.glob("hindsight-node-crash-boundary.state.tmp.*"))

    # A retry on either side of the remote call reuses the exact pending nonce.
    retry = run("update")
    assert retry.returncode != 0
    retried = _state_values(state)
    assert retried["RECOVERY_CONTROL_PENDING_NONCE"] == pending_nonce
    assert retried["RECOVERY_CONTROL_PENDING_ROLL_SHA256"] == pending_roll

    # A different logical roll cannot replace an ambiguous pending deployment.
    mismatched_env = env.copy()
    mismatched_env["HINDSIGHT_RECONCILE_MANIFEST_SHA256"] = "c" * 64
    mismatch = run("update", run_env=mismatched_env)
    assert mismatch.returncode != 0
    assert "pending recovery-control roll fingerprint mismatch" in mismatch.stderr
    assert _state_values(state) == retried

    # Adoption cannot trust an unbound pending deployment, even with the exact
    # nonce and one-shot fingerprint.
    compose_hash = "c" * 64
    vm_id = original["VM_ID"]
    unbound = run(
        "adopt-recovery-control", pending_nonce, pending_roll, compose_hash, vm_id
    )
    assert unbound.returncode != 0
    assert _state_values(state) == retried

    # Simulate the durable pre-UpgradeApp identity binding.  The richer test
    # below drives this through the actual update path; this first test remains
    # focused on credential and pending-control persistence.
    bound = retried.copy()
    bound["RECOVERY_CONTROL_PENDING_COMPOSE_HASH"] = compose_hash
    bound["RECOVERY_CONTROL_PENDING_VM_ID"] = vm_id
    state.write_text("".join(f"{key}={value}\n" for key, value in bound.items()))
    state.chmod(0o600)

    # A fake read-only VMM endpoint supplies an exact descriptor proof without
    # letting this unit test contact or mutate the real box.
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "scp").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "ssh").write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [[ "$*" == *"hindsight-node-box.py 'hash'"* ]]; then
              printf '%s\\n' "${{FAKE_RENDERED_HASH:-{compose_hash}}}"
              exit 0
            fi
            if [[ "$*" == *"hindsight-node-box.py 'describe'"* ]]; then
              case "${{FAKE_DESCRIPTOR_MODE:-ok}}" in
                read-loss) exit 9 ;;
                stopped|boot-error) exit 8 ;;
              esac
              printf '%s\\n' '{{"identity_match":true,"vm_id":"{vm_id}","compose_hash":"{compose_hash}"}}'
              exit 0
            fi
            exit 7
            """
        )
    )
    (fake_bin / "scp").chmod(0o755)
    (fake_bin / "ssh").chmod(0o755)
    proof_env = env.copy()
    proof_env["PATH"] = f"{fake_bin}:{proof_env['PATH']}"

    # Adoption is nonce-, compose-, and VM-bound and idempotent. It models
    # UpgradeApp success followed by a process crash before local promotion.
    wrong_nonce = "d" * 32
    rejected = run(
        "adopt-recovery-control",
        wrong_nonce,
        pending_roll,
        compose_hash,
        vm_id,
        run_env=proof_env,
    )
    assert rejected.returncode != 0
    assert _state_values(state) == bound
    wrong_roll = run(
        "adopt-recovery-control",
        pending_nonce,
        "f" * 64,
        compose_hash,
        vm_id,
        run_env=proof_env,
    )
    assert wrong_roll.returncode != 0
    assert _state_values(state) == bound
    wrong_vm = run(
        "adopt-recovery-control",
        pending_nonce,
        pending_roll,
        compose_hash,
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        run_env=proof_env,
    )
    assert wrong_vm.returncode != 0
    assert _state_values(state) == bound
    changed_compose_env = proof_env.copy()
    changed_compose_env["FAKE_RENDERED_HASH"] = "e" * 64
    changed_compose = run(
        "adopt-recovery-control",
        pending_nonce,
        pending_roll,
        compose_hash,
        vm_id,
        run_env=changed_compose_env,
    )
    assert changed_compose.returncode != 0
    assert "current reviewed compose differs" in changed_compose.stderr
    assert _state_values(state) == bound
    for descriptor_mode in ("read-loss", "stopped", "boot-error"):
        bad_descriptor_env = proof_env.copy()
        bad_descriptor_env["FAKE_DESCRIPTOR_MODE"] = descriptor_mode
        bad_descriptor = run(
            "adopt-recovery-control",
            pending_nonce,
            pending_roll,
            compose_hash,
            vm_id,
            run_env=bad_descriptor_env,
        )
        assert bad_descriptor.returncode != 0
        assert _state_values(state) == bound

    adopted = run(
        "adopt-recovery-control",
        pending_nonce,
        pending_roll,
        compose_hash,
        vm_id,
        run_env=proof_env,
    )
    assert adopted.returncode == 0, adopted.stderr
    promoted = _state_values(state)
    assert promoted["RECOVERY_CONTROL_NONCE"] == pending_nonce
    assert promoted["RECOVERY_CONTROL_ROLL_SHA256"] == pending_roll
    assert promoted["RECOVERY_CONTROL_COMPOSE_HASH"] == compose_hash
    assert promoted["RECOVERY_CONTROL_VM_ID"] == vm_id
    assert promoted["RECOVERY_CONTROL_PENDING_NONCE"] == ""
    assert promoted["RECOVERY_CONTROL_PENDING_ROLL_SHA256"] == ""
    assert promoted["RECOVERY_CONTROL_SEQUENCE"] == "1"
    assert promoted["UPDATE_SEQUENCE"] == "8"
    assert promoted["H"] == compose_hash
    assert promoted["VM_ID"] == vm_id
    adopted_again = run(
        "adopt-recovery-control",
        pending_nonce,
        pending_roll,
        compose_hash,
        vm_id,
        run_env=proof_env,
    )
    assert adopted_again.returncode == 0, adopted_again.stderr
    assert _state_values(state) == promoted

    # Idempotent adoption also rechecks the canonical node H/VM fields. A local
    # contradiction is a hold, never a reason to overwrite state silently.
    contradicted = promoted.copy()
    contradicted["H"] = "f" * 64
    state.write_text(
        "".join(f"{key}={value}\n" for key, value in contradicted.items())
    )
    state.chmod(0o600)
    h_mismatch = run(
        "adopt-recovery-control",
        pending_nonce,
        pending_roll,
        compose_hash,
        vm_id,
        run_env=proof_env,
    )
    assert h_mismatch.returncode != 0
    assert "node state compose hash" in h_mismatch.stderr
    assert _state_values(state) == contradicted
    contradicted["H"] = compose_hash
    contradicted["VM_ID"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    state.write_text(
        "".join(f"{key}={value}\n" for key, value in contradicted.items())
    )
    state.chmod(0o600)
    vm_mismatch = run(
        "adopt-recovery-control",
        pending_nonce,
        pending_roll,
        compose_hash,
        vm_id,
        run_env=proof_env,
    )
    assert vm_mismatch.returncode != 0
    assert "node state VM ID" in vm_mismatch.stderr
    assert _state_values(state) == contradicted
    state.write_text("".join(f"{key}={value}\n" for key, value in promoted.items()))
    state.chmod(0o600)

    # Disabled controls must have empty secrets, and mutually enabled one-shots
    # are rejected before a new nonce is created.
    disabled_with_manifest = proof_env.copy()
    disabled_with_manifest["HINDSIGHT_RECONCILE_AMBIGUOUS_ENABLED"] = "0"
    invalid_disabled = run("update", run_env=disabled_with_manifest)
    assert invalid_disabled.returncode != 0
    assert "disabled ambiguous reconciliation requires an empty manifest" in invalid_disabled.stderr
    assert _state_values(state) == promoted
    mixed = proof_env.copy()
    mixed["HINDSIGHT_RESET_PROVIDER_AUTH_CIRCUIT_ENABLED"] = "1"
    mixed["HINDSIGHT_PROVIDER_AUTH_RESET_TOKEN"] = "e" * 64
    invalid_mixed = run("update", run_env=mixed)
    assert invalid_mixed.returncode != 0
    assert "cannot be enabled in the same roll" in invalid_mixed.stderr
    assert _state_values(state) == promoted

    # Once the prior roll is promoted, the next logical roll gets a fresh nonce.
    next_roll = run("update", run_env=proof_env)
    assert next_roll.returncode != 0
    next_state = _state_values(state)
    assert next_state["RECOVERY_CONTROL_NONCE"] == pending_nonce
    assert next_state["RECOVERY_CONTROL_PENDING_NONCE"] != pending_nonce
    assert re.fullmatch(
        r"[0-9a-f]{32}", next_state["RECOVERY_CONTROL_PENDING_NONCE"]
    )
    assert next_state["RECOVERY_CONTROL_SEQUENCE"] == "1"
    assert next_state["UPDATE_SEQUENCE"] == "8"


def test_recovery_control_nonce_is_part_of_the_sealed_box_environment() -> None:
    root = Path(__file__).resolve().parents[3]
    driver = (root / "deploy/hindsight-node.sh").read_text()
    helper = (root / "deploy/hindsight-node-box.py").read_text()
    assert (
        "E_HINDSIGHT_RECOVERY_CONTROL_NONCE='$HINDSIGHT_RECOVERY_CONTROL_NONCE'"
        in driver
    )
    assert (
        "E_HINDSIGHT_RECOVERY_CONTROL_ROLL_SHA256='$HINDSIGHT_RECOVERY_CONTROL_ROLL_SHA256'"
        in driver
    )
    assert '"HINDSIGHT_RECOVERY_CONTROL_NONCE",' in helper
    assert '"HINDSIGHT_RECOVERY_CONTROL_ROLL_SHA256",' in helper


def test_update_binds_compose_and_vm_before_upgrade_and_refuses_render_drift(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[3]
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    home = tmp_path / "home"
    (home / ".teesql").mkdir(parents=True)
    (home / ".teesql/ghcr-pull.toml").write_text(
        'username = "fixture"\ntoken = "fixture-token"\n'
    )
    vm_id = "11111111-2222-3333-4444-555555555555"
    state = log_dir / "hindsight-node-bind-boundary.state"
    original = {
        "X": "0x1111111111111111111111111111111111111111",
        "H": "a" * 64,
        "VM_ID": vm_id,
        "UPDATED_AT": "2026-07-14T00:00:00Z",
        "UPDATE_SEQUENCE": "3",
        "CLUSTER": "0x2222222222222222222222222222222222222222",
        "MEMBER_IMPL": "0x3333333333333333333333333333333333333333",
        "GATEWAY_DOMAIN": "gateway.attestmesh.xyz",
        "TAK": "tenant-token",
        "CPK": "control-plane-token",
        "BPT": "proxy-token",
        "BAT": "budget-token",
        "MESH_IP": "10.9.0.42",
        "DB_MODE": "pg0",
        "DB_PASSWORD": "",
    }
    state.write_text("".join(f"{key}={value}\n" for key, value in original.items()))
    state.chmod(0o600)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    update_calls = tmp_path / "update-calls"
    (fake_bin / "scp").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "cast").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            case "$1" in
              call) printf 'true\\n' ;;
              block-number) printf '1\\n' ;;
              calldata) printf '0x01\\n' ;;
              *) exit 8 ;;
            esac
            """
        )
    )
    (fake_bin / "ssh").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "$*" == *"sudo bash -s"* ]]; then
              cat >/dev/null
              printf '%s\\n' '{"block":2,"allowed":true}'
              exit 0
            fi
            if [[ "$*" == *"hindsight-node-box.py hash"* ]]; then
              printf '%s\\n' "$FAKE_RENDERED_HASH"
              exit 0
            fi
            if [[ "$*" == *"hindsight-node-box.py update"* ]]; then
              printf 'call\\n' >> "$FAKE_UPDATE_CALLS"
              exit 9
            fi
            exit 7
            """
        )
    )
    for name in ("scp", "sleep", "cast", "ssh"):
        (fake_bin / name).chmod(0o755)

    compose_hash = "c" * 64
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LOGDIR": str(log_dir),
            "RPC_URL": "http://127.0.0.1:1",
            "PRIVATE_KEY": "test-only",
            "CHAIN_ID": "8453",
            "DEPLOYER_ADDR": "0x4444444444444444444444444444444444444444",
            "HINDSIGHT_CVM_RPC_URL": "http://127.0.0.1:2",
            "HINDSIGHT_ROUTER_MESH_IP": "10.9.0.8",
            "HINDSIGHT_ROUTER_API_KEY": "provider-token",
            "HINDSIGHT_RECONCILE_AMBIGUOUS_ENABLED": "1",
            "HINDSIGHT_RECONCILE_MANIFEST_SHA256": "b" * 64,
            "HINDSIGHT_RESET_PROVIDER_AUTH_CIRCUIT_ENABLED": "0",
            "HINDSIGHT_PROVIDER_AUTH_RESET_TOKEN": "",
            "PRODUCTION_ENV_FILE": str(tmp_path / "absent-production.env"),
            "HINDSIGHT_ROUTER_API_KEY_FILE": str(tmp_path / "absent-router.json"),
            "FAKE_RENDERED_HASH": compose_hash,
            "FAKE_UPDATE_CALLS": str(update_calls),
        }
    )
    script = root / "deploy/hindsight-node.sh"

    first = subprocess.run(
        ["bash", str(script), "bind-boundary", "update"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert first.returncode != 0
    assert "in-place update failed" in first.stderr
    pending = _state_values(state)
    assert pending["RECOVERY_CONTROL_STATE_VERSION"] == "2"
    assert pending["RECOVERY_CONTROL_PENDING_COMPOSE_HASH"] == compose_hash
    assert pending["RECOVERY_CONTROL_PENDING_VM_ID"] == vm_id
    assert pending["H"] == original["H"]
    assert pending["VM_ID"] == vm_id
    assert pending["UPDATE_SEQUENCE"] == "3"
    assert update_calls.read_text().splitlines() == ["call"]

    # A changed local compose is rejected before another update call, even
    # though the nonce and one-shot inputs are otherwise identical.
    changed = env.copy()
    changed["FAKE_RENDERED_HASH"] = "d" * 64
    retry = subprocess.run(
        ["bash", str(script), "bind-boundary", "update"],
        cwd=root,
        env=changed,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert retry.returncode != 0
    assert "compose hash differs from the current reviewed render" in retry.stderr
    assert _state_values(state) == pending
    assert update_calls.read_text().splitlines() == ["call"]


def test_first_recovery_roll_promotes_real_legacy_state_without_sequence_fields(
    tmp_path: Path,
) -> None:
    """A successful first UpgradeApp must not crash on absent legacy counters."""
    root = Path(__file__).resolve().parents[3]
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    home = tmp_path / "home"
    (home / ".teesql").mkdir(parents=True)
    (home / ".teesql/ghcr-pull.toml").write_text(
        'username = "fixture"\ntoken = "fixture-token"\n'
    )
    vm_id = "11111111-2222-3333-4444-555555555555"
    app_id = "0x1111111111111111111111111111111111111111"
    state = log_dir / "hindsight-node-legacy-first-roll.state"
    legacy = {
        "X": app_id,
        "H": "a" * 64,
        "VM_ID": vm_id,
        "CLUSTER": "0x2222222222222222222222222222222222222222",
        "MEMBER_IMPL": "0x3333333333333333333333333333333333333333",
        "GATEWAY_DOMAIN": "gateway.attestmesh.xyz",
        "TAK": "tenant-token",
        "CPK": "control-plane-token",
        "BPT": "proxy-token",
        "BAT": "budget-token",
        "MESH_IP": "10.9.0.42",
        "DB_MODE": "pg0",
        "DB_PASSWORD": "",
    }
    assert "UPDATE_SEQUENCE" not in legacy
    assert "RECOVERY_CONTROL_SEQUENCE" not in legacy
    state.write_text("".join(f"{key}={value}\n" for key, value in legacy.items()))
    state.chmod(0o600)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    update_calls = tmp_path / "update-calls"
    (fake_bin / "scp").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "cast").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            case "$1" in
              call) printf 'true\\n' ;;
              block-number) printf '1\\n' ;;
              calldata) printf '0x01\\n' ;;
              *) exit 8 ;;
            esac
            """
        )
    )
    (fake_bin / "ssh").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "$*" == *"sudo bash -s"* ]]; then
              cat >/dev/null
              printf '%s\\n' '{"block":2,"allowed":true}'
              exit 0
            fi
            if [[ "$*" == *"hindsight-node-box.py hash"* ]]; then
              printf '%s\\n' "$FAKE_RENDERED_HASH"
              exit 0
            fi
            if [[ "$*" == *"hindsight-node-box.py update"* ]]; then
              printf 'call\\n' >> "$FAKE_UPDATE_CALLS"
              printf '{"app_id":"%s","compose_hash":"%s","vm_id":"%s","mode":"upgrade"}\\n' \\
                "$FAKE_APP_ID" "$FAKE_RENDERED_HASH" "$FAKE_VM_ID"
              exit 0
            fi
            exit 7
            """
        )
    )
    for name in ("scp", "sleep", "cast", "ssh"):
        (fake_bin / name).chmod(0o755)

    compose_hash = "c" * 64
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LOGDIR": str(log_dir),
            "RPC_URL": "http://127.0.0.1:1",
            "PRIVATE_KEY": "test-only",
            "CHAIN_ID": "8453",
            "DEPLOYER_ADDR": "0x4444444444444444444444444444444444444444",
            "HINDSIGHT_CVM_RPC_URL": "http://127.0.0.1:2",
            "HINDSIGHT_ROUTER_MESH_IP": "10.9.0.8",
            "HINDSIGHT_ROUTER_API_KEY": "provider-token",
            "HINDSIGHT_RECONCILE_AMBIGUOUS_ENABLED": "0",
            "HINDSIGHT_RECONCILE_MANIFEST_SHA256": "",
            "HINDSIGHT_RESET_PROVIDER_AUTH_CIRCUIT_ENABLED": "0",
            "HINDSIGHT_PROVIDER_AUTH_RESET_TOKEN": "",
            "PRODUCTION_ENV_FILE": str(tmp_path / "absent-production.env"),
            "HINDSIGHT_ROUTER_API_KEY_FILE": str(tmp_path / "absent-router.json"),
            "FAKE_RENDERED_HASH": compose_hash,
            "FAKE_UPDATE_CALLS": str(update_calls),
            "FAKE_APP_ID": app_id,
            "FAKE_VM_ID": vm_id,
            # Caller values cannot fill absent deployment-state counters.
            "UPDATE_SEQUENCE": "900",
            "RECOVERY_CONTROL_SEQUENCE": "901",
        }
    )
    result = subprocess.run(
        [
            "bash",
            str(root / "deploy/hindsight-node.sh"),
            "legacy-first-roll",
            "update",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    promoted = _state_values(state)
    assert update_calls.read_text().splitlines() == ["call"]
    assert promoted["H"] == compose_hash
    assert promoted["VM_ID"] == vm_id
    assert promoted["UPDATE_SEQUENCE"] == "1"
    assert promoted["RECOVERY_CONTROL_SEQUENCE"] == "1"
    assert promoted["RECOVERY_CONTROL_STATE_VERSION"] == "2"
    assert re.fullmatch(r"[0-9a-f]{32}", promoted["RECOVERY_CONTROL_NONCE"])
    assert re.fullmatch(
        r"[0-9a-f]{64}", promoted["RECOVERY_CONTROL_ROLL_SHA256"]
    )
    assert promoted["RECOVERY_CONTROL_COMPOSE_HASH"] == compose_hash
    assert promoted["RECOVERY_CONTROL_VM_ID"] == vm_id
    for key in (
        "RECOVERY_CONTROL_PENDING_NONCE",
        "RECOVERY_CONTROL_PENDING_ROLL_SHA256",
        "RECOVERY_CONTROL_PENDING_COMPOSE_HASH",
        "RECOVERY_CONTROL_PENDING_VM_ID",
    ):
        assert promoted[key] == ""


def test_legacy_partial_recovery_state_fails_closed(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    state = log_dir / "hindsight-node-legacy-partial.state"
    state.write_text(
        textwrap.dedent(
            f"""\
            X=0x1111111111111111111111111111111111111111
            H={'a' * 64}
            VM_ID=11111111-2222-3333-4444-555555555555
            UPDATE_SEQUENCE=4
            RECOVERY_CONTROL_SEQUENCE=1
            RECOVERY_CONTROL_NONCE={'b' * 32}
            RECOVERY_CONTROL_ROLL_SHA256={'c' * 64}
            CLUSTER=0x2222222222222222222222222222222222222222
            MEMBER_IMPL=0x3333333333333333333333333333333333333333
            TAK=t
            CPK=c
            BPT=p
            BAT=a
            DB_MODE=pg0
            """
        )
    )
    before = state.read_text()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LOGDIR": str(log_dir),
            "RPC_URL": "http://127.0.0.1:1",
            "PRIVATE_KEY": "test-only",
            "CHAIN_ID": "8453",
            "DEPLOYER_ADDR": "0x4444444444444444444444444444444444444444",
            "HINDSIGHT_CVM_RPC_URL": "http://127.0.0.1:2",
            "HINDSIGHT_ROUTER_MESH_IP": "10.9.0.8",
            "HINDSIGHT_ROUTER_API_KEY": "provider-token",
            "PRODUCTION_ENV_FILE": str(tmp_path / "absent-production.env"),
            "HINDSIGHT_ROUTER_API_KEY_FILE": str(tmp_path / "absent-router.json"),
        }
    )
    result = subprocess.run(
        ["bash", str(root / "deploy/hindsight-node.sh"), "legacy-partial", "update"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert "legacy recovery-control state lacks exact deployment identity" in result.stderr
    assert state.read_text() == before


def test_vmm_descriptor_proof_requires_exact_running_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_id = "0x1111111111111111111111111111111111111111"
    vm_id = "11111111-2222-3333-4444-555555555555"
    compose_file = '{"manifest_version":2,"name":"reviewed"}'
    compose_hash = hashlib.sha256(compose_file.encode()).hexdigest()
    response = {
        "found": True,
        "info": {
            "id": vm_id,
            "app_id": app_id.removeprefix("0x"),
            "boot_error": "",
            "status": "running",
            "configuration": {
                "app_id": app_id.removeprefix("0x"),
                "compose_file": compose_file,
            },
        },
    }
    module = _load_node_box(monkeypatch, lambda method, body: response)
    proof = module.describe_exact_deployment(app_id, vm_id, compose_hash)
    assert proof == {
        "version": 1,
        "read_only": True,
        "found": True,
        "identity_match": True,
        "vm_id": vm_id,
        "app_id": app_id,
        "compose_hash": compose_hash,
        "status": "running",
        "boot_error": "",
    }

    invalid: list[dict[str, object]] = []
    missing = copy.deepcopy(response)
    missing["found"] = False
    invalid.append(missing)
    stopped = copy.deepcopy(response)
    stopped["info"]["status"] = "stopped"  # type: ignore[index]
    invalid.append(stopped)
    boot_error = copy.deepcopy(response)
    boot_error["info"]["boot_error"] = "failed to boot"  # type: ignore[index]
    invalid.append(boot_error)
    wrong_vm = copy.deepcopy(response)
    wrong_vm["info"]["id"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # type: ignore[index]
    invalid.append(wrong_vm)
    wrong_app = copy.deepcopy(response)
    wrong_app["info"]["app_id"] = "2" * 40  # type: ignore[index]
    invalid.append(wrong_app)
    wrong_configured_app = copy.deepcopy(response)
    wrong_configured_app["info"]["configuration"]["app_id"] = "3" * 40  # type: ignore[index]
    invalid.append(wrong_configured_app)
    wrong_compose = copy.deepcopy(response)
    wrong_compose["info"]["configuration"]["compose_file"] = "changed"  # type: ignore[index]
    invalid.append(wrong_compose)

    for value in invalid:
        module.m.vmm = lambda method, body, value=value: value
        with pytest.raises(SystemExit):
            module.describe_exact_deployment(app_id, vm_id, compose_hash)

    def read_loss(method, body):
        raise OSError("authoritative VMM read unavailable")

    module.m.vmm = read_loss
    with pytest.raises(OSError, match="authoritative VMM read unavailable"):
        module.describe_exact_deployment(app_id, vm_id, compose_hash)
