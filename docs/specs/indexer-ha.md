# Indexer High Availability — Dog-Fooded Indexer Cluster

**Status:** APPROVED
**Author:** LSDan
**Created:** 2026-06-10
**Last Updated:** 2026-06-10
**Parent spec:** [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) §6, [`indexer.md`](./indexer.md) §3/§9
**Components:** `indexer/`, `sidecar/`, `contracts/` (IndexerRegistry), `deploy/`

## Overview

The live Indexer is a single attested CVM — one process, one attestation-bound signing
key, one `IndexerRegistry` record. If it dies, every cluster on the chain loses event
push until an operator intervenes. Members keep functioning (the sidecar's chain-read
reconcile poll is the authoritative path; indexer pushes are a latency cut — master
spec §7.2), but the latency tier is a single point of failure and `setIndexer` rotation
churns every subscriber.

Milestone B makes the Indexer **a customer of its own product**: N replicas form an
AttestMesh cluster (the "indexer cluster"), and the two hard HA problems fall out of
primitives we already shipped:

1. **Identity.** Today each TEE instance derives a *different* signing key, and the
   registry stores exactly one pubkey. Instead, every replica derives the envelope
   signing key **from the indexer cluster's CSK** — a key that, by construction, only
   attested members of that cluster can hold (P2P pull gated by on-chain membership,
   verified against the `keccak256(CSK)` commitment). One pubkey in the registry, any
   replica signs, attestation-boundness is preserved transitively.
2. **Discovery/failover.** The registry record points at the indexer *cluster
   contract* instead of one endpoint. Sidecars enumerate its live members from chain
   state and derive each replica's gRPC ingress hostname exactly the way mesh peers
   already derive each other's (`<member>-50051s.<gateway-domain>`). Replicas joining
   or leaving never require a registry write — the chain stays the sole coordination
   layer (CRITICAL directive).

The supposed circular dependency ("the indexer cluster needs an indexer") does not
exist: sidecar bring-up has no indexer dependency — registration, peer exchange, mesh
convergence, and CSK distribution all run off direct RPC polling, with indexer
subscription as a non-gating optimization. The indexer cluster bootstraps itself cold.

## Requirements

### Must Have

- [ ] N ≥ 2 indexer replicas, each a CVM running the sidecar + the indexer process,
      members of a dedicated AttestMesh cluster on the chain they serve.
- [ ] Envelope signing key derived from the indexer cluster's CSK:
      `seed = HKDF-SHA512(CSK, info="attestmesh.indexer.signing.v2")` → Ed25519.
      All replicas produce signatures verifiable against the single registry pubkey.
- [ ] Each replica still serves its own attestation quote on subscribe, with
      `report_data = keccak256(abi.encode("attestmesh.indexer.v1", shared_pubkey))` —
      i.e. each instance attests *its own* TEE committing to the *shared* key, so a
      subscriber can verify any replica without caching per-replica identities.
- [ ] `IndexerRecord` v2 carries `{ cluster: address, pubKey: bytes32, codeId:
      bytes32, updatedAt: uint64 }`; sidecar discovery becomes: read registry → read
      indexer-cluster members (`listMembers`) → derive gRPC ingress per member →
      connect. Replica set changes require zero registry writes.
- [ ] Replica selection: rendezvous hashing of `(subscriber memberId, replica
      memberId)` for load spreading; on connect failure or stream death, fail over to
      the next-ranked replica with backoff. Signature verification is identical on
      every replica (shared key), so failover is invisible above the transport.
