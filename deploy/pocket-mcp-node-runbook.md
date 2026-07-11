# pocket-mcp node — deploy runbook

App tier for the Pocket recording store as a C3 mesh member on the self-hosted dstack box:
a 2-min **poller** (`python -m poller`) + an **MCP server** (`python -m server.mcp_server`)
over external HA Postgres+pgvector + an optional Hindsight layer. Embeddings use
Redpill Qwen after the mesh fugu endpoint failed the recovery preflight. Mesh-only
app surface; gateway ON at the node level.

Trio: `deploy/pocket-mcp-node.sh` (driver) · `deploy/pocket-mcp-node-box.py` (box helper) ·
`deploy/compose/pocket-mcp-node.yaml` (mesh wiring). Modeled on the agent-session-mcp node.

## Sealed env manifest (what the CVM receives)

| Var | Value / source | Secret |
|---|---|---|
| `DATABASE_URL` | `postgresql://pocket:${APP_DB_PASSWORD}@sidecar:15431,sidecar:15432,sidecar:15433/pocket?target_session_attrs=read-write&connect_timeout=5` | ✅ |
| `APP_DB_PASSWORD` | auto-gen (openssl) → `~/.attestmesh/pocket-mcp.env`; superuser is CSK-derived in-CVM | ✅ |
| `EMBEDDING_URL` | `https://api.redpill.ai/v1`; client appends `/embeddings` | |
| `EMBEDDING_MODEL` | `qwen/qwen3-embedding-8b` | |
| `EMBEDDING_DIM` | `1024` — native is 4096; app truncates leading-1024 + L2-renorm (Matryoshka). **Fixes pgvector `vector(1024)` at init.** | |
| `EMBEDDING_API_KEY` | Redpill key from `~/.attestmesh/redpill-key` (Bearer) | ✅ |
| `HINDSIGHT_URL` | `http://sidecar:18888` (forwarder → `10.18.78.76:18888`) | |
| `HINDSIGHT_TOKEN` | `TAK=` from `deploy/logs/hindsight-node-hindsight-node.state` (Bearer, NOT X-Api-Key) | ✅ |
| `HINDSIGHT_BANK` | `dans-pocket` (create on startup: `PUT /v1/default/banks/dans-pocket`) | |
| `POCKET_API_KEY` | `~/.attestmesh/dans-pocket.key` | ✅ |
| `POCKET_API_BASE` | `https://public.heypocketai.com/api/v1`; egress-fw allows host `public.heypocketai.com:443`, re-resolved every 30s | |
| `POCKET_API_CIDRS` | `3.19.247.64/32 3.21.239.100/32` (today's us-east-2 EIP floor; host re-resolution is primary, handles rotation) | |
| `MCP_AUTH_TOKEN` | empty in v1 (mesh membership is the trust boundary); set to enforce | ✅ |

`MCP_HOST=0.0.0.0`, `MCP_PORT=20800` are compose literals. cluster identity
(`CLUSTER`/`MEMBER_IMPL`) is seeded from `deploy/logs/pg-ha-pg-ha.state`.

## Open items before `deploy_app`

1. **App image digest** — replace `REPLACE_WITH_DIGEST` (×3: `init`, `mcp`, `poller`) in the
   compose with `ghcr.io/attestmesh/pocket-mcp@sha256:…`. Box daemon is logged into GHCR.
2. ~~`POCKET_API_BASE`~~ **RESOLVED** — `https://public.heypocketai.com/api/v1`; egress host
   `public.heypocketai.com:443` (host-based allowlist, re-resolved every 30s) + today's two
   /32 EIPs as a floor. Baked into the driver defaults.
3. **MCP path/port** — server currently speaks SSE at `/sse`; `mesh-proxy-mcp` forwards TCP
   on `<mesh-ip>:20800` (path-agnostic). Confirm final path (`/sse` vs `/mcp`) + that the
   server binds `MCP_PORT=20800`. Consumers hit `http://<mesh-ip>:20800/<path>`.
4. **init_db command** — `python scripts/init_db.py`, gated after `pg-provision`, run with
   `EMBEDDING_DIM=1024` present (compose does this). Confirm the module path.

## Deploy

```bash
source deploy/env.sh
POCKET_API_BASE=https://<pocket-host>/... deploy/pocket-mcp-node.sh pocket-mcp all
```

`all` = preflight → deploy (stopped) → prime (allowlist KMS root + compose hash + app id) →
bind (`upgradeToAndCall` → ClusterMember impl) → start → register-direct → verify
(waits for on-chain `memberIdOf` != 0). Roll after an image/config change:
`deploy/pocket-mcp-node.sh pocket-mcp update`.

## Verify (post-boot)

- On-chain: `memberIdOf(app_id) != 0`, `memberCount` +1.
- Boot order: `pg-provision` (role+db `pocket`+pgvector) → `init` (schema, `vector(1024)`) →
  `mcp` + `poller` up, egress locked.
- Embeddings: a real Qwen embed returns 200 from Redpill and at least 1024 dimensions.
- Hindsight: synchronization is disabled and recall uses Postgres until the isolated canary passes.
- MCP: `http://<mesh-ip>:20800/<path>` reachable from a C3 mesh member (off-mesh: tunnel via
  ssh-node — `ssh -L …:<mesh-ip>:20800 attestmesh-mesh-node`; ssh-node is frozen, jump only).
- Poller: one cycle imports/updates recordings without egress-fw drops.

## Notes / gotchas

- **Gateway remains ON** (`BOX_GATEWAY_ENABLED=true`, the default here) for ordinary
  bridge egress and mesh reachability; app ports stay mesh-only (`BOX_PORTS=[]`).
- **The mesh fugu endpoint timed out during recovery preflight.** Pocket now uses the
  same live-verified Redpill Qwen path as agent-session-mcp. The embedding client retries
  429/5xx with capped backoff and batches 16.
- Egress image is `agent-egress-fw@sha256:c6c00d1f…` (honors `INTERNAL_HOST_PORTS`); do NOT
  use `c75c9668` (silently drops internal forwarders).
- C3 has **no `removeMember`** — this member is permanent on-chain.
