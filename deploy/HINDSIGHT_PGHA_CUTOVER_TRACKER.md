# Hindsight PG-HA Cutover Tracker

Last updated: 2026-07-08 01:46 UTC

## Operating Rule

Read this file before continuing Hindsight PG-HA work after any context reset or long debugging pause. Update it after every reroll, new failure mode, backup/import action, or material hypothesis change. Do not repeat a fix attempt unless this document says why it is being retried.

## Goal

Move the existing Hindsight deployment from embedded `pg0` storage to the shared `pg-ha` Postgres deployment, preserve/export the existing Hindsight data first, import that data into the new PG-HA-backed instance, and leave the service healthy over the mesh.

## Current State

- PG-HA is up and healthy.
- Hindsight node app id: `0xa151d945e3bdB7d68f8dFdc3602c98f80D2F46bc`.
- Hindsight VM id: `9190f502-7f67-4224-ba54-d29446df608e`.
- Hindsight mesh IP: `10.18.78.76`.
- Hindsight state is set to `DB_MODE=pgha`.
- Latest local state compose hash after the BusyBox log-tap reroll: `b0473cc5df50f3eb929c215ce0e31ff11493a99e2482036f54748666050eec29`.
- The interrupted reroll completed; no abandoned local roll process remained.
- Sidecar health was live at bridge IP `10.0.100.64:9090`.
- Mesh ports `18888`, `18999`, and `19090` were open after the reroll.
- PG-HA target DB `hindsight` was reachable with the Hindsight role, but as of the last check still had `0` public base tables. That means Hindsight migrations had not completed.
- Hindsight API mesh proxy on `10.18.78.76:18888` opened, but `/health` returned `curl: (52) Empty reply from server`, indicating the proxy was reachable while the backend API process was not serving.

## Backups Taken

- API/export backup:
  - Directory: `deploy/backups/hindsight-20260707T233054Z/`
  - Tarball: `deploy/backups/hindsight-20260707T233054Z.tar.gz`
  - Contains banks `dstack-expert`, `attestmesh-smoke`, document-transfer zips/manifests/stats/templates.
- VM disk backup:
  - Box path: `/srv/data/dstack/backups/hindsight-20260707T233054Z/hindsight-node-9190f502-7f67-4224-ba54-d29446df608e-hda.img`
  - SHA256: `f45f0f8b1981cc7d3165698804c160dec6b97092f724f0407601e79972dfd433`
  - Local metadata under `deploy/backups/hindsight-20260707T233054Z/`.
- Fresh pre-cutover API backup intended for import:
  - Directory: `deploy/backups/hindsight-precutover-20260707T235627Z/`
  - Tarball: `deploy/backups/hindsight-precutover-20260707T235627Z.tar.gz`
  - Tarball checksum verified.
  - Bank list counts:
    - `dstack-expert`: `fact_count=1155`, `last_document_at=2026-07-07T05:33:33.481855+00:00`
    - `attestmesh-smoke`: `fact_count=2`, `last_document_at=2026-07-01T23:10:10.559879+00:00`
  - Document-transfer manifest counts:
    - `dstack-expert`: `document_count=38`, `fact_count=685`, `observation_count=423`
    - `attestmesh-smoke`: `document_count=1`, `fact_count=1`, `observation_count=1`
- Removed partial failed backup directory:
  - `deploy/backups/hindsight-precutover-20260707T235547Z`

## PG-HA Work Completed

- Added `postgresql-16-pgvector` to `deploy/pg-ha/Dockerfile`.
- Built and pushed PG-HA image:
  - `ghcr.io/attestmesh/pg-ha@sha256:3cce594b31cc9c57dea6f1b7fc4e6a3e9b31be15853c49e22c4c6423d7a9b066`
- Updated `deploy/compose/pg-ha-node.yaml` to pin that digest.
- Rolled all three PG-HA nodes using `RPC_URL=https://base-rpc.publicnode.com` because the Alchemy RPC in `deploy/env.sh` returned `403`.
- PG-HA compose hash verified earlier:
  - `b526d3c5e52ded9e4f2096d9e192d612d0ee405b2f3e77f5010d2009c75698eb`
