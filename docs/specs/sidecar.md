# TeeMesh Sidecar — Component Spec

**Status**: Draft v0.1
**Parent spec**: [`teemesh-coordination-layer.md`](./teemesh-coordination-layer.md) (especially §7, §8)
**Component**: `sidecar/`
**Binary**: `cluster-mesh-agent`
**Last updated**: 2026-05-30

---

## 1. Purpose

The sidecar is the per-CVM process that turns "a CVM running in dstack" into "a TeeMesh cluster member." It owns key derivation, on-chain registration, Indexer subscription, wireguard setup, heartbeats, the first-convergence gate, the CSK lifecycle, and the gRPC façade the application container talks to. Master spec §7 defines *what* it does; this spec defines *what gets built*.

---

## 2. Toolchain

- **Rust edition 2021**, MSRV `1.78` (revisit when `tonic`/`tokio` need newer).
- **Target**: `x86_64-unknown-linux-gnu`, statically linkable against musl for the OCI image (`x86_64-unknown-linux-musl` variant for distroless final image).
- **Build**: `cargo build --release`; CI also runs `cargo clippy -- -D warnings` and `cargo fmt --check`.

### 2.1 Key dependencies

| Crate | Purpose |
|---|---|
| `tokio` (full) | async runtime |
| `tonic` + `prost` | gRPC client (Indexer) + server (app facade) |
| `alloy` (`alloy-primitives`, `alloy-provider`, `alloy-signer`, `alloy-sol-types`) | EVM RPC, signing, ABI |
| `dalek-cryptography` family (`x25519-dalek`, `ed25519-dalek`) | Curve25519 ops |
| `crypto_box` | NaCl sealed-box (x25519 + XSalsa20-Poly1305) |
| `zeroize` | zero-on-drop key material |
| `defguard_wireguard_rs` | userspace wireguard control via `WG_QUICK`-equivalent netlink |
| `dstack-sdk` (vendored if not crates.io-published) | dstack runtime client (`derive_key`, `seal`, `get_quote`) |
| `tracing` + `tracing-subscriber` (json output) | structured logging |
| `prometheus` + `axum` | metrics endpoint (milestone B; v1 wires the crate in but exposes minimal counters) |

### 2.2 Build-time codegen

- gRPC `.proto` for the Indexer subscription (`proto/indexer.proto`) — shared with the indexer component.
- gRPC `.proto` for the app facade (`proto/agent.proto`) — owned by this component.
- Solidity ABI bindings for AttestFacet / DstackFacet / NetworkFacet / MessageFacet / IndexerRegistry via `alloy-sol-types` / `sol!` macro from the JSON ABIs emitted by `forge build` in `contracts/`.

---

## 3. Process model

One process per CVM. Runs as a docker-compose service named `mesh-agent`. The application container declares `depends_on: { mesh-agent: { condition: service_healthy } }` so it does not start until the sidecar reports converged + CSK-acquired.

Capabilities:
- `NET_ADMIN` — wireguard interface management.
- `SYS_ADMIN` — only if the dstack `seal`/`get_quote` syscalls require it; verified at build-time, dropped otherwise.

No `--privileged`. The sidecar mounts:
- A unix domain socket directory shared with the app container (for the app facade gRPC).
- The dstack guest-agent socket (`/var/run/dstack.sock` or whatever the dstack runtime exposes).

Restart policy: `unless-stopped`. The sidecar crashing post-convergence brings down its healthcheck; existing wireguard peers remain reachable to the application until the sidecar comes back, but the app cannot send messages or learn about new members during the gap.

---

## 4. File layout

