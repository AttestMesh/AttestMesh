#!/usr/bin/env python3
"""Small token-gated HTTP wrapper for generic AttestMesh CVM deploys."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("TEEMESH_ROOT", "/home/ubuntu/teemesh"))
JOBS = Path(os.environ.get("ORCHESTRATOR_JOBS_DIR", str(ROOT / "deploy/orchestrator/jobs")))
COMPOSES = Path(os.environ.get("ORCHESTRATOR_COMPOSE_DIR", str(ROOT / "deploy/orchestrator/compose")))
TOKEN = os.environ.get("ORCHESTRATOR_TOKEN", "")
HOST = os.environ.get("ORCHESTRATOR_HOST", "0.0.0.0")
PORT = int(os.environ.get("ORCHESTRATOR_PORT", "8789"))
DEPLOY_RPC_URL = os.environ.get("ORCHESTRATOR_RPC_URL", "https://base-rpc.publicnode.com")
DEPLOY_BUNDLER_URL = os.environ.get("ORCHESTRATOR_BUNDLER_URL", "")
DEPLOY_GAS_POLICY_ID = os.environ.get("ORCHESTRATOR_GAS_POLICY_ID", "")
DEPLOY_BOX_RPC = os.environ.get("ORCHESTRATOR_BOX_RPC", DEPLOY_RPC_URL)

CATALOG_IMAGES = {
    "postgres": "postgres:16-alpine",
    "redis": "redis:7-alpine",
    "qdrant": "qdrant/qdrant:latest",
    "agent-runtime": "alpine:3.20",
}

FLAVORS = {
    "small": {"vcpu": "2", "mem": "4096", "disk": "40"},
    "medium": {"vcpu": "4", "mem": "8192", "disk": "80"},
    "large": {"vcpu": "8", "mem": "16384", "disk": "160"},
}

SIDECAR_IMAGE = "ghcr.io/dmvt/cluster-mesh-agent@sha256:1f7ac9c51ec86a9076a1cabac8a8aef2d7cd2cea43c8fe8052b20af1730e31c3"
AUTO_CLEANUP_FAILED = os.environ.get("ORCHESTRATOR_AUTO_CLEANUP_FAILED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

RUNNING_LOCK = threading.Lock()
RUNNING: dict[str, subprocess.Popen[str]] = {}


def validate_deploy_config() -> None:
    if not DEPLOY_BUNDLER_URL:
        raise RuntimeError(
            "ORCHESTRATOR_BUNDLER_URL is not configured. Cluster CVMs need an "
            "EIP-4337 bundler/paymaster endpoint; a plain Base RPC URL cannot register members."
        )
    if DEPLOY_BUNDLER_URL == DEPLOY_RPC_URL:
        raise RuntimeError(
            "ORCHESTRATOR_BUNDLER_URL must be a real EIP-4337 bundler/paymaster endpoint, "
            "not the same plain RPC URL used for chain reads."
        )
    if not DEPLOY_GAS_POLICY_ID:
        raise RuntimeError(
            "ORCHESTRATOR_GAS_POLICY_ID is not configured. Cluster CVMs need the "
            "paymaster sponsorship policy id that matches ORCHESTRATOR_BUNDLER_URL."
        )


def slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return s[:48] or "workload"


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def load_job(job_id: str) -> dict[str, Any] | None:
    path = JOBS / f"{job_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_job(job: dict[str, Any]) -> None:
    job["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    atomic_write(JOBS / f"{job['id']}.json", job)


def parse_env(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    parsed: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
            raise ValueError(f"Invalid environment line: {line!r}")
        key, value = stripped.split("=", 1)
        parsed[key] = value
    return parsed


def env_text(env: Mapping[str, str]) -> str:
    return "".join(f"{k}={v}\n" for k, v in sorted(env.items()))


def env_for_catalog(catalog_id: str | None, user_env: dict[str, str]) -> dict[str, str]:
    env = dict(user_env)
    if catalog_id == "postgres":
        if not env.get("POSTGRES_PASSWORD"):
            raise ValueError("Postgres requires POSTGRES_PASSWORD in Advanced environment.")
        env.setdefault("POSTGRES_USER", "postgres")
        env.setdefault("POSTGRES_DB", "postgres")
        env.setdefault("POSTGRES_INITDB_ARGS", "--encoding=UTF8 --locale=C")
    return env


def workload_image(body: dict[str, Any]) -> str:
    if body.get("source") == "image":
        image = str(body.get("imageRef") or "").strip()
        if not image:
            raise ValueError("imageRef is required for custom-image deploys")
        return image
    catalog_id = str(body.get("catalogId") or "").strip()
    image = CATALOG_IMAGES.get(catalog_id)
    if not image:
        supported = ", ".join(sorted(CATALOG_IMAGES))
        raise ValueError(f"Catalog item {catalog_id or '<missing>'} is not deployable yet. Supported catalog CVMs: {supported}")
    return image


def postgres_compose(name: str) -> str:
    return f"""services:
  sidecar:
    image: {SIDECAR_IMAGE}
    restart: unless-stopped
    environment:
      - CHAIN_ID=${{CHAIN_ID}}
      - RPC_URL=${{RPC_URL}}
      - BUNDLER_URL=${{BUNDLER_URL}}
      - GAS_POLICY_ID=${{GAS_POLICY_ID}}
      - INDEXER_REGISTRY_ADDR=${{INDEXER_REGISTRY_ADDR}}
      - GATEWAY_DOMAIN=${{GATEWAY_DOMAIN}}
      - DSTACK_SOCKET=/var/run/dstack.sock
      - HEALTH_HTTP_ADDR=0.0.0.0:9090
      - LOG_FORMAT=pretty
      - LOG_LEVEL=info,cluster_mesh_agent=debug
    volumes:
      - /var/run/dstack.sock:/var/run/dstack.sock
    ports:
      - "9090:9090"
      - "51900:51900"
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun

  registration-helper:
    image: {SIDECAR_IMAGE}
    restart: unless-stopped
    environment:
      - CLUSTER=${{CLUSTER}}
      - DSTACK_SOCKET=/var/run/dstack.sock
      - REGISTRATION_HELPER_ADDR=0.0.0.0:9092
    volumes:
      - /var/run/dstack.sock:/var/run/dstack.sock
    entrypoint: ["/usr/local/bin/attestmesh-registration-calldata"]

  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    env_file:
      - /tmp/attestmesh-workload.env
    volumes:
      - pgdata:/var/lib/postgresql/data
    labels:
      attestmesh.name: "{name}"
      attestmesh.source: "fleet-control"

  postgres-mesh-proxy:
    image: alpine/socat:1.8.0.0
    restart: unless-stopped
    network_mode: service:sidecar
    depends_on:
      sidecar:
        condition: service_started
      postgres:
        condition: service_started
    entrypoint:
      - /bin/sh
      - -ec
    command:
      - |
        while :; do
          ip="$$(ip -4 -o addr show dev attestmesh0 2>/dev/null | awk '{{print $$4}}' | cut -d/ -f1)"
          [ -n "$$ip" ] && break
          sleep 1
        done
        exec socat -d -d TCP-LISTEN:5432,fork,reuseaddr,bind="$${{ip}}" TCP:postgres:5432
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true

