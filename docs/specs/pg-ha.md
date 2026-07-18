# PG-HA — Highly-Available PostgreSQL on AttestMesh

Status: **IMPLEMENTING** (v0.2, 2026-07-16; live rollout gated on updated images/facet)

Deployment profile `andrew-xyn-pg` adds the encrypted command protocol and sender MCP in
[`pg-ha-chain-commands.md`](pg-ha-chain-commands.md). Its backup policy is continuous WAL with
a 12-hour object window plus logical dumps (R2 server-side encryption required) every six
hours (retain one day), daily (retain seven days), and monthly (retain one year).

A Patroni + etcd PostgreSQL cluster deployed as the only N (default 3) members of a new,
dedicated AttestMesh cluster. Each pg node is a full AttestMesh node (sidecar, Path-A
registration, wireguard mesh) that additionally runs an etcd member, a Patroni-managed
Postgres, and an HAProxy front. Any mesh client can connect to **any pg node's mesh IP
`:5432`** and always reach the current primary; `:5433` load-balances the replicas.

Delivered as the canonical Path-A deploy trio (`deploy/pg-ha-node.sh`,
`deploy/pg-ha-node-box.py`, `deploy/compose/pg-ha-node.yaml`), one support image
(`ghcr.io/attestmesh/pg-ha`), the `pgha-command-agent`, command MCP, and a Smithers
workflow (`deploy/workflows/pg-ha.tsx`).

The cluster has a stock SafeL2 1.4.1 proxy as both cluster owner and diamond owner. It is
configured 1-of-1 with the global deployer as its sole signer. There is no Matrix, Tailscale,
SSH member, SSH daemon in a member CVM, or administrative host port.

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

N pg nodes form the new cluster (production: C3, mesh `10.18.0.0/16`). The first admitted
PG node deterministically originates the CSK and the others acquire it through the normal
sidecar protocol. Node names are `pg1..pgN`. Each CVM runs:

| Container | netns | Role |
|---|---|---|
| `sidecar` | own (shared below) | cluster-mesh-agent: Path-A registration, wg mesh, app gRPC |
| `etcd` | `service:sidecar` | one etcd v3.5 member; Patroni DCS |
| `patroni` | `service:sidecar` | Patroni + Postgres 16 (+ WAL-G) |
| `haproxy` | `service:sidecar` | primary/replica routing on the mesh IP |
| `pgha-command-agent` + `command-agent-egress-fw` | own | Encrypted Safe-owner commands from `MessageFacet`; deny-all egress except local runtime and redpill |
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
| Patroni REST | 8008 | 0.0.0.0 (`connect_address` = mesh IP:8008) | peers' HAProxy and node-local command agent |
| Postgres (real) | **5434** | 0.0.0.0 (`connect_address` = mesh IP:5434) | replication, HAProxy backends, node-local agent |
| HAProxy → primary | 5432 | mesh IP only | mesh clients |
| HAProxy → replicas | 5433 | mesh IP only | mesh clients |
| HAProxy stats | 7000 | 127.0.0.1 | local only |

Postgres runs on 5434 so HAProxy can own `mesh_ip:5432/5433` in the same netns.
`0.0.0.0` binds are CVM-internal only. No application port is published to the host;
sidecar `51900` is the wireguard-over-TCP transport, not an administrative endpoint.

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
| non-empty | (any) | start from preserved state, then remove etcd members absent from the sealed `PGHA_PEERS` map (covers disk-preserving rolls and scale-in) |
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
unauthenticated for HAProxy and node-local diagnostics, but are reachable only inside the
CVM/mesh. `PGHA_VERIFY_PASSWORD` is a sealed, low-privilege smoke-test credential.

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

WAL-G (from day 1, `BACKUP_ENABLED=true`): the node-local encrypting gateway's `r2`
bucket with `BACKUP_PREFIX=wal-g`, shared by **all** nodes — `archive_command` only runs on the
primary (Postgres semantics) and the base-backup loop skips while
`pg_is_in_recovery()` is true, so exactly one node pushes; wal-g handles timeline
switches across failovers. WAL objects are client-encrypted with `.pgha.walg.v1`.
Logical dumps require R2 server-side encryption. The logical retention tiers are six-hourly
for 24 hours, daily for seven days, and monthly for 366 days. WAL objects older than 12 hours
are removed. Restore path: `BACKUP_RESTORE=LATEST` (§7 total loss).

Each member runs its own loopback-only r2host gateway at `127.0.0.1:19000`. The gateway
does not own the backup data: it encrypts/decrypts object contents and names with the
cluster CSK and keeps only a disposable VFS write cache; upstream R2 remains authoritative.
All members derive the same crypt key, so the Patroni primary on any node can continue the
same `r2/wal-g` history after failover. No gateway port is exposed to the mesh or CVM host.