```
sidecar/
├── Cargo.toml                       # workspace root (single crate for v1)
├── proto/
│   ├── indexer.proto                # shared with indexer/; v1 lives here, indexer/ imports
│   └── agent.proto                  # owned here; defines the app facade
├── build.rs                         # tonic-build for the two protos + alloy sol! bindings
├── src/
│   ├── main.rs                      # entry, arg parsing, tokio runtime spawn
│   ├── config.rs                    # env-var schema + load
│   ├── dstack.rs                    # dstack runtime client wrappers (derive, seal, get_quote)
│   ├── keys.rs                      # all key derivation + zeroize wrappers
│   ├── chain/
│   │   ├── mod.rs                   # provider + signer setup
│   │   ├── registry.rs              # IndexerRegistry read
│   │   ├── attest.rs                # AttestFacet view + register selectors
│   │   ├── dstack_facet.rs          # dstack_register builder + tx submit
│   │   ├── network_facet.rs         # publishWgKey
│   │   └── message_facet.rs         # send envelope builder
│   ├── indexer_client.rs            # gRPC client + reconnect logic
│   ├── wg/
│   │   ├── mod.rs                   # netlink + interface lifecycle
│   │   ├── cidr.rs                  # the §7.3 IP-derivation
│   │   └── peer.rs                  # peer add/remove/update
│   ├── heartbeat/
│   │   ├── mod.rs                   # send/receive loops
│   │   ├── packet.rs                # wire format + ed25519 sign/verify
│   │   └── liveness.rs              # the rolling view + convergence calc
│   ├── csk.rs                       # origination, onboarding receive/send, sealed storage
│   ├── envelopes.rs                 # PeerEndpoint + CskOnboardingV1 (de)serialize + sealed-box
│   ├── state/
│   │   ├── mod.rs                   # the bring-up state machine
│   │   └── gates.rs                 # first-convergence + CSK-acquired gating
│   ├── agent_grpc.rs                # app-facing tonic server
│   └── health.rs                    # healthcheck endpoints (gRPC + HTTP fallback)
└── tests/
    ├── integration/                 # end-to-end with mock dstack + mock indexer
    └── unit/                        # per-module tests
```

---

## 5. Configuration

All configuration is via environment variables (no config files). The sidecar fails fast on missing required vars.

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `MEMBER_CONTRACT` | yes | — | hex address of this CVM's ClusterMember proxy |
| `CHAIN_ID` | yes | — | EVM chain id (`84532` for Base Sepolia v1, `8453` for Base mainnet) |
| `RPC_URL` | yes | — | EVM RPC endpoint URL |
| `INDEXER_REGISTRY_ADDR` | yes | — | hex address of the per-chain IndexerRegistry. (Hardcoded per chain id in v1 sidecar binary; env var allows overriding for tests.) |
| `DSTACK_SOCKET` | no | `/var/run/dstack.sock` | path to dstack guest-agent socket |
| `AGENT_GRPC_SOCKET` | no | `/var/run/teemesh/agent.sock` | path the app facade listens on |
| `HEALTH_HTTP_ADDR` | no | `127.0.0.1:9090` | HTTP /healthz endpoint (for docker-compose healthcheck) |
| `LOG_FORMAT` | no | `json` | `json` or `pretty` |
| `LOG_LEVEL` | no | `info` | standard `tracing` filter |

No secrets in env vars. All key material is derived from the dstack TEE seed at runtime.

---

## 6. Key derivation

All key material is derived via `dstack.derive_key(purpose, algo)` and never persisted in plaintext outside the sidecar's process memory + the dstack sealed store.

| Purpose string | Algo | Used for |
|---|---|---|
| `teemesh.identity.v1` | curve25519 | Curve25519 root seed. x25519 (encryption) and Ed25519 (signing) are derived from this via standard ed25519 → curve25519 conversion (`x25519-dalek::PublicKey::from(&ed25519_sk)`) so they share one stored secret. |
| `teemesh.wireguard.v1` | curve25519 | wireguard private key (Curve25519 scalar) |
| `teemesh.binding.v1` | k256 | secp256k1 derived key used for the *one-shot* `dstack_register` binding signature (master spec §4.2). Used once, never again. |
| `teemesh.cluster-shared.v1` (purpose) / `csk-v1` (version) | aes-256 raw bytes | Cluster Shared Key. **Only derived by the originator.** Onboardees never call this. |

All keys are wrapped in `zeroize::Zeroizing` containers and zeroed on drop. Cargo-deny is configured to reject any dependency that prints derived key material.

**Identity-key public values** — exposed to the rest of the sidecar as:

- `x_pub: [u8; 32]` (x25519 public)
- `ed25519_pub: [u8; 32]` (Ed25519 public)
- `wg_pub: [u8; 32]` (wireguard public)

These are computed at boot, logged at info as a sanity check, and stored in the `KeyMaterial` struct passed to every subsystem.

---

## 7. Boot state machine