- Repeated `deploy/pg-ha-node.sh pg-ha verify-ha` passed:
  - leader `pg1`
  - `pg2` and `pg3` streaming
  - HAProxy primary/replica routing ok
  - replicated smoke write ok
- Extensions available:
  - `pg_trgm:1.6`
  - `vector:0.8.4`
- PG-HA peers:
  - `pg1=10.18.147.86`
  - `pg2=10.18.251.71`
  - `pg3=10.18.172.186`

## Hindsight Code/Compose Changes Made

- `deploy/compose/hindsight-node.yaml`
  - Added optional PG-HA forwarders in the sidecar netns:
    - `pg1-proxy`: sidecar `15431` to `pg1:5432`
    - `pg2-proxy`: sidecar `15432` to `pg2:5432`
    - `pg3-proxy`: sidecar `15433` to `pg3:5432`
    - gated by `HINDSIGHT_PGHA_ENABLED`
  - Added `agent-sock` volume and `AGENT_GRPC_SOCKET=/var/run/attestmesh/agent.sock`.
  - Added `pg-provision` one-shot using the PG-HA image digest:
    - derives PG-HA superuser password from CSK
    - creates/updates role `hindsight`
    - creates DB `hindsight`
    - creates extensions `vector` and `pg_trgm`
    - verifies Hindsight role login
  - Set Hindsight to depend on `pg-provision` success.
  - Added env:
    - `HINDSIGHT_API_DATABASE_URL`
    - `HINDSIGHT_API_VECTOR_EXTENSION=pgvector`
    - `HINDSIGHT_API_STARTUP_WAIT_SECONDS=900`
    - `LITELLM_LOCAL_MODEL_COST_MAP=True`
    - `LITELLM_LOCAL_BLOG_POSTS=True`
  - Added temporary debug wrapper around `/app/start-all.sh`:
    - prepends `/app/api/.venv/bin` to `PATH`
    - writes `/home/hindsight/.pg0/start-all.log`
  - Added temporary mesh-only log tap `hindsight-log-proxy` on `19090`.
    - Original `nc` loop was unreliable/closed.
    - Patched to a pure `socat` listener, but the interrupted reroll must be verified.
  - Fixed egress firewall config:
    - image only supports `INTERNAL_HOSTS`, not `INTERNAL_HOST_PORTS`
    - set `INTERNAL_HOSTS=sidecar` so Hindsight can reach sidecar PG-HA forwarders after default DROP
- `deploy/hindsight-node.sh`
  - Added `PGHA_STATE`.
  - State now saves `DB_MODE` and generated `DB_PASSWORD`.
  - `_resolve_database_env` supports `HINDSIGHT_DB_MODE=pg0|pgha`.
  - Parses PG-HA peers from `deploy/logs/pg-ha-pg-ha.state`.
  - Generates/persists the Hindsight DB password.
  - Seals PG-HA flags/IPs/password and database URL.
  - Current intended DSN:
    - `postgresql://hindsight:<urlencoded-password>@/hindsight?host=sidecar:15431&host=sidecar:15432&host=sidecar:15433&target_session_attrs=read-write`
- `deploy/hindsight-node-box.py`
  - Added new sealed env keys.
  - Fixed stop polling so `stopping` is not treated as stopped.
- `deploy/pg-ha-node-box.py`
  - Same stop-polling fix.

## Failed or Rejected Attempts

- `postgresql://...&connect_timeout=5`
  - Rejected because Hindsight/asyncpg passed `connect_timeout` as an unexpected keyword and crashed.
- `postgresql+asyncpg://...`
  - Rejected because Hindsight runtime calls `asyncpg.create_pool()` directly, and asyncpg rejects `postgresql+asyncpg://` as an invalid DSN.