- [ ] Resume is **subscriber-authoritative**: the replica honors `Hello.from_block`
      (floored at the member's registration block) as the resume point. Per-replica
      sled cursors become a local optimization (replay-from-memory window), never
      cross-replica state. No shared cursor store, no leader, no replica-to-replica
      consistency protocol on the serving path.
- [ ] Replicas are independent watchers: each polls the chain, discovers clusters, and
      builds its member cache alone (these paths are already deterministic from chain
      state — `runtime.rs` boot catch-up + block watcher unchanged in shape).
- [ ] Dedup stays where it is: subscribers already drop duplicate `(blockNumber,
      logIndex)` — receiving overlapping pushes from two replicas during failover is
      harmless by construction; an integration test must prove it.
- [ ] The indexer cluster is deployed/joined via the existing `deploy/indexer.sh` +
      smithers workflow, extended to `ensure` the indexer *cluster* (per the standing
      rule: shared topology, never per-cluster deployments; one indexer cluster serves
      all networks and clusters it watches).
- [ ] Compromise/rotation runbook documented: a compromised replica ⇒ the shared
      signing key is burned ⇒ rotate by deploying a fresh indexer cluster (new CSK ⇒
      new key) and repointing the registry record once. Until CSK rotation exists as a
      primitive, this is the rotation story and must be stated honestly.

### Should Have

- [ ] Completeness cross-check: replicas exchange `last_indexed_block` heartbeats over
      their own mesh (peer gRPC) and export a `replica_lag_blocks` metric; alerting on
      divergence catches a stuck watcher even though serving never depends on it.
- [ ] Subscriber-side multi-home option (`INDEXER_FANIN=2`): subscribe to two replicas
      simultaneously and dedupe, trading bandwidth for zero-gap failover.

### Must NOT Have

- No shared mutable state between replicas (no shared sled/Postgres, no distributed
  lock, no leader election).
- No external key-management or remote-signing service (Model C); the CSK *is* the
  shared-identity mechanism and it's already attestation-gated.
- No change to envelope wire format, repro stubs, or the per-event verification story.

## Non-Requirements

- Geographic/multi-provider distribution of replicas (operational choice; nothing here
  precludes it).
- Subscription sharding for 10k+ streams (indexer.md §15 item 4 — separate concern,
  composes with this design since any replica can serve any subscriber).
- Byzantine replicas: a replica is a cluster member or it isn't; within the cluster the
  threat model is the TEE + boot-gate story, same as any AttestMesh cluster.

## Design

### Architecture

```
                       IndexerRegistry (per chain)
                       record: { cluster: 0xIDX..., pubKey: P_csk, codeId, updatedAt }
                                      │ read once
   subscriber sidecar ────────────────┤
        │  listMembers(0xIDX...) → [m1, m2, m3]          indexer cluster 0xIDX...
        │  rank by rendezvous(self, mi)                 ┌──────────────────────────┐
        ├── gRPC → <m1>-50051s.<gw-domain>  ──────────► │ replica 1  (sidecar+idx) │
        │     verify envelopes against P_csk            │ replica 2  (sidecar+idx) │◄─ wg mesh,
        └── on failure: next-ranked replica             │ replica 3  (sidecar+idx) │   CSK, heartbeats
                                                        └──────────────────────────┘
                                                          each: own RPC watcher,
                                                          own local cursor cache,
                                                          shared CSK-derived signer
```

Each replica is two cooperating processes in one CVM compose: the standard
`cluster-mesh-agent` (registers the node into the indexer cluster, converges the mesh,
acquires the CSK) and `attestmesh-indexer` (gets the CSK from the sidecar over the
existing agent gRPC, derives the signing key, runs the watcher + gRPC server).
The indexer process gates "ready" on `csk_acquired` from the sidecar health surface.

### Components

1. **`contracts/src/registry/IndexerRegistry.sol` v2** — record gains `cluster`
   (replaces `endpoint` as the primary pointer; `endpoint` is kept and served as a
   legacy fallback so v1 sidecars keep working against a designated replica during
   migration). Owner (org Safe) writes it once per indexer-cluster generation.
2. **`indexer/src/identity.rs` v2** — `Identity::derive_shared(csk: &[u8;32],
   dstack: &dyn DstackRuntime)`: HKDF the signing seed from the CSK, quote binds the
   shared pubkey. The v1 per-instance derivation path remains for single-instance
   deployments (config: `INDEXER_IDENTITY=instance|cluster-shared`).
3. **`indexer/src/main.rs` / `config.rs`** — new config: `AGENT_GRPC_ADDR` (to fetch
   the CSK from the co-located sidecar), `INDEXER_IDENTITY`. The indexer subscribes to
   *no* indexer for its own cluster's events (it watches all clusters directly,
   including the indexer cluster itself — self-watching is just another cluster
   address in the discovered set).
4. **`indexer/src/grpc/service.rs`** — make `Hello.from_block` authoritative for
   resume (today it's `max(persisted_cursor, from_block, registration_block)`,
   `service.rs:123-132`; the persisted-cursor max() term is dropped to floor-only,
   because a replica that never served this subscriber has no cursor and must not
   start it at chain-head). Replay below the in-memory window falls back to
   `eth_getLogs` paging for that range (bounded by `INDEXER_START_BLOCK`).
5. **`sidecar/src/chain/registry.rs` + `indexer_client.rs`** — read record v2;
   enumerate indexer-cluster members; derive ingress hostnames via the existing
   `sni_for` convention (`bringup.rs:69-72`, gateway TLS-passthrough on the indexer's
   gRPC port, the `assume_http2(true)` lesson already encoded); rendezvous-rank;
   reconnect loop walks the ranking. Re-enumerate membership on every reconnect so
   replica churn is picked up without restart.
