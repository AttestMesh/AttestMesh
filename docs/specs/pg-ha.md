# PG-HA — Highly-Available PostgreSQL on AttestMesh

Status: **APPROVED** (v0.1, 2026-07-02)

A Patroni + etcd PostgreSQL cluster deployed as N (default 3) additional members of an
existing AttestMesh cluster. Each pg node is a full AttestMesh node (sidecar, Path-A
registration, wireguard mesh) that additionally runs an etcd member, a Patroni-managed
Postgres, and an HAProxy front. Any mesh client can connect to **any pg node's mesh IP
`:5432`** and always reach the current primary; `:5433` load-balances the replicas.

Delivered as the canonical Path-A deploy trio (`deploy/pg-ha-node.sh`,
`deploy/pg-ha-node-box.py`, `deploy/compose/pg-ha-node.yaml`), one support image
(`ghcr.io/attestmesh/pg-ha`), a Patroni-aware `postgres-admin-agent`, and a Smithers
workflow (`deploy/workflows/pg-ha.tsx`).

## 1. Goals / non-goals

Goals:
- Automatic failover (Patroni leader election over an etcd quorum), no operator in the loop.
- Zero off-chain coordination for bring-up: membership, peer identity, and mesh IPs come
  from the chain; all shared secrets derive from the cluster shared key (CSK).
- Mesh-only exposure: Postgres, Patroni, and etcd are reachable **only** on the AttestMesh
  wireguard interface. Host-isolation invariant holds (no port reachable from the box).
- Day-2: disk-preserving rolls, fresh-disk re-provisions, and scale-out without breaking
  quorum; WAL-G continuous backups (encrypted to a CSK-derived key) from day 1.

Non-goals (v1):
- Synchronous replication (`synchronous_mode: false` — async; small RPO on failover).
- etcd peer TLS (traffic already rides the attested wireguard mesh).
- Per-service egress firewall inside the shared sidecar netns (see §8 deviations).
- On-chain member removal (the contracts have no `removeMember`; see §7).

## 2. Topology

N pg nodes join the existing cluster (production: C3, mesh `10.18.0.0/16`; the Matrix
node is the CSK originator, pg nodes are onboardees). Node names `pg1..pgN`. Each CVM
runs:

| Container | netns | Role |
|---|---|---|
| `sidecar` | own (shared below) | cluster-mesh-agent: Path-A registration, wg mesh, app gRPC |
| `etcd` | `service:sidecar` | one etcd v3.5 member; Patroni DCS |
| `patroni` | `service:sidecar` | Patroni + Postgres 16 (+ WAL-G) |
| `haproxy` | `service:sidecar` | primary/replica routing on the mesh IP |
| `matrix-mesh-proxy` | `service:sidecar` | socat :18080 → Matrix mesh listener (agent path) |
| `postgres-admin-agent` + `agent-egress-fw` | own | Matrix chat-ops (`!pg`, `!pgha`), deny-all egress |
| `prometheus`, `node-exporter`, `cadvisor` | own | observability (parity with postgres-node) |

### 2.1 Why the sidecar netns

`attestmesh0` exists only in the sidecar's namespace. etcd peer traffic, Patroni→remote
etcd, replica streaming from the primary, and HAProxy health checks to N remote `:8008`
are all *outbound* mesh flows; socat-per-flow would need ~4·(N−1) forwarders and the
advertised addresses would still be wrong. So the HA trio shares the sidecar netns and
binds the mesh IP directly. Every entrypoint wait-loops for `attestmesh0` before binding
(same pattern as the existing `postgres-mesh-proxy`).

### 2.2 Port map (inside the sidecar netns; sidecar keeps 9090/51820/51821/51900)

| Service | Port | Bind | Reachable by |
|---|---|---|---|
| etcd client | 2379 | mesh IP + 127.0.0.1 | pg peers (Patroni), self |
| etcd peer | 2380 | mesh IP | pg peers |
| Patroni REST | 8008 | 0.0.0.0 (`connect_address` = mesh IP:8008) | peers' HAProxy, admin agent (`sidecar:8008`), verify shell |
| Postgres (real) | **5434** | 0.0.0.0 (`connect_address` = mesh IP:5434) | replication, HAProxy backends, admin agent (`sidecar:5434`) |
| HAProxy → primary | 5432 | mesh IP only | mesh clients |
| HAProxy → replicas | 5433 | mesh IP only | mesh clients |
| HAProxy stats | 7000 | 127.0.0.1 | local only |

