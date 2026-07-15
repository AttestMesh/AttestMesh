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
4. Sidecars reconnect, re-read the registry, and resume strictly after their durable protocol-v3 `(blockNumber, logIndex)` cursor. Whole-block replay is a legacy protocol-v1 fallback only; it is not the shared-pool delivery contract.

The sidecar persists the last signed checkpoint in `SIDECAR_STATE_DIR/indexer-cursor.v1`. Every production sidecar must run the protocol-v3 exact-cursor implementation and mount `SIDECAR_STATE_DIR` on durable storage before a shared pool is activated. An ephemeral or missing state directory can lose the exact resume cursor on restart.

The driver stores its mode-`0600`, crash-durable transaction journal at `~/.attestmesh/indexer-lb/indexer-lb-transaction-<lb-name>.json` by default. It signs the exact raw registry transaction locally, records its raw bytes, hash, sender nonce, purpose, desired tuple, and finality requirement, fsyncs the file and directory, and only then publishes. The raw transaction and control bearer are passed to helper processes over private pipes rather than command-line arguments. If the driver exits or loses an SSH/HTTP response, run `deploy/indexer-lb-node.sh <lb-name> recover`; recovery republishes only those exact signed bytes and resolves the exact transaction receipt rather than inferring success from `IndexerRegistry.current()`. A missing receipt remains paused while the transaction is pending or unseen. A nonce replacement is conclusive only after finalized-nonce proof plus the configured observation depth; a mined success or revert is conclusive only when its receipt block is at or below Base's `finalized` L2 head. Unsupported or unavailable finality RPC data fails closed.

Base finality is normally much slower than ordinary block inclusion. `INDEXER_LB_TX_WAIT_SECONDS` defaults to 1800 seconds and may be increased; a timeout does not roll forward or back—it retains the signed journal and paused controller for a later `recover`. If a commit response is lost, recovery waits for the controller's serialized operation status. It never races an in-flight commit with rollback. If commit is definitively quiescent and failed after the forward transaction finalized, the driver signs and journals a separate rollback transaction, restores the previous endpoint/code ID/pubkey tuple with a fresh on-chain `updatedAt`, waits for rollback finality, and only then restores the authoritative previous HAProxy pool.

Registry writes currently require the RPC chain ID to equal `CHAIN_ID`, the configured private key to derive exactly `DEPLOYER_ADDR`, and `DEPLOYER_ADDR` to equal `IndexerRegistry.owner()`. The driver fails before prepare if any binding differs; it does not yet emit an org Safe transaction flow.

### Private local authority and upgrades

`INDEXER_LB_STATE_DIR` defaults to `~/.attestmesh/indexer-lb`. The directory must be owned by the operator with exact mode `0700`, its immediate ancestor must not be group/world writable, and authority files must be operator-owned mode `0600`. It contains the LB's generic deployment state, routing state, control key, transaction journal, cutover lock, verified candidate snapshots, response records, and verification scratch data. `deploy/logs` is not an authority directory.

On the first upgraded invocation, the driver can migrate the former generic and routing files from `deploy/logs` only after a single `O_NOFOLLOW` read of an operator-owned mode-`0600` file. Migration additionally binds the LB app ID to `INDEXER_LB_EXPECTED_APP_ID` or the current canonical registry endpoint, binds its mesh IP on-chain, requires the exact stable endpoint, and compares routing fields with authenticated controller `/active`. The former `~/.attestmesh/indexer-lb.env` control key is copied without rotation and retained as a rollback copy. An old unfinished transaction journal has no exact signed-transaction/finality proof and therefore blocks all new mutations; resolve it using the prior driver or an audited manual procedure before removing it. Do not delete it merely to unblock a switch.

Always source trusted `CLUSTER`, `MEMBER_IMPL`, and `KMS_ROOT` values; the LB driver no longer accepts Matrix or generic authority from `deploy/logs`. `PRIVATE_KEY` and authenticated `RPC_URL` are still passed to local Foundry commands under the operator-host trust boundary. Moving signing into a keystore, hardware signer, or organization Safe is follow-up work; the current driver explicitly does not claim to hide `PRIVATE_KEY` from local process inspection.

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