Implemented in `src/state/mod.rs` as an explicit enum + event-handler loop. Every transition is logged. Every gate is observable from the healthcheck.

```
States:
  Booting → DeriveKeys → ReadMember → (Restart | FreshRegister | LostState)
  FreshRegister → DetermineCskRole → (Originator | Onboardee)
  Restart → ReuseMember → SkipToPublishWg
  LostState → Exit(non-zero)
  Originator → DeriveCsk → SealCsk → PublishWg
  Onboardee → SubscribeIndexer (parallel with PublishWg)
  PublishWg → SubscribeIndexer (if not already)
  SubscribeIndexer → WaitPeerEndpoints (and, if Onboardee, WaitCskEnvelope in parallel)
  WaitPeerEndpoints + WaitCskEnvelope (gated) → ConfigureWireguard → StartHeartbeat
  StartHeartbeat → ComputeConvergence → FirstConverged
  FirstConverged + CskAcquired → Healthy
  Healthy → SteadyState (handles new joiners, peer drops, indexer reconnects)
```

### 7.1 Step-by-step

The 12 numbered steps from master spec §7.1 map onto modules as follows. This is the canonical ordering; modules run on tokio tasks and the state machine ticks on events.

| Master step | Module | Notes |
|---|---|---|
| 1 Discover cluster | `chain::attest::cluster_of` | reads `member.cluster()` via RPC |
| 2 Derive keys | `keys::derive_all` | + `csk` originator path uses `csk::derive` later |
| 3 Construct proof | `dstack::request_kms_chain` + sign binding | builds `DstackProof` (contracts spec §6.3) |
| 4 Register or recognize | `state::check_existing_member` | branches on `AttestFacet.memberOf` |
| 4a Determine CSK role | `state::determine_role` | reads `AttestFacet.memberCount()` post-register |
| 5 Publish wireguard | `chain::network_facet::publish` | one tx; idempotent (no-op if value unchanged) |
| 6 Subscribe to Indexer | `indexer_client::connect` | reads `IndexerRegistry` → opens gRPC stream |
| 7 Wait peer endpoints | `envelopes::handle_message_sent` | decrypt sealed-box, parse, hand to wg + heartbeat |
| 7' Wait CSK envelope (onboardee) | `csk::wait_for_onboarding` | parallel with 7 |
| 8 Send own endpoint | `envelopes::send_peer_endpoint` | per discovered peer |
| 9 Heartbeat | `heartbeat::send_loop` + `heartbeat::recv_loop` | 2s interval, 3-miss threshold |
| 10 Convergence gate | `state::gates::first_converged` | fires exactly once |
| 11 Become healthy | `health::set_ready` | dual-gate: first-converged AND CSK-acquired |
| 12 Steady-state | `state::steady_state` | handles new-member onboarding (with [0,500]ms backoff), peer drops, indexer reconnects |

### 7.2 State transitions are observable

Every state change emits a `tracing::info!` event with a `phase` field (`booting`, `registering`, `subscribing`, `waiting-peers`, `waiting-csk`, `wg-configuring`, `heartbeating`, `converging`, `healthy`). The healthcheck (§14) exposes the current phase so operators can diagnose where a boot stuck.

---

## 8. Chain interaction

### 8.1 Provider + signer

- Provider: `alloy-provider` with HTTP transport against `RPC_URL`. WebSocket fallback in milestone B.
- Signer: an `alloy-signer-local::PrivateKeySigner` constructed from the `teemesh.binding.v1` k256 derived key — but **only** for the one `dstack_register` tx. After registration, the sidecar's *runtime* txes (publishWgKey, send) are sent by a separate alloy signer built from a *short-lived* k256 key the sidecar derives at boot from `teemesh.tx-sender.v1` and uses for its lifetime. This key has no chain-level authority; it just pays gas. The contracts gate on `AttestFacet.isClusterMember(msg.sender)` where `msg.sender` is the member's ClusterMember proxy address, not the sender of the meta-tx; for v1 we send directly from the member's binding key and rely on the gas-payer-equals-binding-key shape. (Account abstraction for runtime txes is milestone B.)

