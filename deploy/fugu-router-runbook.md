# Fugu-Router + redis-ha + clickhouse-ha — Operator Runbook

How to deploy, operate, roll, and verify the Sakana-Fugu subscription-pooling
stack: a **fugu-router** node (LiteLLM proxy + Langfuse v3 observability) backed
by three first-class HA clusters on the C3 mesh — the existing **pg-ha**
(Postgres) and **r2-host** (S3), plus two NEW clusters this runbook also covers:
**redis-ha** and **clickhouse-ha** (both mirror the pg-ha pattern). This is the
do-this-then-that; the measured runtime payloads with inline design comments are
the composes, the drivers are the `.sh` files:

| Deployment | Compose | Driver | Box helper |
|---|---|---|---|
| redis-ha (r1–r3) | `deploy/compose/redis-ha-node.yaml` | `deploy/redis-ha-node.sh` | `deploy/redis-ha-node-box.py` |
| clickhouse-ha (ch1–ch3) | `deploy/compose/clickhouse-ha-node.yaml` | `deploy/clickhouse-ha-node.sh` | `deploy/clickhouse-ha-node-box.py` |
| fugu-router (1 CVM) | `deploy/compose/fugu-router-node.yaml` | `deploy/fugu-router-node.sh` | `deploy/fugu-router-node-box.py` |

> **UPDATE 2026-07-04 — langfuse split.** Langfuse (web/worker + its ClickHouse,
> S3, tailnet, and pg role/db wiring) moved OFF this node onto its own CVM,
> **langfuse-node** (`deploy/langfuse-node.sh` + `deploy/compose/langfuse-node.yaml`,
> own runbook). fugu-router is now a **strictly mesh-only LiteLLM proxy**: pg-ha
> + redis-ha forwarders only, and a `:18420` sidecar-netns forwarder to
> `LANGFUSE_NODE_IP:18420` (read from `deploy/logs/langfuse-node-langfuse-node.state`)
> for the trace callback. The tailnet membership, `verify-clickhouse` /
> `verify-s3` / `verify-tailnet` phases, and the multipart/presigned-URL risks
> all moved with langfuse; `verify-langfuse-trace` now queries the langfuse-node
> API directly. Rolling fugu-router onto this slimmed compose (fresh Langfuse
> stack on langfuse-node, fresh `LANGFUSE_INIT_*` state) is also the clean-slate
> path for known issue #1 below (the 401 api-key mismatch on the churned DB).
> Sections below describing langfuse-on-fugu-router are kept for history.

**Design stance: MESH-ONLY** — fugu-router itself has NO tailnet membership.
The Langfuse dashboard's tailnet exposure lives on langfuse-node now; LiteLLM
stays strictly mesh-only (`:18410`).

---

## 1. Architecture recap

