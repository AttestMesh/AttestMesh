# AttestMesh Indexer — Component Spec

**Status**: Implemented v1 + protocol-v3 exact-cursor delivery + Stage A shared-worker mode — protocol-v3/shared-pool rollout pending; see [`docs/deployment.md`](../deployment.md)
**Parent spec**: [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) (especially §6)
**Component**: `indexer/`
**Binary**: `attestmesh-indexer`
**Last updated**: 2026-07-14

---

## 1. Purpose

The Indexer is the off-chain event distribution layer. It watches every AttestMesh ClusterDiamond on its target chain (any diamond it has been asked to follow), and pushes events to the specific members of the cluster that emitted them — eliminating the need for each node sidecar to maintain its own chain RPC subscription. Master spec §6 defines *what* it does at the architectural level; this spec defines *what gets built*.

Each push is signed by the Indexer's registry-pinned key and paired with an `eth_getLogs`-style "repro stub" for diagnostics or a future sampling policy. Protocol-v2 and protocol-v3 sidecars verify the signature and trust the Indexer for event correctness, liveness, and completeness; they do not execute the repro stub or maintain a direct-log fallback. Protocol v3 adds a sidecar-owned, exact durable cursor so reconnecting through a different worker cannot inherit that worker's stale or advanced local cursor.

---

## 2. Toolchain

- **Rust edition 2021**, MSRV `1.78`.
- **Target**: `x86_64-unknown-linux-gnu` for normal builds, `x86_64-unknown-linux-musl` for the OCI image baked into the CVM.
- **Build**: `cargo build --release`; CI also runs `cargo clippy --all-targets -- -D warnings` and `cargo fmt --all -- --check`.

### 2.1 Key dependencies

| Crate | Purpose |
|---|---|
| `tokio` (full) | async runtime |
| `tonic` + `prost` | gRPC server (member subscription) |
| `alloy` (`alloy-primitives`, `alloy-provider`, `alloy-sol-types`, `alloy-rpc-types-eth`) | EVM RPC: `eth_getLogs`, `eth_blockNumber`, `eth_chainId`, ABI decoding |
| `ed25519-dalek` | signs every push envelope |
| `zeroize` | zero-on-drop key material |
| `serde_json` + `tokio::net::UnixStream` | dstack guest-agent UDS client (`/GetKey`, `/Info`, `/GetQuote` — dstack 0.5.x removed `/DeriveKey`) |
| `sled` | local persistent store for per-member cursors |
| `tracing` + `tracing-subscriber` (json) | structured logging |
| `prometheus` + `axum` | metrics endpoint (counters per cluster/per member; gauge of subscriber count + chain head lag) |

### 2.2 Build-time codegen

- gRPC definitions from the sidecar's canonical `proto/indexer.proto` and `proto/agent.proto`. The Indexer serves the former and uses the latter as a UDS client only in shared mode; `build.rs` compiles both in place so the wire formats cannot drift.
- Alloy generates the small read/event ABI surface from inline `sol!` declarations at compile time; the Indexer does not submit transactions.

---

## 3. Process model

One Indexer process runs inside each dstack CVM and serves **every workload cluster on the chain it watches** (live: Base mainnet `8453`). It remains chain-scoped infrastructure, never one Indexer per workload cluster. Two identity modes are implemented:

- `instance` (default): the deployed-compatible v1 shape. One worker uses its per-CVM signing key and the existing blue/green stable-LB cutover flow.
- `cluster-shared`: Stage A HA. Two or more homogeneous workers belong to a fresh, dedicated Indexer ClusterDiamond, derive one generation signing key from that cluster's CSK, and sit behind the unchanged stable LB endpoint. This code path is implemented but its production rollout is pending.

Stage A removes the worker single point of failure without changing `IndexerRegistry` v1 or requiring shared worker storage. The stable LB remains a single point of failure; eliminating or replicating that front door is deferred beyond Stage A.

Runtime requirements:

