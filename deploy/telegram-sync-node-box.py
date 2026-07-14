#!/usr/bin/env python3
"""Box-side deploy helper for deploy/telegram-sync-node.sh.

Runs ON the self-hosted on-chain dstack box. Mirrors r2-host-node-box.py: wraps
the SAME mcp_dstack primitives the MCP tool uses to register a stock DstackApp on
Base, seal the runtime env, and CreateVm / UpgradeApp.

telegram-sync-specific vs r2-host:
  - Sealed env carries the Telegram app credentials (TELEGRAM_API_ID/HASH, optional
    TG_PHONE), the pg-ha service password + full DATABASE_URL, and the Matrix/LLM
    config for the co-located admin agent. Key NAMES are measured into the
    compose_hash, VALUES are sealed.
  - gateway_enabled: False + no_instance_id: True — mesh-only node (runyard/r2-host
    pairing; dstack 0.5.11 boot-loops on no_instance_id WITH gateway). The Telethon
    session lives on a named volume; with the app-bound disk key it survives
    in-place rolls, and after a deliberate fresh-disk roll the admin agent re-auths.

Three modes:
  deploy          — register a stock DstackApp + seal env + bridge CreateVm.
                    Prints {app_id, compose_hash, vm_id, gateway_url}.
  hash            — print only the measured compose_hash (no secrets, no chain). Used to
                    allowlist a new hash on the cluster BEFORE an in-place update.
  update <app_id> <vm_id>
                  — roll a new compose/env onto an existing app. Default = IN-PLACE
                    UpgradeApp (StopVm -> UpgradeApp same vm_id -> StartVm). With
                    BOX_FRESH_DISK=1 it does a fresh-disk CreateVm (wipes the Telethon
                    session + control state — re-auth required afterwards).

Secrets arrive via E_* env vars (piped over ssh stdin, never argv) and are NEVER
written to disk. Keep the app-compose (allowed_envs, pre_launch_script,
no_instance_id) in sync with the deploy/update paths — the compose_hash must match
on every roll.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/opt/dstack-mcp")
import mcp_dstack as m  # noqa: E402

NAME = os.environ.get("BOX_NAME", "telegram-sync-node")
COMPOSE_PATH = os.environ.get("BOX_COMPOSE", "/tmp/telegram-sync-node.yaml")
VCPU = int(os.environ.get("BOX_VCPU", "2"))
MEM = int(os.environ.get("BOX_MEM", "4096"))
DISK = int(os.environ.get("BOX_DISK", "40"))
PORTS = json.loads(os.environ.get("BOX_PORTS", "[]"))
# CVM networking: "bridge" (default) -> routable on dstack-br0; the box reaches
# the sidecar health (:9090) at the CVM's bridge IP. In bridge mode KMS is the
# SLIRP alias 10.0.2.2 (RA-TLS cert SAN).
NET_MODE = (os.environ.get("BOX_NET_MODE", "bridge").strip().lower() or "bridge")
# Gateway OFF: mesh-only node, nothing served publicly. Measured into compose_hash.
GATEWAY_ENABLED = os.environ.get("BOX_GATEWAY_ENABLED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
# no_instance_id: app-bound disk key (see module docstring).
NO_INSTANCE_ID = os.environ.get("BOX_NO_INSTANCE_ID", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

ENV_KEYS = [
    # --- sidecar / on-chain infra (Path A) ---
    "CHAIN_ID",
    "RPC_URL",
    "BUNDLER_URL",
    "GAS_POLICY_ID",
    "INDEXER_REGISTRY_ADDR",
    "GATEWAY_DOMAIN",
    # --- Telegram app credentials + sync config ---
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TG_PHONE",
    "TG_DB_PASSWORD",
    "DATABASE_URL",
    "XAI_API_KEY",
    "XAI_MODEL",
    "XAI_BASE_URL",
    # --- read-only FTS MCP server (searches the archive over the mesh) ---
    "SEARCH_DB_PASSWORD",
    "SEARCH_DATABASE_URL",
    # --- admin agent: Matrix identity + pinned LLM ---
    "MATRIX_USER_ID",
    "MATRIX_PASSWORD",
    "MATRIX_ROOM_ID",
    "MATRIX_ADMIN_MXIDS",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    # --- registry pull creds (private ghcr.io/attestmesh/* images) ---
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
        "no_instance_id": NO_INSTANCE_ID,  # app-bound disk key
        "secure_time": False,
    }
    # Log in to the private registry inside the guest so ghcr.io/attestmesh/*
    # (sidecar, telegram-sync, admin agent, egress-fw, pg-ha) pull. Creds arrive
    # sealed as DSTACK_DOCKER_*; the guard makes it a no-op when absent.
    app_compose["pre_launch_script"] = (
        'if [ -n "$DSTACK_DOCKER_PASSWORD" ]; then '
        'echo "$DSTACK_DOCKER_PASSWORD" | docker login "${DSTACK_DOCKER_REGISTRY:-ghcr.io}" '
        '-u "$DSTACK_DOCKER_USERNAME" --password-stdin; fi'
    )
    rendered = json.dumps(app_compose, indent=4, ensure_ascii=False)
    return rendered, hashlib.sha256(rendered.encode()).hexdigest()


def kms_urls() -> list[str]:
    return ["https://10.0.2.2:9101"] if NET_MODE == "bridge" else m.KMS_URLS


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
                "stopped": False,
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
        _, digest = app_compose_and_hash(ENV_KEYS)
        print(digest)
        return

    if mode == "update":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        if not app_id or not vm_id:
            raise SystemExit("usage: telegram-sync-node-box.py update <app_id> <vm_id>")

        env = build_env()
        compose_file, compose_hash = app_compose_and_hash(list(env.keys()))
        sealed = dict(env)
        sealed["APP_ID"] = app_id
        fresh = os.environ.get("BOX_FRESH_DISK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        # Best-effort: the VM may already be stopped, or removed entirely. Either
        # way the fresh-disk CreateVm below reuses the app_id.
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
                info = gi.get("info") or {}
                status = str(info.get("status") or "").lower()
                if status.startswith("stop") or status.startswith("exit"):
                    stopped = True
                    break
            except Exception:
                stopped = True
                break
            time.sleep(2)

        if fresh:
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
                    "stopped": False,
                    "no_tee": False,
                    "kms_urls": kms_urls(),
                    "networking": {"mode": NET_MODE},
                    "gateway_urls": [m.GATEWAY_RPC] if GATEWAY_ENABLED else [],
                    "encrypted_env": m._seal_env(sealed, m._app_env_encrypt_pubkey(app_id)),
                },
            )
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

        # IN-PLACE roll. UpgradeApp has NO networking field — bridge mode must
        # already be on the VM manifest (set at CreateVm).
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

    raise SystemExit("usage: telegram-sync-node-box.py [deploy|hash|update <app_id> <vm_id>]")


if __name__ == "__main__":
    main()
