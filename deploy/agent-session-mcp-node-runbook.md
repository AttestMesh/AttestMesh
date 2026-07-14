# agent-session-mcp node — deploy runbook

The stateless central app tier for `agent-session-mcp` as a **mesh-only C3 node** on the
self-hosted dstack box. Backing stores are already live on C3: **HA Postgres+pgvector**
(pg-ha) and **HA Hindsight**. See also [`docs/specs/`] and the memory note
`agent-session-mcp-deploy`.

## Architecture (mesh-only, egress-locked)

```
mesh peers (ssh-node shipper, agents)
      │  <our-mesh-ip>:8080/ingest   <our-mesh-ip>:8081/mcp
      ▼
 sidecar netns (unlocked: chain egress + wg mesh)
   ├─ mesh-proxy-ingest / mesh-proxy-mcp  →  ingest:8080 / :8081
   ├─ pg1/2/3-proxy  sidecar:1543N  →  pg-ha 10.18.147.86/251.71/172.186 :5432
   └─ hindsight-proxy sidecar:18888 →  Hindsight 10.18.78.76:18888
      ▲
 app netns (deny-all egress except pg/hindsight forwarders + embeddings :443)
   ├─ ingest  (uvicorn app:app :8080)      ← owns the netns
   ├─ mcp     (mcp_server.py :8081)
   ├─ migrate (python db.py, one-shot)
   └─ app-egress-fw (locks the netns)
 pg-provision (one-shot, sidecar netns): CSK-derives pg superuser →
   CREATE ROLE/DB agent_sessions + CREATE EXTENSION vector
```

Why the app is NOT `network_mode: service:sidecar`: the sidecar needs open egress to the
Base RPC/bundler/gateway for on-chain registration; an egress lock on that netns would
break it. So the app runs in its own netns and reaches mesh services via sidecar-netns
socat forwarders (same pattern as `telegram-sync-node.yaml`).

## Live values (baked into the compose / wrapper)

| Thing | Value |
|---|---|
| Cluster C3 | `0x5ab4706fCa998A0792E5c06432b13c73c54E4557` |
| MEMBER_IMPL | `0xf3f594bdb5cc2d4a56e79fb6d4c616d65b0fbb7d` |
| KMS_ROOT | `0x52d3cf51c8a37a2ccfc79bbb98c7810d7dd4ce51` (from `deploy/env.sh`) |
| pg-ha nodes | `10.18.147.86 / 10.18.251.71 / 10.18.172.186` `:5432`=primary |
| pg-ha image (r6, pgvector) | `ghcr.io/attestmesh/pg-ha@sha256:3cce594b31cc9c57dea6f1b7fc4e6a3e9b31be15853c49e22c4c6423d7a9b066` |
| Hindsight API | `http://10.18.78.76:18888`, bank `agent-sessions`, token = `TAK` in `deploy/logs/hindsight-node-hindsight-node.state` |
| sidecar image | `ghcr.io/attestmesh/cluster-mesh-agent@sha256:f455ad9e…` |
| egress-fw image | `ghcr.io/attestmesh/agent-egress-fw@sha256:c75c9668…` |

## Embeddings — RedPill `qwen/qwen3-embedding-8b`

`EMBEDDING_URL=https://api.redpill.ai/v1`, `EMBEDDING_MODEL=qwen/qwen3-embedding-8b`,
`EMBEDDING_DIM=1024`. The key is the cluster's RedPill key (`~/.attestmesh/redpill-key`),
auto-sourced by `-node.sh` as `EMBEDDING_API_KEY` (same key hindsight uses).