```
Hermes agents (mesh) ──► fugu-router <mesh-ip>:18410  (LiteLLM, LITELLM_MASTER_KEY)
Operator (tailnet)   ──► https://fugu-router-N.tail39cb2e.ts.net  (Langfuse UI ONLY)
Operator (mesh)      ──► fugu-router <mesh-ip>:18420  (Langfuse UI)

fugu-router CVM (2 vcpu / 4096 MB / 30 GB, bridge, gateway OFF, no_instance_id):
  sidecar (wg mesh, :9090/:51900 only published ports)
  socat forwarders in the sidecar netns:
    :15431-3 → pg-ha        10.18.147.86 / 10.18.251.71 / 10.18.172.186 :5432
    :16379-81 → redis-ha    <r1/r2/r3 mesh IP> :6379   (any = current master, HAProxy)
    :18123-5 + :19001-3 → clickhouse-ha <ch1/2/3 mesh IP> :8123/:9000
    :19000 → r2-host        10.18.163.210:19000
  pg-provision (one-shot, CSK-derives pg-ha superuser; creates litellm+langfuse roles/dbs)
  csk-derive   (one-shot, CSK-derives redis/CH client passwords for the stock langfuse images)
  litellm (+ egress-fw: SAKANA_CIDRS:443 + internal only)
  langfuse-web / langfuse-worker (+ egress-fw's: ZERO external — telemetry-off at L3)
  mesh-proxy-litellm :18410 / mesh-proxy-langfuse :18420 (mesh-IP-bound socat)
  tailscale + ts-firewall (serve :443 → langfuse-web:3000; everything else DROPped)

redis-ha (3 CVMs, 2/2048/20 each): redis :6380 + sentinel :26379 (quorum 2) +
  HAProxy on the mesh IP — :6379 → master (tcp-check role:master), :6381 → replicas.
  Auth CSK-derived (attestmesh.redisha.auth.v1); sealed meshverify credential for drills.

clickhouse-ha (3 CVMs, 2/4096/60 each): ClickHouse Keeper raft (:9181/:9234) +
  clickhouse-server (internal :18123/:19001) + HAProxy on the mesh IP — :8123/:9000
  round-robin over healthy replicas (httpchk /ping). Cluster `default` = 1 shard ×
  3 replicas, ReplicatedMergeTree; auth CSK-derived (attestmesh.chha.client.v1).
```

Key propagation is trust-minimized: pg-ha superuser, redis, and ClickHouse client
passwords are HKDF-derived **from the CSK in-CVM** — any C3 member derives them;
none of them ride the sealed env or leave the mesh.

Port **:18430** on fugu-router is reserved for the future ACP placement adapter.

## 2. Order of operations (all three deployments)

**Wave 1 (done before any deploy):** images built + digests pinned — replace in
the composes: `PLACEHOLDER_REDIS_HA`, `PLACEHOLDER_CLICKHOUSE_HA`,
`PLACEHOLDER_FUGU_ROUTER`, plus the `PIN-DIGEST` comments (langfuse ×2,
tailscale). Resolve `SAKANA_CIDRS`, pre-create the r2 bucket + `langfuse-events/`
/ `langfuse-media/` prefixes, mint `TS_AUTHKEY`.

**Wave 2 — redis-ha ∥ clickhouse-ha (two parallel tracks, each serial inside):**

```bash
source deploy/env.sh
# per track (redis-ha shown; clickhouse-ha is identical with its driver):
deploy/redis-ha-node.sh redis-ha register-all      # N stock DstackApps, NO VMs yet
deploy/redis-ha-node.sh redis-ha compute-peers     # off-chain mesh-IP precompute
#   → REDIS_PEERS now in deploy/logs/redis-ha-redis-ha.state; splice/read the IPs
#     for the fugu-router forwarders (the fugu driver reads the node state files).
deploy/redis-ha-node.sh redis-ha create-all        # CreateVm with the sealed peer list
deploy/redis-ha-node.sh redis-ha prime             # addComposeHash + addAllowedAppId
deploy/redis-ha-node.sh redis-ha bind              # upgradeToAndCall → ClusterMember
deploy/redis-ha-node.sh redis-ha verify            # memberIdOf != 0 + mesh-IP drift guard
deploy/redis-ha-node.sh redis-ha verify-ha
deploy/redis-ha-node.sh redis-ha verify-failover   # StopVm master → promote → rejoin
deploy/redis-ha-node.sh redis-ha verify-isolation
```

⚠ **Serialize the CHAIN steps across the two tracks** (prime/bind send txs from
the same deployer key — send_seq nonce discipline); parallelize everything else.

**GATE (Dan, blocking):** `deploy/fugu-router-node.sh fugu-router setup`
generates `~/.attestmesh/fugu-router.env` (0600) with everything EXCEPT the
Sakana keys; it prints the **training-opt-out reminder** and exits nonzero until
`SAKANA_API_BASE`, `SAKANA_SUB_1_KEY` (+ optional 2/3), `SAKANA_PAYG_KEY`,
`SAKANA_CIDRS`, and `TS_AUTHKEY` are filled in. Wave 2 runs while waiting — the
gate only blocks Wave 3.