Postgres runs on 5434 so HAProxy can own `mesh_ip:5432/5433` in the same netns.
`0.0.0.0` binds are CVM-internal only — the CVM publishes no host `ports:` beyond the
sidecar's 51900, and §6 verification asserts nothing is reachable from the box.

## 3. Deterministic peer precompute (etcd bootstrap)

Static etcd bootstrap needs every member's address at first boot. Mesh IPs are
deterministic off-chain as soon as the N dstack app_ids exist (before any CVM boots):

```
attestorId = keccak256("attestmesh.attestor.dstack")
memberId   = keccak256(abi.encode(cluster, app_id, attestorId))       // AttestFacet.sol:114 (Path A: memberContract == app_id)
meshIp     = cidr | (uint32(keccak256(memberId)) % (hostCount − 2) + 1) // AttestFacet.sol:77; hostCount = 2^(32−prefix)
```

The driver therefore splits CVM deployment:

1. `register-all` — create N DstackApp contracts (`_deploy_app_contract`) → N app_ids.
   The compose_hash is **identical for all N** (only sealed env *values* differ; the
   measured `allowed_envs` name set is the same), so the gate needs one hash + N app ids.
2. `compute-peers` — compute the N mesh IPs with `cast abi-encode`/`cast keccak`;
   **collision-check** pairwise and against every existing member's `meshIpOf`
   (`listMembers()`); on a collision, register a replacement app_id for that slot and
   recompute. Persist `PGHA_PEERS="pg1=10.18.a.b,pg2=…,…"` in the cluster state file.
3. `create-all` — CreateVm per node with sealed env `PGHA_NODE_NAME=pg<i>`,
   `PGHA_PEERS`, `PGHA_BOOTSTRAP=new`, plus the standard sidecar/backup/agent env.

Runtime guards (both hard-fail before any data is written):
- driver `verify-all`: on-chain `meshIpOf(memberIdOf(app_id))` must equal the precomputed IP;
- etcd entrypoint: `GetSelf.mesh_ip` (sidecar gRPC) must equal `PGHA_PEERS[PGHA_NODE_NAME]`.

### 3.1 etcd entrypoint state machine

| Data dir | `PGHA_BOOTSTRAP` | Action |
|---|---|---|
| non-empty | (any) | plain start — etcd ignores `--initial-cluster*` after init (covers disk-preserving rolls) |
| empty | `new` | static bootstrap: `ETCD_INITIAL_CLUSTER` from `PGHA_PEERS`, `initial-cluster-state=new`, token `attestmesh-pg-ha`. Quorum forms when ⌈(N+1)/2⌉ arrive; no ordering constraints. |
| empty | `join` | poll peers' `:2379` until one answers; `etcdctl member list`; if a stale member holds **my** peer URL (fresh-disk re-provision: same app_id ⇒ same mesh IP) → `member remove` it; `member add pg<i> --peer-urls=http://<my_ip>:2380`; start with `initial-cluster-state=existing`. |

## 4. Secrets — all CSK-derived

The CSK (32 bytes, on-chain-committed, app-gRPC `GetClusterSharedKey`) is identical on
every member, so every pg node derives identical credentials with no secret exchange.
Recipe = `walg-csk-key.sh`: poll `GetMeshStatus.cskAcquired` → fetch CSK → HKDF
(Extract with zero salt, Expand with a versioned info label):

| Label | Use |
|---|---|
| `attestmesh.pgha.superuser.v1` | Postgres superuser (`postgres`) password |
| `attestmesh.pgha.replication.v1` | `replicator` streaming-replication password |
| `attestmesh.pgha.rewind.v1` | `rewind_user` password (`pg_rewind`) |
| `attestmesh.pgha.patroni-api.v1` | Patroni REST auth for unsafe (POST) endpoints |
| `attestmesh.pgha.etcd.v1` | etcd root password (client-API auth) |
| `attestmesh.pgha.walg.v1` | WAL-G libsodium backup-encryption key |

