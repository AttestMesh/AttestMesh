#!/usr/bin/env python3
"""Box-side deploy helper for deploy/matrix-node.sh.

Runs ON the self-hosted on-chain dstack box. Three modes:

  deploy           — call the SAME mcp_dstack.deploy_app the MCP tool wraps (registers a stock
                     DstackApp on Base + seals env + CreateVm). Prints {app_id, compose_hash, vm_id}.
  hash             — print only the measured compose_hash (no secrets, no chain). Used to allowlist
                     a new hash on the cluster BEFORE an in-place update.
  update <app_id> [vm_id]  — roll a new compose/env onto an existing app. With <vm_id> (the default),
                     does an IN-PLACE, DISK-PRESERVING UpgradeApp (StopVm → UpgradeApp → StartVm on the
                     SAME vm_id) so Postgres/Synapse data + the tailscale-state volume (→ stable node
                     name) survive the roll — only hda.img is kept; the compose_hash change is just an
                     on-chain auth gate, not part of the disk key. Without <vm_id>, or with
                     BOX_FRESH_DISK=1 (a DELIBERATE wipe), it falls back to a fresh-disk CreateVm. Either
                     way the cluster membership / CSK-originator identity (app_id) is reused; the new
                     compose_hash must already be allowlisted on the cluster.

Secrets arrive via E_* env vars (passed in-memory by the SSH caller) and are NEVER written to disk.
The app-compose built here for hash/update MUST mirror mcp_dstack.deploy_app's app_compose (incl. the
docker-login pre_launch_script + APP_ID in allowed_envs); keep in sync if that tool changes.

  sudo E_RPC_URL=… … BOX_COMPOSE=/tmp/x.yaml /opt/dstack-mcp/venv/bin/python matrix-node-box.py deploy
"""
import sys, os, json, hashlib

sys.path.insert(0, "/opt/dstack-mcp")
import mcp_dstack as m  # noqa: E402