### Dedicated worker bootstrap

Use `deploy/indexer-ha-replica-node.sh`; do not infer ad-hoc cluster or Safe commands from this LB runbook. Run its `bootstrap-help` action for the current ordered procedure. `prepare` writes the exact Safe admission payload, a human executes that payload through the configured organization Safe, and `finish` waits for the admission before binding, starting, registering, and validating the warmed candidate. The helper pins the reviewed worker artifacts and keeps IndexerRegistry mutation exclusively in the LB cutover flow.

Select a prepared shared pool with one comma-separated argument:

```bash
source deploy/env.sh

export INDEXER_BACKEND_PUBKEY='0x<verified 64-hex-character shared key>'
export INDEXER_PROTOCOL_V3_FLEET_CONFIRMED=1
export INDEXER_BACKEND_STATE_DIR="$HOME/.attestmesh/indexer-ha"
# Seal the confirmed gate into an existing LB controller before its first pool switch.
deploy/indexer-lb-node.sh attestmesh-indexer-lb update
deploy/indexer-lb-node.sh attestmesh-indexer-lb switch indexer-ha-r1,indexer-ha-r2
deploy/indexer-lb-node.sh attestmesh-indexer-lb active
deploy/indexer-lb-node.sh attestmesh-indexer-lb verify-lb
```

Pool preparation intentionally does not require `grpcAccepting=true`: a worker may keep gRPC closed while the registry still names the old identity. Preparation does require every worker's exact shared identity, code ID, dedicated cluster, distinct member ID, RPC reachability, chain-head lag below ten blocks, and a populated read model. After the unchanged v1 registry record is written, commit waits up to 90 seconds for every prepared worker to report `grpcAccepting=true` and healthy before exposing any of them.

HAProxy round-robins new TCP connections over healthy, identity-verified workers. The gRPC slots forward to `:50052` but use each worker's `:9090/healthz` response as their native health check, so an RPC-dead or lagging worker is removed and its sessions are closed even if the controller is unavailable. On controller or HAProxy restart, the controller binds `/status` identity before checking serving health, tolerates an unavailable member only when at least one identity-matching member is healthy, and leaves unverified slots in `MAINT`. It refreshes identities every 15 seconds and enables a recovered slot only if the durable active generation is unchanged. A reachable identity mismatch, redirect, malformed response, oversized response, or LB-local/self-loop address fails closed, disables all runtime slots, and closes sessions. A prepared operation remains paused for explicit recovery. `active` reports the exact persisted backend list and identity metadata; `verify-lb` uses the same at-least-one-healthy policy while treating reachable identity divergence as fatal.

Stage A removes a single Indexer worker as a serving-path dependency, but the stable LB remains a front-door SPOF. Planned worker maintenance must first switch to a pool that omits the worker, then create a durable drain reservation with `assert-drained` before stopping it. The authenticated proof is bound to the exact backend and active operation, contains a random reservation ID and release token, survives response loss/controller restart, and prevents a later prepare, restore, or initial adoption from reusing that address. A failed or uncertain stop keeps the reservation. Release it explicitly only after the worker lifecycle is safe; release is token-bound and idempotent through a bounded durable tombstone. If compromise of the shared cluster key is suspected, routing eviction is not key rotation: build a fresh dedicated cluster and shared identity, then use the same two-phase switch to rotate the registry and entire pool.

## One-time migration from a direct endpoint

Deploy and verify a separate green candidate first; do not use the currently registered direct backend as the LB's first candidate.