- Entrypoint override without PATH fix
  - Rejected because `hindsight-api: command not found` appeared in the startup log.
  - Fixed by exporting `PATH="/app/api/.venv/bin:$PATH"` in the temporary wrapper.
- `INTERNAL_HOST_PORTS=sidecar:15431 ...`
  - Rejected because current `agent-egress-fw` image does not implement that env var.
  - Fixed by using `INTERNAL_HOSTS=sidecar`.
- Allowing outbound GitHub for LiteLLM startup fetch
  - Not chosen. Better fix is `LITELLM_LOCAL_MODEL_COST_MAP=True` and keep deny-all egress intact.
- Log tap with busybox `nc`
  - Rejected because mesh port `19090` was closed/unreliable and generated noisy listener behavior.
  - Replaced with a `socat` listener; verify after reroll.

## Current Working Hypothesis

Hindsight is still exiting before API readiness. PG-HA provision/login works, but migrations are not running to completion. The most useful next signal is the captured `/home/hindsight/.pg0/start-all.log` via the debug tap on mesh port `19090`; if that tap is still unavailable after the latest reroll, inspect VM serial and compose service lifecycle again.

## Timeline Notes

- 2026-07-08 01:15 UTC:
  - Verified live VM hash matches local state hash `0ac2a2e...`.
  - Verified no lingering `hindsight-node.sh`, `hindsight-node-box`, `cast send`, `UpgradeApp`, or `StartVm` process.
  - Verified sidecar health from bridge IP.
  - Verified mesh debug log tap `19090` is open.
  - PG-HA DB `hindsight` still had `0` public base tables.
- 2026-07-08 01:16 UTC:
  - `http://10.18.78.76:18888/health` still returned `curl: (52) Empty reply from server`.
  - `http://10.18.78.76:19090/` also returned `curl: (52) Empty reply from server`.
  - Serial log showed Hindsight container network interfaces repeatedly disappearing/reappearing roughly every 30 seconds, consistent with the Hindsight container crash-looping.
  - The `socat SYSTEM:'printf ...; tail ...'` log tap opens the TCP port but does not return a body. Treat that as a failed diagnostic transport and do not repeat it.
  - Next diagnostic fix: replace the log tap with BusyBox `httpd` serving `/logs/start-all.log` directly.
- 2026-07-08 01:18 UTC:
  - Rerolled with BusyBox `httpd` log tap.
  - New compose hash: `b0473cc5df50f3eb929c215ce0e31ff11493a99e2482036f54748666050eec29`.
  - Update completed in-place on existing VM/disk.
- 2026-07-08 01:21 UTC:
  - Verified live VM contains `busybox httpd`, but `http://10.18.78.76:19090/start-all.log` still returned an empty reply.
  - Serial log showed `dstack-hindsight-log-proxy-1` repeatedly starting, indicating the command exits.
  - Local test of `alpine/socat:1.8.0.0` with `--entrypoint /bin/sh` proved BusyBox in that image has no `httpd` applet.
  - Local test also proved raw `socat ... SYSTEM:'cat /logs/start-all.log 2>&1'` works and can be read with `nc`.
  - Do not retry `busybox httpd` in `alpine/socat:1.8.0.0`.
- 2026-07-08 01:22 UTC:
  - Rerolled with raw `socat cat` log tap.
  - New compose hash: `1dabd7941072dfbd80dd4cf25a553b822c19ec44e9f41bf20701e7a4f8b92bd0`.
  - Update completed in-place on existing VM/disk.
- 2026-07-08 01:24 UTC:
  - Sidecar returned after boot; mesh ports `18888`, `18999`, and `19090` were open.
  - Raw log tap works.
  - Startup log content was only: `Existing pg0 data directory detected at /home/hindsight/.pg0`.
  - `/health` still returned `curl: (52) Empty reply from server`.
  - PG-HA DB `hindsight` still had `0` public base tables.
  - Next step: inspect the Hindsight image's `/app/start-all.sh` locally because the process exits before emitting Python/API logs.