**Important — dimensions:** RedPill returns a **native 4096-dim** vector and does **NOT**
honor the OpenAI `dimensions` param (upstream rejects it). The app MUST **truncate +
renormalize client-side to 1024** (stays under pgvector's 2000-dim HNSW cap). Do not send
`dimensions`.

Egress: the app netns is locked to `api.redpill.ai:443` (`ALLOW_CIDRS=66.220.6.0/24` +
`ALLOW_BASE_URL`=`EMBEDDING_URL`) + Docker DNS; pg/hindsight are same-mesh via forwarders.
Text egresses to RedPill (Phala confidential-compute) — the accepted in-infra-ish tradeoff.

## Steps

1. **Build + push the app image**, then pin its digest (3 occurrences) in
   `deploy/compose/agent-session-mcp-node.yaml` (replace `REPLACE_WITH_PUSHED_DIGEST`):
   ```
   cd /home/ubuntu/agent-session-mcp
   docker build -t ghcr.io/attestmesh/agent-session-mcp:v0.1.0 .
   docker push ghcr.io/attestmesh/agent-session-mcp:v0.1.0
   docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/attestmesh/agent-session-mcp:v0.1.0
   ```

2. **Secrets** — `-node.sh` auto-generates `~/.attestmesh/agent-session-mcp.env` (0600) with
   strong `APP_DB_PASSWORD` + `INGEST_TOKEN` on first run, and auto-sources the RedPill key
   from `~/.attestmesh/redpill-key` as `EMBEDDING_API_KEY`. Nothing to create by hand. (To
   override embeddings, add `EMBEDDING_URL/MODEL/DIM/API_KEY` to that file.)

3. **Deploy** (self-registers into C3, gets a mesh IP):
   ```
   cd /home/ubuntu/teemesh
   source deploy/env.sh
   deploy/agent-session-mcp-node.sh agent-session-mcp all
   ```
   `all` = preflight → deploy (stopped) → prime (allowlist KMS root/compose hash/app id
   on-chain) → bind (upgrade DstackApp → ClusterMember) → start → register-direct → verify.

4. **Find our mesh IP** (memberId-derived; from the box or ssh-node mesh shell):
   ```
   cast call $CLUSTER 'memberIdOf(address)(bytes32)' <app_id> --rpc-url $RPC_URL
   ```
   or read the sidecar's mesh status. Then smoke-test from the ssh-node mesh shell:
   ```
   ssh attestmesh-mesh-node 'curl -s -XPOST http://<our-mesh-ip>:8080/ingest \
      -H "Authorization: Bearer <INGEST_TOKEN>" -H "Content-Type: application/json" -d @sample.json'
   ```

5. **Shippers**:
   - **ssh-node** (frozen — do NOT roll): SSH into the mesh shell, install `uv`, run the
     shipper reading its Hermes `state.db` read-only, POST to `http://<our-mesh-ip>:8080/ingest`.
   - **this box** (off-mesh): tunnel via ssh-node —
     `ssh -L 8080:<our-mesh-ip>:8080 attestmesh-mesh-node` then POST to `127.0.0.1:8080`.

## Updates / rollback

- In-place roll (new compose hash auto-allowlisted, disk preserved):
  `deploy/agent-session-mcp-node.sh agent-session-mcp update`
- Stop (cleanup refuses while registered unless `FORCE_CLEANUP=1`):
  `deploy/agent-session-mcp-node.sh agent-session-mcp cleanup`

### Cost-gated Hindsight releases

Persistent worker controls live in
`~/.attestmesh/agent-session-hindsight.env`. The wrapper treats those values as
defaults and preserves explicit command-line overrides. Keep the persistent file
at the same cap before a roll so a later unattended update cannot restore an
unbounded worker. The staged recovery setting is two Agent documents in flight;
Hindsight allows three LLM calls per worker and the provider guard opens Agent
above six total provider calls.

`HINDSIGHT_OUTBOX_RUN_LIMIT` is cumulative: the worker counts `submitting`,
`submitted`, `succeeded`, `failed`, and `blocked`. A safe staged release is:

1. Open `hindsight_sync_state.circuit_open` and wait for provider `in_flight=0`.
2. Set the persistent cumulative run limit, then roll the CVM with the same
   explicit value. Keep total cross-bank concurrency at eight or less.
3. Confirm the API is healthy and the circuit stayed open before closing it.
4. Stop immediately on any failed/blocked row, 401/402, duplicate operation or
   document ID, budget circuit, or attempted count above the cumulative cap.
5. At the cap, require zero active Hindsight operations and stable spend before
   projecting and authorizing another stage.

An explicit retry must first prove that the old operation is terminal and the
document is absent. The app snapshots that attempt in
`hindsight_outbox_retry_history`; never clear or resubmit a `submitted` row.

## Notes / caveats

- Gateway is OFF (mesh-only). We dial all peers, so inbound mesh reachability works once
  our tunnels are up (matches telegram-sync). Nothing is exposed to the box/internet.
- `pg-provision` uses the **r6** pg-ha image (pgvector); do NOT downgrade to r5 `05c6d46c…`
  (no pgvector — `CREATE EXTENSION vector` would fail).
- `db.py` (migrate) must be idempotent — it re-runs on every boot.
- pg-ha is now load-bearing for Hindsight too; treat it as production.