- Listens on TCP for inbound gRPC subscriptions from member sidecars (`INDEXER_GRPC_ADDR`, default `0.0.0.0:50051`). Externally TLS-terminated at a load balancer; the worker process itself speaks h2c.
- Holds open one EVM RPC connection per chain (HTTP/2 against `RPC_URL`; WebSocket fallback in milestone B).
- Checks its own `IndexerRegistry` v1 record against its signing pubkey and code ID. Instance mode retains the non-fatal sanity check. Shared mode keeps gRPC closed until both values match, monitors them while serving, and fails closed after sustained registry read errors.
- Persistent worker state: per-(clusterAddress, memberId) cursor (highest acknowledged `(blockNumber, logIndex)`), stored in local sled on the persistent volume; it contains no secrets and is not sealed. In protocol v3 this replica-local cursor is only a fallback: an explicit durable subscriber cursor is authoritative.

Process lifecycle:

- Instance mode: boot → derive the per-CVM key → load persistent cursors → catch up against chain head → open gRPC listener → enter steady state.
- Shared mode: boot → read the measured code ID from dstack `/Info` → fetch CSK and self facts from the co-located sidecar's Agent UDS → validate that member in the configured dedicated Indexer cluster → derive the shared key → start/warm HTTP and the read model → wait for the exact registry pubkey/code-ID pair → open gRPC. Registry mismatch or sustained read failure closes serving.
- Steady state runs three concurrent loops (described in §7).
- SIGTERM triggers graceful shutdown: stop accepting new subscriptions, finish flushing in-flight pushes, persist cursors, exit. Restart picks up exactly where it left off.

Restart policy: docker-compose `restart: unless-stopped`. A crashed worker brings down its gRPC listener, which subscribing sidecars notice via stream-drop. In instance mode they reconnect to the replacement selected by the existing cutover. In a shared pool they reconnect through the LB to any healthy worker and present their exact durable cursor. In-flight unacked pushes are replayed.

---

## 4. File layout

```
indexer/
├── Cargo.toml                       # single Rust crate
├── build.rs                         # compiles canonical sidecar protos
├── src/
│   ├── main.rs                      # entry, arg parsing, tokio runtime
│   ├── config.rs                    # env-var schema + load
│   ├── agent.rs                     # co-located sidecar Agent UDS client (shared mode)
│   ├── dstack.rs                    # dstack guest-agent UDS client + validation
│   ├── identity.rs                  # instance/shared key derivation + attestation request
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
│   ├── runtime.rs                    # catch-up, discovery, and steady-state loops
│   ├── query.rs                      # public HTTP read model
│   ├── state/
│   │   ├── mod.rs                   # in-memory + persistent state mgmt
│   │   ├── cursor.rs                # per-(cluster, member) cursor storage
│   │   └── subscribers.rs           # per-cluster subscriber set
│   ├── registry.rs                  # IndexerRegistry self-check + shared serving gate
│   ├── metrics.rs                   # prometheus counters + gauges
│   └── health.rs                    # /healthz, /status, metrics, and read-model HTTP
└── tests/
    ├── integration.rs               # ignored anvil/forge prototype harness
    └── protocol_v2.rs               # executable protocol-v2/v3 service tests
```

---

## 5. Configuration

Env vars only.

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `CHAIN_ID` | yes | — | `8453` (Base mainnet — the v1 deployment) |
| `RPC_URL` | yes | — | EVM RPC endpoint (Alchemy in v1) |
| `INDEXER_REGISTRY_ADDR` | yes | — | per-chain IndexerRegistry contract address |
| `CLUSTER_DIAMOND_FACTORY_ADDR` | yes | — | per-chain ClusterDiamondFactory address (used to discover which clusters exist) |
| `INDEXER_GRPC_ADDR` | no | `0.0.0.0:50051` | gRPC listen address |
| `HEALTH_HTTP_ADDR` | no | `0.0.0.0:9090` | HTTP /healthz + /metrics endpoint |
| `GATEWAY_DOMAIN` | no | empty | optional gateway domain used by the HTTP read model |
| `DSTACK_SOCKET` | no | `/var/run/dstack.sock` | dstack guest-agent socket |
| `STATE_DIR` | no | `/var/lib/attestmesh-indexer` | sled/rocksdb data dir (mounted as a persistent volume) |
| `BLOCK_POLL_INTERVAL_MS` | no | `2000` | how often to call `eth_blockNumber` |
| `BLOCK_BATCH_SIZE` | no | `200` | max blocks per `eth_getLogs` request when catching up (the live deployment runs `2000`) |
| `INDEXER_START_BLOCK` | no | `0` | floor for the boot catch-up scan — the factory's deploy block (no clusters can exist before it). `0` means genesis, which on a mainnet is effectively unbootable (~47M Base blocks of paged `eth_getLogs`); `deploy/indexer.sh` computes the floor via a `getCode` binary search (live value: `46868742`) |
| `INDEXER_IDENTITY` | no | `instance` | `instance` preserves the per-CVM v1 key; `cluster-shared` enables the Stage A shared-generation key and strict serving gate |
| `INDEXER_CLUSTER_ADDR` | shared mode | — | fresh, dedicated Indexer ClusterDiamond containing this worker; must be a nonzero address |
| `AGENT_GRPC_ADDR` | shared mode | — | absolute path (or `unix:/path`) for the co-located sidecar Agent UDS; TCP/HTTP addresses are rejected |
| `INDEXER_CODE_ID` | no | zero | legacy instance-mode expected code ID; shared mode ignores it and uses dstack `/Info.compose_hash` |
| `LOG_LEVEL` | no | `info` | `tracing` filter |
| `LOG_FORMAT` | no | `json` | `json` or `pretty` |

