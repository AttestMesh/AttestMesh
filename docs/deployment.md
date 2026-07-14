# AttestMesh Deployment Runbook

**Target:** Base **mainnet** (chain `8453`), interoperating with the Phala **dstack-base-prod5** KMS that the live dstack CVM fleet boots against. Mainnet (not Sepolia) because the real KMS — the one whose `isAppAllowed` gate and KMS-root signer we must match for a real node to register — lives there. This is the same chain + KMS the dstackgres fleet runs on.

> All secret values live in `~/.teesql` and are read at runtime by `deploy/env.sh`; nothing secret is committed. `source deploy/env.sh` to load the environment; `deploy/lib.sh` provides `run_step`/`log` that tee every step to `deploy/logs/` so failures are visible on re-runs.

## Resolved configuration

| Key | Value | Source |
|---|---|---|
| Chain | Base mainnet `8453` | — |
| RPC / bundler | `https://base-mainnet.g.alchemy.com/v2/<key>` | `~/.teesql/alchemy-api.key` |
| Gas Manager policy | `56444921-…` (`GAS_POLICY_ID`) | `~/.teesql/alchemy-policy.id` |
| Deployer | `0x60b174704AdAf2b0BF87B426B364D6EbD81818E1` (~0.0104 ETH) | `~/.teesql/global-deployer.{address,key}` |
| ORG_SAFE | deployer (bring-up); transfer to hub Safe `0xd97b…66f0` later | `~/.teesql/config.toml [hub_safe]` |
| **KMS root signer** | `0x52d3cf51c8a37a2ccfc79bbb98c7810d7dd4ce51` | `config.toml [kms].kms_roots` (the corrected root — see premortem 1780517097) |
| Compose hash (real) | `0x206a322e7ad0cfec0a2080822a4dfef10c4240a33e1dc553dd9e76b606b8ed0f` | `config.toml` (a live CVM) |
| KMS endpoint | `https://kms.dstack-base-prod5.phala.network` | `config.toml [kms].url` |
| Basescan verify key | set | `~/.teesql/hub.env` |

## Tracks (dependency order)

```
A. Contracts (on-chain)         ── ready ──►  unblocks B, C, D
   A1 DeployInfra  (facets, factories, IndexerRegistry)   →  script/deployments/8453.json
   A2 DeployCluster (one ClusterDiamond, allowlists seeded: KMS root + compose hash)
   A3 (verify on Basescan)

B. Gas-sponsorship webhook       ── ready ──►  needs A (factory addr)
   B1 wrangler.toml: chain 8453, factory addr, RPC; B2 wrangler deploy; B3 point the
      Alchemy Gas Manager policy at the webhook URL.

C. Indexer                       ── ready ──►  needs A (IndexerRegistry)
   C1 run verified blue/green Indexer candidates; C2 keep IndexerRegistry on the stable
      Indexer-LB gateway endpoint; C3 rotate backend codeId+pubkey with the LB's
      prepare/registry-update/commit cutover.

D. Node (dstack CVM via Phala)   ── MILESTONE-A WORK ──►  needs A,B,C
   The sidecar bring-up is built+unit-tested but NOT wired (docs/specs/sidecar.md §1.1).
   To register a real node, this track must:
   D1 [code] wire state::run() to drive registration→subscribe→mesh.
   D2 [code] add a DstackRuntime KMS-sig-chain request method (the real dstack guest-agent
            call) so dstack_register proof material can be sourced from the runtime.
   D3 [code] fix the isAppAllowed boot-gate bootstrap circularity (owner-seeded app_id
            allowlist / isOurMember(appId)) — else the KMS refuses first boot.
   D4 [build] sidecar OCI image + node compose (app + cluster-mesh-agent).
   D5 [deploy] `phala deploy` a CVM with the ClusterMember address as app_id; the KMS
            gate releases keys; the sidecar registers. **This is where the real KMS proof
            format is finally validated on-chain** (resolves premortem risk A/B/C).
   D6 repeat ×3 → mesh converges.
   Phala auth: `~/.teesql` has no obvious Phala Cloud API key; `npx phala` reports
   "not authenticated". Resolve before D5 (find the key or `phala login`).
```

## Standardized routines (smithers + logged bash)

Standardized two layers deep:
- **Logged bash routines** hold the actual, idempotent commands; every step tees to
  `deploy/logs/` via `deploy/lib.sh::run_step`, so on a re-run you see exactly what failed:
  - `deploy/onchain.sh` — `{preflight | infra | cluster | patha-upgrade <cluster> | seed-appid}`.
    `infra` no-ops if already deployed (`FORCE=1` to redeploy); `patha-upgrade` diamond-cuts the
    Path A DstackFacet + deploys the upgrade-target impl.
  - `deploy/webhook.sh` — `{deploy | route}`. `wrangler deploy` + ensures the custom-domain
    route `gas-webhook.teesql.com/*` points at the AttestMesh worker (a stale route → bundler 401).
  - `deploy/node-pathA.sh <node>` — `{env-file | deploy | prime | upgrade | setup | verify |
    update | restart | mesh-verify | all}`.
    The Path A node bring-up: builds the sealed env (ghcr pull-creds; MEMBER_CONTRACT omitted →
    self-discovered), phala-deploys a stock DstackApp CVM, primes the cluster gate, upgrades the
    proxy to ClusterMember, then polls for the sidecar's self-registration. Day-2: `update` rolls
    a new compose/env onto the live CVM (learns the new compose hash via `--prepare-only`,
    allowlists it on the cluster FIRST, then plain-updates — the prepare/commit token flow is
    broken); `restart` re-pulls `:latest` (image-only roll); `mesh-verify` polls the gateway
    healthz for `phase=healthy`. Gated on a `phala login` session; persists CVM_ID/app_id in a
    state file so every subcommand is re-entrant.
  - `deploy/indexer.sh <name>` — `{ensure | env-file | deploy | register | verify | update}`.
    The **shared** indexer (ONE instance serves every cluster + the networks it watches — never
    per-cluster): a stock dstack app (no Path A upgrade, no cluster gating). `register` scrapes
    the boot-derived Ed25519 pubkey from the CVM logs + the compose hash and calls
    `IndexerRegistry.setIndexer`; `ensure` no-ops when the registry already has an endpoint —
    that is the per-cluster workflow entry.
  - `deploy/indexer-member-node.sh <name> candidate` — deploys and fully verifies a C3 Indexer
    candidate without changing `IndexerRegistry`.
  - `deploy/indexer-lb-node.sh <name>` — stable public gRPC/HTTP front door with a mesh-only
    authenticated control API. `switch <candidate>` coordinates HAProxy prepare/commit around
    the registry-pinned signing-key update and rolls back the registry if commit fails. See
    [`deploy/indexer-lb-runbook.md`](../deploy/indexer-lb-runbook.md).
  - `deploy/node.sh` — the legacy `--custom-app-id` flow (unsupported on base KMS; kept for
    reference / a future custom-app-id KMS).
