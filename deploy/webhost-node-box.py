#!/usr/bin/env python3
"""Box-side deploy helper for deploy/webhost-node.sh.

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

  sudo E_RPC_URL=… … BOX_COMPOSE=/tmp/webhost-node.yaml \
    /opt/dstack-mcp/venv/bin/python webhost-node-box.py deploy
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/opt/dstack-mcp")
import mcp_dstack as m  # noqa: E402

NAME = os.environ.get("BOX_NAME", "webhost-node")
COMPOSE_PATH = os.environ.get("BOX_COMPOSE", "/tmp/webhost-node.yaml")
VCPU = int(os.environ.get("BOX_VCPU", "4"))
MEM = int(os.environ.get("BOX_MEM", "8192"))
DISK = int(os.environ.get("BOX_DISK", "60"))
PORTS = json.loads(os.environ.get("BOX_PORTS", "[]"))
# CVM networking: "bridge" (default) → routable on dstack-br0; the box haproxy SNI
# backend + the dstack gateway reach tlsproxy:443 at the CVM's bridge IP. In bridge
# mode KMS is the SLIRP alias 10.0.2.2 (RA-TLS cert SAN), reached via the host DNAT.
NET_MODE = (os.environ.get("BOX_NET_MODE", "bridge").strip().lower() or "bridge")
# Gateway ON: tenant apps at *.app.synclave.net route through the dstack gateway,
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
    # --- Open webhost application (key NAMES measured into compose_hash, VALUES sealed;
    #     some are non-secret config, still sealed for a single measured surface) ---
    "TEE_DAEMON_TOKEN",
    "WEBHOST_MCP_TOKEN",
    "GITHUB_ID",
    "GITHUB_SECRET",
    "NEXTAUTH_SECRET",
    "NEXTAUTH_URL",
    "APP_DOMAIN",
    "DIRECTORY_HOST",
    "WEBHOST_ADMIN_HOST",
    "ACME_EMAIL",
    "CONSOLE_HOST",
    "REDPILL_API_KEY",
    "REDPILL_BASE_URL",
    "REDPILL_MODEL",
    "VENICE_API_KEY",
    "VENICE_BASE_URL",
    "VENICE_MODEL",
    "RUNYARD_HUB_URL",
    "RUNYARD_HUB_TOKEN",
    "RUNYARD_PRIVACY_PREFLIGHT_CAPABILITY",
    "RUNYARD_PRIVACY_AUDIT_CAPABILITY",
    "RUNYARD_EXECUTION_MODE",
    "RUNYARD_RUNNER_LOCATION",
    "RUNYARD_PRIVACY_AUDIT_LLM",
    "RUNYARD_CALLBACK_SECRET",
    "RUNYARD_CALLBACK_URL",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_SYNCLAVE_API_TOKEN",
    "BACKUP_STORAGE",
    "BACKUP_S3_ENDPOINT",
    "BACKUP_S3_BUCKET",
    "BACKUP_S3_REGION",
    "BACKUP_S3_ACCESS_KEY_ID",
    "BACKUP_S3_SECRET_ACCESS_KEY",
    "BACKUP_PREFIX",
    "BACKUP_APP_ID",
    "BACKUP_INTERVAL_SECONDS",
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
    # Install/register gVisor before Compose starts, then log in to the private
    # registry so ghcr.io/dmvt/* + ghcr.io/attestmesh/* images pull. Creds
    # arrive sealed as DSTACK_DOCKER_*.
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

# dstack's minimal guest /dev does not populate loop device nodes. The kernel
# loop driver is built in, so create the standard control and block-device nodes
# before the privileged storage helper starts.
if command -v modprobe >/dev/null 2>&1; then
  modprobe loop 2>/dev/null || true
fi
if [ ! -e /dev/loop-control ]; then
  mknod -m 660 /dev/loop-control c 10 237
fi
for n in $(seq 0 255); do
  if [ ! -e "/dev/loop$n" ]; then
    mknod -m 660 "/dev/loop$n" b 7 "$n"
  fi
done

# The storage helper mounts loopback ext4 filesystems under the daemon_data
# volume. Make that subtree a shared host mount so child mounts created in the
# helper namespace propagate back to Docker and can be bind-mounted into tenant
# containers. Creating the named volume here also makes its host path stable
# before Compose evaluates the helper's bind mount.
DAEMON_VOLUME="${DAEMON_VOLUME_NAME:-dstack_daemon_data}"
docker volume create "$DAEMON_VOLUME" >/dev/null
DAEMON_VOLUME_ROOT="$(docker volume inspect -f '{{ .Mountpoint }}' "$DAEMON_VOLUME")"
STORAGE_ROOT="$DAEMON_VOLUME_ROOT/storage"
mkdir -p "$STORAGE_ROOT"
if ! mountpoint -q "$STORAGE_ROOT"; then
  mount --bind "$STORAGE_ROOT" "$STORAGE_ROOT"
fi
mount --make-rshared "$STORAGE_ROOT"

if [ -n "${DSTACK_DOCKER_PASSWORD:-}" ]; then
  echo "$DSTACK_DOCKER_PASSWORD" | docker login "${DSTACK_DOCKER_REGISTRY:-ghcr.io}" -u "$DSTACK_DOCKER_USERNAME" --password-stdin
fi'''
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
            raise SystemExit("usage: webhost-node-box.py update <app_id> <vm_id>")

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

    raise SystemExit("usage: webhost-node-box.py [deploy|hash|update <app_id> <vm_id>]")


if __name__ == "__main__":
    main()