No secret is supplied through configuration. Instance mode derives its key from dstack. Shared mode receives the raw CSK only from the co-located sidecar over the node-local UDS and retains it only in zeroizing process memory.

---

## 6. Identity and attestation

Identity selection is explicit and instance mode remains the default for compatibility.

| Mode / purpose | Derivation | Used for |
|---|---|---|
| `instance` / `attestmesh.indexer.signing.v1` | Ed25519 seed derived from this CVM's dstack state | deployed-compatible per-worker envelope signing key |
| `cluster-shared` / `attestmesh.indexer.signing.v2` | `HKDF-SHA512(ikm=CSK, salt=none, info="attestmesh.indexer.signing.v2")` → 32-byte Ed25519 seed | one envelope-signing key shared by all members of one dedicated Indexer-cluster generation |
| `attestmesh.indexer.tls.v1` | per-CVM dstack derivation | reserved for future worker-side TLS; Stage A still terminates TLS at the stable LB |

Keys are not sealed-stored; they are re-derived at boot. A fresh dedicated cluster/CSK produces a different shared pubkey. Reusing a workload cluster CSK is forbidden because every holder of that CSK could sign as the Indexer generation.

### 6.1 Attestation request

At boot, the Indexer requests a TEE quote whose user-data slot commits to its signing pubkey:

```
report_data = keccak256(abi.encode("attestmesh.indexer.v1", indexer_signing_pubkey))
```

The 32-byte hash fits in the 64-byte report_data slot with zero padding. The Indexer attaches the quote to its first push envelope per subscriber session as diagnostic attestation material committing to the signing pubkey. The current sidecar verifies the registry-pinned envelope signature; it does not use this quote as challenge-bound per-replica authentication.

The quote is stored in memory after generation. On reconnects (from any subscriber), the same quote is reused. Quote regeneration happens on TEE restart.

In `cluster-shared` mode, each replica can produce a quote committing to the same shared pubkey, but that quote is **diagnostic only**. The current quote/signature protocol has no subscriber challenge and does not bind a verifiable instance key, cluster, or member ID. It proves neither which physical replica served the stream nor that the advisory `servingMemberId` was the signer. Stage A authorization is the dedicated cluster policy plus the LB and registry-pinned shared signature, not per-replica cryptographic authentication.

### 6.2 Self-check against IndexerRegistry

After deriving identity, the Indexer reads the unchanged v1 `IndexerRegistry.current()` tuple. It checks:

- `current.codeId` matches its expected code ID. Instance mode preserves the existing operator-supplied image value. Shared mode obtains the actual nonzero 32-byte compose hash from dstack `/Info.compose_hash` and never trusts a blank `INDEXER_CODE_ID`.
- `current.pubKey` matches the just-derived signing pubkey.

Instance mode preserves v1 behavior: mismatch is logged but does not prevent serving, because registry rotation remains separate from a legacy worker rollout.

Shared mode is strict. Before deriving the key it also requires `GetSelf.member_contract == /Info.app_id` and confirms that the configured `INDEXER_CLUSTER_ADDR` reports the same active `memberId`. The candidate may expose `/status` and warm its read model, but it does not bind gRPC until **both** registry values match. Once serving, a mismatch closes the listener and existing streams; transient registry errors consume a bounded retry budget and sustained errors fail closed. The v1 registry has no cluster-pointer field, so `/status` and LB admission carry the configured cluster/member metadata without changing the deployed ABI.

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

