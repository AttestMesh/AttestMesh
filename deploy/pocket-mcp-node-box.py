#!/usr/bin/env python3
"""Box-side deploy helper for deploy/pocket-mcp-node.sh.

Deploys the pocket-mcp app-tier CVM: cluster-mesh-agent sidecar + pg-provision one-shot +
the pocket-mcp image (init / mcp / poller) + socat forwarders + egress firewall. Each
app secret/config value is sealed individually as an encrypted env (dstack app-compose does
NOT support env_file) and referenced as ${VAR} in compose/pocket-mcp-node.yaml.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/opt/dstack-mcp")
import mcp_dstack as m  # noqa: E402

NAME = os.environ.get("BOX_NAME", "pocket-mcp")
COMPOSE_PATH = os.environ.get("BOX_COMPOSE", "/tmp/pocket-mcp.yaml")
VCPU = int(os.environ.get("BOX_VCPU", "2"))
MEM = int(os.environ.get("BOX_MEM", "4096"))
DISK = int(os.environ.get("BOX_DISK", "40"))
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
    "CLUSTER",
    "MEMBER_IMPL",
    # App-tier secrets/config — sealed per-var and referenced as ${VAR} in the compose
    # (the proven pattern; env_file is NOT supported by dstack app-compose).
    "APP_DB_PASSWORD",
    "DATABASE_URL",
    "HINDSIGHT_URL",
    "HINDSIGHT_MESH_IP",
    "HINDSIGHT_TOKEN",
    "HINDSIGHT_BANK",
    "HINDSIGHT_SYNC_ENABLED",
    "HINDSIGHT_OUTBOX_MAX_IN_FLIGHT",
    "HINDSIGHT_OUTBOX_TICK_SECONDS",
    "HINDSIGHT_OUTBOX_RUN_LIMIT",
    "RECALL_BACKEND",
    "EMBEDDING_URL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "POCKET_API_KEY",
    "POCKET_API_BASE",
    "POCKET_API_CIDRS",
    "MCP_AUTH_TOKEN",
    "DSTACK_DOCKER_USERNAME",
    "DSTACK_DOCKER_PASSWORD",
    "DSTACK_DOCKER_REGISTRY",
]


def build_env() -> dict[str, str]:
    env = {key: os.environ.get("E_" + key, "") for key in ENV_KEYS}
    env["DSTACK_DOCKER_REGISTRY"] = env.get("DSTACK_DOCKER_REGISTRY") or "ghcr.io"
    return env


def app_compose_and_hash(env_keys: list[str]) -> tuple[str, str]:
    app_compose = {
        "manifest_version": 2,
        "name": NAME,
        "runner": "docker-compose",
        "docker_compose_file": open(COMPOSE_PATH).read(),
        "kms_enabled": True,
        "gateway_enabled": GATEWAY_ENABLED,
        "local_key_provider_enabled": False,
        "key_provider_id": "",
        "public_logs": True,
        "public_sysinfo": True,
        "allowed_envs": sorted(set(env_keys) | {"APP_ID"}),
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


def stop_vm(vm_id: str) -> dict[str, object]:
    if not vm_id:
        raise SystemExit("usage: pocket-mcp-node-box.py stop <vm_id>")
    before = m.vmm("GetInfo", {"id": vm_id})
    found = bool(before.get("found", True))
    if found:
        try:
            m.vmm("StopVm", {"id": vm_id})
        except Exception as exc:
            # StopVm is not idempotent on every dstack build. Treat already-gone
            # or already-stopped VMs as cleanup success, but keep the error text.
            return {"vm_id": vm_id, "found": found, "stopped": False, "error": str(exc)}
    return {"vm_id": vm_id, "found": found, "stopped": found}


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "hash"

    if mode == "deploy":
        env = build_env()
        compose_file, compose_hash = app_compose_and_hash(list(env.keys()))
        app_id = m._deploy_app_contract(compose_hash)
        result = m.vmm(
            "CreateVm",
            {
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
                "stopped": True,
                "no_tee": False,
                "kms_urls": kms_urls(),
                "networking": {"mode": NET_MODE},
                "gateway_urls": [m.GATEWAY_RPC] if GATEWAY_ENABLED else [],
                "encrypted_env": m._seal_env(env, m._app_env_encrypt_pubkey(app_id)),
            },
        )
        print(
            json.dumps(
                {
                    "app_id": app_id,
                    "compose_hash": compose_hash,
                    "vm_id": result.get("id"),
                    "gateway_url": (
                        f"https://{app_id[2:].lower()}.gateway.attestmesh.xyz"
                        if GATEWAY_ENABLED
                        else None
                    ),
                }
            )
        )
        return

    if mode == "hash":
        _, digest = app_compose_and_hash(ENV_KEYS + ["DSTACK_DOCKER_REGISTRY"])
        print(digest)
        return

    if mode == "stop":
        vm_id = sys.argv[2] if len(sys.argv) > 2 else ""
        print(json.dumps(stop_vm(vm_id)))
        return

    if mode == "update":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        if not app_id or not vm_id:
            raise SystemExit("usage: pocket-mcp-node-box.py update <app_id> <vm_id>")

        env = build_env()
        compose_file, compose_hash = app_compose_and_hash(list(env.keys()))
        sealed = dict(env)
        sealed["APP_ID"] = app_id
        try:
            m.vmm("StopVm", {"id": vm_id})
        except Exception:
            pass
        stopped = False
        for _ in range(40):
            try:
                gi = m.vmm("GetInfo", {"id": vm_id})
                if not gi.get("found", True):
                    stopped = True
                    break
                status = str((gi.get("info") or {}).get("status") or "").lower()
                if status.startswith("stop") or status.startswith("exit"):
                    stopped = True
                    break
            except Exception:
                stopped = True
                break
            time.sleep(2)

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

    raise SystemExit("usage: pocket-mcp-node-box.py [deploy|hash|stop <vm_id>|update <app_id> <vm_id>]")


if __name__ == "__main__":
    main()
