from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
MODULE = ROOT / "deploy/hindsight-recovery/verify-agent-runtime.py"
SPEC = importlib.util.spec_from_file_location("verify_agent_runtime", MODULE)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verifier
SPEC.loader.exec_module(verifier)


@pytest.mark.parametrize("separator", [":", "@"])
def test_expected_source_sha_is_bound_to_build_id(separator: str) -> None:
    digest = "a" * 64
    assert (
        verifier.expected_source_sha256(f"reviewed{separator}sha256:{digest}")
        == digest
    )


@pytest.mark.parametrize(
    "build_id",
    ["", "reviewed", "reviewed:sha256:ABC", "reviewed:sha256:" + "a" * 63],
)
def test_invalid_build_id_source_sha_fails_closed(build_id: str) -> None:
    with pytest.raises(verifier.ProofError):
        verifier.expected_source_sha256(build_id)


def test_last_json_object_line_ignores_noisy_ssh_diagnostics() -> None:
    output = "\n".join(
        (
            "TLS certificate diagnostic: peer name mismatch ignored by tunnel",
            '{"status":"ok","component":"agent"}',
            "channel 4: free: direct-tcpip: listening port 0",
        )
    )
    assert verifier.last_json_object_line(output, "Agent API health") == {
        "status": "ok",
        "component": "agent",
    }


@pytest.mark.parametrize("output", ["", "warning only", "[]\nnull\n42"])
def test_last_json_object_line_fails_closed_without_an_object(output: str) -> None:
    with pytest.raises(verifier.ProofError, match="omitted a JSON object"):
        verifier.last_json_object_line(output, "Agent API health")


def test_cast_binary_uses_absolute_default_without_service_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CAST_BIN", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert verifier.cast_binary() == str(Path.home() / ".foundry/bin/cast")


def test_cast_binary_requires_an_absolute_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CAST_BIN", "relative/cast")
    with pytest.raises(verifier.ProofError, match="absolute path"):
        verifier.cast_binary()

    not_executable = tmp_path / "cast"
    not_executable.write_text("not executable")
    monkeypatch.setenv("CAST_BIN", str(not_executable))
    with pytest.raises(verifier.ProofError, match="not an executable"):
        verifier.cast_binary()


def test_worker_connection_selects_primary_with_read_only_transactions() -> None:
    values = verifier.worker_connection_kwargs(
        "secret", (55439, 55440, 55441)
    )
    assert values["host"] == "127.0.0.1,127.0.0.1,127.0.0.1"
    assert values["port"] == "55439,55440,55441"
    assert values["target_session_attrs"] == "primary"
    assert values["options"] == "-c default_transaction_read_only=on"
    assert values["password"] == "secret"


def test_exact_vmm_descriptor_accepts_only_canonical_running_identity() -> None:
    vm_id = "11111111-2222-3333-4444-555555555555"
    app_id = "0x1111111111111111111111111111111111111111"
    compose_file = '{"manifest_version":2,"name":"agent-session-mcp"}'
    compose_hash = hashlib.sha256(compose_file.encode()).hexdigest()
    response = {
        "found": True,
        "info": {
            "id": vm_id,
            "app_id": app_id.removeprefix("0x"),
            "status": "running",
            "boot_error": "",
            "configuration": {
                "app_id": app_id.removeprefix("0x"),
                "compose_file": compose_file,
            },
        },
    }
    assert verifier.exact_vmm_descriptor(
        response,
        vm_id=vm_id,
        app_id=app_id,
        compose_hash=compose_hash,
    ) == {
        "vm_id": vm_id,
        "app_id": app_id,
        "compose_hash": compose_hash,
        "status": "running",
        "boot_error": "",
    }

    invalid = []
    missing = copy.deepcopy(response)
    missing.pop("found")
    invalid.append(missing)
    absent = copy.deepcopy(response)
    absent["found"] = False
    invalid.append(absent)
    stopped = copy.deepcopy(response)
    stopped["info"]["status"] = "stopped"
    invalid.append(stopped)
    boot_error = copy.deepcopy(response)
    boot_error["info"]["boot_error"] = "measurement failed"
    invalid.append(boot_error)
    wrong_vm = copy.deepcopy(response)
    wrong_vm["info"]["id"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    invalid.append(wrong_vm)
    wrong_app = copy.deepcopy(response)
    wrong_app["info"]["app_id"] = "2" * 40
    invalid.append(wrong_app)
    wrong_configuration_app = copy.deepcopy(response)
    wrong_configuration_app["info"]["configuration"]["app_id"] = "3" * 40
    invalid.append(wrong_configuration_app)
    wrong_compose = copy.deepcopy(response)
    wrong_compose["info"]["configuration"]["compose_file"] = "changed"
    invalid.append(wrong_compose)
    nested_decoy = copy.deepcopy(response)
    nested_decoy["info"]["id"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    nested_decoy["decoy"] = {
        "vm_id": vm_id,
        "app_id": app_id,
        "compose_hash": compose_hash,
    }
    invalid.append(nested_decoy)

    for value in invalid:
        with pytest.raises(verifier.ProofError):
            verifier.exact_vmm_descriptor(
                value,
                vm_id=vm_id,
                app_id=app_id,
                compose_hash=compose_hash,
            )