Wait — let me restate that cleanly: **v1 sends every tx from the same `teemesh.binding.v1`-derived secp256k1 key.** That is the only k256 the sidecar holds. It is used both for the one-shot registration binding sig (recovered on chain) and for paying gas on subsequent txes. The contracts' `isClusterMember` check resolves `msg.sender` against `MemberStorage.memberIdOf[msg.sender]`; the binding-derived address is one of the dstack-attested addresses for this CVM, so the check passes. Funding that address with a small amount of testnet ETH is an operator step (logged as an open question in §17 — *funding model for the binding-derived sender key*).

### 8.2 Tx submission

- Gas estimation: `eth_estimateGas` + 20% headroom for v1.
- Nonce: in-memory tracking against `eth_getTransactionCount(pending)` at boot; resync on revert.
- Retry policy: on `nonce too low` or `replacement transaction underpriced`, bump priority fee 1.25× and retry once. On other reverts, surface to the state machine — registration revert is fatal (state goes to `Exit(non-zero)`); a later `publishWgKey` revert is logged at warn and retried once.

### 8.3 Reads

- All chain reads go through the Indexer once subscribed. The only direct RPC reads from the sidecar are at startup: `member.cluster()`, `IndexerRegistry.current()`, `AttestFacet.memberOf(memberAddr)`, `AttestFacet.memberCount()`.

---

## 9. Indexer subscription

### 9.1 `.proto`

```proto
service Indexer {
  rpc Subscribe(stream SubscribeMessage) returns (stream PushEnvelope);
}

message SubscribeMessage {
  oneof inner {
    Hello hello = 1;        // initial handshake
    Ack ack = 2;            // delivery acknowledgement
  }
}

message Hello {
  bytes32 cluster_addr = 1;
  bytes32 member_id = 2;
  bytes attestation = 3;    // proof against AttestFacet.memberOf
  uint64 from_block = 4;    // resume cursor; 0 means "from this member's MemberRegistered"
}

message Ack {
  uint64 block_number = 1;
  uint64 log_index = 2;
}

message PushEnvelope {
  EventBlob event = 1;            // ABI-encoded event topics + data
  bytes32 cluster_addr = 2;
  uint64 block_number = 3;
  bytes32 tx_hash = 4;
  uint64 log_index = 5;
  RpcReproStub rpc_repro = 6;     // contract addr + block range + topic filter for independent verify
  bytes indexer_signature = 7;    // Ed25519 over keccak256(envelope-without-signature)
  IndexerAttestation indexer_attestation = 8;  // TEE quote proving the Indexer's identity
}
```

(Lives in `proto/indexer.proto`; this is the v1 shape — extend in milestone B.)

### 9.2 Client behavior

- Read `IndexerRegistry.current()` at boot. Cache `indexerPubKey` and `endpoint`.
- Open a single bidi stream. Send `Hello`. Verify the first `PushEnvelope`'s `indexer_attestation` matches the registry's `codeId`. Verify `indexer_signature` against the registry's `pubKey`.
- On every push: verify signature, decode event, dispatch to the appropriate handler (`MessageSent` → envelopes; `MemberRegistered` → state machine + maybe-onboard; `WgKeyPublished` → wg peer-key refresh; `MemberRemoved` (milestone B) → wg peer drop).
- On every successfully-handled push: send `Ack(blockNumber, logIndex)` so the Indexer can advance its cursor for this subscriber.
- On stream drop: exponential backoff `min(2^n s, 30s)` reconnect; resume from last-acked cursor. Re-read `IndexerRegistry.current()` on each retry in case the Indexer pubkey rotated.
- Sampling spot-check: every Nth push (configurable, default disabled in v1, off-by-design opt-in), execute `rpc_repro` against `RPC_URL` and compare. Mismatch → log loudly, do not abort.

---

## 10. Wireguard management

### 10.1 Interface lifecycle

- Interface name: `teemesh0` (single mesh per CVM in v1).
- Created at boot via netlink (`defguard_wireguard_rs`).
- Listen port: random ephemeral, exposed in PeerEndpoint.
- Self IP: derived per §7.3 from own `memberId` + cluster CIDR.
- MTU: 1420 (standard wg overhead on 1500 underlay).

### 10.2 Peer add/update

On receiving a `PeerEndpoint` for peer `p`:
1. Compute `p`'s mesh IP from §7.3.
2. Configure wg peer with `(public_key = p.wgPublicKey, endpoint = "{p.host}:{p.port}", allowed_ips = [p_mesh_ip/32], persistent_keepalive = 25)`.
3. Mark `p` as known; emit a `PeerEvent::Up` on the app-facing stream.