NAME = os.environ.get("BOX_NAME", "matrix-node")
COMPOSE_PATH = os.environ.get("BOX_COMPOSE", "/tmp/matrix-node.yaml")
VCPU = int(os.environ.get("BOX_VCPU", "4"))
MEM = int(os.environ.get("BOX_MEM", "8192"))
DISK = int(os.environ.get("BOX_DISK", "60"))
PORTS = json.loads(os.environ.get("BOX_PORTS", '["tcp:127.0.0.1:8080:80","tcp:127.0.0.1:9091:9090"]'))
# Public dstack-gateway exposure. When ON, the gateway publishes https://<app_id>.gateway.<domain>
# straight to the CVM (for a Matrix node that means nginx:80 → Synapse, reachable from the public
# internet). A Matrix node must run this OFF so the homeserver is reachable ONLY over the private
# tailnet. Default ON for backwards-compat; matrix-node.sh sets it OFF. Measured into compose_hash.
GATEWAY_ENABLED = os.environ.get("BOX_GATEWAY_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
# CVM networking mode. "user" = QEMU SLIRP (10.0.2.0/24, no routable presence → Tailscale relays via DERP).
# "bridge" = TAP on the host's dstack-br0 → routable → the CVM's own Tailscale gets a DIRECT path (fast).
# In bridge mode the SLIRP KMS alias 10.0.2.2 is reached via a host DNAT (KMS RA-TLS cert is pinned to
# 10.0.2.2), so kms_urls must stay 10.0.2.2. forward_service_enabled=false (global) → no host port-forward.
NET_MODE = (os.environ.get("BOX_NET_MODE", "user").strip().lower() or "user")
ENV_KEYS = ["RPC_URL", "BUNDLER_URL", "GAS_POLICY_ID", "POSTGRES_PASSWORD", "TS_AUTHKEY",
            "DSTACK_DOCKER_USERNAME", "DSTACK_DOCKER_PASSWORD",
            # matrix-admin-agent (docs/specs/matrix-admin-agent.md §5); key NAMES are
            # measured into compose_hash, VALUES sealed. Optional ones may be empty.
            "BOT_USERNAME", "BOT_PASSWORD", "MATRIX_ADMIN_MXIDS", "MATRIX_ADMIN_SENDERS",
            "INITIAL_ADMIN", "INITIAL_ADMIN_PASSWORD", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY",
            # wal-g → Cloudflare R2 backups (deploy/postgres-walg). R2_ACCESS_KEY_ID/SECRET are secret; the
            # rest are config. BACKUP_RESTORE* are set only on a deliberate restore deploy. All optional.
            "BACKUP_ENABLED", "BACKUP_PREFIX", "BACKUP_RESTORE", "BACKUP_RESTORE_TARGET_TIME",
            "R2_ENDPOINT", "R2_BUCKET", "R2_REGION", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]


def build_env():
    # Optional keys (e.g. MATRIX_ADMIN_SENDERS, INITIAL_ADMIN) may be unset → "".
    env = {k: os.environ.get("E_" + k, "") for k in ENV_KEYS}
    env["DSTACK_DOCKER_REGISTRY"] = os.environ.get("E_DSTACK_DOCKER_REGISTRY", "ghcr.io")
    return env


def app_compose_and_hash(env_key_set):
    """Replicate mcp_dstack.deploy_app's measured app-compose, for hash/update."""
    ac = {
        "manifest_version": 2, "name": NAME, "runner": "docker-compose",
        "docker_compose_file": open(COMPOSE_PATH).read(), "kms_enabled": True,
        "gateway_enabled": GATEWAY_ENABLED, "local_key_provider_enabled": False,
        "key_provider_id": "", "public_logs": True, "public_sysinfo": True,
        "allowed_envs": sorted(set(env_key_set) | {"APP_ID"}),
        "no_instance_id": False, "secure_time": False,
    }
    ac["pre_launch_script"] = (
        'if [ -n "$DSTACK_DOCKER_PASSWORD" ]; then '
        'echo "$DSTACK_DOCKER_PASSWORD" | docker login "${DSTACK_DOCKER_REGISTRY:-ghcr.io}" '
        '-u "$DSTACK_DOCKER_USERNAME" --password-stdin; fi'
    )
    cf = json.dumps(ac, indent=4, ensure_ascii=False)
    return cf, hashlib.sha256(cf.encode()).hexdigest()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "hash"

    if mode == "deploy":
        env = build_env()
        r = m.deploy_app(name=NAME, docker_compose=open(COMPOSE_PATH).read(), env=env,
                         gateway_enabled=GATEWAY_ENABLED, ports=PORTS, vcpu=VCPU, memory_mb=MEM, disk_gb=DISK)
        print(json.dumps({"app_id": r["app_id"], "compose_hash": r["compose_hash"],
                          "vm_id": r["vm_id"], "gateway_url": r.get("gateway_url")}))
        return

    if mode == "hash":
        # env values irrelevant — only the KEY set affects allowed_envs/hash.
        _, h = app_compose_and_hash(ENV_KEYS + ["DSTACK_DOCKER_REGISTRY"])
        print(h)
        return

    if mode == "update":
        import httpx, time
        app_id = sys.argv[2]
        vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        env = build_env()
        cf, h = app_compose_and_hash(list(env.keys()))
        sealed = dict(env); sealed["APP_ID"] = app_id
        enc = m._seal_env(sealed, m._app_env_encrypt_pubkey(app_id))
        BASE = "http://127.0.0.1:9080/prpc"
        kms = (["https://10.0.2.2:9101"] if NET_MODE == "bridge" else m.KMS_URLS)
        gw = ([m.GATEWAY_RPC] if GATEWAY_ENABLED else [])
        ports = [m._parse_port(p) for p in PORTS]
        # A deliberate wipe (BOX_FRESH_DISK=1) or no vm_id → fresh-disk CreateVm; otherwise in-place.
        fresh = os.environ.get("BOX_FRESH_DISK", "").strip().lower() in ("1", "true", "yes", "on")

        if vm_id and not fresh:
            # IN-PLACE, DISK-PRESERVING roll: StopVm → UpgradeApp(same id) → StartVm. Keeps hda.img, so
            # Postgres/Synapse data + the tailscale-state volume (stable node name) survive. UpgradeApp
            # (UpdateVmRequest) has NO networking field — bridge mode must already be on the VM manifest
            # (set at the original CreateVm). vcpu/memory/disk_size/image are omitted → unchanged.
            httpx.post(f"{BASE}/StopVm?json", json={"id": vm_id}, timeout=120)
            stopped = False
            for _ in range(40):
                try:
                    gi = httpx.post(f"{BASE}/GetInfo?json", json={"id": vm_id}, timeout=30).json()
                    st = ((gi.get("info") or {}).get("status") or "").lower()
                    if (not gi.get("found")) or st.startswith("stop") or st.startswith("exit"):
                        stopped = True; break
                except Exception:
                    pass
                time.sleep(2)
            up = {
                "id": vm_id, "compose_file": cf, "encrypted_env": enc,
                "update_ports": True, "ports": ports,
                "update_kms_urls": True, "kms_urls": kms,
                "update_gateway_urls": True, "gateway_urls": gw,
            }
            r = httpx.post(f"{BASE}/UpgradeApp?json", json=up, timeout=120)
            s = httpx.post(f"{BASE}/StartVm?json", json={"id": vm_id}, timeout=120)
            print(json.dumps({"compose_hash": h, "vm_id": vm_id, "mode": "upgrade", "stopped": stopped,
                              "upgrade_status": r.status_code, "start_status": s.status_code,
                              "upgrade": r.text[:200]}))
            return

        # FRESH-DISK path (brand-new instance or an explicit BOX_FRESH_DISK wipe). Stop the old VM first
        # (if any) so we don't leave it running, then CreateVm a new instance (loses all prior CVM data).
        if vm_id:
            try: httpx.post(f"{BASE}/StopVm?json", json={"id": vm_id}, timeout=120)
            except Exception: pass
        params = {
            "name": NAME, "image": "dstack-0.5.11", "compose_file": cf,
            "vcpu": VCPU, "memory": MEM, "disk_size": DISK, "app_id": app_id,
            "user_config": "", "ports": ports,
            "hugepages": False, "pin_numa": False, "stopped": False, "no_tee": False,
            "kms_urls": kms, "networking": {"mode": NET_MODE},
            "gateway_urls": gw, "encrypted_env": enc,
        }
        r = httpx.post(f"{BASE}/CreateVm?json", json=params, timeout=120)
        vm = ""
        try:
            vm = r.json().get("id", "")
        except Exception:
            pass
        print(json.dumps({"compose_hash": h, "vm_id": vm, "mode": "createvm",
                          "createvm_status": r.status_code, "createvm": r.text[:200]}))
        return

    sys.exit("usage: matrix-node-box.py [deploy|hash|update <app_id> [vm_id]]")


if __name__ == "__main__":
    main()
