# DRY AttestMesh deployment + deterministic Smithers routines

> **Status: PLANNED — implementation not started** (2026-07-05).
> Supersedes/executes the findings of the 2026-07-01/03 deploy-script reviews (~55 confirmed findings, none fixed).
> Branch when work starts: `deploy-dry` off `main`.

## Context

The 2026-07-01/03 deploy-script review found ~55 confirmed issues across what were then 12 node families; its core diagnosis was **copy-paste drift**: ~400 lines duplicated per `*-node.sh`, 16 near-identical `*-box.py` drivers, with fixes landing in one family and never propagating. None of the findings were fixed. Since then the tree grew to ~16 families (fugu-router, langfuse, redis-ha, clickhouse-ha), the `9090:9090` publish spread to 14/17 composes, and the HA families flipped gateway-ON — widening the exposure class the review flagged.

Goals:
1. **DRY the deployment code** — shared `deploy/node-lib.sh` + `deploy/ha-cluster-lib.sh` (best-of-breed = pg-ha/langfuse variants) + one parameterized box driver, with per-family files reduced to declarative config + family-specific verify steps.
2. **Encode repeated deploy actions as Smithers routines** that are deterministic: every step a shell command with machine-checkable success (exit codes / literal markers), so small cheap open-source models (redpill) can drive them with near-zero interpretation.
3. Fix the systemic findings (9090 publishes, argv secrets, broken matrix restore, etc.) as part of the consolidation rather than as 16 one-off patches.

Key safety invariant: the rendered app-compose is measured on-chain (compose hash). Consolidated code must render **byte-identical** app-compose output per family vs. the current drivers — verified mechanically before any deploy path switches over.

## Exploration findings (condensed)

**Smithers** (`smithers-orchestrator` v0.22.0, `deploy/workflows/*.tsx`):
- "Routine" is not a Smithers primitive — it's this repo's word for a deterministic `.tsx` workflow wrapping idempotent bash subcommands. The existing 5 workflows (deploy, matrix-node, postgres-node, pg-ha, r2-host-node) are already 100% deterministic: `<Task>` with a function child, `execSync("… deploy/<x>-node.sh <name> <sub>")`, success = exit code, zero LLM anywhere. Durable/resumable via sqlite (`--run-id … --resume`).
- A `<Task>` only becomes agentic if given an `agent=` prop (OpenAIAgent/HermesAgent support `baseURL` → redpill for small OSS models). Components like `DecisionTable`, `Poller`, `Approval`, `Subflow` exist if ever needed.
- fugu-router-runbook.md:265 already flags "Smithers routines not yet authored for redis-ha / clickhouse-ha / fugu-router; pg-ha.tsx is the model". 11 of 16 families have no workflow.
- Verified: dynamic task generation from `ctx.input` is resume-safe (input durably persisted; resume refuses without the stored input row; graph = pure function of input + checked-in config). Verified: engine does NOT enforce zod refinements/literals on input — the build fn's `Input.parse(ctx.input)` is the authoritative guard. Verified: `up` exit codes 0=finished/1=failed/2=cancelled/3=waiting-approval/4=usage; `inspect/output/why --json` exist. Verified: **smithers `cron` is unusable here** (no `--input`; scheduler spawns with a package-relative cwd) → use OS cron. Footgun: a stale second `smithers.db` at repo root misroutes any CLI call from there (the live one is `deploy/smithers.db`).

**Shell scripts** (16 families, 3 archetypes: single-node ×11; single+cluster-bootstrap ×2 = matrix, webhost; multi-node HA ×3 = pg-ha, redis-ha, clickhouse-ha):
- ~400 lines/family duplicated; redis-ha/clickhouse-ha are near-verbatim pg-ha clones. Best-of-breed per function: `send_seq` (uniform everywhere), `_box_run` stdin-`%q` secrets (pg-ha:179 — 5 families still pass secrets on remote argv: ssh, postgres, matrix, runyard, hindsight), state save `umask 077` heredoc (pg-ha; matrix lacks it and stores PGPW/IAPW), `_ensure_secrets` (langfuse:143), idempotent `prime_gate` guarding hash+appId (langfuse:283), `bind_member` with hash pre-check (langfuse:301 — newer than pg-ha), `/dev/tcp` isolation probe (pg-ha:601 — 7 families have NO isolation check; 3 use leaky curl), health probe (langfuse:359), serialized verify-gated rolling update (pg-ha:710).
- Verb surface: universal `deploy/prime/bind/verify/update/all` (+`setup`); HA adds `register-all/compute-peers/create-all/…-all/verify-ha/verify-failover`; per-family `verify-*` extras are the genuinely unique part.
- 8 extraction blockers catalogued (matrix prime omits addComposeHash by design; `_box_run` arity single vs HA; update signature; HA ordering: register-all→compute-peers→create-all; hardcoded gateway domain/box IP/sibling mesh IPs; box.py `hash`-mode contract; two different mesh jump-host mechanisms; matrix umask tightening is a behavior change).