- 2026-07-08 01:25 UTC:
  - Local image inspection shows `/app/start-all.sh` runs `hindsight-api &` immediately after the pg0 integrity/writability checks, then waits for `/health`.
  - `hindsight-api` should print its banner before migrations. The absence of that banner means the child is likely hanging or dying during import/early `main()`.
  - No OOM, segfault, traceback, or kernel kill message appeared in serial logs.
  - Next patch should instrument the `hindsight-api` child with Python `faulthandler.dump_traceback_later()` and replace only the child command inside a copied `/tmp/start-all-debug.sh`.
- 2026-07-08 01:26 UTC:
  - Rerolled with instrumented `hindsight-api` child.
  - New compose hash: `05c035a365a5acbcd7b9929b0ec0fbc166cd02f01adb06eb5e2b28b5884b790e`.
  - Update completed in-place on existing VM/disk.
- 2026-07-08 01:30 UTC:
  - Instrumented log captured the actual failure.
  - `hindsight-api` imports successfully and starts.
  - It initializes local embeddings/reranker and verifies Redpill LLM egress successfully.
  - Migrations fail in Alembic/configparser:
    - `invalid interpolation syntax in 'postgresql://.../hindsight?host=sidecar%3A15431&host=sidecar%3A15432&host=sidecar%3A15433&target_session_attrs=read-write'`
  - Cause: Hindsight's migration path normalizes the multi-host query string through `urlencode`, turning `sidecar:15431` into `sidecar%3A15431`; Alembic `Config.set_main_option()` uses `configparser`, where `%3` is treated as invalid interpolation syntax.
  - Fix plan: keep runtime `HINDSIGHT_API_DATABASE_URL` multi-host for asyncpg, and add a separate `HINDSIGHT_API_MIGRATION_DATABASE_URL=postgresql://hindsight:<pw>@sidecar:15431/hindsight?target_session_attrs=read-write`.
  - Rationale: PG-HA node port `5432` is HAProxy primary routing, so `sidecar:15431 -> pg1:5432` still reaches the current primary while avoiding `%` characters in the Alembic URL.
- 2026-07-08 01:30 UTC:
  - Added `HINDSIGHT_API_MIGRATION_DATABASE_URL` to compose, `deploy/hindsight-node.sh`, and `deploy/hindsight-node-box.py`.
  - Rerolled with new compose hash: `1f1eaacaf62fff31615b90da9812b91cf6876cad384a55e6bc1770a9b8564e57`.
  - Update completed in-place on existing VM/disk.
- 2026-07-08 01:34 UTC:
  - Hindsight started successfully on PG-HA.
  - Migration log showed `Database migrations completed successfully for schema 'public'`.
  - `/health` over mesh returned `200 OK` with `{"status":"healthy","database":"connected"}`.
  - PG-HA `hindsight` DB now has 21 public base tables:
    - `alembic_version, async_operations, audit_log, bank_stats_cache, banks, chunks, directives, documents, entities, entity_cooccurrences, file_storage, graph_maintenance_queue, invalidated_memory_units, llm_requests, memory_links, memory_units, mental_model_history, mental_models, observation_history, unit_entities, webhooks`
  - Temporary instrumentation is still present and emits repeated faulthandler stack dumps every 45 seconds. Remove it before the final production reroll.
- 2026-07-08 01:42 UTC:
  - Pre-cutover backup import completed.
  - `dstack-expert` operation `193d8052-4672-4bfe-93d5-0a5695ee7582` completed:
    - documents imported: 38
    - facts imported: 685
    - observations imported: 423
    - skipped documents/observations: 0
  - `attestmesh-smoke` operation `0f830a7a-aef3-483c-962b-ebd155000ac8` completed:
    - documents imported: 1
    - facts imported: 1
    - observations imported: 1
    - skipped documents/observations: 0
  - Fresh imported stats are saved under `deploy/backups/hindsight-precutover-20260707T235627Z/import-results/banks/<bank>/imported-stats.json`.
  - Stats differences to remember:
    - `dstack-expert` total documents match backup (38). Total nodes are now 1158 vs backup 1155 because live background consolidation created more observations after import.
    - link counts differ because document-transfer re-embeds and re-resolves links/entities on import; the operation metadata is the authoritative transfer-count check.
    - `attestmesh-smoke` documents/nodes/observations match; semantic link count differs for the same re-resolution reason.
