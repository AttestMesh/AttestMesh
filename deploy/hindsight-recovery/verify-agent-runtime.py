#!/usr/bin/env python3
"""Emit a read-only proof of the actual Agent CVM, API, and worker runtime."""

from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


def recovery_root() -> Path:
    configured = os.getenv("RECOVERY_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        root = Path(__file__).resolve().parents[2]
        if not (root / "deploy").is_dir():
            raise RuntimeError(
                "RECOVERY_ROOT is required when the verifier runs outside "
                "an AttestMesh checkout"
            )
    if not root.is_absolute():
        raise RuntimeError("RECOVERY_ROOT must be an absolute path")
    return root.resolve()


ROOT = recovery_root()
STATE = Path(
    os.getenv(
        "AGENT_NODE_STATE_FILE",
        str(ROOT / "deploy/logs/agent-session-mcp-node-agent-session-mcp.state"),
    )
)
COMPOSE = Path(
    os.getenv(
        "AGENT_COMPOSE_FILE",
        str(ROOT / "deploy/compose/agent-session-mcp-node.yaml"),
    )
)
BOX_HELPER = ROOT / "deploy/agent-session-mcp-node-box.py"
AGENT_ENV = Path.home() / ".attestmesh/agent-session-mcp.env"
RUNTIME_ENV = Path.home() / ".attestmesh/agent-session-hindsight.env"
DEFAULT_CAST_BIN = Path.home() / ".foundry/bin/cast"


class ProofError(RuntimeError):
    pass


def cast_binary() -> str:
    """Return one existing executable absolute path independent of service PATH."""
    candidate = Path(os.getenv("CAST_BIN", str(DEFAULT_CAST_BIN))).expanduser()
    if not candidate.is_absolute():
        raise ProofError("CAST_BIN must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProofError("CAST_BIN does not resolve to an existing file") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ProofError("CAST_BIN is not an executable file")
    return str(resolved)


def expected_source_sha256(build_id: str) -> str:
    """Extract the reviewed source digest carried by an immutable build ID."""
    match = re.search(r"(?:^|[@:])sha256:([0-9a-f]{64})$", build_id)
    if not match:
        raise ProofError(
            "EXPECTED_AGENT_OUTBOX_BUILD_ID must end in sha256:<64 lowercase hex>"
        )
    return match.group(1)


def env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def checked(command: list[str], *, timeout: float = 30) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise ProofError(
            f"read-only verifier command failed ({result.returncode}): "
            f"{result.stdout[-800:]}"
        )
    return result.stdout.strip()


def last_json_object_line(output: str, description: str) -> dict[str, Any]:
    """Return the last complete JSON object line from noisy command output."""
    for raw in reversed(output.splitlines()):
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ProofError(f"{description} omitted a JSON object")


def deployment_rpc_url() -> str:
    return checked(
        [
            "bash",
            "-lc",
            "source deploy/env.sh >/dev/null; printf '%s' \"$RPC_URL\"",
        ]
    )


def local_compose_hash() -> str:
    # Import only the pure renderer while stubbing its box-only dependency.
    previous = {
        key: os.environ.get(key)
        for key in ("BOX_NAME", "BOX_COMPOSE", "BOX_GATEWAY_ENABLED")
    }
    os.environ.update(
        {
            "BOX_NAME": os.getenv("AGENT_NODE_NAME", "agent-session-mcp"),
            "BOX_COMPOSE": str(COMPOSE),
            "BOX_GATEWAY_ENABLED": os.getenv(
                "AGENT_BOX_GATEWAY_ENABLED", "true"
            ),
        }
    )
    sys.modules.setdefault("mcp_dstack", types.ModuleType("mcp_dstack"))
    try:
        spec = importlib.util.spec_from_file_location(
            "agent_session_mcp_node_box_proof", BOX_HELPER
        )
        if not spec or not spec.loader:
            raise ProofError("could not load Agent compose renderer")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _, digest = module.app_compose_and_hash(
            module.ENV_KEYS + ["DSTACK_DOCKER_REGISTRY"]
        )
        return str(digest)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def outbox_image_digest() -> str:
    section = re.search(
        r"(?ms)^  hindsight-outbox:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        COMPOSE.read_text(),
    )
    if not section:
        raise ProofError("Agent compose lacks hindsight-outbox service")
    match = re.search(
        r"(?m)^    image:\s*\S+@(sha256:[0-9a-f]{64})\s*$",
        section.group("body"),
    )
    if not match:
        raise ProofError("Agent outbox image is not pinned to one digest")
    return match.group(1)


def exact_vmm_descriptor(
    response: dict[str, Any],
    *,
    vm_id: str,
    app_id: str,
    compose_hash: str,
) -> dict[str, str]:
    """Validate the canonical GetInfo schema, not incidental recursive text."""
    if response.get("found") is not True:
        raise ProofError("VMM GetInfo did not find the exact Agent VM")
    info = response.get("info")
    if not isinstance(info, dict):
        raise ProofError("VMM GetInfo omitted the Agent VM descriptor")
    if str(info.get("id") or "") != vm_id:
        raise ProofError("VMM descriptor VM ID differs from Agent state")
    if str(info.get("status") or "").lower() != "running":
        raise ProofError("VMM descriptor does not report a running Agent VM")
    if str(info.get("boot_error") or ""):
        raise ProofError("VMM descriptor reports an Agent boot error")

    expected_app = app_id.lower().removeprefix("0x")
    if str(info.get("app_id") or "").lower().removeprefix("0x") != expected_app:
        raise ProofError("VMM descriptor app ID differs from Agent state")
    configuration = info.get("configuration")
    if not isinstance(configuration, dict):
        raise ProofError("VMM descriptor omitted the measured configuration")
    if (
        str(configuration.get("app_id") or "").lower().removeprefix("0x")
        != expected_app
    ):
        raise ProofError("VMM measured configuration app ID differs from Agent state")
    compose_file = configuration.get("compose_file")
    if not isinstance(compose_file, str) or not compose_file:
        raise ProofError("VMM descriptor omitted the measured compose file")
    measured_hash = hashlib.sha256(compose_file.encode()).hexdigest()
    if measured_hash != compose_hash:
        raise ProofError("VMM measured compose hash differs from Agent state")
    return {
        "vm_id": vm_id,
        "app_id": "0x" + expected_app,
        "compose_hash": measured_hash,
        "status": "running",
        "boot_error": "",
    }


def vmm_info(vm_id: str) -> dict[str, Any]:
    box_host = os.getenv("AGENT_BOX_HOST", "ubuntu@173.231.234.133")
    box_python = os.getenv(
        "AGENT_BOX_PYTHON", "/opt/dstack-mcp/venv/bin/python"
    )
    remote_code = (
        "import json,sys;"
        "sys.path.insert(0,'/opt/dstack-mcp');"
        "import mcp_dstack as m;"
        f"print(json.dumps(m.vmm('GetInfo',{{'id':{vm_id!r}}}),sort_keys=True))"
    )
    output = checked(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            box_host,
            "sudo " + shlex.quote(box_python) + " -c " + shlex.quote(remote_code),
        ],
        timeout=20,
    )
    return last_json_object_line(output, "VMM GetInfo")


def worker_connection_kwargs(
    password: str, ports: tuple[int, ...]
) -> dict[str, Any]:
    """Select the primary while forcing every verifier transaction read-only."""
    return {
        "host": ",".join("127.0.0.1" for _ in ports),
        "port": ",".join(str(value) for value in ports),
        "user": "agent_sessions",
        "password": password,
        "dbname": "agent_sessions",
        # `read-write` examines transaction_read_only and rejects the very
        # sessions made safe by the startup option below. `primary` selects on
        # recovery role instead, preserving HA routing without write authority.
        "target_session_attrs": "primary",
        "connect_timeout": 2,
        "options": "-c default_transaction_read_only=on",
        "row_factory": dict_row,
    }


def worker_runtime() -> tuple[dict[str, Any], float]:
    secrets = env_file(AGENT_ENV)
    ports = tuple(
        int(value)
        for value in os.getenv("PG_LOCAL_PORTS", "55439,55440,55441").split(",")
    )
    with psycopg.connect(
        **worker_connection_kwargs(secrets["APP_DB_PASSWORD"], ports)
    ) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT circuit_open,reason,extract(epoch FROM updated_at) AS updated_at
               FROM hindsight_sync_state
               WHERE bank_id='agent-sessions-worker-runtime'"""
        )
        row = cur.fetchone()
    if not row or row["circuit_open"]:
        raise ProofError("Agent worker-runtime heartbeat row is absent/invalid")
    try:
        payload = json.loads(str(row["reason"] or ""))
    except json.JSONDecodeError as exc:
        raise ProofError("Agent worker-runtime heartbeat is not canonical JSON") from exc
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if canonical != str(row["reason"]):
        raise ProofError("Agent worker-runtime heartbeat is not canonical")
    age = time.time() - float(row["updated_at"])
    maximum_age = float(os.getenv("AGENT_WORKER_HEARTBEAT_MAX_AGE_SECONDS", "90"))
    if age < -5 or age > maximum_age:
        raise ProofError("Agent worker-runtime heartbeat is stale")
    return payload, float(row["updated_at"])


def main() -> int:
    state = env_file(STATE)
    runtime = env_file(RUNTIME_ENV)
    app_id = str(state.get("X") or "")
    vm_id = str(state.get("VM_ID") or "")
    actual_compose = str(state.get("H") or "").removeprefix("0x")
    expected_compose = os.getenv("EXPECTED_AGENT_COMPOSE_HASH", "").removeprefix(
        "0x"
    )
    expected_image = os.getenv("EXPECTED_AGENT_IMAGE_DIGEST", "")
    expected_build = os.getenv("EXPECTED_AGENT_OUTBOX_BUILD_ID", "")
    expected_source = expected_source_sha256(expected_build)
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", app_id):
        raise ProofError("Agent app ID is invalid")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", vm_id):
        raise ProofError("Agent VM ID is invalid")
    rendered_compose = local_compose_hash()
    image_digest = outbox_image_digest()
    if not expected_compose or actual_compose != expected_compose:
        raise ProofError("Agent state compose hash differs from expected")
    if rendered_compose != expected_compose:
        raise ProofError("reviewed local compose does not produce expected hash")
    if not expected_image or image_digest != expected_image:
        raise ProofError("reviewed outbox image digest differs from expected")

    cast = cast_binary()
    rpc_url = deployment_rpc_url()
    cluster = str(state.get("CLUSTER") or "")
    member_id = checked(
        [
            cast,
            "call",
            cluster,
            "memberIdOf(address)(bytes32)",
            app_id,
            "--rpc-url",
            rpc_url,
        ]
    ).splitlines()[0]
    if member_id == "0x" + "0" * 64:
        raise ProofError("Agent app is not an on-chain cluster member")
    raw_ip = checked(
        [
            cast,
            "call",
            cluster,
            "meshIpOf(bytes32)(uint32)",
            member_id,
            "--rpc-url",
            rpc_url,
        ]
    )
    match = re.search(r"\d+", raw_ip)
    if not match:
        raise ProofError("Agent on-chain mesh IP is absent")
    mesh_ip = str(ipaddress.IPv4Address(int(match.group())))
    if checked(
        [
            cast,
            "call",
            cluster,
            "allowedComposeHashes(bytes32)(bool)",
            "0x" + actual_compose,
            "--rpc-url",
            rpc_url,
        ]
    ).splitlines()[0].strip() != "true":
        raise ProofError("Agent compose hash is not allowlisted")

    info = vmm_info(vm_id)
    vmm_descriptor = exact_vmm_descriptor(
        info,
        vm_id=vm_id,
        app_id=app_id,
        compose_hash=actual_compose.lower(),
    )

    mesh_target = os.getenv("RECOVERY_SSH_TARGET", "attestmesh-mesh-node")
    health_output = checked(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            mesh_target,
            f"curl -sf --max-time 8 http://{mesh_ip}:8080/healthz",
        ],
        timeout=15,
    )
    health = last_json_object_line(health_output, "Agent API health")
    if health.get("status") != "ok":
        raise ProofError("Agent API health is not ok")

    worker, worker_updated_at = worker_runtime()
    if (
        int(worker.get("version") or 0) != 2
        or worker.get("bank_id") != "agent-sessions"
        or int(worker.get("run_limit") or -1)
        != int(os.getenv("STAGE_CAP", "5142"))
        or int(worker.get("max_in_flight") or -1)
        != int(os.getenv("MAX_AGENT_ACTIVE", "2"))
        or not str(worker.get("process_start_token") or "")
        or int(worker.get("pid") or 0) <= 0
        or not expected_build
        or worker.get("image_build_id") != expected_build
        or worker.get("source_sha256") != expected_source
        or runtime.get("HINDSIGHT_OUTBOX_IMAGE_BUILD_ID") != expected_build
    ):
        raise ProofError("Agent worker-runtime settings/build proof drift")

    descriptor = {
        "app_id": app_id,
        "vm_id": vm_id,
        "image_digest": image_digest,
        "compose_hash": actual_compose,
        "run_limit": int(worker["run_limit"]),
        "max_in_flight": int(worker["max_in_flight"]),
        "outbox_build_id": expected_build,
        "outbox_source_sha256": expected_source,
        "member_id": member_id,
        "mesh_ip": mesh_ip,
        "worker_process_start_token": str(worker["process_start_token"]),
        "worker_pid": int(worker["pid"]),
        "worker_updated_at": worker_updated_at,
        "vmm_status": vmm_descriptor["status"],
    }
    proof = {
        "version": 1,
        "read_only": True,
        "verified_at": time.time(),
        "api_healthy": True,
        "vm_healthy": True,
        "app_id": app_id,
        "vm_id": vm_id,
        "image_digest": image_digest,
        "compose_hash": actual_compose,
        "run_limit": int(worker["run_limit"]),
        "max_in_flight": int(worker["max_in_flight"]),
        "outbox_build_id": expected_build,
        "outbox_source_sha256": expected_source,
        "worker_updated_at": worker_updated_at,
        "measured_descriptor": descriptor,
        "descriptor_sha256": hashlib.sha256(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    print(json.dumps(proof, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