If a peer's `PeerEndpoint` is re-received with changed values (different host/port — milestone B migration scenario), update the existing wg peer in place. v1 does not exercise this path.

### 10.3 Peer drop

v1 has no on-chain eviction → no peer drop. Heartbeat-based liveness marks a peer "down" in the local view (§11) but does *not* tear down the wg peer config. The application sees this only via `PeerEvent::Liveness` on the app stream.

---

## 11. Heartbeat protocol

### 11.1 Wire format

UDP packet over the wg interface, port `51820` (cluster-wide convention; configurable in milestone B).

```
struct Heartbeat {
  uint8  version;            // = 1
  uint64 sender_member_id_hi; // first 8 bytes of memberId
  uint64 sender_member_id_lo;
  uint32 sender_member_id_rest; // bytes 16..20 (memberId is 32 bytes; truncated for wire)
  // ... actually: send full 32-byte memberId
  bytes32 sender_member_id;
  uint64 timestamp_ms;       // monotonic clock since unix epoch in CVM
  uint16 connected_count;
  bytes32[] connected_member_ids;   // sender's current connected-set view
  bytes64 ed25519_signature;        // over keccak256(version || sender_member_id || timestamp_ms || connected_member_ids)
}
```

(Concrete encoding: `serde_cbor` for v1 — compact and well-supported. Switch to `prost` if heartbeats end up on the gRPC bus in milestone B.)

### 11.2 Send loop

- Interval: 2 seconds (master spec §13 item 10).
- For every known peer, send one packet to `peer_mesh_ip:51820`.

### 11.3 Receive loop

- Bind UDP on `0.0.0.0:51820` on the `teemesh0` interface.
- For every received packet:
  - Verify Ed25519 signature against the sender's stored `ed25519_pub` (learned via PeerEndpoint, §7 step 7).
  - If sender's Ed25519 is not yet known: buffer the packet for up to 5 seconds, then drop. Log at debug.
  - Update the rolling-window view: `peer_last_seen[sender] = now()`, `peer_connected_view[sender] = packet.connected_member_ids`.

### 11.4 Liveness calculation

Continuous, but the first-converged gate fires exactly once.

- **Peer is up**: `now() - peer_last_seen[peer] < 3 * interval` (i.e. last 3 heartbeats received).
- **Peer is live (cluster-wide property)**: at least one peer's `connected_view` contains this peer's memberId.
- **Mesh is converged**: ∀p₁,p₂ ∈ live: `peer_connected_view[p₁] == peer_connected_view[p₂]` and that set equals the live set.

First-converged gate (§7.1 step 10): set once on first `mesh_converged == true`. Never reset.

Steady-state liveness changes (peer up/down) flow to the application as `PeerEvent::Liveness(member_id, up: bool)`.

---

## 12. App-facing gRPC

### 12.1 `.proto`

```proto
service Agent {
  rpc GetMeshStatus(Empty) returns (MeshStatus);
  rpc ListPeers(Empty) returns (PeerList);
  rpc SendMessage(SendRequest) returns (SendResponse);
  rpc SubscribeMessages(Empty) returns (stream IncomingMessage);
  rpc SubscribePeerEvents(Empty) returns (stream PeerEvent);
  rpc GetClusterSharedKey(Empty) returns (ClusterSharedKey);
  rpc GetSelf(Empty) returns (SelfInfo);
}

message MeshStatus {
  string phase = 1;                  // "booting" | "registering" | ... | "healthy"
  bool first_converged = 2;
  bool csk_acquired = 3;
  uint32 live_peer_count = 4;
}

message SelfInfo {
  bytes32 member_id = 1;
  bytes member_contract = 2;         // 20-byte address
  uint32 mesh_ip = 3;                // derived per §7.3
}

message PeerList {
  repeated PeerInfo peers = 1;
}
message PeerInfo {
  bytes32 member_id = 1;
  uint32 mesh_ip = 2;
  bool live = 3;
}

message SendRequest {
  bytes32 recipient_member_id = 1;
  bytes payload = 2;                 // app-defined; sidecar sealed-boxes to recipient
  bytes32 envelope_id = 3;           // optional; if zero, sidecar generates a hash of payload
}
message SendResponse {
  bytes32 envelope_id = 1;
  bytes32 tx_hash = 2;
}

message IncomingMessage {
  bytes32 sender_member_id = 1;
  bytes payload = 2;                 // decrypted
  uint64 block_number = 3;
}

message PeerEvent {
  oneof kind {
    PeerJoined joined = 1;            // MemberRegistered observed + endpoint received
    PeerLiveness liveness = 2;        // heartbeat-derived up/down change
  }
}
message PeerJoined { bytes32 member_id = 1; uint32 mesh_ip = 2; }
message PeerLiveness { bytes32 member_id = 1; bool up = 2; }

message ClusterSharedKey { bytes key = 1; }   // 32 bytes
```