- 2026-07-08 01:43 UTC:
  - Removed temporary `hindsight-api` faulthandler wrapper and `hindsight-log-proxy`.
  - Clean compose services now: `sidecar, pg1-proxy, pg2-proxy, pg3-proxy, pg-provision, mesh-proxy-api, mesh-proxy-ui, hindsight, hindsight-egress-fw`.
  - Rerolled clean production compose.
  - New compose hash: `41341743ce9adaa28f962fddb5629eaa4877e4d97f24dfa49959c3c0002e8528`.
- 2026-07-08 01:46 UTC:
  - Final clean Hindsight health over mesh: `{"status":"healthy","database":"connected"}`.
  - Mesh ports:
    - `18888` open
    - `18999` open
    - `19090` closed (debug log tap removed)
  - Auth gate verified:
    - unauthenticated `/v1/default/banks` returned `401`
    - authenticated `/v1/default/banks` returned both banks: `attestmesh-smoke`, `dstack-expert`
  - Host isolation passed:
    - box bridge IP refused `8888`, `9999`, `18888`, `18999`
    - only sidecar health `9090` answered as expected
  - PG-HA verification passed after the cutover:
    - `pg1` leader
    - `pg2` and `pg3` streaming
    - HAProxy primary/replica routing ok
    - replicated smoke write ok
  - Final stats saved:
    - `deploy/backups/hindsight-precutover-20260707T235627Z/import-results/banks/dstack-expert/final-stats-after-clean-reroll.json`
    - `deploy/backups/hindsight-precutover-20260707T235627Z/import-results/banks/attestmesh-smoke/final-stats-after-clean-reroll.json`
  - Final observed data:
    - PG-HA DB: 21 tables, 2 banks, 39 documents, 1163 memory units, 122 chunks at the direct DB count check.
    - `dstack-expert` final API stats: 38 documents, 1171 total nodes, 486 observations, 0 failed operations. Background consolidation still had active processing and pending consolidation, so node/observation totals may continue changing.
    - `attestmesh-smoke` final API stats: 1 document, 2 nodes, 1 observation, 0 failed operations.

## Current Follow-Up

1. No required cutover work remains.
2. Optional later cleanup: let `dstack-expert` background consolidation drain and capture another stats snapshot if exact derived-observation counts matter.
3. Optional commit/review hygiene: separate the Hindsight/PG-HA cutover changes from unrelated dirty worktree changes before committing.

## Import Procedure Used

Use the API over mesh at `http://10.18.78.76:18888` with the sealed tenant key from local state. Import each bank from `deploy/backups/hindsight-precutover-20260707T235627Z/banks/<bank>/document-transfer.zip`, poll the returned operation until completion, then save post-import stats. Expected document-transfer counts:

- `dstack-expert`: 38 documents, 685 facts, 423 observations
- `attestmesh-smoke`: 1 document, 1 fact, 1 observation

The bank-list `fact_count` values differ from document-transfer manifest counts because Hindsight stats include derived/linked nodes.

## Handoff Notes

- Do not print or commit tenant API key, CP key, or Hindsight DB password.
- The local state file contains secrets and should not be copied into this document.
- 2026-07-08T01:51Z: Backed up the Hindsight local state file containing TAK/CPK to `~/.attestmesh/hindsight-node-hindsight-node.state` with `0600` permissions.
- Public RPC has been required for roll commands:
  - `RPC_URL=https://base-rpc.publicnode.com`
- Always run Hindsight roll commands with:
  - `HINDSIGHT_DB_MODE=pgha`
  - `unset HINDSIGHT_API_DATABASE_URL`
- Do not use `BOX_FRESH_DISK=1` unless the user explicitly asks to wipe the existing Hindsight disk.
