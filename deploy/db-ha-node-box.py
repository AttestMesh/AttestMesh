#!/usr/bin/env python3
"""Box-side deploy helper for deploy/db-ha-node.sh.

Runs on the self-hosted dstack box. The combined db-ha node co-tenants one member
each of Postgres HA, Redis HA, and ClickHouse HA behind one AttestMesh sidecar.

  register                 -> deploy a stock DstackApp contract only
  create <app_id>          -> CreateVm for a previously registered app with sealed env
  hash                     -> print the measured compose hash
  update <app_id> <vm_id>  -> StopVm/UpgradeApp/StartVm, or CreateVm with BOX_FRESH_DISK=1
  stop <vm_id> / start <vm_id>
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/opt/dstack-mcp")
import mcp_dstack as m  # noqa: E402

APP_NAME = os.environ.get("BOX_APP_NAME", "db-ha-node")
VM_NAME = os.environ.get("BOX_NAME", APP_NAME)
COMPOSE_PATH = os.environ.get("BOX_COMPOSE", "/tmp/db-ha-node.yaml")
VCPU = int(os.environ.get("BOX_VCPU", "16"))
MEM = int(os.environ.get("BOX_MEM", "32768"))
DISK = int(os.environ.get("BOX_DISK", "160"))
PORTS = json.loads(os.environ.get("BOX_PORTS", "[]"))
NET_MODE = (os.environ.get("BOX_NET_MODE", "bridge").strip().lower() or "bridge")
GATEWAY_ENABLED = os.environ.get("BOX_GATEWAY_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

ENV_KEYS = [
    "CHAIN_ID",
    "RPC_URL",
    "BUNDLER_URL",
    "GAS_POLICY_ID",
    "INDEXER_REGISTRY_ADDR",
    "GATEWAY_DOMAIN",
    "PGHA_NODE_NAME",
    "PGHA_PEERS",
    "PGHA_BOOTSTRAP",
    "PGHA_MESH_CIDR",
    "PGHA_VERIFY_PASSWORD",
    "BACKUP_ENABLED",
    "BACKUP_PREFIX",
    "BACKUP_RESTORE",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_ENDPOINT",
    "R2_BUCKET",
    "R2_REGION",
    "REDISHA_NODE_NAME",
    "REDIS_PEERS",
    "REDISHA_BOOTSTRAP",
    "REDISHA_VERIFY_PASSWORD",
    "CHHA_NODE_NAME",
    "CH_PEERS",
    "CHHA_BOOTSTRAP",
    "CHHA_VERIFY_PASSWORD",
    "MATRIX_MESH_IP",
    "MATRIX_USER_ID",
    "MATRIX_PASSWORD",
    "MATRIX_ROOM_ID",
    "MATRIX_ADMIN_MXIDS",
    "MATRIX_MENTION_ALIASES",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "DSTACK_DOCKER_USERNAME",
    "DSTACK_DOCKER_PASSWORD",
    "DSTACK_DOCKER_REGISTRY",
]


def build_env() -> dict[str, str]:
    env = {key: os.environ.get("E_" + key, "") for key in ENV_KEYS}
    env["DSTACK_DOCKER_REGISTRY"] = env.get("DSTACK_DOCKER_REGISTRY") or "ghcr.io"
    return env


def app_compose_and_hash() -> tuple[str, str]:
    app_compose = {
        "manifest_version": 2,
        "name": APP_NAME,
        "runner": "docker-compose",
        "docker_compose_file": open(COMPOSE_PATH).read(),
        "kms_enabled": True,
        "gateway_enabled": GATEWAY_ENABLED,
        "local_key_provider_enabled": False,
        "key_provider_id": "",
        "public_logs": True,
        "public_sysinfo": True,
        "allowed_envs": sorted(set(ENV_KEYS) | {"APP_ID"}),
        # Gateway-backed mesh transport needs a normal instance id. Data survives
        # day-2 rolls because update mode uses UpgradeApp on the same VM.
        "no_instance_id": False,
        "secure_time": False,
    }
    app_compose["pre_launch_script"] = (
        'if [ -n "$DSTACK_DOCKER_PASSWORD" ]; then '
        'echo "$DSTACK_DOCKER_PASSWORD" | docker login "${DSTACK_DOCKER_REGISTRY:-ghcr.io}" '
        '-u "$DSTACK_DOCKER_USERNAME" --password-stdin; fi'
    )
    rendered = json.dumps(app_compose, indent=4, ensure_ascii=False)
    return rendered, hashlib.sha256(rendered.encode()).hexdigest()


def kms_urls() -> list[str]:
    return ["https://10.0.2.2:9101"] if NET_MODE == "bridge" else m.KMS_URLS


def create_vm_params(app_id: str, compose_file: str, env: dict[str, str]) -> dict:
    return {
        "name": VM_NAME,
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
        "stopped": False,
        "no_tee": False,
        "kms_urls": kms_urls(),
        "networking": {"mode": NET_MODE},
        "gateway_urls": [m.GATEWAY_RPC] if GATEWAY_ENABLED else [],
        "encrypted_env": m._seal_env(env, m._app_env_encrypt_pubkey(app_id)),
    }


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "hash"

    if mode == "hash":
        _, digest = app_compose_and_hash()
        print(digest)
        return

    if mode == "register":
        _, compose_hash = app_compose_and_hash()
        app_id = m._deploy_app_contract(compose_hash)
        print(json.dumps({"app_id": app_id, "compose_hash": compose_hash}))
        return

    if mode == "create":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not app_id:
            raise SystemExit("usage: db-ha-node-box.py create <app_id>")
        env = build_env()
        compose_file, compose_hash = app_compose_and_hash()
        result = m.vmm("CreateVm", create_vm_params(app_id, compose_file, env))
        print(
            json.dumps(
                {
                    "app_id": app_id,
                    "compose_hash": compose_hash,
                    "vm_id": result.get("id"),
                }
            )
        )
        return

    if mode in {"stop", "start"}:
        vm_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not vm_id:
            raise SystemExit(f"usage: db-ha-node-box.py {mode} <vm_id>")
        result = m.vmm("StopVm" if mode == "stop" else "StartVm", {"id": vm_id})
        print(json.dumps({"vm_id": vm_id, "mode": mode, "result": result}))
        return

    if mode == "update":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        if not app_id or not vm_id:
            raise SystemExit("usage: db-ha-node-box.py update <app_id> <vm_id>")

        env = build_env()
        compose_file, compose_hash = app_compose_and_hash()
        sealed = dict(env)
        sealed["APP_ID"] = app_id
        fresh = os.environ.get("BOX_FRESH_DISK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        m.vmm("StopVm", {"id": vm_id})
        stopped = False
        for _ in range(40):
            try:
                info = m.vmm("GetInfo", {"id": vm_id}).get("info") or {}
                status = str(info.get("status") or "").lower()
                if status == "stopped" or status.startswith("exit"):
                    stopped = True
                    break
            except Exception:
                pass
            time.sleep(2)

        if fresh:
            result = m.vmm("CreateVm", create_vm_params(app_id, compose_file, sealed))
            print(
                json.dumps(
                    {
                        "app_id": app_id,
                        "compose_hash": compose_hash,
                        "vm_id": result.get("id"),
                        "mode": "createvm",
                        "stopped": stopped,
                    }
                )
            )
            return

        upgrade = m.vmm(
            "UpgradeApp",
            {
                "id": vm_id,
                "compose_file": compose_file,
                "encrypted_env": m._seal_env(sealed, m._app_env_encrypt_pubkey(app_id)),
                "update_ports": True,
                "ports": [m._parse_port(port) for port in PORTS],
                "update_kms_urls": True,
                "kms_urls": kms_urls(),
                "update_gateway_urls": True,
                "gateway_urls": [m.GATEWAY_RPC] if GATEWAY_ENABLED else [],
            },
        )
        start = m.vmm("StartVm", {"id": vm_id})
        print(
            json.dumps(
                {
                    "app_id": app_id,
                    "compose_hash": compose_hash,
                    "vm_id": vm_id,
                    "mode": "upgrade",
                    "stopped": stopped,
                    "upgrade": upgrade,
                    "start": start,
                }
            )
        )
        return

    raise SystemExit(
        "usage: db-ha-node-box.py [register|create <app_id>|hash|update <app_id> <vm_id>|stop <vm_id>|start <vm_id>]"
    )


if __name__ == "__main__":
    main()
