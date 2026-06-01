# AttestMesh Indexer — Component Spec

**Status**: Draft v0.1
**Parent spec**: [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) (especially §6)
**Component**: `indexer/`
**Binary**: `attestmesh-indexer`
**Last updated**: 2026-05-30

---

## 1. Purpose

The Indexer is the off-chain event distribution layer. It watches every AttestMesh ClusterDiamond on its target chain (any diamond it has been asked to follow), and pushes events to the specific members of the cluster that emitted them — eliminating the need for each node sidecar to maintain its own chain RPC subscription. Master spec §6 defines *what* it does at the architectural level; this spec defines *what gets built*.

Each push is signed by the Indexer's attestation-bound key and paired with an `eth_getLogs`-style "repro stub" so subscribing members can independently verify any individual push against any chain RPC provider they trust. The Indexer is therefore trusted by members for liveness and completeness of event delivery, but not for correctness of event contents.

---

## 2. Toolchain

- **Rust edition 2021**, MSRV `1.78`.
- **Target**: `x86_64-unknown-linux-gnu` for normal builds, `x86_64-unknown-linux-musl` for the OCI image baked into the CVM.
- **Build**: `cargo build --release`; CI also runs `cargo clippy -- -D warnings` and `cargo fmt --check`.

### 2.1 Key dependencies

| Crate | Purpose |
|---|---|
| `tokio` (full) | async runtime |
| `tonic` + `prost` | gRPC server (member subscription) |
| `alloy` (`alloy-primitives`, `alloy-provider`, `alloy-sol-types`, `alloy-rpc-types-eth`) | EVM RPC: `eth_getLogs`, `eth_blockNumber`, `eth_chainId`, ABI decoding |
| `ed25519-dalek` | signs every push envelope |
| `zeroize` | zero-on-drop key material |
| `dstack-sdk` (vendored) | dstack runtime client (`derive_key`, `seal`, `get_quote`) |
| `sled` or `rocksdb` | local persistent store for per-member cursors |
| `tracing` + `tracing-subscriber` (json) | structured logging |
| `prometheus` + `axum` | metrics endpoint (counters per cluster/per member; gauge of subscriber count + chain head lag) |

### 2.2 Build-time codegen

- gRPC `.proto` from `proto/indexer.proto` — shared with the sidecar component. The sidecar repo's `proto/indexer.proto` is the canonical copy; this component's `build.rs` references it via a relative path during builds.
- Solidity ABI bindings for AttestFacet, MessageFacet, NetworkFacet, DstackFacet, and IndexerRegistry via the `sol!` macro against the JSON ABIs emitted by `forge build` in `contracts/`.

---

## 3. Process model

A single process runs inside a dstack CVM. v1 ships **one instance** per chain (Sepolia for v1; Base mainnet for milestone B); HA is deferred. Master spec §13 item 5 captures the operational shape — multi-replica + dog-fooded-on-AttestMesh shapes remain open but are explicitly deferred.

Runtime requirements:

- Listens on TCP for inbound gRPC subscriptions from member sidecars (`INDEXER_GRPC_ADDR`, default `0.0.0.0:50051`). Externally TLS-terminated at a load balancer; the worker process itself speaks h2c.
- Holds open one EVM RPC connection per chain (HTTP/2 against `RPC_URL`; WebSocket fallback in milestone B).
- Reads its own `IndexerRegistry` record once at boot to verify it matches its own attested identity (sanity check; logs at warn on mismatch).
- Persistent state: per-(clusterAddress, memberId) cursor (highest delivered `(blockNumber, logIndex)`), stored in a local key-value store sealed via dstack.

Process lifecycle:

- Boot → derive identity keys → load persistent cursors → catch up against chain head → open gRPC listener → enter steady state.
- Steady state runs three concurrent loops (described in §7).
- SIGTERM triggers graceful shutdown: stop accepting new subscriptions, finish flushing in-flight pushes, persist cursors, exit. Restart picks up exactly where it left off.

Restart policy: docker-compose `restart: unless-stopped`. A crashed Indexer brings down its gRPC listener, which all subscribing sidecars notice via stream-drop and reconnect against. No state is lost across restarts beyond in-flight unacked pushes (which are replayed from cursor).

