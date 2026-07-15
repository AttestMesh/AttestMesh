# Indexer High Availability — Shared-Identity Replica Pool

**Status:** STAGE-A CODE COMPLETE; PRODUCTION ROLLOUT PENDING
**Author:** LSDan
**Created:** 2026-06-10
**Last Updated:** 2026-07-14
**Parent specs:** [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md), [`indexer.md`](./indexer.md)
**Components:** `indexer/`, `sidecar/`, `deploy/`

## Decision

Stage A removes the Indexer worker single point of failure without changing the
deployed `IndexerRegistry` ABI. A dedicated AttestMesh cluster contains two or
more homogeneous Indexer replicas. Every replica derives the same Ed25519
envelope-signing key from that cluster's CSK and is admitted to the existing
stable HAProxy endpoint as part of one backend pool.

The registry continues to contain exactly the deployed v1 tuple:

```text
(stableEndpoint, composeCodeId, sharedPubKey, updatedAt)
```

Old and new sidecars therefore use the same discovery path. Backend loss causes
HAProxy to route a reconnect to another replica, while protocol-v3's exact
subscriber cursor makes that reconnect independent of the selected replica's
local cursor database.

Direct on-chain replica discovery, a new registry address, subscriber fan-in,
and on-chain member tombstones are deferred to Stage B. The original PR changed
the immutable v1 registry's tuple in place and replaced newer delivery logic;
neither is a safe migration.

## Architecture

```text
                         immutable IndexerRegistry v1
                     stable endpoint + P_csk + codeId
                                   |
                                   v
 subscriber sidecars ------> stable Indexer LB
                              /      |      \
                             /       |       \
                    replica A   replica B   replica C
                       |            |           |
                       +--- dedicated dstack-only cluster ---+
                                      |
                                     CSK
                                      |
                         HKDF-SHA512 -> shared Ed25519 key
```

The stable LB remains a front-door single point of failure in Stage A. Its
existing deployment and two-phase cutover machinery are retained because they
already provide one registry-pinned endpoint and safe rollback. Stage A changes
its active backend from one worker to a bounded, health-checked pool.

## Requirements

### Identity and admission

- [x] Replicas belong to a fresh, dedicated Indexer cluster. Reusing the main C3
      CSK is forbidden because every C3 member would become an Indexer signer.
- [x] `INDEXER_IDENTITY=cluster-shared` fetches the CSK and self identity only
      from the co-located sidecar's Agent UDS.
- [x] Signing seed derivation is
      `HKDF-SHA512(CSK, info="attestmesh.indexer.signing.v2")`.
- [x] Different replicas with the same CSK derive the same pubkey; a fresh
      cluster/CSK produces a different pubkey.
- [x] The replica obtains its code ID from dstack `/Info.compose_hash`, requires
      a nonzero 32-byte value, and never trusts a blank/operator-supplied value in
      cluster-shared mode.
- [x] Before accepting gRPC subscriptions, a shared replica must match the v1
      registry's exact shared pubkey and code ID. Candidates may warm their HTTP
      read model before publication but fail closed on the serving path.
- [x] `/status` exposes `identityMode`, `indexerCluster`, `servingMemberId`,
      `codeId`, pubkey, read-model progress, and serving state for pool admission.

### Delivery continuity

- [x] Existing protocol-v2 replay/live ordering, awaited replay backpressure,
      signed checkpoints, and explicit overflow termination remain unchanged.
- [x] Protocol v3 adds an optional exact `DeliveryCursor(blockNumber, logIndex)`
      to `Hello`. Presence distinguishes a fresh client from the real cursor
      `(0, 0)`.
- [x] When supplied, the subscriber cursor is authoritative even if the selected
      replica's local Ack cursor is further ahead.
- [x] A normal event cursor replays strictly after that log within the boundary
      block. A checkpoint cursor starts at the following block.
- [x] The sidecar makes its handled cursor durable before sending the Ack. A
      failed durability write closes the stream without Ack so another replica
      replays the position.
- [x] An existing cursor file that is unreadable, malformed, or bound to another
      member fails closed; only a genuinely absent file is treated as fresh.
- [x] The Indexer accepts an Ack only for the exact next position emitted in that
      session, and the sidecar refuses to Ack without an active delivery consumer.
- [x] Sidecars retain `from_block` as a boundary-block fallback for older
      Indexers during the protocol rollout.

### Pool rollout

- [x] The LB supports one legacy backend or a shared-identity pool of two to
      eight distinct backends.
- [x] Pool prepare probes every candidate and requires identical nonzero pubkey,
      code ID, and Indexer-cluster address plus distinct serving member IDs.
- [x] Shared-pool prepare rejects the LB/C3 cluster as the signer cluster.
- [x] Prepare warms the pool without changing the data plane, then pauses new
      accepts while existing streams remain open.
