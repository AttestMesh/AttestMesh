# Indexer Load Balancer / Blue-Green and Stage A HA Runbook

## Shape

`attestmesh-indexer-lb` is a small C3 member that owns the stable public Indexer endpoints:

```text
sidecars ── gRPC/TLS ──> <lb-app>-50052.gateway.attestmesh.xyz
                              │
                              └─ HAProxy TCP ──> active Indexer or worker pool :50052

operators ── C3 mesh ──> <lb-mesh-ip>:50053 prepare / commit / abort
public diagnostics ─────> <lb-app>-9090.gateway.attestmesh.xyz
                              │
                              └─ HAProxy HTTP ─> same active worker(s) :9090
```

The LB has no Indexer signing key and never terminates the gRPC protocol. `IndexerRegistry.current()` keeps the stable LB endpoint but always carries the active backend or pool's compose/code ID and Ed25519 signing pubkey. The on-chain v1 registry record and its endpoint shape are unchanged by Stage A HA.

## Current Base deployment

As of 2026-07-11, the production blue/green pair is:

| Role | App ID | Detail |
| --- | --- | --- |
| Stable LB | `0x42B122e37c9805E7C5A7c77aFF0f6C3c80603602` | mesh control `10.18.1.13:50053` |
| Active blue | `0x0ab50663D241C2A34a57Bb05c50018E680a92a21` | compose/code ID `0xf6e46e33dce5ecb83e5877e7ff519f8e48974af160646c840575f60298edb9df` |
| Standby green | `0x74C0370C675af3a5a2c89FE8D90dBC433fBDB7d1` | retained as the immediate rollback backend |

The stable gRPC endpoint is `https://42b122e37c9805e7c5a7c77aff0f6c3c80603602-50052.gateway.attestmesh.xyz`; stable HTTP diagnostics use the same host with port `9090`. Both colors run Indexer image `ghcr.io/attestmesh/attestmesh-indexer@sha256:14e9e3f6869ada585642c14e4e2e43bee197978ffc68d146dd6d3cddbc49d2bb`.

Treat the on-chain registry and `active` command as authoritative if this record is stale. Keep green running until the protocol-v3 exact-cursor Sidecar rollout is complete and subscriber/checkpoint diagnostics remain caught up.

## Why the switch is two-phase

An Indexer backend has an attestation-derived signing key and its own cursor database. A plain HAProxy address change would send reconnecting sidecars to a key that does not match `IndexerRegistry`, and a new backend could otherwise treat them as new subscribers.

The implemented cutover is:

1. `prepare`: create a durable operation ID, probe the candidate or every pool member, verify its identity/readiness, and pause new LB frontend accepts while existing streams continue.
2. Persist registry-mutation intent in the controller journal, then update `IndexerRegistry` to the stable LB endpoint plus candidate code ID/pubkey.
3. `commit`: present the same operation ID, re-probe, switch both HAProxy backends to the selected worker(s), close old LB streams, and reopen the frontends.
4. Sidecars reconnect, re-read the registry, and send their last handled block. The candidate replays that block, so same-block duplicates are possible but gaps are not.

The sidecar persists the last signed checkpoint in `SIDECAR_STATE_DIR/indexer-cursor.v1`. Every production sidecar must run the protocol-v3 exact-cursor implementation and mount `SIDECAR_STATE_DIR` on durable storage before a shared pool is activated. An ephemeral or missing state directory can lose the exact resume cursor on restart.

The driver stores its crash-durable transaction journal in `deploy/logs/indexer-lb-transaction-<lb-name>.json`. If it exits or loses an SSH/HTTP response, run `deploy/indexer-lb-node.sh <lb-name> recover`; recovery queries the operation ID instead of assuming a timed-out commit failed. If the registry transaction fails, the driver keeps the old backend. If commit definitively fails after the transaction, it restores the previous endpoint/code ID/pubkey tuple with a fresh on-chain `updatedAt`, then restores the previous HAProxy pool. Registry rollback happens first so the old data plane is never deliberately reopened against the new identity.

Registry writes currently require the configured `DEPLOYER_ADDR` to equal `IndexerRegistry.owner()`. The driver fails before prepare if ownership differs; it does not yet emit an org Safe transaction flow.

## Stage A shared-identity worker pool