Health GETs on Patroni (`/primary`, `/replica`, `/cluster`, `/health`) stay
unauthenticated — required by HAProxy checks and driver verification. The only
driver-known credential is `PGHA_VERIFY_PASSWORD` (sealed env): a low-privilege
`meshverify` bootstrap user used for end-to-end SQL checks from the mesh shell.

## 5. Patroni / HAProxy / WAL-G configuration

Patroni (rendered by the entrypoint into `/run/pgha/patroni.yml`):
- `scope: pg-ha`, `name: $PGHA_NODE_NAME`, `etcd3.hosts` = all N `<ip>:2379` (+ auth).
- `restapi`: listen `0.0.0.0:8008`, `connect_address <mesh_ip>:8008`, authentication from
  `.patroni-api.v1`.
- `postgresql`: listen `0.0.0.0:5434`, `connect_address <mesh_ip>:5434`, superuser /
  replication / rewind credentials from CSK, `use_pg_rewind: true`.
- `bootstrap.dcs`: `synchronous_mode: false`, `postgresql.parameters`: `wal_level=replica`,
  `archive_mode=on`, `archive_command=/usr/local/bin/walg-archive %p`,
  `archive_timeout`, `max_wal_senders`, `wal_keep_size`, hot standby on.
- `pg_hba`: replication + client access for the mesh CIDR and the compose subnet
  (scram-sha-256); local socket trust.
- `bootstrap.users`: `meshverify` (password `PGHA_VERIFY_PASSWORD`, LOGIN, its own
  `verify` schema via `post_init`).
- `bootstrap.method: walg_restore` when `BACKUP_RESTORE` is set (full-cluster DR: wraps
  `walg-restore.sh`, then Patroni owns the timeline).

HAProxy (rendered from `PGHA_PEERS`): `listen primary` binds `<mesh_ip>:5432`, servers =
all N `<ip>:5434` with `option httpchk GET /primary` against `<ip>:8008`, `http-check
expect status 200`, `on-marked-down shutdown-sessions`; `listen replicas` binds `:5433`
with `GET /replica`. Stats on `127.0.0.1:7000`.

WAL-G (from day 1, `BACKUP_ENABLED=true`): same R2 bucket as matrix/postgres nodes,
`BACKUP_PREFIX=pg-ha` shared by **all** nodes — `archive_command` only runs on the
primary (Postgres semantics) and the base-backup loop skips while
`pg_is_in_recovery()` is true, so exactly one node pushes; wal-g handles timeline
switches across failovers. Encryption key = `.pgha.walg.v1` (label differs from the
matrix walg key). Restore path: `BACKUP_RESTORE=LATEST` (§7 total loss).

## 6. Deployment, verification, day-2

Driver `deploy/pg-ha-node.sh <name> <action>`, `PGHA_COUNT` (default 3, warn if even).
State: per-node `$LOGDIR/pg-ha-node-<name>-pg<i>.state` (X/H/VM_ID/MESH_IP) + cluster
`$LOGDIR/pg-ha-<name>.state` (CLUSTER, MEMBER_IMPL, PGHA_PEERS, PGHA_VERIFY_PASSWORD).
CLUSTER/MEMBER_IMPL inherited from `matrix-node-matrix-node.state`. Secrets go to the
box **via ssh stdin** (`%q`-quoted payload, synclave pattern) — never argv. Box knobs
per node: `no_instance_id:false`, gateway ON, bridge, no host ports, ~2vcpu/4GB/40GB.

Pipeline (`all`): `deploy-all` (= register-all + compute-peers + create-all) →
`prime-all` → `bind-all` → `verify-all` → `verify-ha` → `verify-isolation-all` →
`verify-agent`. Per-node repair actions (`deploy|prime|bind|verify pg<i>`) exist for
resume. Smithers workflow `attestmesh-pg-ha` mirrors this sequence; `--input
'{"count":N}'`.