6. **`sidecar/src/agent_grpc.rs`** — expose the CSK to the co-located indexer process
   over the existing app-facing surface (it already serves the CSK to the application
   container; the indexer *is* the application container here — no new surface).
7. **`deploy/`** — `indexer.sh` grows `ensure-cluster` (deploy the indexer cluster
   diamond via the standard factory path), `join` (bring up replica i: CVM with
   sidecar+indexer compose), and the registry-write step moves to once-per-generation.
   The smithers workflow gains a fan-out over replicas.

### Interfaces

| Surface | Change |
|---|---|
| `IIndexerRegistry` | `IndexerRecord` v2 `{cluster, endpoint(legacy), codeId, pubKey, updatedAt}` |
| `indexer.proto` | optional advisory `served_by` on the push envelope (unsigned; ignored by v1 subscribers); `Hello.from_block` semantics tightened, documented |
| indexer config | `INDEXER_IDENTITY`, `AGENT_GRPC_ADDR` |
| sidecar config | `INDEXER_FANIN` (default 1) |
| sidecar↔indexer (co-located) | existing agent gRPC `GetClusterSharedKey` — unchanged |

### Data Model

- No new persistent stores. Per-replica sled cursor store is retained as a cache;
  losing it costs a re-page from RPC, not correctness.
- CSK→signing-key derivation is deterministic, so all replicas of one cluster
  generation agree on `P_csk` with no exchange; the registry pubkey is written from
  the first replica's derivation and every later replica's boot self-check
  (`registry.rs:31-64`) verifies it matches — now a hard failure (refuse to serve)
  rather than v1's log-and-continue, since a mismatch means wrong cluster or wrong
  CSK, never a benign rollout state.

## Open Questions

- [ ] CSK-rotation primitive: fresh-cluster-per-generation is the honest v2 story, but
      a real rotation mechanism (tied to the deferred RecoveryFacet / break-glass
      research) would make compromise response cheaper. Track as a follow-on spec.
- [x] ~~Member removal: does milestone B need deregistration on the diamond?~~
      **Resolved (2026-06-10): yes — owner-only `AttestFacet.removeMember(memberId)`**
      (cluster owner = org Safe for the indexer cluster). Additive core-facet change,
      generally useful beyond HA. Removal semantics to pin down in the contracts
      design: the freed mesh IP must not be reusable while live members may still
      route to it (proposal: removal tombstones the memberId — `isClusterMember`
      false, record retained); and a removed member **still holds the CSK** — removal
      is eviction from coordination, not key revocation, which remains the
      fresh-generation rotation story below.
- [x] ~~Should envelopes name which replica served them?~~ **Resolved (2026-06-10):
      yes — advisory `served_by` field** (the serving replica's memberId) in the push
      envelope, explicitly **excluded from the signed bytes** so it is never an
      authenticated claim and identical events keep identical signatures across
      replicas. Used for subscriber-side logs and per-replica lag attribution only.
      (This is the one `indexer.proto` addition; the "no wire change" line in
      §Interfaces is amended accordingly — the field is optional and ignored by v1
      subscribers.)
- [ ] Indexer gRPC through the gateway requires the TLS-passthrough `s` route per
      ingress; confirm the gateway's connection limits are comfortable with
      (subscribers × replicas-tried) reconnect storms after a chain RPC outage.

## Alternatives Considered

### Multi-record registry, fully independent replicas (per-replica keys)
Each replica registers its own `(endpoint, pubkey)`; subscribers verify per-replica.
Honest and simple, but subscribers must track N identities and N attestations, failover
churns identity caches, and the registry needs writes on every replica change —
exactly the off-chain-coordination shape this project avoids. The CSK-shared key gives
one identity with the same attestation guarantee.

### Active-passive failover (operator repoints the single record)
Minutes of downtime per failover, operator on the critical path, subscriber churn on
every key change. Strictly dominated by the cluster design once the CSK insight is on
the table.

### Shared identity via external KMS / remote signing (Model C)
Introduces a trusted external service on the signing path — violates the
trust-minimization thesis. The CSK already *is* an attestation-gated shared secret
with an on-chain commitment; no new trusted party needed.

### Shared cursor store (Postgres/replicated sled)
Cross-replica mutable state, monotonic-advance conflicts (`cursor.rs:95-109`), and a
new stateful dependency — all to optimize something the subscriber already knows
(its own resume point). Subscriber-authoritative resume deletes the problem.

## Traceability

*Filled in during implementation*

| Requirement | Implementation | Tests |
|-------------|----------------|-------|
| | | |

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-06-10 | LSDan | Initial draft |