Stage A makes the Indexer workers redundant while retaining the existing stable LB boundary. A pool is two to eight private worker addresses behind the same HAProxy front door. Every worker must report all of the following from `/status`:

- `identityMode="cluster-shared"`
- the same nonzero `pubKey` and `codeId`
- the same exact nonzero `indexerCluster`
- a distinct, nonzero `servingMemberId`
- an RPC-reachable, caught-up read model

The `indexerCluster` must be a dedicated Dstack-only Indexer cluster. It must not be the main C3 cluster that contains the LB or general workloads. Named worker state is used as the expected cluster and compose hash; the driver reads only the fixed `VM_ID`, `X`, `CLUSTER`, and `H` keys as untrusted data, validates their exact shapes, and never sources the state file as shell code. It rejects a status response that differs from that state and requires every replica to have the same hash. Shared named workers must be reachable over the same host's private bridge because the main-C3 LB has no mesh route into the dedicated cluster. Alternatively, pass explicitly private routable IPs together with `INDEXER_BACKEND_CODE_ID` and `INDEXER_HA_CLUSTER`.

Every shared-pool operation also requires `INDEXER_BACKEND_PUBKEY` as a nonzero, independently obtained pin. Obtain and verify this key from trusted enclave serial output or attestation evidence out of band; never copy it from the unauthenticated bridge `/status` response used by the switch. The driver requires every worker's reported key to match the pin before it can prepare the registry transaction.

Shared activation is additionally fail-closed behind `INDEXER_PROTOCOL_V3_FLEET_CONFIRMED=1`. Set this operator confirmation only after fleet inventory proves that all production sidecars have both durable `SIDECAR_STATE_DIR` storage and protocol-v3 exact-cursor support. The driver and the sealed LB controller both require the exact value `1`; update an existing LB once to seal the confirmation before its first pool switch. Without it, every multi-backend switch and shared initial pool is rejected, while legacy single-backend blue/green switching remains available.

### Dedicated worker bootstrap placeholder

The exact production bootstrap sequence for the dedicated Indexer cluster and its shared worker identity is intentionally pending the reviewed worker deployment slice. It must pin the approved artifact/compose hash and document the owner or Safe-mediated cluster-acceptance flow before operators use it. Until that procedure lands, do not infer ad-hoc cluster or Safe commands from this LB runbook; use the existing deployment helpers only in their documented modes and do not activate an unreviewed production pool.

Select a prepared shared pool with one comma-separated argument:

```bash
source deploy/env.sh

export INDEXER_BACKEND_PUBKEY='0x<verified 64-hex-character shared key>'
export INDEXER_PROTOCOL_V3_FLEET_CONFIRMED=1
# Seal the confirmed gate into an existing LB controller before its first pool switch.
deploy/indexer-lb-node.sh attestmesh-indexer-lb update
deploy/indexer-lb-node.sh attestmesh-indexer-lb switch indexer-ha-r1,indexer-ha-r2
deploy/indexer-lb-node.sh attestmesh-indexer-lb active
deploy/indexer-lb-node.sh attestmesh-indexer-lb verify-lb
```

Pool preparation intentionally does not require `grpcAccepting=true`: a worker may keep gRPC closed while the registry still names the old identity. Preparation does require every worker's exact shared identity, code ID, dedicated cluster, distinct member ID, RPC reachability, chain-head lag below ten blocks, and a populated read model. After the unchanged v1 registry record is written, commit waits up to 90 seconds for every prepared worker to report `grpcAccepting=true` and healthy before exposing any of them.

HAProxy round-robins new TCP connections over healthy workers. The gRPC slots forward to `:50052` but use each worker's `:9090/healthz` response as their native health check, so an RPC-dead or lagging worker is removed and its sessions are closed even if the controller is unavailable. The controller watches the HAProxy socket generation; an isolated HAProxy restart automatically reloads and revalidates the crash-durable active pool before reopening. A prepared operation remains paused for explicit recovery. Existing streams otherwise remain pinned to their selected worker until a cutover closes them. `active` reports the exact persisted backend list and identity metadata; `verify-lb` probes every active shared worker, not only the public endpoint.

Stage A removes a single Indexer worker as a serving-path dependency, but the stable LB remains a front-door SPOF. Planned worker maintenance must first switch to a pool that omits the worker, then run `assert-drained` before stopping it. That authenticated assertion requires the current active operation token, no prepared operation, an exact active-pool/runtime match, and the worker's address to be absent. If compromise of the shared cluster key is suspected, routing eviction is not key rotation: build a fresh dedicated cluster and shared identity, then use the same two-phase switch to rotate the registry and entire pool.

