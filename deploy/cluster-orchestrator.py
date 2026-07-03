#!/usr/bin/env python3
"""Small token-gated HTTP wrapper for generic AttestMesh CVM deploys."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
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
    image: ghcr.io/attestmesh/cluster-mesh-agent@sha256:db0a74f5cb68aab6441ac187522276999fc45ee760db3ae3dea0240e86d5c1af
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
      - "51900:51900"
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun

  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    env_file:
      - /tmp/attestmesh-workload.env
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $${{POSTGRES_USER:-postgres}} -d $${{POSTGRES_DB:-postgres}}"]
      interval: 10s
      timeout: 5s
      retries: 18
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
        condition: service_healthy
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

  postgres-egress-fw:
    image: ghcr.io/attestmesh/agent-egress-fw@sha256:c75c9668e905ca00f86e9583505d52624ba87cb7114a4633f84330bb497ca58f
    restart: unless-stopped
    network_mode: service:postgres
    depends_on:
      - postgres
    environment:
      - INTERNAL_HOSTS=
      - ALLOW_BASE_URL=
      - ALLOW_CIDRS=
      - ALLOW_DNS=127.0.0.11
      - RERESOLVE_SECONDS=30
    cap_add:
      - NET_ADMIN

volumes:
  pgdata:
"""


def generic_compose(name: str, image: str, catalog_id: str | None) -> str:
    command = ""
    if catalog_id == "agent-runtime":
        command = "\n    command: [\"sh\", \"-lc\", \"while true; do sleep 3600; done\"]"
    return f"""services:
  sidecar:
    image: ghcr.io/attestmesh/cluster-mesh-agent@sha256:db0a74f5cb68aab6441ac187522276999fc45ee760db3ae3dea0240e86d5c1af
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
      - "51900:51900"
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun

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


def run_job(job_id: str) -> None:
    job = load_job(job_id)
    if not job:
        return
    try:
        job["status"] = "running"
        save_job(job)
        node = job["nodeName"]
        log_path = ROOT / "deploy/logs" / f"orchestrator-{node}.log"
        cmd = [
            "bash",
            "-lc",
            (
                "set -euo pipefail; "
                "source deploy/env.sh; "
                f"CLUSTER={job['cluster']} MEMBER_IMPL={job['memberImpl']} "
                f"COMPOSE={job['composePath']} APP_ENV_B64={job['appEnvB64']} "
                f"BOX_VCPU={job['box']['vcpu']} BOX_MEM={job['box']['mem']} BOX_DISK={job['box']['disk']} "
                f"deploy/generic-node.sh {node} all"
            ),
        ]
        proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=3600)
        log_path.write_text(proc.stdout)
        job["exitCode"] = proc.returncode
        job["logPath"] = str(log_path)
        job["logTail"] = "\n".join(proc.stdout.splitlines()[-40:])
        state_path = ROOT / "deploy/logs" / f"generic-node-{node}.state"
        if state_path.exists():
            state: dict[str, str] = {}
            for line in state_path.read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    state[k] = v
            job["appId"] = state.get("X") or None
            job["vmId"] = state.get("VM_ID") or None
            job["composeHash"] = state.get("H") or None
        job["status"] = "succeeded" if proc.returncode == 0 else "failed"
        if proc.returncode != 0:
            job["error"] = f"generic-node deploy failed with exit code {proc.returncode}"
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
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
            self._json(200, {"ok": True})
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
        if self.path != "/v1/deployments":
            self._json(404, {"error": "not found"})
            return
        if not self._auth():
            return
        try:
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


def main() -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    JOBS.chmod(0o700)
    COMPOSES.mkdir(parents=True, exist_ok=True)
    COMPOSES.chmod(0o700)
    print(f"cluster-orchestrator listening on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
