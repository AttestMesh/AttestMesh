#!/usr/bin/env python3
"""Box-side dstack helper for the PitchRotator MCP Path-A node."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/opt/dstack-mcp")
import mcp_dstack as m  # noqa: E402

NAME = os.environ.get("BOX_NAME", "pitchrotator-mcp")
COMPOSE_PATH = os.environ.get("BOX_COMPOSE", "/tmp/pitchrotator-mcp-node.yaml")
VCPU = int(os.environ.get("BOX_VCPU", "2"))
MEM = int(os.environ.get("BOX_MEM", "4096"))
DISK = int(os.environ.get("BOX_DISK", "40"))
PORTS = json.loads(os.environ.get("BOX_PORTS", "[]"))
NET_MODE = os.environ.get("BOX_NET_MODE", "bridge").strip().lower() or "bridge"
GATEWAY_ENABLED = os.environ.get("BOX_GATEWAY_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

# Every value is sealed by dstack. The compose only contains ${NAME} references.
ENV_KEYS = [
    "CHAIN_ID",
    "RPC_URL",
    "BUNDLER_URL",
    "GAS_POLICY_ID",
    "INDEXER_REGISTRY_ADDR",
    "GATEWAY_DOMAIN",
    "OPENROUTER_API_KEY",
    "DSTACK_DOCKER_USERNAME",
    "DSTACK_DOCKER_PASSWORD",
    "DSTACK_DOCKER_REGISTRY",
]


def build_env() -> dict[str, str]:
    env = {key: os.environ.get("E_" + key, "") for key in ENV_KEYS}
    env["DSTACK_DOCKER_REGISTRY"] = env["DSTACK_DOCKER_REGISTRY"] or "ghcr.io"
    return env


def app_compose_and_hash(env_keys: list[str]) -> tuple[str, str]:
    with open(COMPOSE_PATH, encoding="utf-8") as stream:
        docker_compose = stream.read()
    app_compose = {
        "manifest_version": 2,
        "name": NAME,
        "runner": "docker-compose",
        "docker_compose_file": docker_compose,
        "kms_enabled": True,
        "gateway_enabled": GATEWAY_ENABLED,
        "local_key_provider_enabled": False,
        "key_provider_id": "",
        # Upstream currently logs MCP session IDs (bearer credentials). Keep all
        # guest logs/sysinfo private until a hardened release removes that log.
        "public_logs": False,
        "public_sysinfo": False,
        "allowed_envs": sorted(set(env_keys) | {"APP_ID"}),
        "no_instance_id": False,
        "secure_time": True,
    }
    app_compose["pre_launch_script"] = (
        'set -eu; if [ -n "$DSTACK_DOCKER_PASSWORD" ]; then '
        'echo "$DSTACK_DOCKER_PASSWORD" | docker login '
        '"${DSTACK_DOCKER_REGISTRY:-ghcr.io}" -u "$DSTACK_DOCKER_USERNAME" '
        '--password-stdin >/dev/null 2>&1; fi'
    )
    rendered = json.dumps(app_compose, indent=4, ensure_ascii=False)
    return rendered, hashlib.sha256(rendered.encode()).hexdigest()


def kms_urls() -> list[str]:
    return ["https://10.0.2.2:9101"] if NET_MODE == "bridge" else m.KMS_URLS


def stop_vm(vm_id: str) -> dict[str, object]:
    if not vm_id:
        raise SystemExit("usage: pitchrotator-mcp-node-box.py stop <vm_id>")
    before = m.vmm("GetInfo", {"id": vm_id})
    found = bool(before.get("found", True))
    if found:
        try:
            m.vmm("StopVm", {"id": vm_id})
        except Exception as exc:
            return {"vm_id": vm_id, "found": found, "stopped": False, "error": str(exc)}
    return {"vm_id": vm_id, "found": found, "stopped": found}


def payload(compose_file: str, app_id: str, env: dict[str, str], stopped: bool) -> dict[str, object]:
    return {
        "name": NAME,
        "image": "dstack-0.5.11",
        "compose_file": compose_file,
        "vcpu": VCPU,
        "memory": MEM,
        "disk_size": DISK,
        "app_id": app_id,
        "user_config": "",
        "ports": [m._parse_port(port) for port in PORTS],
        "hugepages": False,
        "pin_numa": False,
        "stopped": stopped,
        "no_tee": False,
        "kms_urls": kms_urls(),
        "networking": {"mode": NET_MODE},
        "gateway_urls": [m.GATEWAY_RPC] if GATEWAY_ENABLED else [],
        "encrypted_env": m._seal_env(env, m._app_env_encrypt_pubkey(app_id)),
    }


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "hash"
    if mode == "hash":
        print(app_compose_and_hash(ENV_KEYS)[1])
        return
    if mode == "stop":
        print(json.dumps(stop_vm(sys.argv[2] if len(sys.argv) > 2 else "")))
        return

    env = build_env()
    compose_file, compose_hash = app_compose_and_hash(list(env))
    if mode == "deploy":
        app_id = m._deploy_app_contract(compose_hash)
        result = m.vmm("CreateVm", payload(compose_file, app_id, env, True))
        print(json.dumps({
            "app_id": app_id,
            "compose_hash": compose_hash,
            "vm_id": result.get("id"),
            "gateway_url": None,
        }))
        return

    if mode == "update":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        if not app_id or not vm_id:
            raise SystemExit("usage: pitchrotator-mcp-node-box.py update <app_id> <vm_id>")
        sealed = dict(env)
        sealed["APP_ID"] = app_id
        try:
            m.vmm("StopVm", {"id": vm_id})
        except Exception:
            pass
        for _ in range(40):
            try:
                info = m.vmm("GetInfo", {"id": vm_id})
                status = str((info.get("info") or {}).get("status") or "").lower()
                if not info.get("found", True) or status.startswith(("stop", "exit")):
                    break
            except Exception:
                break
            time.sleep(2)
        upgrade = m.vmm("UpgradeApp", {
            "id": vm_id,
            "compose_file": compose_file,
            "encrypted_env": m._seal_env(sealed, m._app_env_encrypt_pubkey(app_id)),
            "update_ports": True,
            "ports": [m._parse_port(port) for port in PORTS],
            "update_kms_urls": True,
            "kms_urls": kms_urls(),
            "update_gateway_urls": True,
            "gateway_urls": [m.GATEWAY_RPC] if GATEWAY_ENABLED else [],
        })
        start = m.vmm("StartVm", {"id": vm_id})
        print(json.dumps({
            "app_id": app_id, "compose_hash": compose_hash, "vm_id": vm_id,
            "mode": "upgrade", "upgrade": upgrade, "start": start,
        }))
        return
    raise SystemExit("usage: pitchrotator-mcp-node-box.py [deploy|hash|stop|update]")


if __name__ == "__main__":
    main()