- [x] The operator updates the unchanged v1 registry at the unchanged stable
      endpoint, commits the pool, and force-closes old sessions so clients
      rediscover the shared key and reconnect.
- [x] Failed commit restores the previous endpoint, code ID, pubkey, and LB pool
      before accepts reopen. The rollback is a new registry write and therefore
      uses a fresh `updatedAt`; it cannot reproduce the old four-field tuple.
- [x] Every mutation is serialized and bound to a durable operation ID. An
      incomplete or uncertain prepared generation leaves accepts paused until
      explicit recovery reconciles the journal, registry tuple, and runtime pool;
      a response lost after durable commit may already have reopened frontends,
      and recovery reports that committed outcome.
- [x] The exact signed registry transaction is fsynced before publication. A
      forward or rollback decision is not committed until the receipt and
      canonical registry tuple agree at Base's `finalized` head.
- [x] A registered worker cannot be stopped without a durable drain reservation
      bound to its exact backend and active generation. The reservation survives
      process/controller restart and blocks address reuse until an explicit,
      token-bound release is reconciled.
- [x] Worker identity, LB authority, control credentials, transaction journals,
      and release tokens live in owner-only state directories with symlink-safe,
      mode-checked reads; `deploy/logs/` is not an authority boundary.
- [x] Image publication is gated on Rust formatting, clippy, tests, deployment
      script parsing, and shell lint.

## Security invariants

### Cluster policy is signing policy

The raw CSK is deliberately exposed to the co-located Indexer over a node-local
UDS. Consequently, membership in the dedicated cluster is equivalent to
authority to sign Indexer envelopes. The cluster must:

1. be owned by the org Safe;
2. use only the dstack attestor facet;
3. allow only the exact replica compose hash;
4. use an explicit device allowlist (`allowAnyDevice=false`);
5. require an up-to-date TCB; and
6. contain no unrelated workload or weaker attestor.

The deployment helper fails closed unless it receives an exact compose hash,
one or more device IDs, and the intended Safe owner.

### Per-replica attestation is not claimed

The existing `IndexerAttestation` quote is diagnostic in Stage A. A quote that
binds only the shared public key is replayable by any CSK holder, and the current
wire protocol has no subscriber nonce or per-instance signing key. It therefore
does not prove which replica answered a connection.

A future cryptographic per-replica proof needs a challenge-bound instance key and
a verified quote binding at least:

```text
(instanceKey, sharedKey, generation, indexerCluster, memberId, codeMeasurement, nonce)
```

Stage A instead trusts the dstack cluster boot gate, LB admission checks, public
client-to-LB TLS routing, and the registry-pinned shared signature. LB-to-worker
status and gRPC traffic is unauthenticated HTTP/h2c on the same-host private
bridge, so host and bridge-network integrity remain part of the trust boundary.

### Removal versus compromise

Removing a worker from the LB is routing eviction, not key revocation. That
worker still knows the CSK and can produce valid signatures.

- Planned maintenance: drain the worker, close its sessions, verify zero active
  connections, then stop it.
- Suspected compromise or CSK exposure: deploy an entirely fresh dedicated
  cluster, validate a new shared key across at least two replicas, and roll
  forward through the registry/LB transaction. Never reuse or roll back to the
  burned generation.

### Retained trust and durability boundaries

Stage A deliberately does not claim the following properties:

- The stable LB is still a single front-door failure domain.
- The deployed v1 `IndexerRegistry` has an immutable owner, which is currently
  the configured deployer EOA on Base. The dedicated signer cluster is Safe-owned,
  but registry rotation is not a Safe transaction until a registry migration.
- The reviewed cluster Safe is currently a one-of-two Safe. Deployment tooling
  pins its proxy and singleton code, exact owner set and threshold, module/guard
  state, and fallback handler; any Safe configuration change requires a new
  review before the tooling will proceed.
- Protocol v3 still does not authenticate the subscriber to the Indexer. Message
  bodies remain sealed to the addressed member, and subscriber authentication is
  deferred.
- Cursor persistence proves the sidecar handler completed and gives failover
  continuity from the Indexer to the sidecar. It is not an application inbox or
  outbox transaction. `SubscribeMessages` has no application Ack: the sidecar
  advances after enqueueing to an active in-memory receiver, so an app disconnect,
  crash, or lag overflow can still lose delivery. Durable application delivery
  requires a future inbox/acknowledgment protocol or independent reconciliation.
- Stopping or removing a worker does not erase its on-chain historical membership
  or revoke a CSK it already learned.
- The immutable Base cluster factory installs its reviewed pre-Ed25519
  `NetworkFacet`. The Stage-A compose therefore keeps sealed `PeerEndpoint`
  exchange enabled for this dedicated cluster. Moving heartbeat keys entirely
  on-chain requires a separately reviewed and Safe-approved Network-facet cut;
  it is not bundled into the narrowly scoped 19-selector Dstack cut.