## 6. Deployment, verification, day-2

Driver `deploy/pg-ha-node.sh <name> <action>`, `PGHA_COUNT` (default 3, warn if even).
State: per-node `$LOGDIR/pg-ha-node-<name>-pg<i>.state` (X/H/VM_ID/MESH_IP) + cluster
`$LOGDIR/pg-ha-<name>.state` (CLUSTER, MEMBER_IMPL, PGHA_PEERS, PGHA_VERIFY_PASSWORD).
CLUSTER/MEMBER_IMPL come from this deployment's newly created Safe-owned mesh. Provisioning
secrets go to the dstack box **via SSH stdin** (`%q`-quoted payload) — never argv. This is
box control, not an SSH service or access path inside any member CVM. Box knobs
per node: `no_instance_id:false`, gateway ON, bridge, no host ports, 2 vCPU/4 GB/80 GB.

The self-hosted box KMS root is
`0x7fa63d99495be2129cf28eee54e2ef2724e3aa2e`; it is not the Phala production
root exported by `deploy/env.sh`. `prime-all` checks and allowlists this root via
`PGHA_KMS_ROOT`. If registration simulation reverts with `InvalidSigChain()`
(`0x35565c11`), verify this root first. Sidecars use the keyed Base node RPC for
reads and Pimlico only for EIP-4337 bundling; public node endpoints can return 429
during simultaneous registration.

Target pipeline: deploy Safe → deploy new cluster with `clusterOwner=Safe` → Safe accepts
diamond ownership → install the owner-command facet → `deploy-all` (= register-all +
compute-peers + create-all) → `prime-all` → `bind-all` → `verify-all` → `verify-runtime`
→ `verify-backup` → `verify-isolation-all`. Per-node repair actions exist for
resume. Smithers workflow `attestmesh-pg-ha` mirrors this sequence; `--input
'{"count":N}'`.

`verify-runtime` is deliberately console/control-plane based: it proves every VM is running,
every mesh peer is live, and a quorum of retained serial logs observes the same non-empty Patroni leader lock. It
does not replace the write/replication/failover drill. The Smithers workflow does not claim
chain-agent verification until the Safe-specific production sender described in
`pg-ha-chain-commands.md` replaces the development member-send implementation.

There is no Matrix, Tailscale, SSH member, or administrative host port. Verification is driven
through encrypted Safe-owner blockchain commands and node-local runtime probes:
1. `patroni.status`: node-local probe of `GET :8008/cluster` → exactly one `leader`, N−1 `streaming`,
   bounded lag; `etcdctl endpoint health` across all `:2379`; `psql` as `meshverify`:
   every node's `:5432` → `pg_is_in_recovery()=f`, every `:5433` → `t`; write a row via
   one `:5432`, read it back on every node's `:5433`.
2. `verify-failover`: StopVm the current leader (box) → new leader within ~60s + writes
   succeed → StartVm → old leader rejoins as a streaming replica.
3. `verify-isolation-all`: from the box, per node, ports
   2379/2380/5432/5433/5434/8008/9090 all refuse (host-isolation invariant).
4. command-agent verification: submit an expiring Safe-owner `patroni.status` envelope and
   verify its command ID is committed to the node-local replay/audit ledger.

Day-2:
- **Disk downsize**: virtual disks cannot shrink in place. Set `BOX_FRESH_DISK=1` and
  run `update <replica>` to recreate that member at the target size under the same app
  identity; require mesh, etcd, Patroni streaming, isolation, and backup gates before
  advancing. Fail over the leader only after both replicas have completed. Remove the
  stopped predecessor VMs after the final fleet verification.
- **Roll (disk-preserving `update <i>` / `update-all`)**: allowlist the new hash, then
  StopVm→UpgradeApp→StartVm one node at a time, gating on `verify-ha` between nodes.
  etcd member ID and pgdata survive; a rolled ex-primary rejoins via pg_rewind/stream.
  `update-only` is an emergency diagnostic primitive and refuses to run unless
  `PGHA_ALLOW_UNVERIFIED_ROLL=1`; never use it as a fleet rollout loop. A fixed sleep is
  not an HA gate: require a leader, healthy DCS quorum, and streaming replicas before
  stopping the next member.
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
| Registered peers never converge | Check the serial-console `wg diagnostic` records. `configured=true`, `handshake_unix=0` isolates the fault to the TCP bridge/WireGuard data plane; a current handshake with `live=false` isolates it to heartbeat validation. If chain keys and endpoints are valid but bridge sessions predate registration, perform a disk-preserving roll one node at a time. The July 2026 incident recovered this way: each rolled node reported `first convergence observed` and current handshakes to both peers; node replacement was unnecessary. |
| Sidecar container recreated in-place | netns tenants hold a dead namespace — known trade-off of `service:` netns sharing; a CVM roll recreates the whole project. Accepted (same as existing socat proxies). |