---

## 4. File layout

```
indexer/
├── Cargo.toml                       # workspace root (single crate for v1)
├── proto/indexer.proto              # symlink/import from sidecar/proto/indexer.proto
├── build.rs                         # tonic-build + alloy sol! bindings
├── src/
│   ├── main.rs                      # entry, arg parsing, tokio runtime
│   ├── config.rs                    # env-var schema + load
│   ├── identity.rs                  # attestation-bound key derivation, sealing, attestation request
│   ├── chain/
│   │   ├── mod.rs                   # provider setup
│   │   ├── watcher.rs               # block-by-block eth_getLogs poll
│   │   ├── repro.rs                 # rpc-repro-stub generation
│   │   └── verify_member.rs         # AttestFacet.memberOf check on subscribe
│   ├── grpc/
│   │   ├── mod.rs                   # tonic server bootstrap
│   │   ├── service.rs               # the Indexer service impl
│   │   ├── envelope.rs              # PushEnvelope builder + Ed25519 sign
│   │   └── subscribe.rs             # per-subscription state + send loop
│   ├── state/
│   │   ├── mod.rs                   # in-memory + persistent state mgmt
│   │   ├── cursor.rs                # per-(cluster, member) cursor storage
│   │   └── subscribers.rs           # per-cluster subscriber set
│   ├── registry.rs                  # IndexerRegistry self-check
│   ├── metrics.rs                   # prometheus counters + gauges
│   └── health.rs                    # /healthz HTTP fallback
└── tests/
    ├── integration/                 # against anvil + mock subscribers
    └── unit/                        # per-module tests
```

---

## 5. Configuration

Env vars only.

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `CHAIN_ID` | yes | — | `84532` (Sepolia v1) or `8453` (mainnet B) |
| `RPC_URL` | yes | — | EVM RPC endpoint (Alchemy in v1) |
| `INDEXER_REGISTRY_ADDR` | yes | — | per-chain IndexerRegistry contract address |
| `CLUSTER_DIAMOND_FACTORY_ADDR` | yes | — | per-chain ClusterDiamondFactory address (used to discover which clusters exist) |
| `INDEXER_GRPC_ADDR` | no | `0.0.0.0:50051` | gRPC listen address |
| `HEALTH_HTTP_ADDR` | no | `0.0.0.0:9090` | HTTP /healthz + /metrics endpoint |
| `DSTACK_SOCKET` | no | `/var/run/dstack.sock` | dstack guest-agent socket |
| `STATE_DIR` | no | `/var/lib/attestmesh-indexer` | sled/rocksdb data dir (mounted as a persistent volume) |
| `BLOCK_POLL_INTERVAL_MS` | no | `2000` | how often to call `eth_blockNumber` |
| `BLOCK_BATCH_SIZE` | no | `200` | max blocks per `eth_getLogs` request when catching up |
| `LOG_LEVEL` | no | `info` | `tracing` filter |
| `LOG_FORMAT` | no | `json` | `json` or `pretty` |

No secrets. The dstack TEE seed is sufficient for all key material.

---

## 6. Identity and attestation

The Indexer's identity is derived from its TEE seed at boot, the same way sidecar identities are derived (master spec §6.2, §13 item 2).

| Purpose string | Algo | Used for |
|---|---|---|
| `attestmesh.indexer.signing.v1` | ed25519 | signing every push envelope. Pubkey is what IndexerRegistry's `pubKey` field points to. |
| `attestmesh.indexer.tls.v1` | ed25519 | TLS server certificate for the gRPC listener (milestone B; v1 terminates TLS at a load balancer). |

The signing key is stable across restarts (deterministic from the TEE seed). It is NOT sealed-stored — it's re-derived every boot from `dstack.derive_key`.

### 6.1 Attestation request

At boot, the Indexer requests a TEE quote whose user-data slot commits to its signing pubkey:

```
report_data = keccak256(abi.encode("attestmesh.indexer.v1", indexer_signing_pubkey))
```

The 32-byte hash fits in the 64-byte report_data slot with zero padding. This quote is what the Indexer attaches to its first push envelope per subscriber session — proving to the subscribing member that the Indexer's signing pubkey was actually derived inside the attested TEE code.