## Failure behavior

- **One worker dies:** HAProxy stops selecting it. Affected streams reconnect to
  another worker with their exact cursor.
- **Replica-local cursor is stale or ahead:** the explicit subscriber cursor wins.
- **Subscriber cursor file is damaged:** the sidecar refuses to subscribe until
  the operator repairs or deliberately removes the file; it never silently jumps
  to the indexed head.
- **Replay exceeds the delivery channel:** replay awaits capacity; live delivery
  cannot overtake it. A live overflow closes the stream for replay.
- **Registry does not yet contain the shared key/code ID:** candidates remain
  visible on diagnostic HTTP but do not accept subscriptions.
- **Registry changes away from a running shared generation:** serving fails
  closed and existing connections are terminated.
- **Pool commit fails:** after the forward write is finalized and controller
  failure is proven quiescent, the deployer writes the previous endpoint/code
  ID/pubkey with a fresh timestamp, waits for rollback finality, restores the
  previous pool, and only then reopens the frontends.
- **Stable LB dies:** worker capacity remains, but the public endpoint is down.
  Eliminating this remaining Stage-A SPOF is Stage B scope.

## Rollout order

1. Roll protocol-v3 sidecars before enabling the shared worker pool.
2. Build digest-pinned sidecar and Indexer images and record the rendered compose
   hash.
3. Create a fresh dedicated cluster with the closed policy above, have its Safe
   accept ownership, then use `patha-safe-prepare` and execute only the emitted
   exact `diamondCut` target/value/calldata.
4. Persist and verify the exact installed DstackFacet and ClusterMember runtime
   code, then start at least two replicas and verify identical cluster, code ID, and shared
   pubkey with distinct member IDs and caught-up read models.
5. Prepare the LB pool while the old Indexer remains active.
6. Snapshot and update the v1 registry at the same stable endpoint. Keep the
   controller paused until the signed transaction and tuple are canonical at
   Base's `finalized` head; the default wait budget is 1800 seconds, and timeout
   requires a later explicit `recover` rather than an inferred outcome.
7. Commit the pool, close old sessions, and verify canary delivery and cursor
   advancement.
8. Keep the old, uncompromised generation available through the observation
   window. Rollback is allowed only for operational failure, never suspected key
   exposure.

The operational commands and rollback payloads live in
[`deploy/indexer-lb-runbook.md`](../../deploy/indexer-lb-runbook.md).

## Stage B (deferred)

- Add a companion RegistryV2 at a new address; never mutate the deployed v1 ABI.
- Bind its cluster pointer to a hash of the exact current v1 tuple so any legacy
  registry write automatically disables stale direct discovery.
- Add direct chain-state replica discovery, rendezvous ranking, periodic
  membership/generation revalidation, and optional fan-in through one serialized
  delivery coordinator.
- Add historical-preserving member tombstones with separate active-member views;
  never change `listMembers()` or `memberCount()` semantics because the first
  historical member is the CSK originator.
- Remove the stable-LB SPOF or operate redundant front doors.
- Add challenge-bound, cryptographically verified per-replica attestation if that
  property remains required.

## Traceability

| Requirement | Implementation | Tests |
|---|---|---|
| Shared CSK identity | `indexer/src/identity.rs`, `indexer/src/agent.rs` | identity + Agent client tests |
| Actual code ID and serving gate | `indexer/src/dstack.rs`, `main.rs`, `registry.rs`, `health.rs` | config/registry/health tests |
| Exact failover cursor | `sidecar/proto/indexer.proto`, sidecar client, Indexer service | protocol-v3 unit/integration tests |
| Replica pool cutover | `deploy/indexer-lb-node.sh`, LB compose controller | controller concurrency, restart, recovery, health, and rollback tests |
| Dedicated worker deployment | `deploy/indexer-ha-replica-node.sh`, dedicated compose | worker state, image, config-drift, cleanup, and Safe-admission tests |
| Closed signer-cluster policy | `deploy/onchain.sh indexer-cluster` | signer-cluster host preflight tests |
| Safe Path-A handoff | `PrepareDstackFacetPathASafe.s.sol`, `patha-safe-prepare` | calldata/topology Foundry tests + shell bundle tests |

## Changelog

| Date | Change |
|---|---|
| 2026-06-10 | Initial direct-discovery design. |
| 2026-07-14 | Re-scoped to migration-safe Stage A after review: immutable v1 registry, shared-identity worker pool behind the stable LB, exact subscriber cursors, and explicit trust limits. |
| 2026-07-14 | Stage-A code completed with Safe-owned signer-cluster handoff, digest-pinned workers, crash-recoverable LB operations, and CI-gated deployment tests; production rollout remains pending. |