**Wave 3 — fugu-router (serial, on-chain):**

```bash
deploy/fugu-router-node.sh fugu-router deploy   # reads redis/ch IPs from the state files
deploy/fugu-router-node.sh fugu-router prime
deploy/fugu-router-node.sh fugu-router bind     # runs the hash pre-check first (risk 4)
deploy/fugu-router-node.sh fugu-router verify
deploy/fugu-router-node.sh fugu-router verify-health
```

**Wave 4 — verifies:** `verify-db ∥ verify-clickhouse ∥ verify-s3 ∥ verify-redis
∥ verify-isolation ∥ verify-tailnet`, then `verify-proxy → verify-langfuse-trace`
(the proxy check makes ONE tiny real fugu-ultra completion; the trace check
proves callback → worker → ClickHouse → S3 end-to-end).

## 3. Verify matrix (definition of done)

| Check | Command (driver action) | Good looks like |
|---|---|---|
| redis-ha membership | `redis-ha-node.sh redis-ha verify` | 3 memberIds, mesh IPs match precompute |
| redis-ha HA | `verify-ha` | 1 master + 2 replicas; every node 6379→master, 6381→replica; write on :6379 read on :6381 |
| redis-ha failover | `verify-failover` | promote ≤60s, writes recover, old master rejoins as replica |
| redis-ha isolation | `verify-isolation` | 6379/6380/6381/26379/8009 refused at bridge IP |
| clickhouse-ha membership | `clickhouse-ha-node.sh clickhouse-ha verify` | 3 memberIds, IPs match |
| clickhouse-ha HA | `verify-ha` | system.clusters=3, keeper 1 leader + 2 followers, ON CLUSTER ReplicatedMergeTree write visible on all |
| clickhouse-ha replica-loss | `verify-failover` | writes continue via survivors; restarted replica catches up (system.replicas) |
| clickhouse-ha isolation | `verify-isolation` | 8123/9000/9181/9234/18123/19001/8009 refused |
| fugu membership | `fugu-router-node.sh fugu-router verify` | memberId != 0 |
| sidecar health | `verify-health` | `phase` JSON at bridge-IP:9090 (binds POST-bind only) |
| databases | `verify-db` | litellm has `LiteLLM_VerificationToken`; langfuse has `projects`+`api_keys` |
| clickhouse | `verify-clickhouse` | `/api/public/ready` 200 + langfuse tables replicated on all 3 ch nodes |
| S3 | `verify-s3` | PutObject + FULL multipart round-trip under `langfuse-events/verify/` |
| redis path | `verify-redis` | SET via the CVM :16379 forwarder readable at redis-ha :6379 |
| proxy | `verify-proxy` | `/v1/models` lists fugu-ultra; one real completion returns choices |
| trace | `verify-langfuse-trace` | the completion's trace visible via the public API WITH orchestration-token metadata + non-zero cost |
| isolation | `verify-isolation` | all service ports refused at bridge IP; only 9090/51900 answer |
| tailnet | `verify-tailnet` | Langfuse health 200 over MagicDNS; LiteLLM + all other ports NOT reachable via the tailnet IP |

## 4. Access

- **Hermes / agents (mesh):** base URL `http://<fugu mesh-ip>:18410/v1`, key =
  `LITELLM_MASTER_KEY` from `~/.attestmesh/fugu-router.env`.
- **Langfuse UI:** `https://<fugu-router MagicDNS>.tail39cb2e.ts.net` (name
  bumps on fresh-disk rolls — check `tailscale status`), login =
  `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`. Mesh alternative:
  `ssh -L 3000:<fugu mesh-ip>:18420 attestmesh-mesh-node`.
- **redis-ha from any C3 member:** any node's mesh IP `:6379` (master) /
  `:6381` (reads); password = `csk_derive attestmesh.redisha.auth.v1` in-CVM.