- **A durable smithers workflow** (`deploy/workflows/deploy.tsx`, `attestmesh-deploy-full`)
  sequences those routines as crash-recoverable, resumable compute steps:
  `preflight → infra → cluster → pathaUpgrade → webhook → indexerEnsure → node-1{env-file →
  deploy → prime → upgrade → verify} → node-2{…} → meshVerify`. The node sub-steps are
  individually durable — a `verify` timeout (the sponsored registration UserOp is slow) resumes
  from `verify` without re-deploying the CVM. Validated with `smithers graph` (renders the
  ordered 17-task plan). `deploy/package.json` pins `smithers-orchestrator@0.22.0` + `zod@^4`.

Run:
```bash
source deploy/env.sh                                   # load ~/.teesql creds
( cd deploy && bun install )                           # once — deduped smithers deps
( cd deploy && bunx smithers-orchestrator up workflows/deploy.tsx --input '{"node":"attestmesh-node-1"}' )
# resume from the failed step after fixing it:  … up workflows/deploy.tsx --run-id <id> --resume true
# or a single routine directly:  deploy/onchain.sh all  /  deploy/webhook.sh deploy  /
#                                CLUSTER=… MEMBER_IMPL=… deploy/node-pathA.sh attestmesh-node-1 setup
```

## `deploy/` directory map

`deploy/` is the operator-facing deployment workspace. It is intentionally split into:

- **Thin, durable orchestration** in `deploy/workflows/*.tsx` (Smithers).
- **Logged, re-entrant routines** in `deploy/*.sh` plus the box-side Python helpers.
- **Measured runtime payloads** in `deploy/compose/*.yaml` and the support image directories.
- **Local runtime state** under ignored paths such as `deploy/logs/`, `deploy/.smithers/`,
  `deploy/smithers.db*`, `deploy/node_modules/`, `deploy/*.env`, and `deploy/__pycache__/`.

Tracked layout:

```text
deploy/
├── env.sh                         # non-secret env bridge; reads secrets from ~/.teesql
├── lib.sh                         # shared logging, require(), run_step()
├── onchain.sh                     # contracts, cluster, Path A facet, app-id allowlist
├── webhook.sh                     # Cloudflare Worker deploy + gas-webhook route repair
├── indexer.sh                     # shared Phala indexer deploy/register/update
├── indexer-member-node.sh         # self-hosted C3 Indexer candidate deploy/verify
├── indexer-lb-node.sh             # stable Indexer HAProxy + registry cutover driver
├── indexer-lb-runbook.md          # blue/green migration and day-2 operations
├── node-pathA.sh                  # Phala base-KMS cluster node deploy/update
├── node.sh                        # legacy custom-app-id node path, kept for reference
├── matrix-node.sh                 # self-hosted box Matrix node driver
├── postgres-node.sh               # self-hosted box standalone Postgres node driver
├── pg-ha-node.sh                  # self-hosted box Postgres HA cluster driver (N nodes)
├── ssh-node.sh                    # self-hosted box SSH-ingress node driver (bridge + mesh shells)
├── hindsight-node.sh              # self-hosted box Hindsight (agent memory) node driver
├── matrix-node-box.py             # runs on the box; deploy/hash/update Matrix CVMs
├── postgres-node-box.py           # runs on the box; deploy/hash/update Postgres CVMs
├── pg-ha-node-box.py              # runs on the box; register/create/hash/update/stop/start pg-ha CVMs
├── ssh-node-box.py                # runs on the box; deploy/hash/update SSH CVMs
├── hindsight-node-box.py          # runs on the box; deploy/hash/update Hindsight CVMs
├── matrix-probe.py                # send-a-command / await-bot-reply Matrix room probe (MATRIX_PROBE_* env)
├── package.json                   # Smithers command aliases + dev dependencies
├── bun.lock                       # pinned deploy-workspace JS dependencies
├── workflows/
│   ├── deploy.tsx                 # full Phala stack workflow
│   ├── matrix-node.tsx            # private Matrix node workflow
│   ├── postgres-node.tsx          # standalone Postgres node workflow
│   └── pg-ha.tsx                  # Postgres HA cluster workflow (Patroni + etcd + HAProxy)
├── compose/
│   ├── node-1.yaml                # sidecar-only cluster member on Phala
│   ├── indexer-1.yaml             # attested shared indexer CVM
│   ├── indexer-member-node.yaml   # C3 Indexer backend candidate
│   ├── indexer-lb-node.yaml       # stable public gRPC/HTTP LB + mesh control API
│   ├── matrix-node.yaml           # Matrix homeserver + sidecar + agents + metrics
│   ├── postgres-node.yaml         # Postgres service node + sidecar + admin agent
│   ├── pg-ha-node.yaml            # one Postgres HA node: sidecar-netns Patroni/etcd/HAProxy + admin agent
│   ├── ssh-node.yaml              # sidecar + two sshd workbench shells (bridge :1022, mesh :1023)
│   └── hindsight-node.yaml        # mesh-only Hindsight memory node (sidecar + socat mesh proxies + egress-fw)
├── agent-egress-fw/
│   ├── Dockerfile                 # egress firewall helper image
│   └── egress-fw.sh               # default-DROP outbound policy script
├── postgres-walg/
│   ├── Dockerfile                 # Postgres image with optional WAL-G backup/restore
│   ├── docker-entrypoint-walg.sh  # stock Postgres entrypoint wrapper
│   ├── walg-archive               # archive_command wrapper
│   ├── walg-backup-loop.sh        # periodic base backups + retention
│   ├── walg-csk-key.sh            # writes WAL-G key derived from the cluster shared key
│   ├── walg-env.sh                # WAL-G / R2 environment assembly
│   ├── walg-restore.sh            # restore base backup + WAL replay
│   ├── walg-wal-fetch             # restore_command wrapper
│   └── agent.proto                # sidecar agent gRPC contract copy for CSK access
├── pg-ha/
│   ├── Dockerfile                 # Patroni + etcd + HAProxy on the postgres-walg base
│   ├── pgha-common.sh             # mesh-IP wait/guard + CSK-HKDF secret derivation helpers
│   ├── pgha-etcd-entrypoint.sh    # etcd bootstrap state machine (new/join/restart)
│   ├── pgha-patroni-entrypoint.sh # renders patroni.yml from CSK-derived credentials
│   ├── pgha-haproxy-entrypoint.sh # primary/replica routing on the mesh IP
│   ├── pgha-backup-loop.sh        # WAL-G base backups, primary-only
│   ├── pgha-post-init.sh          # creates the meshverify verification user
│   └── pgha-walg-bootstrap.sh     # Patroni custom bootstrap: DR restore from R2
├── postgres-admin-agent/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── README.md
│   └── postgres_admin_agent/      # Matrix bot for Postgres status/query/admin ops (+ Patroni !pgha)
└── matrix-node-*.md               # Matrix-specific runbooks, journals, and history
```