volumes:
  pgdata:
"""


def generic_compose(name: str, image: str, catalog_id: str | None) -> str:
    command = ""
    if catalog_id == "agent-runtime":
        command = "\n    command: [\"sh\", \"-lc\", \"while true; do sleep 3600; done\"]"
    return f"""services:
  sidecar:
    image: {SIDECAR_IMAGE}
    restart: unless-stopped
    environment:
      - CHAIN_ID=${{CHAIN_ID}}
      - RPC_URL=${{RPC_URL}}
      - BUNDLER_URL=${{BUNDLER_URL}}
      - GAS_POLICY_ID=${{GAS_POLICY_ID}}
      - INDEXER_REGISTRY_ADDR=${{INDEXER_REGISTRY_ADDR}}
      - GATEWAY_DOMAIN=${{GATEWAY_DOMAIN}}
      - DSTACK_SOCKET=/var/run/dstack.sock
      - HEALTH_HTTP_ADDR=0.0.0.0:9090
      - LOG_FORMAT=pretty
      - LOG_LEVEL=info,cluster_mesh_agent=debug
    volumes:
      - /var/run/dstack.sock:/var/run/dstack.sock
    ports:
      - "9090:9090"
      - "51900:51900"
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun

  registration-helper:
    image: {SIDECAR_IMAGE}
    restart: unless-stopped
    environment:
      - CLUSTER=${{CLUSTER}}
      - DSTACK_SOCKET=/var/run/dstack.sock
      - REGISTRATION_HELPER_ADDR=0.0.0.0:9092
    volumes:
      - /var/run/dstack.sock:/var/run/dstack.sock
    entrypoint: ["/usr/local/bin/attestmesh-registration-calldata"]

  workload:
    image: {image}
    restart: unless-stopped
    env_file:
      - /tmp/attestmesh-workload.env{command}
    labels:
      attestmesh.name: "{name}"
      attestmesh.source: "fleet-control"