Verification vantage = the ssh-node mesh shell (`sshd-mesh:1023`, ON the mesh, reached
over the gateway TLS-passthrough):
1. `verify-ha`: each node's `GET :8008/cluster` → exactly one `leader`, N−1 `streaming`,
   bounded lag; `etcdctl endpoint health` across all `:2379`; `psql` as `meshverify`:
   every node's `:5432` → `pg_is_in_recovery()=f`, every `:5433` → `t`; write a row via
   one `:5432`, read it back on every node's `:5433`.
2. `verify-failover`: StopVm the current leader (box) → new leader within ~60s + writes
   succeed → StartVm → old leader rejoins as a streaming replica.
3. `verify-isolation-all`: from the box, per node, ports
   2379/2380/5432/5433/5434/8008/9090 all refuse (host-isolation invariant).
4. `verify-agent`: `!pgha status` in the Matrix ops room, assert a reply.

Day-2:
- **Roll (disk-preserving `update <i>` / `update-all`)**: allowlist the new hash, then
  StopVm→UpgradeApp→StartVm one node at a time, gating on `verify-ha` between nodes.
  etcd member ID and pgdata survive; a rolled ex-primary rejoins via pg_rewind/stream.
- **Fresh-disk re-provision** (`BOX_FRESH_DISK=1`, env re-sealed with
  `PGHA_BOOTSTRAP=join`): same app_id ⇒ same mesh IP; etcd stale-member remove/re-add
  (§3.1); Patroni re-basebackups. Precondition: the remaining nodes hold quorum — never
  fresh-disk more than ⌊(N−1)/2⌋ nodes at once.
- **Scale-out N→M** (two-phase): (1) `PGHA_COUNT=M deploy-all` registers only the new
  slots, recomputes `PGHA_PEERS` (M entries), creates new CVMs with
  `PGHA_BOOTSTRAP=join`; prime/bind/verify them. (2) serialized `update-all` on the old
  nodes re-seals the M-entry `PGHA_PEERS` (their Patroni DCS endpoint lists and HAProxy
  backends come from sealed env). Cluster quorum and service are uninterrupted; old
  nodes' etcd needs nothing (runtime membership).

## 7. Failure modes

| Failure | Behaviour / recovery |
|---|---|
| Primary node down | HAProxy `/primary` checks fail; Patroni promotes a replica within TTL; stale sessions killed; clients reconnect to any `:5432`. |
| Quorum lost (⌈(N+1)/2⌉ down) | etcd unavailable → Patroni demotes everywhere to read-only (no split-brain). Restart the VMs (disk-preserving) to recover. |
| Total loss (all disks gone) | Fresh `deploy-all` with `PGHA_BOOTSTRAP=new` + `BACKUP_RESTORE=LATEST`; the CSK re-derives the WAL-G key (same cluster identity), leader restores from R2, replicas basebackup. |
| Node dead forever | No on-chain `removeMember` — the member record and mesh IP persist (inert). `etcdctl member remove` from any live node restores even-quorum math; HAProxy health-checks it down. Replacement = scale-out with a **new** app_id (new IP), then the phase-2 re-seal roll. |
| Mesh-IP math drift | Caught by both §3 guards before data is written. |
| Sidecar container recreated in-place | netns tenants hold a dead namespace — known trade-off of `service:` netns sharing; a CVM roll recreates the whole project. Accepted (same as existing socat proxies). |

## 8. Accepted v1 deviations (hardening follow-ups)

- No default-DROP egress firewall inside the shared sidecar netns (parity with every
  existing node; the admin agent keeps its own deny-all `agent-egress-fw`).
- No etcd peer TLS / client TLS — mesh transport is already encrypted and attested;
  etcd client API is password-gated (`.pgha.etcd.v1`).
- Patroni health GETs unauthenticated (required by HAProxy; expose no secrets).
- CVM TEE blocks container logs — all entrypoints append progress/errors to a shared
  status volume (`/pgha-status/state`), the walg pattern.

## 9. Interfaces consumed (attestation-method-agnostic)

Only sidecar app-gRPC (`GetMeshStatus`, `GetClusterSharedKey`, `GetSelf`) and public
cluster reads (`memberIdOf`, `meshIpOf`, `listMembers`, `meshCidr`). Nothing dstack-
specific leaks into the HA layer beyond the deploy trio itself (which is the dstack
deployment path by definition).