- **clickhouse-ha from any C3 member:** any node's mesh IP `:8123`/`:9000`;
  user `default`, password = `csk_derive attestmesh.chha.client.v1` in-CVM.
- State files (0600, do NOT commit): `deploy/logs/redis-ha-*.state`,
  `deploy/logs/clickhouse-ha-*.state`, `deploy/logs/fugu-router-node-*.state`.
- Mesh-only status pages on redis-ha/clickhouse-ha nodes: `<mesh-ip>:8009`
  (entrypoint phase self-reports; the TEE blocks container logs).

## 5. Day-2

- **Roll a compose/env change:** `<driver> <name> update [<node>]` — computes
  the new hash, allowlists it FIRST, then in-place StopVm→UpgradeApp→StartVm.
  HA clusters: `update-all` serializes node-by-node with a `verify-ha` gate
  between each so quorum is never at risk.
- **Rotate Sakana keys / SAKANA_CIDRS / any sealed value:** values are sealed,
  not measured — same compose hash, plain `update`.
- **Fresh disk** (`BOX_FRESH_DISK=1 … update`): membership + mesh IP kept
  (no_instance_id → app-bound disk key). redis/ch replicas re-sync from peers;
  on fugu-router it wipes ts-state → the MagicDNS name bumps.
- **Scale-out** (redis/ch): bump `REDISHA_COUNT`/`CHHA_COUNT`, re-run
  register-all → compute-peers → create-all (the initialized flag makes new
  nodes join, not bootstrap), then `update-all` to re-seal the peer list.

## 6. Risks (kept in sync with the fugu-router compose header)

1. **r2-host multipart unproven** — rclone-serve-s3 gateway; event ingest is
   small PutObjects (fine); `verify-s3` exercises multipart explicitly. Kill
   switch: `LANGFUSE_S3_MEDIA_UPLOAD_ENABLED=false`.
2. **Sakana API likely CDN-fronted** — pin the NARROWEST observed CIDRs in
   `SAKANA_CIDRS`; litellm-egress-fw re-resolves on a short interval.
3. **Presigned media URLs are mesh-internal** (`http://sidecar:19000`) — fine
   over mesh SOCKS, unusable publicly by design.
4. **C3 membership is permanent** (no removeMember) — the fugu driver runs a
   `hash`-mode pre-check before bind; validate app configs pre-bind.
5. **Redis clients don't follow sentinel here** — redis-ha's HAProxy
   indirection gives LiteLLM/Langfuse a single endpoint that always points at
   the master; three forwarders for node-failure tolerance.

Also inherited: sealed values transit the box as `sudo E_*` stdin (webhost
pattern — never argv); the sidecar binds :9090 only POST-bind; Prisma
(langfuse) has no multi-host DSN — it pins `sidecar:15431`, whose pg-ha HAProxy
itself tracks the primary.

## 7. Future work

- **Admin agents + prometheus stacks on redis-ha and clickhouse-ha** — v1 keeps
  those composes minimal (sidecar + service trio + status-server); port the
  pg-ha admin-agent/prometheus/matrix-proxy block when chat-ops are wanted.
- **ACP placement adapter** on fugu-router port **:18430** (reserved).
- **Sub-vs-PAYG cooldown calibration**: `cooldown_time: 60` is the safe initial
  value; refine quota-vs-ratelimit handling post-calibration via `update`.
- Repo-wide fix for E_* secret transit (env-file over stdin) tracked in the
  2026-07 deploy-scripts review.

---

## Known issues / follow-ups (as of 2026-07-04 deploy)

The core system is live and verified: both HA clusters (redis-ha, clickhouse-ha) passed
HA + failover + isolation drills; the fugu-router node is a registered C3 member; **real
Fugu Ultra completions flow through `:18410`** and the responses carry the orchestration-token
split (`prompt_tokens_details.orchestration_input_tokens`) that `fugu_telemetry` captures
(the custom callback IS loaded — confirmed in `/health/readiness`). Langfuse UI/storage is
deployed and serving on mesh `:18420` + the tailnet; Postgres (pg-ha), ClickHouse
(clickhouse-ha, DB `langfuse` created ON CLUSTER by `ch-provision`), Redis (redis-ha), and
S3 (r2-host, multipart verified) are all wired and reachable.