"""


def compose_for(name: str, image: str, catalog_id: str | None) -> str:
    if catalog_id == "postgres":
        return postgres_compose(name)
    return generic_compose(name, image, catalog_id)


def absorb_state(job: dict[str, Any], node: str) -> None:
    state_path = ROOT / "deploy/logs" / f"generic-node-{node}.state"
    if not state_path.exists():
        return
    state: dict[str, str] = {}
    for line in state_path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            state[k] = v
    job["appId"] = state.get("X") or None
    job["vmId"] = state.get("VM_ID") or None
    job["composeHash"] = state.get("H") or None
    job["statePhase"] = state.get("STATE_PHASE") or None


def deploy_shell(job: dict[str, Any], body: str) -> list[str]:
    return [
        "bash",
        "-lc",
        (
            "set -euo pipefail; "
            "source deploy/env.sh; "
            f"export RPC_URL={shlex.quote(DEPLOY_RPC_URL)}; "
            f"export BUNDLER_URL={shlex.quote(DEPLOY_BUNDLER_URL)}; "
            f"export GAS_POLICY_ID={shlex.quote(DEPLOY_GAS_POLICY_ID)}; "
            f"export BOX_RPC={shlex.quote(DEPLOY_BOX_RPC)}; "
            f"export CLUSTER={shlex.quote(job['cluster'])} MEMBER_IMPL={shlex.quote(job['memberImpl'])}; "
            f"export COMPOSE={shlex.quote(job['composePath'])} APP_ENV_B64={shlex.quote(job['appEnvB64'])}; "
            f"export BOX_VCPU={job['box']['vcpu']} BOX_MEM={job['box']['mem']} BOX_DISK={job['box']['disk']}; "
            + body
        ),
    ]


def cleanup_job(job: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    node = job["nodeName"]
    log_path = ROOT / "deploy/logs" / f"orchestrator-{node}-cleanup.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = deploy_shell(
        job,
        (
            f"export FORCE_CLEANUP={'1' if force else '0'}; "
            f"deploy/generic-node.sh {shlex.quote(node)} cleanup"
        ),
    )
    proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    log_path.write_text(proc.stdout)
    absorb_state(job, node)
    job["cleanup"] = {
        "status": "succeeded" if proc.returncode == 0 else "failed",
        "forced": force,
        "exitCode": proc.returncode,
        "logPath": str(log_path),
        "logTail": "\n".join(proc.stdout.splitlines()[-40:]),
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return job


def reconcile_interrupted_jobs() -> None:
    interrupted = {"queued", "running", "canceling", "cleanup_running"}
    for path in JOBS.glob("*.json"):
        try:
            job = json.loads(path.read_text())
        except Exception:
            continue
        if job.get("status") not in interrupted:
            continue
        job["status"] = "interrupted"
        job["error"] = "orchestrator restarted before this job reached a terminal state"
        absorb_state(job, job.get("nodeName", ""))
        if AUTO_CLEANUP_FAILED:
            try:
                cleanup_job(job)
                if (job.get("cleanup") or {}).get("status") == "succeeded":
                    job["status"] = "cleaned"
            except Exception as exc:
                job["cleanup"] = {"status": "failed", "error": str(exc)}
        save_job(job)


def run_job(job_id: str) -> None:
    job = load_job(job_id)
    if not job:
        return
    proc: subprocess.Popen[str] | None = None
    try:
        job["status"] = "running"
        save_job(job)
        node = job["nodeName"]
        log_path = ROOT / "deploy/logs" / f"orchestrator-{node}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = deploy_shell(
            job,
            (
                f"deploy/generic-node.sh {shlex.quote(node)} preflight; "
                f"deploy/generic-node.sh {shlex.quote(node)} deploy; "
                f"deploy/generic-node.sh {shlex.quote(node)} prime; "
                f"deploy/generic-node.sh {shlex.quote(node)} bind; "
                f"deploy/generic-node.sh {shlex.quote(node)} start; "
                f"deploy/generic-node.sh {shlex.quote(node)} register-direct; "
                f"deploy/generic-node.sh {shlex.quote(node)} verify"
            ),
        )
        tail: list[str] = []
        last_save = 0.0
        proc = subprocess.Popen(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
        with RUNNING_LOCK:
            RUNNING[job_id] = proc
        assert proc.stdout is not None
        with log_path.open("w") as log:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                tail.append(line.rstrip("\n"))
                if len(tail) > 80:
                    tail = tail[-80:]
                now = time.monotonic()
                if now - last_save >= 2:
                    job["logPath"] = str(log_path)
                    job["logTail"] = "\n".join(tail[-40:])
                    absorb_state(job, node)
                    save_job(job)
                    last_save = now
        exit_code = proc.wait(timeout=5)
        job["exitCode"] = exit_code
        job["logPath"] = str(log_path)
        job["logTail"] = "\n".join(tail[-40:])
        absorb_state(job, node)
        job["status"] = "succeeded" if exit_code == 0 else "failed"
        if exit_code != 0:
            job["error"] = f"generic-node deploy failed with exit code {exit_code}"
            if AUTO_CLEANUP_FAILED:
                try:
                    cleanup_job(job)
                except Exception as cleanup_exc:
                    job["cleanup"] = {"status": "failed", "error": str(cleanup_exc)}
    except Exception as exc:
        if proc and proc.poll() is None:
            proc.kill()
        job["status"] = "failed"
        job["error"] = str(exc)
        if AUTO_CLEANUP_FAILED:
            try:
                cleanup_job(job)
            except Exception as cleanup_exc:
                job["cleanup"] = {"status": "failed", "error": str(cleanup_exc)}
    finally:
        with RUNNING_LOCK:
            RUNNING.pop(job_id, None)
    save_job(job)


class Handler(BaseHTTPRequestHandler):
    server_version = "attestmesh-cluster-orchestrator/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if not TOKEN:
            self._json(500, {"error": "ORCHESTRATOR_TOKEN is not configured"})
            return False
        got = self.headers.get("authorization", "")
        if got != f"Bearer {TOKEN}":
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(
                200,
                {
                    "ok": True,
                    "autoCleanupFailed": AUTO_CLEANUP_FAILED,
                    "bundler": bool(DEPLOY_BUNDLER_URL),
                    "gasPolicy": bool(DEPLOY_GAS_POLICY_ID),
                },
            )
            return
        if not self._auth():
            return
        m = re.match(r"^/v1/deployments/([a-zA-Z0-9_-]+)$", self.path)
        if not m:
            self._json(404, {"error": "not found"})
            return
        job = load_job(m.group(1))
        if not job:
            self._json(404, {"error": "job not found"})
            return
        self._json(200, job)

    def do_POST(self) -> None:
        cleanup_match = re.match(r"^/v1/deployments/([a-zA-Z0-9_-]+)/cleanup$", self.path)
        if cleanup_match:
            if not self._auth():
                return
            job = load_job(cleanup_match.group(1))
            if not job:
                self._json(404, {"error": "job not found"})
                return
            try:
                n = int(self.headers.get("content-length") or "0")
                body = json.loads(self.rfile.read(n) or b"{}")
                force = bool(body.get("force"))
                job["status"] = "cleanup_running"
                save_job(job)
                cleanup_job(job, force=force)
                job["status"] = "stopped" if force else "cleaned"
                save_job(job)
                self._json(200, job)
            except Exception as exc:
                job["status"] = "cleanup_failed"
                job["error"] = str(exc)
                save_job(job)
                self._json(500, {"error": str(exc), "job": job})
            return

        if self.path != "/v1/deployments":
            self._json(404, {"error": "not found"})
            return
        if not self._auth():
            return
        try:
            validate_deploy_config()
            n = int(self.headers.get("content-length") or "0")
            body = json.loads(self.rfile.read(n) or b"{}")
            name = slug(str(body.get("name") or "workload"))
            network_id = str(body.get("networkId") or "")
            cluster = str(body.get("cluster") or "")
            member_impl = str(body.get("memberImpl") or "")
            if not network_id or not cluster or not member_impl:
                raise ValueError("networkId, cluster, and memberImpl are required")
            image = workload_image(body)
            catalog_id = str(body.get("catalogId") or "").strip() or None
            env = env_for_catalog(catalog_id, parse_env(str(body.get("env") or "")))
            job_id = secrets.token_hex(8)
            node = slug(f"{network_id}-{name}-{job_id[:6]}")
            flavor = str(body.get("flavor") or "medium")
            box = FLAVORS.get(flavor, FLAVORS["medium"])
            COMPOSES.mkdir(parents=True, exist_ok=True)
            COMPOSES.chmod(0o700)
            compose_path = COMPOSES / f"{node}.yaml"
            compose_path.write_text(compose_for(name, image, body.get("catalogId")))
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            job = {
                "id": job_id,
                "status": "queued",
                "networkId": network_id,
                "name": name,
                "source": body.get("source"),
                "catalogId": catalog_id,
                "imageRef": image,
                "flavor": flavor,
                "cluster": cluster,
                "memberImpl": member_impl,
                "nodeName": node,
                "composePath": str(compose_path),
                "appEnvB64": base64.b64encode(env_text(env).encode()).decode(),
                "connection": (
                    {
                        "kind": "postgres",
                        "host": "meshIp",
                        "port": 5432,
                        "database": env.get("POSTGRES_DB", "postgres"),
                        "user": env.get("POSTGRES_USER", "postgres"),
                    }
                    if catalog_id == "postgres"
                    else None
                ),
                "box": box,
                "createdAt": now,
                "updatedAt": now,
                "appId": None,
                "vmId": None,
                "composeHash": None,
                "error": None,
            }
            save_job(job)
            threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
            self._json(202, job)
        except Exception as exc:
            self._json(422, {"error": str(exc)})

    def do_DELETE(self) -> None:
        if not self._auth():
            return
        m = re.match(r"^/v1/deployments/([a-zA-Z0-9_-]+)$", self.path)
        if not m:
            self._json(404, {"error": "not found"})
            return
        job_id = m.group(1)
        job = load_job(job_id)
        if not job:
            self._json(404, {"error": "job not found"})
            return
        with RUNNING_LOCK:
            proc = RUNNING.get(job_id)
        if proc and proc.poll() is None:
            proc.kill()
            job["status"] = "canceling"
            save_job(job)
        try:
            cleanup_job(job, force=True)
            job["status"] = "stopped"
            job["error"] = None
            save_job(job)
            self._json(200, job)
        except Exception as exc:
            job["status"] = "cleanup_failed"
            job["error"] = str(exc)
            save_job(job)
            self._json(500, {"error": str(exc), "job": job})


def main() -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    JOBS.chmod(0o700)
    COMPOSES.mkdir(parents=True, exist_ok=True)
    COMPOSES.chmod(0o700)
    reconcile_interrupted_jobs()
    bundler_state = "configured" if DEPLOY_BUNDLER_URL else "missing"
    policy_state = "configured" if DEPLOY_GAS_POLICY_ID else "missing"
    print(
        f"cluster-orchestrator listening on {HOST}:{PORT} "
        f"rpc={DEPLOY_RPC_URL} bundler={bundler_state} gasPolicy={policy_state}",
        flush=True,
    )
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