### 7.1 Former-primary crash recovery

If all members report `Lock owner: None` after an unclean former-primary shutdown, do not
blindly promote a replica. Identify the last confirmed primary/timeline first. Patroni may run
that member in single-user crash recovery so local WAL reaches a consistent checkpoint before
leader election or `pg_rewind`. If it remains in that state beyond a bounded startup interval:

1. Stop further fleet rolls and preserve every disk.
2. Seal `PGHA_CRASH_RECOVERY_ONLY=true` on the former primary only and perform one
   disk-preserving update. This runs PostgreSQL single-user mode with stdin explicitly closed,
   `archive_command=false`, a 900-second timeout, and records `pg_controldata`; it never resets WAL.
3. Require `explicit crash recovery: finished rc=0` in the serial status evidence.
4. Roll that same node normally with `PGHA_CRASH_RECOVERY_ONLY=false`. If automatic election
   still needs direction, seal `PGHA_RECOVERY_CANDIDATE=<former-primary>` on one member; the
   one-shot loopback Patroni request uses the derived API credential.
5. Once a leader exists and replicas follow it, clear the recovery candidate with a normal
   serialized replica roll. Run `verify-ha` and `verify-backup`.

Never use `pg_resetwal`, replace the former-primary disk, or promote a lower-timeline replica
without verified backup/restore evidence.

### 7.2 July 2026 lessons learned

- Mesh registration success is not mesh convergence. Require current WireGuard handshakes and
  `first convergence observed`; stale pre-registration TCP bridge sessions may require a
  disk-preserving single-node roll.
- A gateway on another mesh is unreachable by design. Every PG member therefore runs a
  loopback-only r2host gateway; upstream credentials are read from the TOML named by
  `R2_UPSTREAM_CREDS` (for this deployment, `~/.attestmesh/r2-teleport-pg-ha.toml`).
- rclone serve-s3 buckets are encrypted directories. An empty `mkdir` is not durable on object
  storage, so gateway startup writes an encrypted `.attestmesh-bucket` marker for local bucket
  `r2`. Upstream R2 is authoritative; the gateway cache remains disposable.
- Backup workers must wait for PostgreSQL and poll replicas for promotion. Startup failure or a
  replica role must not consume an entire six-hour interval. Failures retry in bounded intervals.
- Verification requires fresh evidence: all local gateways ready, a base-backup success and a
  logical-dump upload no older than seven hours, plus WAL archive status. Historical success is
  insufficient.
- A provider rotation is scale-out followed by scale-in: prove the new replica is streaming,
  reseal the final odd-sized peer map on a surviving member, observe the retired etcd voter being
  removed, and only then stop the old CVM. `PGHA_PEERS_OVERRIDE` must reach both box and Phala
  sealed environments; a deployment-time override that only affects one provider is unsafe.
- If an etcd voter is removed accidentally while its PostgreSQL disk remains valid, set
  `PGHA_ETCD_FORCE_REJOIN=true` for one roll of that node. It clears only local etcd state and
  rejoins through live peers. Immediately reseal it to `false`; never delete PostgreSQL data for
  an etcd-only membership repair.
- Serial diagnostics include bounded PostgreSQL collector logs. `could not locate a valid
  checkpoint record` is local PGDATA corruption: with quorum and two verified copies, replace that
  replica's disk and let Patroni base-backup it. Do not use `pg_resetwal`.

## 8. Accepted v1 deviations (hardening follow-ups)

- No default-DROP egress firewall inside the shared sidecar netns. The command agent keeps
  its own deny-all `command-agent-egress-fw` network namespace.
- No etcd peer TLS / client TLS — mesh transport is already encrypted and attested;
  etcd client API is password-gated (`.pgha.etcd.v1`).
- Patroni health GETs unauthenticated (required by HAProxy; expose no secrets).
- CVM TEE blocks container logs — all entrypoints append progress/errors to a shared
  status volume (`/pgha-status/state`), the walg pattern.

## 9. Interfaces consumed (attestation-method-agnostic)

Only sidecar app-gRPC (`GetMeshStatus`, `GetClusterSharedKey`, `GetSelf`,
`SubscribeMessages`) and public cluster reads (`memberIdOf`, `meshIpOf`, `listMembers`,
`meshCidr`, `xPubkeyOf`). Nothing dstack-
specific leaks into the HA layer beyond the deploy trio itself (which is the dstack
deployment path by definition).