The quote is stored in memory after generation. On reconnects (from any subscriber), the same quote is reused. Quote regeneration happens on TEE restart (the attestation is bound to the current instance).

### 6.2 Self-check against IndexerRegistry

After deriving identity, the Indexer reads `IndexerRegistry.current()`. It checks:

- `current.codeId` matches its expected codeId (compile-time constant, derived from the OCI image hash).
- `current.pubKey` matches the just-derived signing pubkey.

Mismatch on either is logged at error level and the Indexer continues running but raises an alert metric. Mismatch means either (a) IndexerRegistry hasn't been updated to point at this new image yet (operator step), or (b) someone is running an old image against a new registry record (deployment bug).

The Indexer does **not** refuse to start on mismatch — refusing would defeat the purpose of having the registry update separately from the Indexer rollout. Members reading the registry will see the older pubkey until the org Safe rotates the record, at which point new subscribers verify against the new pubkey.

---

## 7. Architecture

Three concurrent loops, plus the gRPC listener.

### 7.1 Block-watcher loop

```
loop {
    let latest = provider.eth_blockNumber().await?;
    if latest > last_indexed {
        let logs = provider.eth_getLogs(filter_for_known_clusters(last_indexed + 1, latest)).await?;
        for log in logs {
            dispatch_to_per_cluster_queue(log).await;
        }
        last_indexed = latest;
    }
    sleep(BLOCK_POLL_INTERVAL_MS).await;
}
```

Filter shape: every log whose `address` is in the set of known cluster diamonds (populated lazily — see §7.2) AND whose `topics[0]` is in the allowlist of events we care about (`MemberRegistered`, `WgKeyPublished`, `MessageSent`, allowlist-mutation events, owner-transfer events).

`BLOCK_BATCH_SIZE` bounds a single `eth_getLogs` call so we don't blow past RPC provider limits. If `latest - last_indexed > BLOCK_BATCH_SIZE`, we paginate.

Catchup on startup: the Indexer reads `last_indexed` from persistent state, and the chain head from RPC, and pages forward `BLOCK_BATCH_SIZE` blocks at a time until caught up. Then it transitions to the steady-state poll.

### 7.2 Cluster-discovery loop

Runs every ~60 seconds (configurable):

```
loop {
    let known_clusters_onchain = provider.query_logs_for(
        ClusterDiamondFactory::ClusterDeployed_signature,
        last_factory_block,
        latest_block,
    ).await?;
    for log in known_clusters_onchain {
        known_clusters.insert(log.cluster_addr);
        // The watcher's filter will now include this cluster on the next poll.
    }
    last_factory_block = latest_block;
    sleep(60s).await;
}
```

The Indexer discovers clusters by listening to `ClusterDeployed` events on `ClusterDiamondFactory`. v1 uses one factory address (per master spec); a future multi-factory deployment can be supported by adding addresses to a set.

### 7.3 Per-cluster dispatch + push loop

Each log emitted from the block-watcher is routed to the cluster's local queue. A per-cluster task drains its queue and pushes to every subscribed member:

```
loop {
    let log = cluster.queue.recv().await;
    let push = build_envelope(log, cluster.address);   // signs with indexer key + adds repro stub
    for subscriber in cluster.subscribers() {
        if log.is_relevant_for(subscriber.member_id) {
            subscriber.send_with_backpressure(push.clone()).await;
            cursor.advance(cluster.address, subscriber.member_id, log.block_number, log.log_index);
        }
    }
}
```

"Relevant for this member" filters per master spec §6.2:

- `MessageSent` where `recipient_member_id == subscriber.member_id`: yes (this is the recipient).
- `MemberRegistered`, `WgKeyPublished`: yes (all cluster members care about who's in the cluster).
- `MemberRemoved`: **milestone B only.** On-chain eviction is not in v1 (master spec §11 — deferred indefinitely). The v1 indexer never emits this push because the underlying event is never emitted.
- Allowlist mutations / owner transfers: yes (for observability; sidecars may ignore but operators may consume via a separate subscription pattern in milestone B).
- `MessageSent` where `recipient_member_id != subscriber.member_id`: **no**. The Indexer enforces this so it never leaks the existence of a message to a non-recipient member. (The plaintext is sealed anyway, but suppressing the event metadata adds defense-in-depth.)

### 7.4 gRPC listener

Spawned at boot, accepts subscriptions. See §8.

---

## 8. Subscription protocol (gRPC server side)

### 8.1 `.proto` (shared with sidecar)

```proto
service Indexer {
  rpc Subscribe(stream SubscribeMessage) returns (stream PushEnvelope);
}

message SubscribeMessage {
  oneof inner {
    Hello hello = 1;
    Ack ack = 2;
  }
}

message Hello {
  bytes32 cluster_addr = 1;
  bytes32 member_id = 2;
  bytes attestation = 3;       // sidecar's attestation proof; unused by v1 indexer (see §8.3)
  uint64 from_block = 4;       // resume cursor; 0 means "from this member's MemberRegistered"
}

message Ack {
  uint64 block_number = 1;
  uint64 log_index = 2;
}

message PushEnvelope {
  bytes event_data = 1;        // RLP-encoded log topics + data (verifiable against the chain)
  bytes32 cluster_addr = 2;
  uint64 block_number = 3;
  bytes32 tx_hash = 4;
  uint64 log_index = 5;
  RpcReproStub rpc_repro = 6;
  bytes indexer_signature = 7; // Ed25519 over canonical-bytes(envelope minus this field minus indexer_attestation)
  IndexerAttestation indexer_attestation = 8; // present on the first push of a session only; omitted thereafter
}

message RpcReproStub {
  string method = 1;           // "eth_getLogs"
  string params_json = 2;      // exact params to reproduce
}

message IndexerAttestation {
  bytes quote = 1;             // TEE quote with report_data binding to indexer's signing pubkey
  bytes32 expected_code_id = 2;
  bytes32 expected_pubkey = 3; // == ed25519 signing key
}
```

### 8.2 Server-side flow

For each incoming `Subscribe(stream)`:

1. Read first message; require it to be `Hello`. Else close stream with `InvalidArgument`.
2. Validate `Hello`:
   - `cluster_addr` is in the discovered-clusters set. Else close with `NotFound`.
   - `member_id` exists in that cluster's MemberStorage. (Done via cached read of `AttestFacet.memberOf` indexed by the on-chain MemberRecord at the time of `MemberRegistered`.) Else close with `NotFound`.
   - `from_block` is `0` or `>= the member's MemberRegistered block` (no rewinding past their join). Else clamp to the floor and log at warn.
3. Determine the effective cursor:
   - If a persistent cursor exists for `(cluster_addr, member_id)`, use `max(persistent, hello.from_block)`.
   - Else use `member's MemberRegistered block` (master spec §6.4).
4. Register the subscription in the cluster's subscriber set.
5. Send `PushEnvelope`s for all events in `[cursor, latest_indexed]` that are relevant for this member, with `indexer_attestation` populated on the first envelope only.
6. Continue streaming new events as they're indexed.
7. Process inbound `Ack` messages to advance the persistent cursor.

### 8.3 Attestation verification on subscribe

v1 takes the subscribing member's claim at face value. The on-chain MemberStorage already records the result of attestor-facet verification; the Indexer trusts that the diamond did its job at registration time and that `memberId` existing in storage is sufficient proof of membership. The `Hello.attestation` field is reserved for future use — when milestone B adds member-eviction or member-key-rotation, the Indexer will need to re-verify on every subscribe to ensure it isn't streaming events to a recently-evicted member. That re-verification re-runs the member's registration check off-chain, dispatching to the attestor for the member's attestation method and confirming the attestation matches the recorded pubkeys per that method. v1 ignores the field.

### 8.4 Backpressure

Each subscription has a bounded mpsc channel (capacity 1024 push envelopes). On full channel, the per-cluster dispatch loop **does not block** — it logs at warn and drops the *oldest unsent* envelope from that subscriber's queue. The subscriber will see a gap in its event stream and can request catchup by reconnecting (which resumes from its persisted cursor + the gap is replayed).

Per master spec §6.1, the Indexer must not silently drop within its subscription window. The "oldest dropped + reconnect-replays" approach satisfies this — a slow subscriber gets explicit gaps in the live stream, but every event it has ever seen via `Ack` is durably tracked, and reconnect closes the gap. Operators monitor a `subscriber_dropped_events_total` metric.

### 8.5 Reconnects

A dropped TCP connection terminates the stream. The subscriber's persistent cursor is unaffected. On reconnect, the subscriber sends a fresh `Hello` with `from_block` = their last Ack'd block + 1 (or 0 to let the server decide). The server resumes from the persistent cursor (which is at the higher of the two values) and streams the gap before catching up to head.

---

## 9. Cursor management

### 9.1 Storage

Per-`(cluster_addr, member_id)` tuple, store a single 16-byte value: `(blockNumber: u64, logIndex: u64)` = the highest-Ack'd push.

Backend: sled v1; rocksdb if sled hits any production issue (sled is currently in "production-ready" but operator preference applies).

Key format: `b"cursor:" || cluster_addr || member_id`.

Value format: big-endian `blockNumber` (8 bytes) || big-endian `logIndex` (8 bytes).

### 9.2 Sealing

The cursor store contains no secrets. v1 stores it as plaintext on a persistent volume.

Milestone B (HA): if multiple Indexer replicas share the cursor store across instances, sealing won't help (the seal is per-TEE). At that point we need a different shape — a shared multi-key-encrypted store or a quorum protocol. v1 punts.

### 9.3 Ack durability

`Ack` does not flush to disk immediately on every message. Indexer batches writes every 100 ms or every 32 acks (whichever is sooner). A crash between ack and flush can re-deliver a few seconds of events on reconnect — subscriber must dedup off `(block_number, log_index)`. The sidecar's envelope handlers are already idempotent off the on-chain `envelopeId` so this is benign.

---

## 10. RPC repro stub generation

For every push, build an `RpcReproStub` that lets any member with an RPC endpoint reproduce the exact log:

```json
{
  "method": "eth_getLogs",
  "params": [{
    "address": "<cluster_addr>",
    "fromBlock": "<block_number_hex>",
    "toBlock": "<block_number_hex>",
    "topics": ["<topic0>", "<topic1>", "<topic2>", "<topic3>"]
  }]
}
```

The member runs this against its own trusted RPC, gets back a logs array, finds the one with matching `logIndex`, and confirms the bytes match `event_data`. Any mismatch is grounds to drop the Indexer subscription and re-discover via IndexerRegistry.

v1 sidecar does this opt-in only (master spec §11 was deferred to milestone B; sidecar default is to trust signature verification alone). The repro stub is generated regardless so the verification path is always available.

---

## 11. Signing every envelope

After constructing a `PushEnvelope` (with `indexer_signature` and `indexer_attestation` zeroed):

1. Serialize the envelope to canonical CBOR (same encoder as the sidecar's heartbeat — sidecar spec §17 item 2).
2. Compute `signing_input = keccak256("attestmesh.indexer.envelope.v1" || canonical_cbor_bytes)`.
3. Sign with the Indexer's Ed25519 key.
4. Set `indexer_signature = signature`.
5. Set `indexer_attestation` on the *first* envelope of each session, then `None` thereafter (the sidecar caches the attestation result for the duration of the stream).
6. Wrap in the tonic message and send.

The subscriber-side verification mirrors this: serialize the envelope with `indexer_signature = empty` and `indexer_attestation = empty`, recompute `signing_input`, verify the signature against the pubkey from IndexerRegistry.

---

## 12. Healthcheck

HTTP at `HEALTH_HTTP_ADDR`:

- `GET /healthz` — `200` if (chain head lag < 10 blocks) AND (gRPC listener is accepting) AND (RPC reachable). Else `503` with a JSON body explaining which check failed.
- `GET /metrics` — Prometheus exposition. Key metrics:
  - `indexer_chain_head_lag_blocks` (gauge)
  - `indexer_clusters_watched_total` (gauge)
  - `indexer_subscribers_total{cluster}` (gauge)
  - `indexer_events_pushed_total{cluster,event}` (counter)
  - `indexer_events_dropped_total{cluster,member}` (counter — see §8.4)
  - `indexer_acks_received_total{cluster,member}` (counter)
  - `indexer_rpc_errors_total{kind}` (counter)
  - `indexer_uptime_seconds` (gauge)

---

## 13. Failure modes

- **RPC down**: block-watcher loop retries with exponential backoff. `/healthz` flips to 503 after one missed poll. Existing subscriptions stay open (no new events to push); reconnecting subscribers can still verify membership cache, get the indexer attestation, and wait.
- **RPC behind** (chain reorg, provider lag): tolerated. Events from the original chain head are re-emitted when the new head exceeds the old. Subscribers dedup off `(block_number, log_index)`.
- **Reorgs ≤ shallow finality**: v1 treats Sepolia/Base confirmations as final. If a deep reorg removes events the Indexer already pushed, subscribers see the original push as Ack'd; the chain no longer reflects it. v1 logs at error but does not re-emit corrections. This is the same posture as master spec §13 item 4 — Sepolia is finality-on-confirmation for v1.
- **Out-of-disk on cursor store**: writes start failing. `/healthz` flips to 503. Operator must add disk capacity.
- **gRPC stream errors mid-session**: individual subscriptions terminate; subscribers reconnect; cursor is durable. No global impact.
- **Crash and restart**: state restored from sled. Subscribers reconnect after their stream-drop detection (couple seconds). Continuity gap of ~seconds, no event loss.

---

## 14. Tests (v1 scope)

### 14.1 Unit

- `chain::repro::build_stub` against fixture logs.
- `grpc::envelope::sign_and_verify` round-trip.
- `state::cursor` advance / persist / restore.
- `state::subscribers` add / remove / iteration under concurrent mutation.
- `chain::watcher::filter` against fixture logs.

### 14.2 Integration

`tests/integration/` runs the Indexer against an anvil chain. The harness:

1. Deploys the AttestMesh infra and a single cluster (using forge scripts compiled into the test fixture).
2. Boots the Indexer pointing at anvil's RPC.
3. Spins up three mock subscribers (in-process gRPC clients) presenting different `(cluster, member_id)` tuples.
4. Asserts:
   - All three subscribers receive `MemberRegistered` events for the other two.
   - A `MessageSent` event with recipient_member_id=A is received by A but not by B or C.
   - Reconnecting a subscriber with `from_block = 0` after disconnect replays from the persisted cursor (no duplicate Acks land).
   - Killing the Indexer process and restarting it preserves cursors; subscribers reconnect and resume.
   - A subscriber that doesn't Ack for 5s and then receives 2048 events sees the oldest 1024 dropped and the rest delivered with the `indexer_events_dropped_total` metric incremented.
5. Cleanup.

No mainnet-fork tests. No fuzz tests for v1.

---

## 15. Open questions

1. **Multi-cluster ordering across the chain.** v1's block-watcher pulls logs in chain order; per-cluster queues preserve that order. But the dispatch loop is per-cluster, so two events emitted in the same block by different clusters can arrive at their subscribers in either order. This doesn't matter today (each cluster's events are independent), but if the dog-fooded Indexer-cluster shape ever lands, we'd need cross-cluster ordering for the Indexer's own coordination events. v1 leaves cross-cluster ordering undefined.
2. **Sled vs rocksdb.** Sled is "production-ready" per its docs but has known edge cases under sustained heavy write. v1 picks sled for ergonomics; if write pressure becomes an issue in milestone B we switch. The DB is behind a small trait (`CursorStore`) so the swap is a single-file change.
3. **TLS for the gRPC listener.** v1 terminates TLS at a load balancer in front of the Indexer (the Indexer speaks h2c internally). Milestone B: the Indexer terminates TLS itself using its attestation-bound `attestmesh.indexer.tls.v1` keypair, and subscribing members pin the cert against the IndexerRegistry pubkey. v1's "trust the LB" posture is fine because the LB is in the same trust domain as the Indexer (AttestMesh-org-operated), but the milestone-B shape eliminates that intermediate trust.
4. **Subscription scaling.** v1 holds one open gRPC stream per subscribed member. For a cluster of 50 members spread across 10 clusters, that's 500 open streams. tonic + tokio handles this fine on a single instance. For ~10,000+ streams (milestone B at scale), we'd shard subscribers across Indexer instances by member_id hash. v1 stays single-instance.