### 12.2 Transport

- Unix domain socket at `AGENT_GRPC_SOCKET`. App container mounts the same socket directory.
- No transport-level auth — the trust domain is the CVM. Anything inside the CVM is trusted.

### 12.3 Semantics

- `SendMessage`: sidecar encrypts payload with sealed-box to `recipient_member_id`'s `xPubKey` (read from local cache → fallback `AttestFacet.xPubKeyOf` via RPC), submits `MessageFacet.send(recipient, envelope_id, ciphertext)`. Returns when the tx is mined (12s on Sepolia worst case). On revert (`DuplicateEnvelope`, `RecipientNotMember`), returns `FailedPrecondition` with a descriptive message.
- `SubscribeMessages`: stream every successfully-decrypted incoming message whose recipient is self and whose kind is *not* a sidecar-internal type (`peer-endpoint.v1`, `csk-onboarding.v1`). Failed decryptions are silently dropped. Catchup behavior: streams everything the sidecar has accumulated since startup; the app is expected to handle dedup if it restarts.
- `SubscribePeerEvents`: stream `PeerJoined` (on MemberRegistered + endpoint) and `PeerLiveness` (on heartbeat status changes).
- `GetClusterSharedKey`: returns the 32-byte CSK. Returns `Unavailable` before acquisition.
- `GetSelf` / `GetMeshStatus` / `ListPeers`: trivial reads of in-memory state.

### 12.4 Backpressure

- `SubscribeMessages` and `SubscribePeerEvents` use bounded channels (capacity 1024). On full channel, the sidecar logs at warn and drops the *oldest* unconsumed event. The app should consume promptly or lose history. Milestone B revisits with explicit backpressure.

---

## 13. Cluster Shared Key handling

Per master spec §8.

- **Originator path** (§8.1): derive via `dstack.derive_key("teemesh.cluster-shared.v1", "csk-v1") → [u8;32]`. Seal via `dstack.seal("teemesh.csk.v1", csk)`. Cache in process memory. Mark `csk_acquired = true`.
- **Onboardee path** (§8.3): on every `MessageSent` event delivered to this member with `envelope_id == keccak256("teemesh.csk.onboarding.v1")`: sealed-box decrypt → validate `kind == "csk-onboarding-v1"` → read `csk` → check sender is a current cluster member by their event-asserted `senderMemberId` (already Indexer-verified) → seal via dstack → cache → mark `csk_acquired = true`. Subsequent CSK-onboarding events for self are no-ops.
- **Restart path** (§7.1 step 4): `dstack.unseal("teemesh.csk.v1") → csk` → cache → mark `csk_acquired = true`.
- **Onboarding a new peer** (steady-state, master spec §8.2): on `MemberRegistered` for a peer we don't know yet:
  1. Sleep `Uniform([0, 500])` ms.
  2. Query the recipient's MessageFacet channel for envelope `keccak256("teemesh.csk.onboarding.v1")` — implemented as a single Indexer query (`get_envelope(recipient_member_id, envelope_id)`).
  3. If absent: sealed-box-encrypt the CSK with the new peer's `xPubKey`, submit `MessageFacet.send(...)`. If the tx reverts with `DuplicateEnvelope` (someone won the race), log at info and move on.

---

## 14. Healthcheck

Two surfaces, same semantics:

- **HTTP** at `HEALTH_HTTP_ADDR` (`/healthz` only): returns `200 OK` once `first_converged && csk_acquired`, else `503 Service Unavailable` with a JSON body containing the current phase and which gates are open/closed. This is what docker-compose's `healthcheck:` directive curls.
- **gRPC** (`Agent.GetMeshStatus`): structured status (§12.1).

Phases reported (`MeshStatus.phase`):
- `booting` — pre key-derivation
- `registering` — pre-`dstack_register` tx
- `subscribing` — opening Indexer stream
- `waiting-peers` — Indexer connected, awaiting `PeerEndpoint` envelopes
- `waiting-csk` — onboardees only: awaiting `csk-onboarding-v1` envelope
- `wg-configuring` — adding peers to wireguard
- `heartbeating` — peers added, awaiting first convergence
- `healthy` — both gates passed; the application container is now allowed to start

---

## 15. Failure handling

- **Registration revert** (any reason except `AlreadyRegistered` — see §7.1 step 4): exit non-zero. docker-compose restart policy applies; if the cause is allowlist-related, the loop continues until ops intervenes.
- **Indexer down**: exponential backoff reconnect. Healthcheck phase stuck at `subscribing` for joiners; healthy CVMs stay healthy (first-converged gate fires once).
- **CSK envelope never arrives**: phase stuck at `waiting-csk` (onboardees). Operational fix required.
- **Convergence never reached**: phase stuck at `heartbeating`. Same.
- **wg netlink errors**: log at error, retry. Persistent failure → exit non-zero (capability issue or kernel mismatch).
- **dstack socket unavailable**: exit non-zero at boot; cannot operate without the runtime.

---

## 16. Tests (v1 scope)

### 16.1 Unit

- `keys::derive_all` against a mock dstack returning fixed bytes for each purpose string.
- `wg::cidr::derive_ip` against a fixture set of memberIds + CIDRs.
- `envelopes::{seal,open}` round-trip.
- `heartbeat::packet::{sign,verify}` round-trip; malformed sig rejection.
- `heartbeat::liveness::compute` against fixture views.
- `csk::{originator_derive,onboardee_receive,steady_state_onboard}` against mock Indexer events.

### 16.2 Integration

`tests/integration/` runs a fake dstack socket + a fake Indexer + a foundry-anvil RPC, brings up three sidecar instances, asserts:

1. All three reach `healthy`.
2. Originator derives CSK; onboardees receive it; all three `GetClusterSharedKey` return identical bytes.
3. Each sidecar's `ListPeers` returns the other two.
4. Heartbeat liveness reflects connection state.
5. `SendMessage` from A to B is observed in B's `SubscribeMessages` stream as a decrypted payload; not observed in C's stream.
6. Killing the originator and bringing up a fourth onboardee still succeeds (one of the remaining two onboards them).
7. Killing the Indexer mid-flight: existing healthy sidecars stay healthy; a fresh sidecar boots and stays at `subscribing` phase.

No fuzz tests for v1. No mainnet-fork tests. No actual dstack hardware tests; those are milestone B.

---

## 17. Open questions

1. **Funding model for the binding-derived sender key.** The sidecar's k256 derived key needs testnet ETH for gas on every tx (`dstack_register`, `publishWgKey`, every `MessageFacet.send`). For v1 demo: an operator manually funds before starting the CVM, using a CLI helper that reads `member.cluster()` → predicts the address → drops funds. For milestone B: paymaster / account abstraction, or sidecar registers with an off-chain relay. v1 punts to the manual flow.
2. **`SubscribeMessages` catchup boundary.** Currently the sidecar's queue is "since process start." App restarts lose history. Should the sidecar persist incoming messages to a sealed disk store keyed by `(envelopeId, block_number)` so app restarts can replay? Adds storage; v1 leaves out. Application must dedup if it relies on idempotency.
3. **Heartbeat packet encoding stability.** v1 uses `serde_cbor`. CBOR has multiple valid encodings of the same logical value — the signature would not survive a re-encode. Mitigation: canonical CBOR or switch to protobuf. v1 picks canonical CBOR via `ciborium` and pins the encoder.
4. **Multiple cluster membership.** What if one CVM is a member of two TeeMesh clusters (e.g. a hypothetical Indexer dog-fooding scenario where the Indexer cluster's members are also subscribers to the customer clusters)? v1 sidecar is single-cluster only; this is a milestone B+ shape.