**Box drivers** (16 `*-node-box.py`, one 5-part skeleton, CreateVm block copy-pasted verbatim in 15/16):
- Hash-affecting per-family fields (must be reproduced exactly by a unified driver): `name`, `gateway_enabled`, `no_instance_id`, `allowed_envs` (= sorted ENV_KEYS ∪ {APP_ID} — names only), `pre_launch_script` (docker-login everywhere; generic adds APP_ENV_B64→env_file), and the compose YAML itself (incl. `ports:` → the 9090 publishes are measured). Hash = `sha256(json.dumps(app_compose, indent=4))`; dict insertion order matters; `pre_launch_script` is assigned last.
- NOT hashed (free to unify/fix): vcpu/mem/disk, net_mode, BOX_PORTS host-forwards, kms/gateway URLs, StopVm hardening, FRESH_DISK handling, mode set. HA trio adds `register/create/stop/start` modes; 11 drivers have StopVm stale-VM hardening, pg-ha/redis/clickhouse/postgres do NOT; matrix is the legacy outlier (m.deploy_app + raw httpx update, output missing app_id).
- Output contract: one JSON line per mode (`hash` mode = bare 64-hex). cluster-orchestrator.py (:8789) already proves the "one parameterized driver" concept end-to-end via generic-node.sh + APP_ENV_B64 + flavors.