## One-time migration from a direct endpoint

Deploy and verify a separate green candidate first; do not use the currently registered direct backend as the LB's first candidate.

```bash
source deploy/env.sh

# Candidate is fully indexed and healthy but does not touch IndexerRegistry.
deploy/indexer-member-node.sh attestmesh-indexer-c3-green candidate

# Deploy the stable LB and atomically select green.
deploy/indexer-lb-node.sh attestmesh-indexer-lb all attestmesh-indexer-c3-green

# Inspect the stable endpoint, registry key, and active backend.
deploy/indexer-lb-node.sh attestmesh-indexer-lb verify-lb
deploy/indexer-lb-node.sh attestmesh-indexer-lb active
```

Sidecars already connected directly to the old Indexer will remain on that established connection until it closes. After confirming the registry points to the LB and the green backend is active, stop the old direct backend to force those remaining streams through the LB:

```bash
FORCE_CLEANUP=1 deploy/indexer-member-node.sh attestmesh-indexer-c3 stop
```

Confirm sidecar diagnostics show `indexer_connected=true`, `indexer_caught_up=true`, and cursor movement before removing any old deployment resources.

## Subsequent hot switch

```bash
source deploy/env.sh

deploy/indexer-member-node.sh attestmesh-indexer-c3-next candidate
deploy/indexer-lb-node.sh attestmesh-indexer-lb switch attestmesh-indexer-c3-next
deploy/indexer-lb-node.sh attestmesh-indexer-lb verify-lb

# Keep the old candidate available until the fleet has reconnected and checkpointed.
FORCE_CLEANUP=1 deploy/indexer-member-node.sh attestmesh-indexer-c3-green stop
```

Named candidates default to their same-host bridge IP because it avoids routing the Indexer's public serving path back through the mesh gateway transport. For a legacy single-C3 candidate only, set `INDEXER_LB_BACKEND_MODE=mesh` to use its C3 mesh IP. Shared pools reject mesh mode; use the same-host bridge or pass a literal private routable IP with the required identity metadata.

## LB operations

```bash
# Generate the 0600 mesh-control secret.
deploy/indexer-lb-node.sh attestmesh-indexer-lb setup

# Show active/prepared state and HAProxy runtime state.
deploy/indexer-lb-node.sh attestmesh-indexer-lb active

# Recover an interrupted/uncertain switch from its durable local journal.
deploy/indexer-lb-node.sh attestmesh-indexer-lb recover

# Prove a worker is absent from the tokenized active generation and live slots
# before stopping it. Optionally pin the expected generation in the environment.
INDEXER_LB_ACTIVE_OPERATION_ID=<64-hex-operation-id> \
  deploy/indexer-lb-node.sh attestmesh-indexer-lb assert-drained indexer-ha-r1

# Abort only a pre-registry-intent prepare, using its operation ID (or journal).
deploy/indexer-lb-node.sh attestmesh-indexer-lb abort <operation-id>

# Roll the LB compose itself; its active-backend volume survives the update.
deploy/indexer-lb-node.sh attestmesh-indexer-lb update
```

The control key lives in `~/.attestmesh/indexer-lb.env`. The control listener binds only to `attestmesh0:50053`; all state and operation endpoints, including `active`, require that bearer key and are not public gateway APIs.

## Expected delivery semantics

- Backend changes intentionally close gRPC streams; sidecars reconnect with backoff.
- Protocol v3 resumes strictly after the exact durable `(blockNumber, logIndex)` cursor. A checkpoint cursor denotes the next block to process; it does not replay the preceding checkpoint block.
- A crash can still duplicate an application effect if a handler performs that effect before durably advancing its cursor. Such effects must therefore be idempotent at the application boundary.
- A missing or corrupt durable cursor fails closed instead of silently resetting or starting over.
- The transport cursor is resume metadata, not a durable application inbox. Consumers that need durable work ownership must persist their own inbox/queue before acknowledging handling.
- Candidate and old Indexers can index concurrently; they are read-only apart from their independent cursor volumes.
- Do not stop the old backend until the stable public `/status` pubkey matches `IndexerRegistry.current()` and fleet diagnostics have caught up.
