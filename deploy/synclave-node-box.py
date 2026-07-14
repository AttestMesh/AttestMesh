#!/usr/bin/env python3
"""Box-side deploy helper for deploy/synclave-node.sh.

Runs ON the self-hosted on-chain dstack box. Mirrors ssh-node-box.py /
matrix-node-box.py: it wraps the SAME mcp_dstack primitives the MCP tool uses to
register a stock DstackApp on Base, seal the runtime env, and CreateVm / UpgradeApp.

Three modes:
  deploy          — register a stock DstackApp + seal env + bridge CreateVm (gateway ON).
                    Prints {app_id, compose_hash, vm_id, gateway_url}.
  hash            — print only the measured compose_hash (no secrets, no chain). Used to
                    allowlist a new hash on the cluster BEFORE an in-place update.
  update <app_id> <vm_id>
                  — roll a new compose/env onto an existing app. Default = IN-PLACE,
                    DISK-PRESERVING UpgradeApp (StopVm → UpgradeApp same vm_id → StartVm)
                    so the Postgres/daemon/caddy volumes survive; the compose_hash change
                    is only an on-chain KMS auth gate, not part of the disk key. With
                    BOX_FRESH_DISK=1 (a DELIBERATE wipe) it does a fresh-disk CreateVm.

Secrets arrive via E_* env vars (passed in-memory over SSH) and are NEVER written to disk.
The app-compose built here MUST mirror the deploy path's app_compose (incl. the docker-login
pre_launch_script + APP_ID in allowed_envs); keep in sync with ssh-node-box.py.

  sudo E_RPC_URL=… … BOX_COMPOSE=/tmp/synclave-node.yaml \
    /opt/dstack-mcp/venv/bin/python synclave-node-box.py deploy
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/opt/dstack-mcp")
import mcp_dstack as m  # noqa: E402

NAME = os.environ.get("BOX_NAME", "synclave-node")
COMPOSE_PATH = os.environ.get("BOX_COMPOSE", "/tmp/synclave-node.yaml")
VCPU = int(os.environ.get("BOX_VCPU", "4"))
MEM = int(os.environ.get("BOX_MEM", "8192"))
DISK = int(os.environ.get("BOX_DISK", "60"))
PORTS = json.loads(os.environ.get("BOX_PORTS", "[]"))
# CVM networking: "bridge" (default) → routable on dstack-br0; the box haproxy SNI
# backend + the dstack gateway reach tlsproxy:443 at the CVM's bridge IP. In bridge
# mode KMS is the SLIRP alias 10.0.2.2 (RA-TLS cert SAN), reached via the host DNAT.
NET_MODE = (os.environ.get("BOX_NET_MODE", "bridge").strip().lower() or "bridge")
# Gateway ON: tenant apps at *.app.attestmesh.xyz route through the dstack gateway,
# and the CVM is reachable at <app_id>-<port>s.gateway.attestmesh.xyz. Measured into
# compose_hash (gateway_enabled).
GATEWAY_ENABLED = os.environ.get("BOX_GATEWAY_ENABLED", "true").strip().lower() in {
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
    # --- Synclave application (key NAMES measured into compose_hash, VALUES sealed;
    #     some are non-secret config, still sealed for a single measured surface) ---
    "POSTGRES_PASSWORD",
    "TEE_DAEMON_TOKEN",
    "SESSION_SECRET",
    "DATABASE_URL",
    "PRIVY_APP_ID",
    "PRIVY_APP_SECRET",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "GITHUB_OAUTH_CALLBACK_URL",
    "DAEMON_URL",
    "PUBLIC_BASE_URL",
    "APP_DOMAIN",
    "INDEXER_URL",
    "CLUSTER_NETWORKS",
    "CLUSTER_ORCHESTRATOR_URL",
    "CLUSTER_ORCHESTRATOR_TOKEN",
    "CORS_ORIGIN",
    "CONSOLE_HOST",
    # platform superadmins — view-as-user impersonation gate (value sealed, key measured)
    "PLATFORM_ADMIN_EMAILS",
    # waitlist → Telegram bot (turned-away sign-ups); both optional (unset ⇒ no telegram send)
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WAITLIST_CHAT_ID",
    # billing (Stripe) — keys are optional; every money-moving switch defaults off.
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_ALLOW_TEST_MODE",
    "BILLING_LIVE_ENABLED",
    "STRIPE_AUTOMATIC_TAX_ENABLED",
    "STRIPE_MANAGED_PAYMENTS_ENABLED",
    "BILLING_METERING_ENABLED",
    "BILLING_WORKER_INTERVAL_SEC",
    "BILLING_CATALOG_RECONCILE_INTERVAL_SEC",
    "CLOUDFLARE_API_TOKEN",
    # CF app-fronting: zone for the proxied <slug>.app records + the origin IP
    # (box haproxy) they point at. Non-secret, still sealed (one measured surface).
    "CLOUDFLARE_ZONE_ID",
    "CLOUDFLARE_ORIGIN_IP",
    "ADMIN_API_KEY",
    "LABELS_WRITE_TOKENS",
    "TLS_FULLCHAIN_B64",
    "TLS_KEY_B64",
    # --- confidential-sandboxes (sandboxd) provisioning ---
    "SANDBOX_DAEMON_URL",
    "SANDBOX_DAEMON_TOKEN",
    "SANDBOX_DEFAULT_IMAGE",
    "SANDBOX_DEFAULT_PLAN",
    # --- private-registry pull creds (ghcr.io/dmvt/* + attestmesh sidecar) ---
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
        "no_instance_id": False,  # stable per-instance disk (app_id||instance_id)
        "secure_time": False,
    }
    # Log in to the private registry inside the guest so ghcr.io/dmvt/* +
    # ghcr.io/attestmesh/* images pull. Creds arrive sealed as DSTACK_DOCKER_*.
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
        _, digest = app_compose_and_hash(ENV_KEYS + ["DSTACK_DOCKER_REGISTRY"])
        print(digest)
        return

    if mode == "update":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        if not app_id or not vm_id:
            raise SystemExit("usage: synclave-node-box.py update <app_id> <vm_id>")

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

        # Best-effort: the VM may already be stopped, or removed entirely (e.g. a
        # RemoveVm was needed to clear a stale/duplicate vsock CID). Either way the
        # fresh-disk CreateVm below reuses the app_id, so a missing vm_id is fine.
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

        # IN-PLACE, DISK-PRESERVING roll. UpgradeApp (UpdateVmRequest) has NO
        # networking field — bridge mode must already be on the VM manifest (set at
        # the original CreateVm). vcpu/memory/disk/image omitted → unchanged.
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

    raise SystemExit("usage: synclave-node-box.py [deploy|hash|update <app_id> <vm_id>]")


if __name__ == "__main__":
    main()