**1. Langfuse trace ingestion — 401 api-key mismatch (OPEN — the langfuse-node
split IS the clean-slate fix path: fresh PG state + `LANGFUSE_INIT_*` rebuilt
from scratch on the new CVM; keep fugu-router's `LANGFUSE_INIT_PROJECT_*` in
sync with langfuse-node's, then re-fire a completion and re-check).**
LiteLLM's langfuse logger (`LangfusePromptManagement`) is loaded and attempts to POST traces,
but Langfuse rejects them (the ingestion bull-queues stay empty). Direct `curl -u pk:sk` to
`/api/public/*` with the env project keys also returns 401. Diagnosis: the persisted
`api_keys` row's `fast_hashed_secret_key` does not verify against the env
`LANGFUSE_INIT_PROJECT_SECRET_KEY` — the **public** keys match (env == PG row), only the
secret hash differs. `LANGFUSE_SALT`/init keys ARE in `allowed_envs` and reach the container,
and HKDF/derivation is not involved here (these are sealed, not CSK-derived). Most likely a
langfuse headless-init artifact on a database churned by ~15 debug rolls, not a clean-deploy
condition. **Next step to try:** on a clean external Langfuse PG (or after
`DELETE FROM api_keys; DELETE FROM projects; DELETE FROM organizations;` + restart so
`LANGFUSE_INIT` rebuilds from scratch), re-fire a completion and check
`SELECT count() FROM langfuse.observations`. If it still mismatches, dump the container's
actual `SALT`/`LANGFUSE_INIT_PROJECT_SECRET_KEY` (temporary env tap) and compare byte-for-byte
to the sealed env, then reconcile langfuse's hash function. The `fugu_telemetry` orchestration
capture — the paper's novel contribution — is independently proven in the completion payloads.

**2. redis-ha HAProxy does not auto-recover after a failover (OPEN, workaround known).**
After the `verify-failover` drill promoted a new master, all three nodes' HAProxy `:6379`/`:6381`
listeners stopped routing (accept-but-timeout) until the HAProxy containers were restarted
(an in-place roll of the redis nodes fixed it immediately). Backends `:6380` stayed healthy
throughout. The `tcp-check` (`AUTH`→`INFO replication`→expect `role:master`, then a `QUIT`→
`expect +OK` dance) is fragile; harden it (drop the QUIT step, add a `PING`→`+PONG` probe) so
routing re-converges within a check window without a manual restart. Nothing in this stack
uses `:6381` (replica reads), so only `:6379` matters operationally today.

**3. `ch-provision` curl raced the forwarder on first boot (FIXED, verify on next clean deploy).**
The langfuse ClickHouse database must be pre-created (`CREATE DATABASE langfuse ON CLUSTER
default`) because langfuse's migration runner `SHOW TABLES FROM langfuse` before creating it.
The `ch-provision` one-shot now retries + verifies `EXISTS DATABASE` in a loop; the first
version's single curl didn't stick and the DB was created manually during this deploy.

**4. dstack in-place `UpgradeApp` does not reliably recreate changed one-shot/diagnostic
containers.** Several compose edits to already-exited one-shots (and the diag containers) were
not picked up until a `BOX_FRESH_DISK=1` roll. For config changes to `restart: on-failure`
one-shots (csk-derive, ch-provision, pg-provision) whose output persists on a named volume,
prefer a fresh-disk roll, or make their output volumes ephemeral so they always re-run.

**5. Smithers routines not yet authored** for redis-ha / clickhouse-ha / fugu-router (the
trios are directly runnable and were used for this whole deploy). `deploy/workflows/pg-ha.tsx`
is the model for the two HA clusters. Follow-up.