### Root drivers

The root shell scripts are the main operational interface. They all source `deploy/lib.sh`,
write timestamped logs under `deploy/logs/`, and persist small state files there so a failed
step can be re-run without rediscovering app IDs or VM IDs.

| File | Role | Main subcommands / behavior |
|---|---|---|
| `env.sh` | Loads public deployment constants plus secrets from `~/.teesql`. | Exports Base mainnet RPC/bundler, gas policy, deployer key/address, KMS root, compose hash, and optional Phala key. Contains no committed secrets. |
| `lib.sh` | Shared shell helpers. | `log`, `die`, `require`, and `run_step`; every `run_step` writes a per-step logfile and tails failures. |
| `onchain.sh` | Track A on-chain deployment. | `preflight`, `infra`, `cluster [name]`, `patha-upgrade <cluster>`, `seed-appid <cluster> <member>`, `all`. Generates cluster config JSON under `contracts/script/clusters/`. |
| `webhook.sh` | Gas-sponsorship worker deployment. | `deploy` runs Wrangler from `services/gas-sponsorship-webhook` and ensures `gas-webhook.teesql.com/*` points at the current worker; `route` repairs only the route. |
| `indexer.sh` | Shared attested indexer deployment. | `ensure` no-ops if `IndexerRegistry.current()` is already set; otherwise `deploy -> register -> verify`. Also supports `env-file`, `update`, and direct substeps. |
| `indexer-member-node.sh` | C3 Indexer backend candidate. | `candidate` deploys, registers as a cluster member, catches up, and verifies HTTP without touching `IndexerRegistry`; legacy `all` still registers its direct gateway endpoint. |
| `indexer-lb-node.sh` | Stable Indexer blue/green front door. | Deploys the LB once; `switch <candidate>` probes the candidate, pauses new accepts, updates the registry key/code ID, commits both HAProxy backends, and forces cursor-based subscriber reconnect. |
| `node-pathA.sh` | Phala base-KMS cluster member deployment. | `deploy -> prime -> upgrade -> verify`; `setup` skips the long verify poll, `update` allowlists a new compose hash before rolling, `restart` re-pulls images, `mesh-verify` polls `phase=healthy`. |
| `node.sh` | Legacy custom-app-id flow. | Kept as a reference for a future KMS that supports `--custom-app-id`; the Base KMS path uses `node-pathA.sh`. |
| `matrix-node.sh` | Private Matrix homeserver on the self-hosted dstack box. | `deploy -> cluster -> patha -> prime -> bind -> verify -> verify-agent -> verify-client -> verify-isolation`; `update` is disk-preserving by default, `restore` is a deliberate fresh-disk WAL-G recovery, `backup-status` checks R2. |
| `postgres-node.sh` | Standalone PostgreSQL node on the self-hosted box. | Joins the Matrix node's cluster, inherits the Matrix mesh endpoint, then verifies registration, mesh DB endpoint, Matrix admin-agent replies, Prometheus-backed metrics, and host isolation. |
| `pg-ha-node.sh` | Postgres HA cluster (Patroni + etcd + HAProxy) as `PGHA_COUNT` nodes (default 3) on the self-hosted box. | `deploy-all` (= `register-all` -> `compute-peers` -> `create-all`, precomputing every node's mesh IP off-chain for etcd static bootstrap) -> `prime-all` -> `bind-all` -> `verify-all` (incl. on-chain `meshIpOf` cross-check) -> `verify-ha` (leader/replica/routing/replication from the ssh-node mesh shell) -> `verify-isolation-all` -> `verify-agent`. Day-2: `update <pgN>` / serialized `update-all`, `verify-failover` drill. See `docs/specs/pg-ha.md`. |
| `ssh-node.sh` | SSH ingress + operator workbench node on the self-hosted box (8 vcpu / 64 GB / 100 GB, full ubuntu 26.04 shells). | Joins the existing Matrix cluster by default, seals the union key file `~/.attestmesh/ssh-node-authorized-keys` (falls back to `~/.ssh/authorized_keys`), and exposes TWO root shells through gateway TLS: `<app_id>-1022.<gw>:443` (compose bridge) and `<app_id>-1023.<gw>:443` (**inside the sidecar netns = ON the wg mesh**; `ssh -D` = SOCKS onto the mesh). Verifies the gateway SSH banner. |
| `hindsight-node.sh` | Hindsight agent-memory node (vectorize-io Hindsight 0.8.4) on the self-hosted box — **mesh-only, no Tailscale, no public HTTP**. | `deploy -> prime -> bind -> verify -> verify-sidecar -> verify-app -> verify-e2e -> verify-isolation`; verify-app/e2e run over the wg mesh via the ssh-node mesh shell (retain→LLM→recall roundtrip + 401-without-key auth check). `update` is disk-preserving (pg0 memory store survives); LLM model/keys are sealed values → rotate with a plain `update`, no re-allowlist. See `deploy/hindsight-node-runbook.md`. |

### Smithers workflows

The workflows are resumable wrappers around the shell drivers; they do not contain the deployment
logic themselves. They are useful when the sequence matters and a failed step should resume exactly
where it stopped.

| File | Workflow | Sequence |
|---|---|---|
| `workflows/deploy.tsx` | `attestmesh-deploy-full` | `preflight -> infra -> cluster -> pathaUpgrade -> webhook -> indexerEnsure -> node1 env/deploy/prime/upgrade/verify -> node2 env/deploy/prime/upgrade/verify -> meshVerify`. |
| `workflows/matrix-node.tsx` | `attestmesh-matrix-node` | `deploy -> cluster -> patha -> prime -> bind -> verify -> agent -> client -> isolation`, with an optional backup-status task when backups are enabled. |
| `workflows/postgres-node.tsx` | `attestmesh-postgres-node` | `deploy -> prime -> bind -> verify -> meshEndpoint -> agent -> metrics -> isolation`. |
| `workflows/pg-ha.tsx` | `attestmesh-pg-ha` | `deployAll -> primeAll -> bindAll -> verifyAll -> ha -> isolation -> agent`; node count via `--input '{"count":N}'` (looping lives in the driver's re-entrant `-all` actions, so the graph stays static). |

### Compose payloads

The compose files are measured into the dstack app compose hash, so changing any of them is an
auth-gated deployment change. For member CVMs, the new hash must be allowlisted before the updated
CVM boots.

| File | Runtime payload |
|---|---|
| `compose/node-1.yaml` | Minimal Phala member node: the `cluster-mesh-agent` sidecar with gateway-exposed sidecar ports. Used by `node-pathA.sh` for generic cluster members. |
| `compose/indexer-1.yaml` | Attested indexer service plus a small state volume. It is a stock dstack app, not a cluster member. |
| `compose/indexer-member-node.yaml` | C3 sidecar plus an Indexer backend on `:50052`/`:9090`, with an independent cursor volume. |
| `compose/indexer-lb-node.yaml` | C3 sidecar plus HAProxy stable frontends (`:50052` gRPC, `:9090` HTTP) and an authenticated mesh-only two-phase switch API on `:50053`. |
| `compose/matrix-node.yaml` | Full private Matrix stack: sidecar, mesh proxy, WAL-G-enabled Postgres, Synapse init, Synapse, nginx, Tailscale serve, matrix-admin-agent, egress firewalls, Prometheus, node-exporter, cAdvisor, and persistent volumes. The Matrix HTTP path stays tailnet-only; the host-isolation checks assert private ports are not reachable from the box. |
| `compose/postgres-node.yaml` | Standalone Postgres service node: sidecar, mesh proxies to Matrix/Postgres, Postgres, postgres-admin-agent, egress firewalls, Prometheus, node-exporter, cAdvisor, and persistent volumes. It has no Tailscale; Matrix control traffic goes over the AttestMesh mesh. |
| `compose/pg-ha-node.yaml` | One Postgres HA node: sidecar, then Patroni/etcd/HAProxy sharing the SIDECAR netns (they bind the mesh IP directly — clients hit any node's mesh IP `:5432` for the primary, `:5433` for replicas; Postgres itself is on `:5434`), a mesh-only `:8009` status page, the Matrix mesh proxy, the Patroni-aware postgres-admin-agent + egress firewall, and the observability trio. All shared credentials are HKDF-derived from the CSK at boot. |
| `compose/ssh-node.yaml` | Operator workbench node: sidecar plus TWO full-ubuntu-26.04 OpenSSH shells — `sshd` (`:1022`, compose bridge) and `sshd-mesh` (`:1023`, `network_mode: service:sidecar` so it sits ON the wg mesh; wireguard-tools + NET_ADMIN for peer discovery). Both share a `/root` workspace volume; the driver seals `authorized_keys` as base64 and the gateway exposes SSH via the dstack TLS endpoints (`ssh-over-gateway` pattern from `Dstack-TEE/dstack-examples`). |
| `compose/hindsight-node.yaml` | Mesh-only Hindsight memory node: sidecar (publishes only `:9090` health + `:51900` wg), two socat listeners in the SIDECAR netns (`<mesh-ip>:18888` → API, `:18999` → control-plane UI — reachable by cluster members only), the Hindsight 0.8.4 container (embedded pg0, baked-in local embeddings, `HF_HUB_OFFLINE`), and an egress firewall pinning the netns to the single redpill.ai LLM host. API is tenant-key gated (`ApiKeyTenantExtension`), UI access-key gated; keys persist in the driver state file. |

### Box-side helpers

`matrix-node-box.py`, `postgres-node-box.py`, `pg-ha-node-box.py`, `ssh-node-box.py`, and
`hindsight-node-box.py` are copied
over SSH to the self-hosted dstack box and run there with sealed `E_*` environment variables. They are
intentionally small mirrors of the box MCP's `mcp_dstack.deploy_app` shape:

- `deploy` registers a stock DstackApp, seals the runtime env, creates the VM, and prints
  `app_id`, `compose_hash`, and `vm_id`.
- `hash` renders the measured app-compose without secrets and prints the compose hash so the
  cluster can allowlist it before a roll.
- `update <app_id> <vm_id>` stops the VM, performs disk-preserving `UpgradeApp` on the same VM
  when possible, and falls back to fresh-disk `CreateVm` only when explicitly requested.

`pg-ha-node-box.py` additionally splits `deploy` into `register` (DstackApp contract only, so the
driver can precompute mesh IPs before any CVM exists) and `create <app_id>`, and adds
`stop <vm_id>` / `start <vm_id>` power controls for the failover drill.

### Support images

The subdirectories under `deploy/` build images consumed by the compose payloads:

- `agent-egress-fw/` builds the network-namespace firewall used beside admin agents and backup
  egress paths. It installs a default-DROP outbound policy, allows loopback, established traffic,
  DNS, explicitly resolved internal service IPs, and a single configured external host/port.
- `postgres-walg/` wraps Postgres with optional WAL-G support. With `BACKUP_ENABLED=true`, it
  restores into an empty data directory when requested, writes a WAL-G encryption key from the
  cluster shared key, runs periodic base backups, and enables WAL archiving. With backups off, it
  behaves like stock Postgres.
- `postgres-admin-agent/` is a Python Matrix bot for the Postgres nodes. It supports deterministic
  `!pg` status/metrics/query/exec commands, gates write/admin SQL behind explicit confirmation, and
  can route natural-language requests through an OpenAI-compatible LLM. On pg-ha nodes it is
  Patroni-aware: `!pgha status|cluster|lag` plus a confirmation-gated `!pgha switchover`, with the
  DB superuser and Patroni REST credentials read lazily from the CSK-derived `pgha-secrets` volume.
- `pg-ha/` builds `ghcr.io/attestmesh/pg-ha` — Patroni, a pinned etcd, and HAProxy layered on the
  `postgres-walg` base. One image serves three container roles via entrypoint selection; every
  cluster-wide secret (superuser, replication, rewind, Patroni REST, etcd root, WAL-G key) is
  HKDF-derived from the cluster shared key at boot, so all N nodes agree with no secret exchange.

### Deploy-local docs and generated state

The per-node docs under `deploy/` are part of the operator record:

- `matrix-node-deploy.md` is the current authoritative Matrix node deployment guide.
- `matrix-node-runbook.md` is explicitly superseded and kept as historical context.
- `matrix-node-access-journal.md` and `matrix-node-steps-log.md` preserve the debugging history
  and the reasoning behind the current invariants.
- `hindsight-node-runbook.md` is the authoritative Hindsight memory-node runbook (mesh-only
  access, day-2 rolls/rotation, the post-bind sidecar-health gotcha, and the pre-bind
  debug-shell pattern for reading container logs inside a TEE CVM).
- `hermes-node-runbook.md` covers the Hermes agent nodes.

Generated state is intentionally local:

- `deploy/logs/` holds timestamped command logs and state files like
  `node-pathA-<name>.state`, `indexer-<name>.state`, `indexer-lb-node-<name>.state`, `matrix-node-<name>.state`, and
  `postgres-node-<name>.state` / `ssh-node-<name>.state` / `hindsight-node-<name>.state`
  (the matrix and hindsight state files hold live credentials — IAPW/PGPW and the Hindsight
  tenant + UI access keys respectively — treat them as secrets). The pg-ha driver keeps one cluster
  state file `pg-ha-<name>.state` (PGHA_PEERS, verify credential, initialized flag) plus
  per-node `pg-ha-node-<name>-pg<i>.state` files (app id, compose hash, VM id, mesh IP).
- `deploy/.smithers/` and `deploy/smithers.db*` are Smithers execution state.
- `deploy/node_modules/`, temporary env files, and Python `__pycache__/` directories are not
  deployment source.

## Deployed addresses — Base mainnet (8453)

Canonical receipt: `contracts/script/deployments/8453.json` (written by `DeployInfra`).
**Current = the fixed-facet redeploy** (DstackFacet with the cold-start deadlock fix +
the `allowedAppIds` allowlist). The earlier set (`0xB1fA7AD…`/`0x624a5bcE…`) is superseded
and orphaned on-chain.

| Contract | Address |
|---|---|
| ClusterDiamondFactory | `0xf6E85fD138E3208d3AAE63ce4E2A33f20e82b9fb` |
| ClusterMemberFactory | `0xFf9f438EFdAa197f4ae59D1d711C4f09640b8417` |
| ClusterMember impl | `0xd05223da04B4E73AC02ECA9638D490f45f765843` |
| IndexerRegistry | `0xbC003686943fB957100E517D3CEf66c52B5CDdBf` |
| DiamondInit | `0xe3C9CE59b6c164c7b4c81f686C876EB85198cFC3` |
| AttestFacet | `0x10532164ca3BdaCf1dAd13Fb534262DEc5aAA9AA` |
| MessageFacet | `0x0F67cd8c1D8A2F71d2bb8091B2eb166E6b6bB564` |
| NetworkFacet | `0x6AB2b7D506c85C7A9eA22f2ECcE0cc9191006159` |
| DstackFacet (infra bundle) | `0xe9d463974c6E833DC38794d7f2DC5AB692352968` — superseded on the live cluster by the Path A cut below |
| **ClusterDiamond `attestmesh-1`** | **`0xA46273adC86c772C7D8daE896a5fbfdDA2B6ccFA`** (owner=deployer; KMS root + compose hash seeded; allowAnyDevice; 10.13.0.0/16; **cold-start gate verified live**; DstackFacet diamond-cut to the Path A build) |

### Path A (dstack base KMS) — live cluster overrides

Base KMS only mints app_ids it provisions, so a member cannot be a factory-predicted address — it must BE the provisioned app_id. The cluster's DstackFacet was diamond-cut to a Path A build, and a dedicated ClusterMember impl is the UUPS upgrade target for the stock DstackApp proxy `phala deploy` mints.

| Item | Address / value |
|---|---|
| DstackFacet (Path A, cut into the cluster) | `0xC631793fB80d3Bc18435aAD3788B8b31B44bE255` (`dstack_register` also accepts owner-allowlisted app_ids) |
| ClusterMember impl (Path A upgrade target) | `0xBe579F0B8A971d0F8b083Eb3E8bB241985A3C5C4` (`reinitializeFromDstackApp`) |
| Node-1 app_id — **registered member** | `0x54e63929b4d8d09d3c9e3019d54bd20e289ed985` (memberId `0x6c576be9…`, mesh IP `10.13.46.241`, owner = KMS-derived key `0x6EB37a6B…`; registered via sponsored tx `0x577cd15d…`; CVM `7c3234af…`) — CSK originator (memberIds[0]) |
| Node-2 app_id — **registered member** | `0xa87128971070f41c26871ce361d3eded7ecf909b` (memberId `0xdf63f08d…`, mesh IP `10.13.120.109`; CVM `b2d97057…`) |
| Mesh transport | wg over gateway TCP: ingress `<app_id>-51900s.dstack-base-prod5.phala.network`, wg outer port 51821 (in-CVM), heartbeats 51820 in-mesh |
| Sponsorship webhook custom domain | `https://gas-webhook.teesql.com/?token=<~/.teesql/attestmesh-webhook-token>` → CF worker route → `attestmesh-gas-sponsorship-webhook` |

Procedure: `source deploy/env.sh && CLUSTER=… MEMBER_IMPL=… ENV_FILE=… COMPOSE=deploy/compose/node-1.yaml deploy/node-pathA.sh attestmesh-node-1 setup` (deploy stock CVM → prime gate → upgrade proxy), then `… verify`.

## Status log

| Date | Step | Result |
|---|---|---|
| 2026-06-03 | recon + env bridge + preflight | ✔ key→deployer verified, chain 8453, balance OK |
| 2026-06-03 | **A1 DeployInfra (Base mainnet)** | ✔ 9 contracts live (~0.00006 ETH); bytecode verified on-chain |
| 2026-06-03 | **A2 DeployCluster** `attestmesh-1` | ✔ `0x624a…b712`; owner=deployer, allowedKmsRoots[real]=true, allowedComposeHashes[real]=true, allowAnyDevice, meshIp(1)=10.13.124.237 (matches sidecar vector) |
| 2026-06-03 | **B gas-webhook (Cloudflare)** | ✔ live at `attestmesh-gas-sponsorship-webhook.teesql-aa-webhook.workers.dev`; chain 8453 + real factory addrs, 2 KV namespaces, secrets set (RPC_URL, ALCHEMY_WEBHOOK_TOKEN→`~/.teesql/attestmesh-webhook-token`); `GET /healthz`=200. CF token: `~/.teesql/cloudflare-wrangler.toml` (the teebox-llc one lacks Workers-KV perms). |
| 2026-06-03 | B3 (todo, hardening) | point the Alchemy Gas Manager policy `56444921…` at the webhook URL + token (dashboard/Admin API). Sponsorship works via the policy's own rules without it; the webhook is the custom provenance gate. |
| 2026-06-03 | Phala auth | ⚠ not found: no `phala` on PATH, no `~/.phala*` store, no `PHALA_CLOUD_API_KEY` in env/`~/.teesql`; `npx phala` says "not authenticated". Needs `PHALA_CLOUD_API_KEY` exported (dstackgres CLI reads that env var) or `phala login`. Blocks only D5 (CVM boot). |
| 2026-06-03 | **milestone-A code** | ✔ done + tested: cold-start deadlock fix; `DstackRuntime` `/GetKey`+`/Info`; `build_kms_material` (app-pubkey recovery) + `build_proof` — e2e test proves a sidecar-built proof passes the on-chain `DstackSigChain.verify` (37 sidecar tests). |
| 2026-06-03 | **fixed-facet redeploy (final on-chain)** | ✔ infra+cluster redeployed with the deadlock fix; `0xA46273…ccFA`; **`isAppAllowed(allowlisted, unregistered app_id) = (true,"")` verified live** (cold-start works); webhook redeployed to the new factories, `/healthz`=200. |
| 2026-06-03 | (was) remaining = Phala-gated | wire `build_proof` into `state::run()` + the live submit, then `phala deploy` a CVM. |
| 2026-06-09 | **Phala auth ✔** | device-flow `phala login` succeeded (user authorized); `phala status` = logged in as `lsdan`. Track D unblocked. |
| 2026-06-09 | **registration wired** | `state::run()` now derives keys → `build_proof_from_runtime` → sponsored bootstrap `dstack_register` UserOp, heavily logged (commit `3d275a3`). |
| 2026-06-09 | **base KMS reality** | `--custom-app-id` is unsupported on base KMS (proved via a stock DstackApp control deploy); the member must BE a phala-minted app_id → **Path A**: upgrade the stock DstackApp proxy to ClusterMember. |
| 2026-06-09 | **Path A contracts + webhook** | `ClusterMember.reinitializeFromDstackApp` + `dstack_register` accepts owner-allowlisted app_ids; **diamond-cut live** into `0xA46273…` (new DstackFacet `0xC631793f…`, impl `0xBe579F0B…`, 26 contract tests). Webhook gains a Path A branch (cluster-allowlisted app_ids, 73 tests) + Cloudflare redeploy (also fixed a stale factory var). |
| 2026-06-09 | **live CVM bring-up** | `phala deploy` (base KMS, ghcr pull-creds in sealed env) → upgrade proxy → sidecar self-registers. Fixed 3 live-only bugs: `/DeriveKey`→`/GetKey` (dstack 0.5.x removed it), odd-length dummy sig (Alchemy rejected), proof sig recovery-ids 0/1→27/28 (OZ ECDSA reverts on v<27). Codified in `deploy/node-pathA.sh`. |
| 2026-06-09 | **webhook routing bug (B3 root cause)** | Alchemy calls the policy's webhook at the custom domain `gas-webhook.teesql.com`, whose Cloudflare worker route pointed at the OLD `teesql-gas-webhook-prod` worker (→ HTTP 401), not `attestmesh-gas-sponsorship-webhook`. Repointed the route via the CF API (zone `teesql.com`, route `69066fb7…`). The dummy `A 192.0.2.1` placeholder record is fine (proxied; the worker route does the routing). NB: this moved `gas-webhook.teesql.com` away from the dstackgres worker. |
| 2026-06-09 | **✅ NODE REGISTERED (milestone)** | A real dstack CVM (Phala base-KMS prod5) self-registered: `phala deploy` → upgrade proxy → sidecar derived KMS keys, self-discovered app_id `0x54e63929…`, built proof, submitted a **sponsored** EIP-4337 `dstack_register` UserOp → tx `0x577cd15d…` (success). `memberCount=1`, `isClusterMember=true`, `member.owner()=0x6EB37a6B…` (KMS-derived key installed), gas paid by the paymaster. End-to-end Path A proven on Base mainnet. |
| 2026-06-10 | **milestone-B de-risk (live probes)** | Two probe CVMs on prod5 proved: outbound UDP ✓; NAT mapping endpoint-independent ✓; **two-sided simultaneous UDP punch works incl. hairpin** (one-sided does not — filtering is restricted); gateway-domain DNS == egress IP (STUN-free self-discovery); gateway forwards raw (non-HTTP) TCP **only** via the `s` TLS-passthrough suffix (`<app_id>-<port>s.<domain>`); punch coordination done fully P2P over gateway TCP. Probes deleted after. |
| 2026-06-10 | **mesh bring-up wired** (`734838c`) | Sidecar `bringup` + `transport` modules: wg-over-TCP bridges through the gateway TLS-passthrough (self-signed ingress TLS — SNI routing only, wireguard is the security layer), peers enumerated from chain (`listMembers`/`memberById`/`meshIpOf`), PeerEndpoint envelopes via `MessageFacet.send` + `MessageSent` log polling (no Indexer dependency), heartbeats, CSK originate-or-pull, peer gRPC (mesh-only) + agent gRPC (UDS). wg outer port moved to 51821 (51820 collided with the heartbeat recv socket). Compose exposes ingress 51900 + `GATEWAY_DOMAIN` sealed-env var. |
| 2026-06-10 | **live-CVM update procedure** | Compose changes need the new compose hash allowlisted before reboot: deployer `cast send CLUSTER addComposeHash(0xhash)` (the `phala deploy --prepare-only/--commit` token flow rejected every CVM-id form; a plain `phala deploy --cvm-id … --compose … -e …` works once the hash is already on-chain — state-only verification). `phala cvms restart` re-pulls `:latest`. |
| 2026-06-10 | **2 more live-only bugs** | (4) 4337 nonce: `eth_getUserOperationNonce` is not a real bundler method and the error was swallowed → every post-registration UserOp replayed nonce 0 → AA25. Fixed: `EntryPoint.getNonce` via `eth_call` (`8db4fce`). (5) heartbeat connected-view omitted self → a 2-node mesh could never satisfy `view == live_set`, `first_converged` never latched. Fixed (`527dc54`). |
| 2026-06-10 | **✅ MESH CONNECTED (milestone-B core)** | Both nodes formed the wireguard mesh over the gateway TCP leg: sponsored PeerEndpoint envelopes landed (`0x4da2ac5c…`, `0xb1dc6b22…`), Ed25519 keys absorbed from `MessageSent` logs, verified heartbeats → `live_peers=1` on both; node-1 (memberIds[0]) **originated the CSK + set the on-chain commitment** (`0xabf10640…`); node-2 **pulled the CSK over the mesh** (`http://10.13.46.241:50051` — node-1's mesh IP through the tunnel) and verified it against the commitment. |
| 2026-06-10 | **bug 6: CSK restart deadlock** | After a CVM restart the guest-agent `/Seal` data did not survive container recreation; with the commitment already on-chain, the originator joined the pull path and all nodes waited on each other (`pulling-csk` deadlock). Fixed (`b9ba052`): the originator's CSK is deterministically KMS-derived, so on restart it re-derives and verifies against the on-chain commitment before falling back to peer pull; store writes best-effort. **Caveat: dstack `/Seal`/`/Unseal` persistence is unverified — onboardees re-pull on every restart (fine while ≥1 originator-derivable node is up).** |
| 2026-06-10 | **✅ MESH HEALTHY (milestone-B complete)** | Both nodes `GET /healthz` → **200** `{phase:"healthy", live_peers:1, first_converged:true, csk_acquired:true}`. Restart-resilient: node-1 re-derived CSK (log: "CSK re-derived + verified against on-chain commitment"), node-2 re-pulled over the mesh, convergence latched on both. Chain remained the sole coordination layer end-to-end: membership + wg keys + mesh IPs from facet reads, endpoints derived from app_id + `GATEWAY_DOMAIN`, envelopes via `MessageFacet`. **No STUN, no Indexer, no off-chain config.** |
| 2026-06-10 | **indexer deployed (shared infra)** | `attestmesh-indexer-1` (CVM `bb97eedf…`, app `7917d8ec…`) — stock dstack app (NOT a member; ONE indexer serves every cluster + network). 3 more live-only bugs en route: (7) `/DeriveKey`→`/GetKey` (same dstack 0.5.x drift as the sidecar — crash-loop); (8) boot catch-up scanned from genesis (~47M Base blocks; health/gRPC start only after catch-up → never up). Fixed with `INDEXER_START_BLOCK` floor = factory deploy block (computed by `deploy/indexer.sh` via getCode binary search) + chunked discovery → **catch-up in 23s**; (9) pubkey scrape needed ANSI stripping. Registered: `setIndexer(https://7917d8ec…-50051.dstack-base-prod5.phala.network, codeId=compose hash, pubKey=0xa44ecf22…)`. |
| 2026-06-10 | **gateway gRPC reality** | The gateway-terminated route DOES proxy gRPC/h2 (verified via grpcurl: full Subscribe replay with signed envelopes + TDX attestation quote), but answers ALPN with http/1.1 → tonic needs `ClientTlsConfig::assume_http2(true)` (bug 10, `c240d16`). |
| 2026-06-10 | **✅ FULL SYSTEM LIVE** | All four components deployed + working on Base mainnet: contracts, gas webhook, **indexer** (registered, bounded catch-up, signed+attested pushes), and both nodes **subscribed** (`indexer subscription open` on both) while `phase=healthy` — 9 verified pushes absorbed on node-1; pushes wake the chain-read reconcile pass (poll fallback stays). Day-0 bring-up + day-2 ops fully codified in `deploy/{onchain,webhook,indexer,node-pathA}.sh` + the 17-task `attestmesh-deploy-full` smithers workflow. |
| 2026-07-01 | **ssh-node rebuilt as workbench** | The ssh-node VM was lost (removed from the VMM); recreated fresh-disk under the SAME app_id `0x02Cafb3c…` (C3 membership + gateway names kept) at 8 vcpu / 64 GB / 100 GB with full ubuntu 26.04 shells and a second sshd `:1023` INSIDE the sidecar netns — a shell ON the wg mesh (`attestmesh-mesh-node`; `ssh -D` = SOCKS onto the mesh). `ssh-node-box.py` hardened to tolerate a removed VM (StopVm try/except + `GetInfo found:false`). Host keys churn on fresh-disk rolls (`ssh-keygen -R '[<host>]:443'`). |
| 2026-07-01 | **✅ Hindsight memory node LIVE (C3 member #5)** | vectorize-io Hindsight 0.8.4 as a mesh-ONLY node: app_id `0xa151d945…`, memberId `0xd1ab76cf…`, mesh IP `10.18.78.76` (API `:18888`, UI `:18999`, tenant-key auth), LLM `openai/gpt-oss-120b` via redpill with the netns egress-locked to that one host (verified in-TEE: example.com BLOCKED, redpill 200). E2E verified over the mesh (retain→LLM extraction→recall + 401 without key) + host-isolation PASS. Lessons codified in `deploy/hindsight-node-runbook.md`: sidecar `:9090` binds only POST-bind (pre-bind loops "cluster not resolvable"); pre-bind container-log access via a temporary docker.sock debug-shell roll allowlisted on the stock DstackApp (box deployer), rolled back clean before bind; `ip neigh` stale for fresh CVMs → ping-sweep first. |
| 2026-07-11 | **CSK boot-latency root cause + fix** | A fugu-router reboot spent 135s inside one CSK pass: every configured member was dialed serially in randomized `HashMap` order, Tonic had no connect deadline, and three dead member routes preceded a holder. The sidecar now prioritizes originator/live peers, probes at most eight concurrently with 1s/2s deadlines, wakes on peer changes, and keeps retries at 250ms–2s. The nonexistent live `/Seal`/`/Unseal` path is replaced by a sidecar-only named volume containing a KMS-wrapped, commitment-bound XChaCha20-Poly1305 cache. |
| 2026-07-11 | **✅ CSK canary thresholds passed** | Published the isolated CSK-only image `ghcr.io/attestmesh/cluster-mesh-agent@sha256:b9e0ae107d9db015c22059c9fecdc28f2b35e09b257eabb4739bd4e46d96f641`. On empty volumes, blue acquired 216ms after its first reachable holder was configured (118ms probe round) and green acquired in 212ms (193ms probe round), both with dead routes present. A stop/start of the same green VM loaded and commitment-verified the encrypted cache 455ms after cluster discovery (13ms cache operation). No `Seal`/`Unseal` service errors occurred; router liveliness/readiness returned 200 after each boot and the LB was restored to green. |

## Milestone-A reference: the real dstack guest-agent API

(From dstackgres `crates/teesql-data-sidecar/.../dstack.rs` — the `DstackClient`.)
The dstack guest agent listens on a unix socket (`/var/run/dstack.sock`) or
`http://localhost:8090`, prpc-style JSON POST:
- **`/Info`** → `{ app_id, instance_id, compose_hash, app_name, device_id, app_cert, tcb_info, key_provider_info }`
- **`/GetKey`** (path, purpose) → **`{ key: hex, signature_chain: Vec<String> }`** ← the KMS sig chain
- `/GetQuote` (report_data) → quote; `/Sign` (algorithm, data); `/GetTlsKey`

So the `DstackRuntime` trait (sidecar/src/dstack.rs) needs `get_key(path, purpose) -> { key, signature_chain }`
and `info() -> { app_id, compose_hash, instance_id, device_id, ... }`. The on-chain
`DstackProof` (codeId, app/kms signatures, app/derived compressed pubkeys, messageHash)
is assembled from `signature_chain` + the derived key. **Next:** read dstackgres
`group_auth.rs` for the exact `signature_chain` → proof mapping — this is the
ground-truth check on AttestMesh's on-chain `DstackSigChain` preimages (premortem risk A/B/C).
NOTE: dstack's real `app_id` (from `/Info`) is what `codeId` must equal — confirm it is the
ClusterMember address in the AttestMesh model (it is set as the CVM's app_id at deploy).

## Phala auth (action needed for D5)

To deploy a real CVM, export the Phala Cloud API key the way the dstackgres CLI expects:
`export PHALA_CLOUD_API_KEY=<key>` (or `npx phala login`). Everything up to the CVM boot
proceeds without it.