**Composes** (17): sidecar digest identical everywhere; `9090:9090` published in 14/17 (only matrix, pg-ha, postgres clean); digest-pinning gaps (synapse/prom-stack/tailscale `:latest`, hindsight tag, synclave front/tls-proxy tags); one-shot `restart:"no"` services in 5 (webhost's hard-gates tee-daemon).

**Live-fleet constraints**: C3 membership is permanent (no removeMember); **ssh-node is frozen** (hosts the Verity Hermes agent — do not roll); a staged node roll is already pending GO (sidecar resend-storm fixes) — hash-changing compose fixes should ride that same roll. Any compose/manifest change ⇒ new attested hash ⇒ prime (addComposeHash) + update roll per family.

## Decisions

1. **Branch**: new branch `deploy-dry` off `main` (current checkout is `matrix-admin-agent`).
2. **Frozen set correction**: the frozen node is **ssh-node** (hosts the Verity Hermes agent — 🔴 do-not-roll per Dan 2026-07-02). hermes-node has **no live deployment** (no state file) — nothing to roll there. Seed `frozen: ["ssh"]`; confirm with Dan before any roll. Live node names ≠ family names in places (webhost's node = `open-webhost`; runyard has 2 nodes: `runyard-node`, `open-webhost-runyard`) — families.json must carry real defaults.
3. **Source of truth for per-family config**: the family `.sh` config block (bash arrays) is authoritative for deploy parameters; `deploy/families.json` (orchestration metadata: script, archetype, defaultName(s), verb lists, update verb, frozen) is **generated** from the scripts via a new `list-verbs` verb (`deploy/gen-families.sh`), checked in, and drift-checked in CI/harness — one source of truth, no hand-sync.
4. **No LLM anywhere in the routines.** Smithers orchestration stays 100% deterministic (exit-code judged). The only sanctioned LLM touchpoint is an *optional, quarantined, advisory* triage script (`routine.sh triage --llm`, redpill small model) that runs only after the deterministic classifier returns no match, and whose output is printed, never executed.
5. **Hash-changing fixes (9090 removal, digest pins) are prepared + primed but rolled only via the already-pending staged fleet roll after Dan's GO** — no second roll is forced; ssh-node excluded; hermes has nothing to roll.
6. **Hash-stability is the merge gate**: no driver/script consolidation ships unless the rendered app-compose JSON is byte-identical per family (old vs new) — verified mechanically, since the hash is on-chain-attested and a mismatch bricks the next boot at the KMS gate.

## Evaluation verdict: what becomes a Smithers routine

"Routine" here = a deterministic Smithers `.tsx` workflow (exit-code-judged `<Task>`s wrapping idempotent script verbs) + a uniform `deploy/routine.sh` entry point. Verified: dynamic task generation from `ctx.input` is resume-safe (input is durably persisted; graph = pure function of input + families.json), so **generic parameterized workflows replace per-family copies** — 9 routine files cover all 16 families instead of 16 `.tsx` clones. Small models interact only via `routine.sh run <name> '<tiny-json>'` → one `RESULT {json}` line; success is only ever an exit code. Repeated *shell* logic is NOT moved into Smithers — it moves into `node-lib.sh`; Smithers stays a thin durable sequencer over script verbs (its proven role).

## Plan

### Phase A — shared shell libs + rebase all 16 families (hash-neutral; no rolls, no compose edits)

New files:
- **`deploy/node-lib.sh`** (~450 lines): promoted best-of-breed primitives — `send_seq` (verbatim), `ssh_box`, `scp_box` (checked, `|| die`), `box_run` (stdin-`%q` secrets ALWAYS, from pg-ha-node.sh:179; single-vs-HA arity via `BOX_MEMBER` env; **forwards `BOX_FRESH_DISK` once, unconditionally** — fixes matrix restore), `state_save/state_load` (umask 077 heredoc; `EXTRA_STATE_KEYS` per family, must be superset of old keys), `ensure_secrets` (langfuse:143; generic-node persists `APP_ENV_B64` here — fixes the day-2 env wipe), `prime_gate` (idempotent, tolerates matrix's no-local-hash case), `bind_member` (langfuse:301 hash pre-check + sleep-2 readback), `register_poll` (+ optional mesh-IP drift assertion), `verify_health` (`HEALTH_VANTAGE=box|mesh`), `verify_isolation` (`/dev/tcp` from pg-ha:601; `ISOLATION_PORTS_CLOSED/OPEN` arrays), `update_member`, `restore_member`, `mesh_ssh`/`mesh_ssh_gw`, `node_main` (dispatch; `all` iterates `ALL_SEQUENCE` with hard-fail per step; `family_dispatch` fallthrough keeps every family-specific verb name; utility verbs `hash`, `print-box-env`, `list-verbs`, `drift`).
  Family contract: config vars (`NODE_FAMILY`, `BOX_*` sizing, `BOX_GATEWAY_ENABLED`/`BOX_NO_INSTANCE_ID` explicit, `ENV_KEYS`, `PRE_LAUNCH_VARIANT`, `ISOLATION_PORTS_*`, `SECRETS_FILE`/`SECRET_KEYS`, `EXTRA_STATE_KEYS`, `ALL_SEQUENCE`, `FAMILY_VERBS`) + hooks (`family_env_values` via `emit_env`, `family_resolve_deps` via `resolve_sibling` with env>state>default>die precedence, `family_pre/post_deploy`, `family_seed_secrets`, `family_dispatch`).
- **`deploy/ha-cluster-lib.sh`** (~350 lines): the N-node choreography promoted verbatim from pg-ha (register_all, compute_peers, create_all, prime_all, bind_all, verify_all with mesh-IP drift hard-fail, update_member `<n>`, serialized `update_all` gated by `ha_verify_gate`, verify_failover skeleton, `ha_main`). Config: `HA_PREFIX/HA_COUNT_VAR/HA_PEERS_VAR/HA_CSTATE_*` — state-file paths/keys frozen (siblings grep them).
- **`deploy/verify-refactor.sh`**: harness — `baseline` (live H= vs on-chain allowlist), `old-vs-new` (hash + full-JSON diff per family, box-side and locally), `new-hashes` (per-family manifest for the prime step).

Edits: all 16 `deploy/<family>-node.sh` rebased to config-block + hooks (`BOX_DRIVER` still points at the existing per-family box.py — the Phase C switch is this one variable); `deploy/lib.sh` `run_step` redaction extended (`--rpc-url`, generic `*KEY*/*TOKEN*/*PASSWORD*/*SECRET*` scrub) + step-log naming standardized to `<family>-<name>-<verb>.<ts>.log` (triage depends on it); `deploy/onchain.sh` `cluster()`/`patha_upgrade()` parameterized (env overrides + print `CLUSTER=`/`MEMBER_IMPL=`) and matrix/webhost inline copies become thin wrappers; `deploy/webhook.sh` CF token via env, healthz asserted.

Closes (hash-neutral): argv-secrets ×5 (+hermes /dev/shm variant), matrix umask/restore/prime, unchecked scp everywhere (incl. pg-ha's own), rpc-url/CF-token log leaks, generic APP_ENV_B64 wipe, missing isolation checks ×7 + leaky curl ×3, `all` failure-chaining, redis/clickhouse≈pg-ha triplication, matrix/webhost onchain duplication.

Verify: `bash -n` + shellcheck; `verify-refactor.sh baseline`; per-family `hash` verb equality vs pre-refactor value (old driver ⇒ proves the rebased env assembly is faithful); state round-trip diff (back up `deploy/logs/*.state` first); live read-only verbs: `verify`, `verify-health`, `verify-isolation`, `verify-ha` (HA trio), `verify-agent` (matrix/pg-ha); grep `workflows/*.tsx` verbs still resolve.

### Phase B — routine layer (read-only routines first)

New files: **`deploy/families.json`** (generated by **`deploy/gen-families.sh`** from `list-verbs`; includes `frozen`, real `defaultName`s); **`deploy/workflows/lib.ts`** (run()/Step/loadFamilies/assertNotFrozen + shared zod input schemas, `.strict()`); **`deploy/routine.sh`** — the sole model-facing entry point: `list|describe|run|resume|status|approve|deny|triage`; pins cwd to `deploy/` (kills the dual-smithers.db footgun — delete stale `/home/ubuntu/teemesh/smithers.db`), sources env.sh, pre-validates input via the same zod schemas (exit 4, db untouched), generates run-id `<name>-<utcstamp>`, maps smithers exit codes (0 ok / 1 failed / 3 approval-required / 4 usage) to **one `RESULT {json}` line** with failedTasks+log paths+verbatim resume/approve commands; **`deploy/routines.json`** + **`deploy/ROUTINES.md`** registry (one invocation shape, one result shape, per-routine input schema + example).
Routines (deterministic .tsx, dynamic task lists from families.json): **`node-verify.tsx`** `{family, name?}`; **`verify-fleet.tsx`** `{families?, maxConcurrency?=3}` (Parallel→Sequence, `continueOnFail`, final `report` aggregator row); **`restore-drill.tsx`** `{}` (non-destructive: r2-host verify-recovery + matrix backup-status); **`drift-check.tsx`** `{families?}` (uses the new `drift` verb: state file vs on-chain hash/membership, one JSON line, exit 0/1).
Scheduling: **OS cron** (smithers cron verified unusable: no `--input`, broken cwd) — nightly verify-fleet + drift-check, weekly restore-drill, RESULT lines appended to `deploy/logs/scheduled/*.jsonl`. Smoke: forced-failure run proves the RESULT/resume contract end-to-end.

### Phase C — unified `deploy/node-box.py`, drivers flipped

One driver (~330 lines): app_compose dict built in the exact legacy field order (`pre_launch_script` assigned last; `json.dumps(indent=4)`); hash-affecting params explicit (`BOX_APP_NAME`, `BOX_GATEWAY_ENABLED`, `BOX_NO_INSTANCE_ID`, `BOX_ENV_KEYS` comma-list → `allowed_envs`, `BOX_PRE_LAUNCH_VARIANT` ∈ {docker-login, docker-login+app-env}); full mode set everywhere (`hash/register/create/deploy/update/stop/start`, one JSON line each, `update` always echoes app_id); StopVm stale-VM hardening + FRESH_DISK everywhere (closes pg-ha/redis/clickhouse/postgres gap); `BOX_NET_MODE` required-explicit (removes matrix's legacy `user` default trap); mcp_dstack import lazy so `hash` runs locally.
Flip = one line per family (`BOX_DRIVER`); matrix's shell update-parsing rebased in the same commit (its legacy driver output differs).
Verify: `verify-refactor.sh old-vs-new` — **16/16 hash + JSON-text equality is the merge gate**; gas-only throwaway `register` (never primed/bound — no C3 membership); live canary #1 = **runyard-node** update (unchanged compose; ephemeral workloads); canary #2 = **`pg-ha update pg3`** (single member, verify-ha gate) before blessing any `update-all`.
Then Phase F deletes the 16 per-family drivers.

### Phase D — mutating routines + deterministic triage

**`prime-hashes.tsx`** `{families, confirm:"PRIME"}` (ApprovalGate); **`node-update.tsx`** `{family, name?, node?, freshDisk?, confirm:"ROLL"|"ROLL-FRESH-DISK"}` (ApprovalGate + throws on frozen; post-update full verify sequence answers "did it actually work"); **`roll-fleet.tsx`** `{families (explicit, no default-all), confirm:"ROLL"}` — serialized, ApprovalGate **per family**, verify suite between families, halt on failure, frozen families refused structurally from families.json (never from model input); **`node-deploy.tsx`** `{family, name?, count?, confirm:"DEPLOY"}` (membership is permanent → gate precedes registration; name-collision vs existing state file → throw); **`matrix-restore.tsx`** `{confirm:"RESTORE-MATRIX"}` (never scheduled).
Triage: **`deploy/known-issues.tsv`** (seeded from documented live gotchas: stale-VM port-alloc, ghcr unauthorized, digest-404→tags, registration-slow, missing env, ch-provision race, redis HAProxy post-failover, one-shot-not-recreated→fresh-disk, phala key) + **`deploy/triage.sh`** (grep-based, first match wins, emits `{knownIssue, diagnosis, next}` JSON; wired into `routine.sh run` failure path). Optional `triage-llm.ts` per Decision 4.
Guardrails: zod literals validated in the build fn (engine doesn't enforce refinements — verified), `approve` documented human-only (+ TTY tripwire).

### Phase E — compose hygiene (hash-changing), riding the pending staged roll

Edit 14 composes: drop `"9090:9090"`; digest-pin floating tags (synapse/prom-stack/tailscale `:latest`, hindsight tag, synclave front/tls-proxy, socat/busybox/alpine/postgres version-tags). Flip per-family `HEALTH_VANTAGE=mesh` and move 9090 to `ISOLATION_PORTS_CLOSED` in the same commit.
Execute: `verify-refactor.sh new-hashes` → `routine.sh run prime-hashes` (idempotent, before any roll) → **the roll itself = `routine.sh run roll-fleet`** as part of the already-pending staged fleet roll, after Dan's GO — this is the routine layer's acceptance test (multi-family, verify-gated, per-family approvals, ssh-node structurally excluded). Post-roll: verify-isolation asserts 9090 now refuses. hermes/ssh: composes fixed in git + new hashes primed; nodes not rolled (ssh frozen; hermes not deployed).
Legacy `node-1.yaml`/`indexer-1.yaml` 9090 handled in Phase F/at indexer's next natural roll.

### Phase F — deletions

All 16 `deploy/<family>-node-box.py`; `deploy/node.sh` (self-documented LEGACY) + `deploy/compose/node-1.yaml`; stale `/home/ubuntu/teemesh/smithers.db`; subsumed `postgres-node.tsx`/`pg-ha.tsx`/`r2-host-node.tsx` after a parity run (keep `deploy.tsx`, `matrix-node.tsx`). Keep `indexer.sh`, `node-pathA.sh` (live Phala path), `cluster-orchestrator.py`. Each deletion justified by a zero-reference grep in the commit.

## Verification (end-to-end)

1. Every phase: `bash -n` + `shellcheck` + no contract changes anywhere in this plan.
2. Hash gates: Phase A per-family `hash` equality (old driver, rebased shell); Phase C 16/16 old-vs-new rendered-JSON equality; Phase E new-hash manifest primed before roll. `baseline` mode flags pre-existing git-vs-live drift before anything is touched.
3. Live read-only sweep after A and B: `routine.sh run verify-fleet '{}'` green (or same failures as pre-refactor baseline).
4. Canaries before fleet trust (Phase C): runyard update + pg3 HA update, each followed by full verify suite.
5. Routine contract: forced-failure run → RESULT line contains failed task id, correct log path, working resume command; `graph --input` used as the free dry-run for every routine.
6. The pending staged fleet roll executed via roll-fleet = the final acceptance test.

## Risk register (top items)

| Risk | Guard |
|---|---|
| Unified driver renders different app_compose → KMS refuses next boot | full-JSON diff harness, 16/16 merge gate, canaries |
| Matrix update/restore semantics (legacy driver output, FRESH_DISK) | driver flip + shell rebase atomic; FRESH_DISK centralized in box_run (Phase A) |
| HA update_all loses serialization/verify gate → quorum loss | choreography promoted verbatim; pg3 canary before update-all |
| State-file key silently dropped (e.g. matrix IAPW → admin lockout) | EXTRA_STATE_KEYS superset audit + round-trip diff + state backup |
| Workflow/sibling contract break (verbs, state paths, PGHA_PEERS names) | all names frozen; grep audit in review |
| 9090 removal breaks health checks pre-roll | HEALTH_VANTAGE flips in same commit; old CVM keeps old hash until rolled |
| ssh-node (Verity) accidentally rolled | `frozen` in families.json (not model input) + script-level guard env |
| families.json drifts from scripts | generated file + drift check; "no edits while a run is open" rule |

## Execution notes

- Work on branch `deploy-dry` off `main`; PR against main. No AI attribution in commits (project rule).
- Rough size: Phase A is the big one (~16 file rewrites + 3 new libs); B and C are parallelizable after A; D–F are small. Phases land as separate commits/PRs in order; every phase leaves the fleet operable.
- Dan-owned gates: GO for the staged roll (Phase E), confirmation of the frozen set, and the Phase C canary rolls (runyard, pg3) since they restart live CVMs.
