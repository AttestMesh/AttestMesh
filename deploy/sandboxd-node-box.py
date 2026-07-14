#!/usr/bin/env python3
"""Box-side deploy helper for confidential-sandboxes sandboxd."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/opt/dstack-mcp")
import mcp_dstack as m  # noqa: E402

NAME = os.environ.get("BOX_NAME", "sandboxd-node")
COMPOSE_PATH = os.environ.get("BOX_COMPOSE", "/tmp/sandboxd-node.yaml")
VCPU = int(os.environ.get("BOX_VCPU", "4"))
MEM = int(os.environ.get("BOX_MEM", "8192"))
DISK = int(os.environ.get("BOX_DISK", "80"))
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
    "PEER_ENVELOPE_FALLBACK",
    "SANDBOX_DAEMON_TOKEN",
    "PUBLIC_BASE_URL",
    "ONCHAIN_CLUSTER_DIAMOND",
    "DSTACK_DOCKER_USERNAME",
    "DSTACK_DOCKER_PASSWORD",
    "DSTACK_DOCKER_REGISTRY",
]


def gateway_url(app_id: str) -> str:
    return f"https://{app_id[2:].lower()}-8080.gateway.attestmesh.xyz"


def build_env(app_id: str = "", compose_hash: str = "") -> dict[str, str]:
    env = {key: os.environ.get("E_" + key, "") for key in ENV_KEYS}
    env["DSTACK_DOCKER_REGISTRY"] = env.get("DSTACK_DOCKER_REGISTRY") or "ghcr.io"
    if app_id and not env.get("PUBLIC_BASE_URL"):
        env["PUBLIC_BASE_URL"] = gateway_url(app_id)
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
    app_compose["pre_launch_script"] = r'''set -euo pipefail
RUNSC_URL="https://storage.googleapis.com/gvisor/releases/release/20260420.0/x86_64/runsc"
RUNSC_SHA512="9efeefada7b9a7bcc21dc3a1ad3531d11dfac267808cced45d047aab742f15ecb91b2bb635dea78a4ae36817f76e5ed223b7ad80f3165f15dd24de9b0c95726f"
INSTALL_DIR="/dstack/persistent/bin"
RUNSC_BIN="$INSTALL_DIR/runsc"

sha512_file() {
  if command -v sha512sum >/dev/null 2>&1; then
    sha512sum "$1" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha512 "$1" | awk '{print $NF}'
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import hashlib,sys;print(hashlib.sha512(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
  else
    echo "no sha512 tool found" >&2
    return 1
  fi
}

mkdir -p "$INSTALL_DIR"
if [ ! -x "$RUNSC_BIN" ] || [ "$(sha512_file "$RUNSC_BIN" 2>/dev/null || true)" != "$RUNSC_SHA512" ]; then
  echo "[prelaunch] installing pinned runsc"
  curl -fsSL -o "$RUNSC_BIN.tmp" "$RUNSC_URL"
  actual="$(sha512_file "$RUNSC_BIN.tmp")"
  if [ "$actual" != "$RUNSC_SHA512" ]; then
    echo "runsc sha512 mismatch: got $actual want $RUNSC_SHA512" >&2
    exit 1
  fi
  mv "$RUNSC_BIN.tmp" "$RUNSC_BIN"
  chmod +x "$RUNSC_BIN"
fi

mkdir -p /etc/docker
if [ -f /etc/docker/daemon.json ] && command -v jq >/dev/null 2>&1; then
  jq --arg p "$RUNSC_BIN" '.runtimes.runsc = {"path": $p}' \
    /etc/docker/daemon.json > /etc/docker/daemon.json.new \
    && mv /etc/docker/daemon.json.new /etc/docker/daemon.json
else
  cat > /etc/docker/daemon.json <<JSON
{
  "runtimes": {
    "runsc": {
      "path": "$RUNSC_BIN"
    }
  }
}
JSON
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl restart docker
elif command -v service >/dev/null 2>&1; then
  service docker restart
else
  echo "no docker service manager found" >&2
  exit 1
fi
"$RUNSC_BIN" --version
docker info 2>/dev/null | grep -iE "runtime" || true
docker info 2>/dev/null | grep -qi "runsc"

if [ -n "${DSTACK_DOCKER_PASSWORD:-}" ]; then
  echo "$DSTACK_DOCKER_PASSWORD" | docker login "${DSTACK_DOCKER_REGISTRY:-ghcr.io}" -u "$DSTACK_DOCKER_USERNAME" --password-stdin
fi'''
    rendered = json.dumps(app_compose, indent=4, ensure_ascii=False)
    return rendered, hashlib.sha256(rendered.encode()).hexdigest()


def kms_urls() -> list[str]:
    return ["https://10.0.2.2:9101"] if NET_MODE == "bridge" else m.KMS_URLS


def create_vm(app_id: str, compose_file: str, env: dict[str, str], *, stopped: bool) -> dict:
    sealed = dict(env)
    sealed["APP_ID"] = app_id
    return m.vmm(
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
            "stopped": stopped,
            "no_tee": False,
            "kms_urls": kms_urls(),
            "networking": {"mode": NET_MODE},
            "gateway_urls": [m.GATEWAY_RPC] if GATEWAY_ENABLED else [],
            "encrypted_env": m._seal_env(sealed, m._app_env_encrypt_pubkey(app_id)),
        },
    )


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "hash"

    if mode == "deploy":
        compose_file, compose_hash = app_compose_and_hash(ENV_KEYS + ["DSTACK_DOCKER_REGISTRY"])
        app_id = m._deploy_app_contract(compose_hash)
        env = build_env(app_id, compose_hash)
        result = create_vm(app_id, compose_file, env, stopped=True)
        print(
            json.dumps(
                {
                    "app_id": app_id,
                    "compose_hash": compose_hash,
                    "vm_id": result.get("id"),
                    "gateway_url": gateway_url(app_id) if GATEWAY_ENABLED else None,
                }
            )
        )
        return

    if mode == "hash":
        _, digest = app_compose_and_hash(ENV_KEYS + ["DSTACK_DOCKER_REGISTRY"])
        print(digest)
        return

    if mode == "start":
        vm_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not vm_id:
            raise SystemExit("usage: sandboxd-node-box.py start <vm_id>")
        print(json.dumps(m.vmm("StartVm", {"id": vm_id})))
        return

    if mode == "update":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        if not app_id or not vm_id:
            raise SystemExit("usage: sandboxd-node-box.py update <app_id> <vm_id>")
        env = build_env(app_id)
        compose_file, compose_hash = app_compose_and_hash(list(env.keys()))
        sealed = dict(env)
        sealed["APP_ID"] = app_id
        try:
            m.vmm("StopVm", {"id": vm_id})
        except Exception:
            pass
        for _ in range(40):
            try:
                gi = m.vmm("GetInfo", {"id": vm_id})
                status = str((gi.get("info") or {}).get("status") or "").lower()
                if not gi.get("found", True) or status.startswith("stop") or status.startswith("exit"):
                    break
            except Exception:
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
        print(json.dumps({"app_id": app_id, "compose_hash": compose_hash, "vm_id": vm_id, "upgrade": upgrade, "start": start}))
        return

    raise SystemExit("usage: sandboxd-node-box.py [deploy|hash|start <vm_id>|update <app_id> <vm_id>]")


if __name__ == "__main__":
    main()
