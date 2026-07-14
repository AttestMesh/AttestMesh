#!/usr/bin/env python3
"""Box-side deploy helper for deploy/pg-ha-node.sh.

Runs on the self-hosted dstack box. Unlike the single-node helpers, CVM deployment is
split so the driver can precompute every node's mesh IP BEFORE any CVM boots
(docs/specs/pg-ha.md §3):

  register            -> deploy a stock DstackApp contract only; prints {app_id, compose_hash}
  create <app_id>     -> CreateVm for a previously registered app with sealed env
  hash                -> print the measured compose hash (no chain, no secrets)
  update <app_id> <vm_id> -> disk-preserving StopVm/UpgradeApp/StartVm (BOX_FRESH_DISK=1 recreates)
  resize <vm_id>      -> resource-only ResizeVm; VM must already be stopped
  info <vm_id>        -> status/resource readback plus recent data-disk boot evidence
  stop <vm_id> / start <vm_id> -> power controls (used by verify-failover)

The compose hash is identical for every pg node: only sealed env VALUES differ per node,
and the measured allowed_envs name set is shared.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/opt/dstack-mcp")
import mcp_dstack as m  # noqa: E402

# APP_NAME is measured into app_compose (and thus the compose hash), so it MUST be identical
# across every node of one cluster. VM_NAME is only the qemu/vmm display label and may vary per
# node. Splitting them keeps the compose hash the same for pg1..pgN (see docs/specs/pg-ha.md §3).
APP_NAME = os.environ.get("BOX_APP_NAME", "pg-ha-node")
VM_NAME = os.environ.get("BOX_NAME", APP_NAME)
COMPOSE_PATH = os.environ.get("BOX_COMPOSE", "/tmp/pg-ha-node.yaml")
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
        # Keep the normal instance id: the dstack gateway registration used by the
        # AttestMesh wg-over-TCP transport rejects empty instance ids, and pg data
        # survives day-2 rolls because update mode uses UpgradeApp on the same VM.
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
            raise SystemExit("usage: pg-ha-node-box.py create <app_id>")
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
            raise SystemExit(f"usage: pg-ha-node-box.py {mode} <vm_id>")
        result = m.vmm("StopVm" if mode == "stop" else "StartVm", {"id": vm_id})
        print(json.dumps({"vm_id": vm_id, "mode": mode, "result": result}))
        return

    if mode == "info":
        vm_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not vm_id:
            raise SystemExit("usage: pg-ha-node-box.py info <vm_id>")
        response = m.vmm("GetInfo", {"id": vm_id})
        info = response.get("info") or {}
        config = info.get("configuration") or {}
        try:
            serial = m.vm_logs(vm_id=vm_id, lines=1500, channel="serial")
        except Exception:
            serial = ""
        disk_lines = [
            line.strip()
            for line in serial.splitlines()
            if "/var/volatile/dstack/persistent" in line
            or "Trying to resize filesystem" in line
            or "zpool online -e" in line
        ]
        print(
            json.dumps(
                {
                    "vm_id": vm_id,
                    "found": bool(response.get("found")),
                    "status": info.get("status"),
                    "boot_progress": info.get("boot_progress"),
                    "boot_error": info.get("boot_error"),
                    "vcpu": config.get("vcpu"),
                    "memory": config.get("memory"),
                    "disk_size": config.get("disk_size"),
                    "image": config.get("image"),
                    "disk_boot_evidence": disk_lines[-6:],
                }
            )
        )
        return

    if mode == "resize":
        vm_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not vm_id:
            raise SystemExit("usage: pg-ha-node-box.py resize <vm_id>")
        response = m.vmm("GetInfo", {"id": vm_id})
        info = response.get("info") or {}
        if not response.get("found"):
            raise SystemExit(f"VM not found: {vm_id}")
        status = str(info.get("status") or "").lower()
        if status not in {"stopped", "exited"}:
            raise SystemExit(f"VM must be stopped before resize: {vm_id} is {status or 'unknown'}")
        config = info.get("configuration") or {}
        current = {
            "vcpu": int(config.get("vcpu") or 0),
            "memory": int(config.get("memory") or 0),
            "disk_size": int(config.get("disk_size") or 0),
        }
        target = {"vcpu": VCPU, "memory": MEM, "disk_size": DISK}
        for key, value in target.items():
            if value < current[key]:
                raise SystemExit(f"refusing to shrink {key}: current={current[key]} target={value}")
        result = m.vmm("ResizeVm", {"id": vm_id, **target})
        after = (m.vmm("GetInfo", {"id": vm_id}).get("info") or {}).get("configuration") or {}
        readback = {key: int(after.get(key) or 0) for key in target}
        if readback != target:
            raise SystemExit(f"resize readback mismatch: target={target} readback={readback}")
        print(json.dumps({"vm_id": vm_id, "before": current, "target": target, "result": result}))
        return

    if mode == "update":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        if not app_id or not vm_id:
            raise SystemExit("usage: pg-ha-node-box.py update <app_id> <vm_id>")

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
        "usage: pg-ha-node-box.py [register|create <app_id>|hash|update <app_id> <vm_id>|resize <vm_id>|info <vm_id>|stop <vm_id>|start <vm_id>]"
    )


if __name__ == "__main__":
    main()