```bash
source deploy/env.sh

# Keep single-C3 candidate authority out of deploy/logs.
install -d -m 0700 "$HOME/.attestmesh/indexer-candidates"
export GENERIC_STATE_DIR="$HOME/.attestmesh/indexer-candidates"
export INDEXER_BACKEND_STATE_DIR="$GENERIC_STATE_DIR"
export REQUIRE_PRIVATE_GENERIC_STATE=1
export STRICT_GENERIC_STATE_BINDINGS=1

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

install -d -m 0700 "$HOME/.attestmesh/indexer-candidates"
export GENERIC_STATE_DIR="$HOME/.attestmesh/indexer-candidates"
export INDEXER_BACKEND_STATE_DIR="$GENERIC_STATE_DIR"
export REQUIRE_PRIVATE_GENERIC_STATE=1
export STRICT_GENERIC_STATE_BINDINGS=1

deploy/indexer-member-node.sh attestmesh-indexer-c3-next candidate
deploy/indexer-lb-node.sh attestmesh-indexer-lb switch attestmesh-indexer-c3-next
deploy/indexer-lb-node.sh attestmesh-indexer-lb verify-lb

# Keep the old candidate available until the fleet has reconnected and checkpointed.
FORCE_CLEANUP=1 deploy/indexer-member-node.sh attestmesh-indexer-c3-green stop
```

Named candidates default to their same-host bridge IP because it avoids routing the Indexer's public serving path back through the mesh gateway transport. New single-C3 candidates must use the private state directory shown above. For a pre-upgrade candidate that exists only in `deploy/logs`, automatic ingestion requires an independently recorded `INDEXER_EXPECTED_BACKEND_STATE_SHA256`; calculating a hash from a directory after suspected tampering is not an independent pin. `INDEXER_EXPECTED_BACKEND_APP_ID` may add an app-address check, but it never replaces the digest because it does not bind the legacy VM ID or code hash. For a legacy single-C3 candidate only, set `INDEXER_LB_BACKEND_MODE=mesh` to use its C3 mesh IP. Shared pools reject mesh mode; use the same-host bridge or pass a literal private routable IP with the required identity metadata.

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
  INDEXER_BACKEND_STATE_DIR="$HOME/.attestmesh/indexer-ha" \
  deploy/indexer-lb-node.sh attestmesh-indexer-lb assert-drained indexer-ha-r1

# List durable active reservations (including the release credentials).
deploy/indexer-lb-node.sh attestmesh-indexer-lb drain-reservations

# Explicitly release one after the worker lifecycle has completed safely.
deploy/indexer-lb-node.sh attestmesh-indexer-lb release-drain \
  <worker-private-ip> <reservation-id> <release-token>

# Abort only a pre-registry-intent prepare, using its operation ID (or journal).
deploy/indexer-lb-node.sh attestmesh-indexer-lb abort <operation-id>

# Roll the LB compose itself; its active-backend volume survives the update.
deploy/indexer-lb-node.sh attestmesh-indexer-lb update
```

The control key lives in operator-owned mode-`0600` `~/.attestmesh/indexer-lb/control.env` by default. The control listener binds only to `attestmesh0:50053`, is not published as a host port, and all state and operation endpoints—including `active` and drain reservations—require that bearer key. Every mutating driver action holds a per-LB nonblocking process lock for its full lifetime.

## Expected delivery semantics

- Backend changes intentionally close gRPC streams; sidecars reconnect with backoff.
- Protocol v3 resumes strictly after the exact durable `(blockNumber, logIndex)` cursor. A checkpoint cursor denotes the next block to process; it does not replay the preceding checkpoint block.
- A crash can still duplicate an application effect if a handler performs that effect before durably advancing its cursor. Such effects must therefore be idempotent at the application boundary.
- A missing or corrupt durable cursor fails closed instead of silently resetting or starting over.
- The transport cursor is resume metadata, not a durable application inbox. Consumers that need durable work ownership must persist their own inbox/queue before acknowledging handling.
- Candidate and old Indexers can index concurrently; they are read-only apart from their independent cursor volumes.
- Do not stop the old backend until the stable public `/status` pubkey matches `IndexerRegistry.current()` and fleet diagnostics have caught up.