Catchup on startup: the Indexer reads `last_indexed` from persistent state, floors it at `INDEXER_START_BLOCK` (the factory deploy block — scanning from genesis is unbootable on a mainnet), runs cluster discovery over the factory's `ClusterDeployed` logs in 10k-block chunks, and pages the watcher forward `BLOCK_BATCH_SIZE` blocks at a time until caught up (live: 23s from the factory deploy block). Then it transitions to the steady-state poll.

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
    let batch = watcher.next_indexed_batch().await;
    for log in batch.logs {
        let push = build_envelope(log, cluster.address); // signed + repro stub
        for subscriber in cluster.subscribers() {
            if log.is_relevant_for(subscriber.member_id) {
                subscriber.send_live_or_close(push.clone()).await;
            }
        }
    }
    for protocol_v2_subscriber in cluster.subscribers() {
        protocol_v2_subscriber.send_signed_checkpoint(batch.indexed_through).await;
    }
}
```

The send path never advances a cursor. Only the inbound Ack handler does so, after the sidecar reports handler completion; a crash before Ack therefore causes intentional at-least-once replay.

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
  bytes cluster_addr = 1;       // 20-byte ClusterDiamond address
  bytes member_id = 2;          // 32-byte memberId
  bytes attestation = 3;       // sidecar's attestation proof; unused by v1 indexer (see §8.3)
  uint64 from_block = 4;       // boundary-block fallback for pre-v3 Indexers
  uint32 protocol_version = 5; // 2 = checkpoints; 3 = exact subscriber cursor
  DeliveryCursor resume_cursor = 6; // present for an exact, durable v3 position
}

message DeliveryCursor {
  uint64 block_number = 1;
  uint64 log_index = 2;
}

message Ack {
  uint64 block_number = 1;
  uint64 log_index = 2;
}

message PushEnvelope {
  bytes event_data = 1;        // RLP-encoded log topics + data (verifiable against the chain)
  bytes cluster_addr = 2;      // 20-byte ClusterDiamond address
  uint64 block_number = 3;
  bytes tx_hash = 4;           // 32 bytes for events; empty for checkpoints
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
  bytes expected_code_id = 2;  // 32 bytes
  bytes expected_pubkey = 3;   // 32-byte Ed25519 verification key
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
   - Protocol v3 accepts `resume_cursor` only when `protocol_version >= 3`. Presence distinguishes no cursor from the valid position `(0, 0)`.
   - An explicit subscriber cursor is authoritative even if this worker's replica-local Ack cursor is further ahead. A normal event cursor replays only logs with a strictly greater `(blockNumber, logIndex)`, including the remaining logs in its boundary block. A checkpoint cursor `(block, uint64::MAX)` starts at `block + 1`.
   - Without an explicit cursor, retain the existing replica-local/full-tuple behavior. For a protocol-v2 subscriber with no cursor and `from_block = 0`, durably initialize at `latest_indexed` instead of replaying legacy pre-upgrade history; legacy clients retain the MemberRegistered floor.
4. Register the subscription behind its per-session ordering gate.
5. Page and send relevant events after the cursor, then send a signed checkpoint through `latest_indexed`. Checkpoints use empty event/tx/repro fields and `(blockNumber, logIndex) = (indexedThrough, uint64::MAX)`.
6. Continue streaming new events as they're indexed.
7. Process inbound `Ack` messages to advance the persistent cursor.

### 8.3 Attestation verification on subscribe

v1 takes the subscribing member's claim at face value. The on-chain MemberStorage already records the result of attestor-facet verification; the Indexer trusts that the diamond did its job at registration time and that `memberId` existing in storage is sufficient proof of membership. The `Hello.attestation` field is reserved for future use — when milestone B adds member-eviction or member-key-rotation, the Indexer will need to re-verify on every subscribe to ensure it isn't streaming events to a recently-evicted member. That re-verification re-runs the member's registration check off-chain, dispatching to the attestor for the member's attestation method and confirming the attestation matches the recorded pubkeys per that method. v1 ignores the field.

### 8.4 Backpressure

Each subscription has a bounded channel (capacity 1024). Catch-up applies backpressure and is serialized before live delivery. A full live channel emits `ResourceExhausted` and closes the stream; no subsequent checkpoint can pass the gap, so reconnect resumes from the last durable Ack. The Indexer never silently drops an envelope while continuing the stream.

### 8.5 Reconnects

A dropped TCP connection terminates the stream. A protocol-v3 sidecar serially completes its handler, atomically persists the exact `(blockNumber, logIndex)` position, updates in-memory progress, and only then sends the Ack. If persistence fails, it tears down the stream without Ack so the position is replayed. On reconnect it sends that exact position in `resume_cursor`; `from_block` remains populated with the same boundary block as an additive fallback for a pre-v3 Indexer.

The explicit subscriber cursor is the cross-replica source of truth. This prevents a newly selected worker's missing, stale, or further-ahead local cursor from creating a gap. At-least-once delivery still permits safe duplicate replay, which the sidecar validates before suppressing redispatch. A genuinely fresh sidecar omits `resume_cursor`; protocol-v2/fallback head-initialization behavior remains unchanged. Catch-up ends with a signed checkpoint Ack before live delivery continues.

### 8.6 Stable load-balancer endpoint

Production registers the stable `indexer-lb` gRPC gateway endpoint, not a worker's direct endpoint. The existing single-instance blue/green path remains supported and is the deployed-compatible default.

Stage A adds a bounded shared-identity worker pool behind that same endpoint. Pool candidates have independent local cursor stores but must report the same nonzero shared pubkey, measured code ID, and dedicated Indexer-cluster address, with distinct serving member IDs. Candidates can prewarm over HTTP while their gRPC listener remains registry-gated. During cutover, the unchanged v1 registry remains pointed at the stable endpoint and is updated to the shared generation's pubkey/code ID before the pool accepts trusted subscriptions. Existing streams are closed so sidecars rediscover the shared key and reconnect with their protocol-v3 exact cursor.

Protocol-v3 sidecars must be rolled out before enabling a shared pool; protocol v2 remains compatible with one worker but cannot make its cursor authoritative over a different worker's local state. The shared-pool code and controller are implemented, but production rollout is pending. Stage A provides worker failover only: HAProxy/the stable LB is still a front-door single point of failure. See the [Indexer LB runbook](../../deploy/indexer-lb-runbook.md).

---

## 9. Cursor management

### 9.1 Storage

Per-`(cluster_addr, member_id)` tuple, each Indexer worker stores a single 16-byte value: `(blockNumber: u64, logIndex: u64)` = the highest-Ack'd push observed by that replica.

Backend: sled v1; rocksdb if sled hits any production issue (sled is currently in "production-ready" but operator preference applies).

Key format: `b"cursor:" || cluster_addr || member_id`.

Value format: big-endian `blockNumber` (8 bytes) || big-endian `logIndex` (8 bytes).

### 9.2 Replica independence and sealing

The worker cursor store contains no secrets and remains plaintext on that worker's persistent volume. Stage A does **not** share this database between replicas. Protocol v3 instead makes the sidecar's exact durable cursor authoritative whenever it is present, so failover is independent of the selected worker's local cursor state.

This design preserves instance mode and protocol-v2 behavior while avoiding a shared-store migration. A future design may centralize cursor state for operational reasons, but it is not required for Stage A correctness.

### 9.3 Ack durability

Event Acks advance monotonically; a crash before Ack causes re-delivery (at-least-once semantics). Checkpoint Acks flush immediately because they certify the complete ordered prefix through a block, including empty blocks. In protocol v3 the sidecar makes the handled cursor durable before Ack; failure to persist terminates the stream without Ack. The sidecar suppresses repeated positions within one stream, while a reconnect may deliberately invoke the handler again; applications retain request-level idempotency across those deliveries.

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

A diagnostic tool or future sampling policy can run this against a trusted RPC, find the matching `logIndex`, and compare the bytes with `event_data`. Protocol-v2 sidecars retain the signed stub but do not execute it; their default and only current correctness check is the registry-pinned Indexer signature.

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
- `GET /status` — machine-readable identity/admission and read-model progress. Its stable top-level admission fields are `codeId`, `identityMode`, `indexerCluster`, and `servingMemberId`; it also exposes the signing pubkey and `health.grpcAccepting`. A shared candidate can therefore report its measured identity and catch-up progress while correctly remaining unhealthy for serving until the registry gate opens.
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
- **Reorgs ≤ shallow finality**: v1 treats Base confirmations as final. If a deep reorg removes events the Indexer already pushed, subscribers see the original push as Ack'd; the chain no longer reflects it. v1 logs at error but does not re-emit corrections. This is the same posture as master spec §13 item 4 — Base is finality-on-confirmation for v1.
- **Out-of-disk on cursor store**: writes start failing. `/healthz` flips to 503. Operator must add disk capacity.
- **gRPC stream errors mid-session**: individual subscriptions terminate; subscribers reconnect; cursor is durable. No global impact.
- **Crash and restart**: state restored from sled. Subscribers reconnect after their stream-drop detection (couple seconds). Continuity gap of ~seconds, no event loss.
- **One shared worker dies**: HAProxy stops selecting it. Affected protocol-v3 sidecars reconnect to another worker and present their exact durable cursor; no worker-local cursor transfer is needed.
- **Shared registry pubkey/code ID does not match**: the candidate may continue warming and exposing diagnostics, but gRPC remains closed. A mismatch after admission terminates existing streams and closes serving.
- **Sustained shared-mode registry read failure**: bounded retries are exhausted and the worker fails closed. A transient read error does not immediately evict a healthy worker.
- **Stable LB dies**: all Stage A workers may remain healthy, but the registered public endpoint is unavailable. The stable LB remains the Stage A single point of failure.

---

## 14. Tests

### 14.1 Unit

- `chain::repro::build_stub` against fixture logs.
- `grpc::envelope::sign_and_verify` round-trip.
- Signed checkpoint construction and tamper rejection.
- Full-tuple same-block replay filtering and empty-cursor protocol-version selection.
- Protocol-v3 cursor presence, exact boundary-block replay, checkpoint-to-next-block behavior, and subscriber-authoritative selection over a further-ahead replica cursor.
- Shared-identity HKDF stability/rotation, Agent UDS CSK gating, strict dstack `/Info` widths, shared registry admission, and `/status` metadata serialization.
- `state::cursor` advance / persist / restore.
- `state::subscribers` add / remove / iteration, replay-before-live ordering, and overflow termination.
- `chain::watcher::filter` against fixture logs.

### 14.2 Protocol integration

Executable cross-component protocol coverage is split between the two Rust crates:

- `indexer/tests/protocol_v2.rs` (the filename is retained for compatibility) drives the real tonic service and covers protocol-v2 empty-cursor initialization/checkpoints plus protocol-v3 explicit-cursor replay and no-Ack behavior.
- `sidecar/tests/indexer_delivery.rs` uses a fake gRPC Indexer to verify that handler completion and durable cursor storage precede Ack, a process restart reloads and transmits the exact cursor, `from_block` remains an older-server fallback, and already-handled positions are not redispatched.

The anvil/forge `indexer/tests/integration.rs` harness remains explicitly ignored and out of scope for the current prototype. There are no Indexer mainnet-fork or fuzz tests.

---

## 15. Open questions

1. **Multi-cluster ordering across the chain.** The block-watcher pulls logs in chain order; per-cluster queues preserve that order. But the dispatch loop is per-cluster, so two events emitted in the same block by different clusters can arrive at their subscribers in either order. Stage A's dedicated Indexer cluster supplies membership and a CSK; it does not make cross-cluster delivery order meaningful. Cross-cluster ordering remains undefined.
2. **Sled vs rocksdb.** Sled is "production-ready" per its docs but has known edge cases under sustained heavy write. v1 picks sled for ergonomics; if write pressure becomes an issue in milestone B we switch. The DB is behind a small trait (`CursorStore`) so the swap is a single-file change.
3. **TLS for the gRPC listener.** v1 terminates TLS at a load balancer in front of the Indexer (the Indexer speaks h2c internally). Milestone B: the Indexer terminates TLS itself using its attestation-bound `attestmesh.indexer.tls.v1` keypair, and subscribing members pin the cert against the IndexerRegistry pubkey. v1's "trust the LB" posture is fine because the LB is in the same trust domain as the Indexer (AttestMesh-org-operated), but the milestone-B shape eliminates that intermediate trust.
4. **Subscription scaling.** Each worker holds one open gRPC stream per sidecar routed to it. Stage A lets HAProxy distribute reconnects across a bounded homogeneous pool, but it does not define deterministic member sharding or remove the stable-LB SPOF. At ~10,000+ streams, deliberate sharding and redundant front doors need a later design.
