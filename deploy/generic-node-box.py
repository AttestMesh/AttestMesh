#!/usr/bin/env python3
"""Box-side deploy helper for deploy/generic-node.sh.

Deploys a generic AttestMesh workload CVM: cluster-mesh-agent sidecar plus one
operator-supplied workload image. User environment is sealed as APP_ENV_B64 and
decoded by pre_launch_script for a workload to bind-mount and source. dstack's
app-compose renderer does not support the Compose `env_file` key.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/opt/dstack-mcp")
import mcp_dstack as m  # noqa: E402

# The box MCP defaults to PublicNode, but a saturated/read-only endpoint must not
# strand on-chain app deployment. The host driver can select a separate transaction
# RPC without sealing that operator-only value into the guest.
if box_rpc := os.environ.get("E_BOX_RPC", "").strip():
    m.RPC = box_rpc

NAME = os.environ.get("BOX_NAME", "generic-node")
# A deployment may need distinct VM/state names while intentionally sharing one
# measured workload identity (for example an active-active Indexer replica pool).
# Defaulting to NAME preserves every existing generic-node compose hash.
COMPOSE_NAME = os.environ.get("BOX_COMPOSE_NAME", "").strip() or NAME
COMPOSE_PATH = os.environ.get("BOX_COMPOSE", "/tmp/generic-node.yaml")
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
    "APP_ENV_B64",
    "DSTACK_DOCKER_USERNAME",
    "DSTACK_DOCKER_PASSWORD",
    "DSTACK_DOCKER_REGISTRY",
]


def build_env() -> dict[str, str]:
    env = {key: os.environ.get("E_" + key, "") for key in ENV_KEYS}
    env["DSTACK_DOCKER_REGISTRY"] = env.get("DSTACK_DOCKER_REGISTRY") or "ghcr.io"
    return env


def app_compose_and_hash(env_keys: list[str]) -> tuple[str, str]:
    with open(COMPOSE_PATH, encoding="utf-8") as compose:
        docker_compose_file = compose.read()
    app_compose = {
        "manifest_version": 2,
        "name": COMPOSE_NAME,
        "runner": "docker-compose",
        "docker_compose_file": docker_compose_file,
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
        '-u "$DSTACK_DOCKER_USERNAME" --password-stdin; fi; '
        'rm -f /tmp/attestmesh-workload.env; '
        'if [ -n "$APP_ENV_B64" ]; then echo "$APP_ENV_B64" | base64 -d > /tmp/attestmesh-workload.env; '
        'else : > /tmp/attestmesh-workload.env; fi; chmod 0600 /tmp/attestmesh-workload.env'
    )
    rendered = json.dumps(app_compose, indent=4, ensure_ascii=False)
    return rendered, hashlib.sha256(rendered.encode()).hexdigest()


def kms_urls() -> list[str]:
    return ["https://10.0.2.2:9101"] if NET_MODE == "bridge" else m.KMS_URLS


def stop_vm(vm_id: str) -> dict[str, object]:
    if not vm_id:
        raise SystemExit("usage: generic-node-box.py stop <vm_id>")
    if len(vm_id) > 128 or not all(ch.isalnum() or ch in "._:-" for ch in vm_id):
        raise SystemExit("vm_id contains unsupported characters")
    before = m.vmm("GetInfo", {"id": vm_id})
    initial_found = before.get("found")
    if initial_found is False:
        return {"vm_id": vm_id, "found": False, "stopped": True, "status": "gone"}
    if initial_found is not True:
        raise RuntimeError(f"GetInfo returned an ambiguous found value for VM {vm_id}")

    stop_error = ""
    try:
        m.vmm("StopVm", {"id": vm_id})
    except Exception as exc:
        # Some VMM builds return an error for an already-stopping VM. Do not
        # declare success from that error: only a subsequent GetInfo terminal
        # state proves that cleanup is complete.
        stop_error = str(exc)

    attempts = int(os.environ.get("BOX_STOP_ATTEMPTS", "40"))
    poll_seconds = float(os.environ.get("BOX_STOP_POLL_SECONDS", "1"))
    if attempts <= 0 or poll_seconds < 0:
        raise RuntimeError("BOX_STOP_ATTEMPTS and BOX_STOP_POLL_SECONDS are invalid")

    last_status = "unknown"
    last_poll_error = ""
    for attempt in range(attempts):
        try:
            current = m.vmm("GetInfo", {"id": vm_id})
            current_found = current.get("found")
            if current_found is False:
                return {
                    "vm_id": vm_id,
                    "found": False,
                    "stopped": True,
                    "status": "gone",
                }
            if current_found is not True:
                last_status = "ambiguous-found"
                last_poll_error = "GetInfo found must be a JSON boolean"
            else:
                last_status = str((current.get("info") or {}).get("status") or "unknown")
                normalized = last_status.lower()
                if normalized in {"stopped", "exited", "dead"}:
                    return {
                        "vm_id": vm_id,
                        "found": True,
                        "stopped": True,
                        "status": last_status,
                    }
                last_poll_error = ""
        except Exception as exc:
            # An unreadable VMM is ambiguous, not evidence that the VM vanished.
            last_poll_error = str(exc)
        if attempt + 1 < attempts:
            time.sleep(poll_seconds)

    detail = f"last status={last_status}"
    if stop_error:
        detail += f", StopVm error={stop_error}"
    if last_poll_error:
        detail += f", GetInfo error={last_poll_error}"
    raise RuntimeError(f"could not prove VM {vm_id} stopped or disappeared ({detail})")


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
            raise SystemExit("usage: generic-node-box.py update <app_id> <vm_id>")

        env = build_env()
        compose_file, compose_hash = app_compose_and_hash(list(env.keys()))
        sealed = dict(env)
        sealed["APP_ID"] = app_id
        stop_result = stop_vm(vm_id)

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
                    "stopped": True,
                    "stop": stop_result,
                    "upgrade": upgrade,
                    "start": start,
                }
            )
        )
        return

    raise SystemExit("usage: generic-node-box.py [deploy|hash|stop <vm_id>|update <app_id> <vm_id>]")


if __name__ == "__main__":
    main()
