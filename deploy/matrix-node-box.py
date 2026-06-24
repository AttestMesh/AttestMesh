#!/usr/bin/env python3
"""Box-side deploy helper for deploy/matrix-node.sh.

Runs ON the self-hosted on-chain dstack box. Three modes:

  deploy           — call the SAME mcp_dstack.deploy_app the MCP tool wraps (registers a stock
                     DstackApp on Base + seals env + CreateVm). Prints {app_id, compose_hash, vm_id}.
  hash             — print only the measured compose_hash (no secrets, no chain). Used to allowlist
                     a new hash on the cluster BEFORE an in-place update.
  update <app_id>  — out-of-band CreateVm that REUSES an existing app_id (skips registration), so the
                     cluster membership / CSK-originator identity is preserved across a compose/env
                     roll. The new compose_hash must already be allowlisted on the cluster.

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
            "INITIAL_ADMIN", "INITIAL_ADMIN_PASSWORD", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"]


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
        import httpx
        app_id = sys.argv[2]
        env = build_env()
        cf, h = app_compose_and_hash(list(env.keys()))
        sealed = dict(env); sealed["APP_ID"] = app_id
        enc = m._seal_env(sealed, m._app_env_encrypt_pubkey(app_id))
        params = {
            "name": NAME, "image": "dstack-0.5.11", "compose_file": cf,
            "vcpu": VCPU, "memory": MEM, "disk_size": DISK, "app_id": app_id,
            "user_config": "", "ports": [m._parse_port(p) for p in PORTS],
            "hugepages": False, "pin_numa": False, "stopped": False, "no_tee": False,
            "kms_urls": (["https://10.0.2.2:9101"] if NET_MODE == "bridge" else m.KMS_URLS),
            "networking": {"mode": NET_MODE},
            "gateway_urls": ([m.GATEWAY_RPC] if GATEWAY_ENABLED else []), "encrypted_env": enc,
        }
        r = httpx.post("http://127.0.0.1:9080/prpc/CreateVm?json", json=params, timeout=120)
        vm = ""
        try:
            vm = r.json().get("id", "")
        except Exception:
            pass
        print(json.dumps({"compose_hash": h, "vm_id": vm,
                          "createvm_status": r.status_code, "createvm": r.text[:200]}))
        return

    sys.exit("usage: matrix-node-box.py [deploy|hash|update <app_id>]")


if __name__ == "__main__":
    main()
