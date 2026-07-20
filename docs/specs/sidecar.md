# AttestMesh Sidecar — Component Spec

**Status**: Implemented v1 + protocol-v2 Indexer delivery — Base canary rollout pending; see §1.1 and [`docs/deployment.md`](../deployment.md)
**Parent spec**: [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) (especially §7, §8)
**Component**: `sidecar/`
**Binary**: `cluster-mesh-agent`
**Last updated**: 2026-07-11

---

## 1. Purpose

The sidecar is the per-node process that turns "a CVM running in dstack" into "an AttestMesh cluster member." It owns key derivation, on-chain registration, Indexer subscription, wireguard setup, heartbeats, the first-convergence gate, the CSK lifecycle, and the gRPC façade the application container talks to. Master spec §7 defines *what* it does; this spec defines *what gets built*.

### 1.1 Implementation status (v1)

The original two-node bring-up is live-proven on Base mainnet (8453); see [`docs/deployment.md`](../deployment.md). `state::run()` drives key derivation, proof construction, sponsored registration, wireguard-over-gateway setup, heartbeats, CSK lifecycle, and the app/peer gRPC façades. Protocol v2 replaces the former sidecar `MessageSent` log poll with signed Indexer envelope dispatch while direct RPC remains limited to current-state views. The v2 path has unit and fake-Indexer transport coverage (including ACK ordering and checkpoints) and awaits the documented Indexer-first canary rollout.

---

## 2. Toolchain

- **Rust edition 2021**, MSRV `1.78` (revisit when `tonic`/`tokio` need newer).
- **Target**: `x86_64-unknown-linux-gnu`, statically linkable against musl for the OCI image (`x86_64-unknown-linux-musl` variant for distroless final image).
- **Build**: `cargo build --release`; CI also runs `cargo clippy -- -D warnings` and `cargo fmt --check`.

### 2.1 Key dependencies

| Crate | Purpose |
|---|---|
| `tokio` (full) | async runtime |
| `tonic` + `prost` | gRPC client (Indexer) + servers (app facade over UDS, peer control over mesh) |
| `alloy` (`alloy-primitives`, `alloy-provider`, `alloy-signer`, `alloy-sol-types`) | EVM RPC reads, signing, ABI binding |
| `alloy-rpc-types-bundler` (or hand-rolled wrappers if upstream isn't ready) | EIP-4337 v0.7 bundler RPC (`eth_sendUserOperation`, `eth_estimateUserOperationGas`, `eth_getUserOperationReceipt`; nonces come from `EntryPoint.getNonce` via `eth_call` — `eth_getUserOperationNonce` is not a real bundler method, see §8.2) |
| `dalek-cryptography` family (`x25519-dalek`, `ed25519-dalek`) | Curve25519 ops |
| `crypto_box` | NaCl sealed-box (x25519 + XSalsa20-Poly1305) |
| `chacha20poly1305` | XChaCha20-Poly1305 encryption for the durable CSK envelope |
| `zeroize` | zero-on-drop key material |
| `defguard_wireguard_rs` | userspace wireguard control via `WG_QUICK`-equivalent netlink |
| `dstack-sdk` (vendored if not crates.io-published) | dstack runtime client (`/GetKey`, `/Info`, `/GetQuote` — dstack 0.5.x removed `/DeriveKey` and does not expose application `Seal`/`Unseal`) |
| `tracing` + `tracing-subscriber` (json output) | structured logging |
| `prometheus` + `axum` | metrics endpoint (milestone B; v1 wires the crate in but exposes minimal counters) |

### 2.2 Build-time codegen

- gRPC `.proto` for the Indexer subscription (`proto/indexer.proto`) — shared with the indexer component.
- gRPC `.proto` for the app facade (`proto/agent.proto`) — owned by this component.
- gRPC `.proto` for the peer-control service (`proto/peer.proto`) — owned by this component.
- Solidity ABI bindings for AttestFacet / DstackFacet / NetworkFacet / MessageFacet / IndexerRegistry via `alloy-sol-types` / `sol!` macro from the JSON ABIs emitted by `forge build` in `contracts/`.

---

## 3. Process model

One process per node. Runs as a docker-compose service named `mesh-agent`. The application container declares `depends_on: { mesh-agent: { condition: service_healthy } }` so it does not start until the sidecar reports converged + CSK-acquired.

Capabilities:
- `NET_ADMIN` — wireguard interface management.

No `--privileged`. The sidecar mounts:
- A unix domain socket directory shared with the app container (for the app facade gRPC).
- The dstack guest-agent socket (`/var/run/dstack.sock` or whatever the dstack runtime exposes).
- A sidecar-only named volume at `/var/lib/attestmesh` when durable state is enabled.

Restart policy: `unless-stopped`. The sidecar crashing post-convergence brings down its healthcheck; existing wireguard peers remain reachable to the application until the sidecar comes back, but the app cannot send messages or learn about new members during the gap.

---

## 4. File layout

```
sidecar/
├── Cargo.toml                       # workspace root (single crate for v1)
├── proto/
│   ├── indexer.proto                # shared with indexer/; v1 lives here, indexer/ imports
│   ├── agent.proto                  # owned here; defines the app facade
│   └── peer.proto                   # owned here; defines the peer-control service (CSK pull over mesh)
├── build.rs                         # tonic-build for the three protos + alloy sol! bindings
├── src/
│   ├── main.rs                      # entry, arg parsing, tokio runtime spawn
│   ├── config.rs                    # env-var schema + load
│   ├── dstack.rs                    # dstack runtime client wrappers (GetKey, Info, GetQuote)
│   ├── keys.rs                      # all key derivation + zeroize wrappers
│   ├── chain/
│   │   ├── mod.rs                   # RPC provider + bundler client + signer setup
│   │   ├── userop.rs                # PackedUserOperation construction + signing
│   │   ├── bundler.rs               # EIP-4337 bundler RPC client (eth_sendUserOperation etc.)
│   │   ├── registry.rs              # IndexerRegistry read (direct RPC)
│   │   ├── attest.rs                # AttestFacet view reads (direct RPC at startup only)
│   │   ├── dstack_facet.rs          # dstack_register UserOp builder
│   │   ├── network_facet.rs         # publishWgKey UserOp builder
│   │   └── message_facet.rs         # send UserOp builder
│   ├── indexer_client.rs            # gRPC client + reconnect logic
│   ├── wg/
│   │   ├── mod.rs                   # netlink + interface lifecycle
│   │   ├── cidr.rs                  # the §7.3 IP-derivation
│   │   └── peer.rs                  # peer add/remove/update
│   ├── heartbeat/
│   │   ├── mod.rs                   # send/receive loops
│   │   ├── packet.rs                # wire format + ed25519 sign/verify
│   │   └── liveness.rs              # the rolling view + convergence calc
│   ├── csk.rs                       # origination, peer pull, commitment verification, encrypted cache
│   ├── storage.rs                   # private atomic durable-state writes
│   ├── envelopes.rs                 # PeerEndpoint (de)serialize + sealed-box
│   ├── state/
│   │   ├── mod.rs                   # the bring-up state machine
│   │   └── gates.rs                 # first-convergence + CSK-acquired gating
│   ├── agent_grpc.rs                # app-facing tonic server (UDS)
│   ├── peer_grpc.rs                 # peer-control tonic server (bound to mesh IP)
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
| `MEMBER_CONTRACT` | no | — | hex address of this node's ClusterMember proxy. Unset → self-discovered from the dstack `/Info` `app_id` at runtime (Path A: the member contract IS the phala-provisioned app_id, unknowable before `phala deploy`) |
| `CHAIN_ID` | yes | — | EVM chain id (`8453` — Base mainnet, the v1 deployment) |
| `RPC_URL` | yes | — | EVM RPC endpoint URL for current-state reads (§8.4); production may keep the local proxyd endpoint as hotfix/rollback support. |
| `BUNDLER_URL` | yes | — | Separate EIP-4337 bundler endpoint (Alchemy in v1). It must not be replaced by the read-RPC/proxyd URL. |
| `GAS_POLICY_ID` | no | `` | Alchemy Gas Manager policy id for `alchemy_requestGasAndPaymasterAndData` (sponsored UserOps — §8.2). |
| `INDEXER_REGISTRY_ADDR` | yes | — | hex address of the per-chain IndexerRegistry. (Hardcoded per chain id in v1 sidecar binary; env var allows overriding for tests.) |
| `GATEWAY_DOMAIN` | no | — | dstack gateway base domain (live value: `dstack-base-prod5.phala.network`). Peer ingress hostnames are `<app_id>-<port>s.<domain>` (§10). Unset → mesh bring-up is skipped (registration-only mode). |
| `WG_TCP_PORT` | no | `51900` | TCP port of the wg-over-TCP ingress, exposed through the gateway (§10) |
| `WG_LISTEN_PORT` | no | `51821` | wireguard outer listen port. Distinct from the in-mesh heartbeat port `51820` (§11.1) because kernel wg owns its UDP socket — the two must not collide |
| `DSTACK_SOCKET` | no | `/var/run/dstack.sock` | path to dstack guest-agent socket |
| `SIDECAR_STATE_DIR` | no | — | sidecar-only durable directory for the KMS-wrapped CSK, public peer-key cache, and last signed Indexer checkpoint. Production mounts `/var/lib/attestmesh` from the `sidecar-state` named volume; unset preserves memory-only restart behavior. |
| `AGENT_GRPC_SOCKET` | no | `/var/run/attestmesh/agent.sock` | path the app facade listens on |
| `HEALTH_HTTP_ADDR` | no | `127.0.0.1:9090` | HTTP /healthz endpoint (for docker-compose healthcheck) |
| `LOG_FORMAT` | no | `json` | `json` or `pretty` |
| `LOG_LEVEL` | no | `info` | standard `tracing` filter |

No secrets in env vars. All key material is derived from the attestation-bound seed provided by the node's attestation method at runtime (on dstack: the guest agent's `/GetKey`).

---

## 6. Key derivation

All key material is provided by the node's attestation method (on dstack: the guest agent's `/GetKey(path, purpose)` — dstack 0.5.x removed the older `/DeriveKey` endpoint; the master spec's `derive_key` maps to this call). Secret plaintext is held only in zeroizing sidecar memory; durable CSK state is encrypted with a distinct KMS-derived wrapping key.

This uses dstack's supported application-key surface: [`/GetKey` is intended for deriving keys that encrypt application data](https://docs.phala.network/dstack/local-development). The sidecar does not call nonexistent guest-agent `Seal`/`Unseal` services.

| Purpose string | Algo | Used for |
|---|---|---|
| `attestmesh.identity.v1` | curve25519 | Curve25519 root seed. x25519 (encryption) and Ed25519 (signing) are derived from this via standard ed25519 → curve25519 conversion (`x25519-dalek::PublicKey::from(&ed25519_sk)`) so they share one stored secret. |
| `attestmesh.wireguard.v1` | curve25519 | wireguard private key (Curve25519 scalar) |
| `attestmesh.binding.v1` | k256 | secp256k1 derived key whose address becomes the ClusterMember's `owner` after first registration. Signs (a) the one-shot dstack_register binding signature (master spec §4.2), and (b) every subsequent UserOpHash. One key, two recoverable signing surfaces. |
| `attestmesh.cluster-shared.v1` (purpose) / `csk-v1` (version) | aes-256 raw bytes | Cluster Shared Key. **Only derived by the originator.** Onboardees never call this. |
| `attestmesh.csk-cache.v1` (purpose) / `wrap-v1` (version) | 256-bit raw bytes | Per-member XChaCha20-Poly1305 key-encryption key for the durable CSK envelope. |

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
  Originator → DeriveCsk → PublishCskCommitment → PersistEncryptedCsk → PublishWg
  Onboardee → SubscribeIndexer (parallel with PublishWg)
  PublishWg → SubscribeIndexer (if not already)
  SubscribeIndexer → WaitPeerEndpoints
  WaitPeerEndpoints → ConfigureWireguard → StartHeartbeat
  StartHeartbeat → ComputeConvergence → FirstConverged
  ConfigureWireguard → (Onboardee, on first live tunnel) PullCsk → CskAcquired
  FirstConverged + CskAcquired → Healthy
  Healthy → SteadyState (handles new joiners, peer drops, indexer reconnects; serves peer CSK-pull requests)
```

### 7.1 Step-by-step

The 12 numbered steps from master spec §7.1 map onto modules as follows. This is the canonical ordering; modules run on tokio tasks and the state machine ticks on events.

| Master step | Module | Notes |
|---|---|---|
| 1 Discover cluster | `chain::attest::cluster_of` | reads `member.cluster()` via RPC |
| 2 Derive keys | `keys::derive_all` | + `csk` originator path uses `csk::derive` later |
| 3 Construct proof | `dstack_facet::build_proof_from_runtime` (`/Info` + `/GetKey`) | builds `DstackProof` (contracts spec §6.3); the binding signer is the `/GetKey`-derived key |
| 4 Register or recognize | `state::check_existing_member` | branches on `AttestFacet.memberOf` |
| 4a Determine CSK role | `state::determine_role` | reads `AttestFacet.memberCount()` post-register |
| 5 Publish wireguard | `chain::network_facet::publish` | one tx; idempotent (no-op if value unchanged) |
| 6 Subscribe to Indexer | `indexer_client::connect` | reads `IndexerRegistry` → opens gRPC stream |
| 7 Wait peer endpoints | `envelopes::handle_message_sent` | decrypt sealed-box, parse, hand to wg + heartbeat |
| 7' Pull CSK (onboardee) | `csk::pull_from_peer` | after first live tunnel |
| 8 Send own endpoint | `envelopes::send_peer_endpoint` | per discovered peer; see the resend rules below |
| 9 Heartbeat | `heartbeat::send_loop` + `heartbeat::recv_loop` | 2s interval, 3-miss threshold |
| 10 Convergence gate | `state::gates::first_converged` | fires exactly once |
| 11 Become healthy | `health::set_ready` | dual-gate: first-converged AND CSK-acquired |
| 12 Steady-state | `state::steady_state` | serves peer CSK-pull requests, handles peer drops, indexer reconnects |

**PeerEndpoint resend rules.** Every send is a sponsored UserOp, so re-announcing is bounded on three fronts (live-found 2026-07: the unbounded 600s resend burned ~1.3k sponsored ops/day fleet-wide):

- *Backoff*: while a peer's Ed25519 key is unknown, resends start at 600s and double per attempt, capped at 24h — an on-chain orphan (dead VM, no `removeMember` on live clusters) costs one envelope/day instead of 144.
- *Persistence*: learned peer Ed25519 keys are public and atomically mirrored to `SIDECAR_STATE_DIR/peer-ed25519.v1`, so a restart doesn't forget peers and re-enter the resend loop.
- *Reply-on-receive*: receiving a peer's PeerEndpoint triggers one reply (suppressed if we sent to that peer within the last 600s, which prevents ping-pong) even when its key is already known — a peer that restarted without its durable cache can re-learn our key from its first announce instead of resending forever.

### 7.2 State transitions are observable

Every state change emits a `tracing::info!` event with a `phase` field (`booting`, `registering`, `subscribing`, `waiting-peers`, `pulling-csk`, `wg-configuring`, `heartbeating`, `converging`, `healthy`). The healthcheck (§14) exposes the current phase so operators can diagnose where a boot stuck.

---

## 8. Chain interaction

The sidecar never submits raw transactions to the chain. Every state-mutating call goes through EIP-4337 as a UserOperation submitted to an Alchemy bundler endpoint, sponsored by the Alchemy paymaster after the AttestMesh gas-sponsorship webhook approves (master spec §13 item 18, gas-webhook spec). The sidecar holds zero ETH.

### 8.1 Provider, bundler, signer

- **EVM RPC provider** (read-only): `alloy-provider` HTTP transport against `RPC_URL`. Used for current contract views listed in §8.4, never event logs.
- **Bundler RPC**: a separate HTTP client against `BUNDLER_URL` (Alchemy's `https://...api.g.alchemy.com/v2/<key>` endpoint with the `eth_sendUserOperation` namespace). The sidecar sends a single `eth_sendUserOperation` per outbound call and polls `eth_getUserOperationReceipt` until inclusion.
- **Signer**: a single `alloy-signer-local::PrivateKeySigner` constructed from the `attestmesh.binding.v1` k256 derived key. This is the key whose address gets set as the ClusterMember's `owner` during `dstack_register`. It signs:
  - The dstack-registration binding hash (recovered inside `DstackFacet.dstack_register`).
  - Every subsequent UserOpHash (recovered inside `ClusterMember.validateUserOp`).

  One key, two signing surfaces, both recoverable on chain.

### 8.2 UserOp construction

For every outbound call (`dstack_register`, `publishWgKey`, `send`, etc.):

1. **Wrap as execute calldata.** The actual selector + args (e.g. `dstack_register(...)`) is encoded as `data`, then wrapped as `ClusterMember.execute(target=clusterDiamond, value=0, data=...)` to match the gas-webhook policy (gas-webhook spec §6 step 4).
2. **Construct PackedUserOperation v0.7.** Sender = our ClusterMember address. Nonce = `EntryPoint.getNonce(memberAddr, key=0)` via `eth_call` (we use a single nonce key for v1; 2D nonces remain available for future use). Live finding: `eth_getUserOperationNonce` is **not** a real bundler method — relying on it silently replayed nonce 0 and every post-registration op was rejected with `AA25`.
3. **Request sponsorship + gas (before signing).** Call `alchemy_requestGasAndPaymasterAndData` (Gas Manager policy `GAS_POLICY_ID`) with the partial UserOp and a dummy signature. The Alchemy Gas Manager calls the AttestMesh gas-sponsorship webhook to approve, then returns the gas limits **and** the paymaster fields (`paymaster`, `paymasterData`, paymaster gas limits), scoped to this exact op.
4. **Populate the op.** Apply the returned gas limits and paymaster fields to the UserOperation. The op is now final.
5. **Sign userOpHash (sponsor-then-sign).** Compute the v0.7 userOpHash over the now-final op — the hash commits to `paymasterAndData` — and sign with the binding key. **Ordering is mandatory:** signing before the paymaster fields are populated produces a hash that differs from the one the EntryPoint recomputes, so `validateUserOp` recovers the wrong signer and the op is rejected with `AA24` (premortem F2).
6. **Submit.** `eth_sendUserOperation`. Returns a userOpHash.
7. **Poll for inclusion.** `eth_getUserOperationReceipt(userOpHash)` every 2 seconds, up to 60 seconds. On success, return the underlying `txHash`. On timeout, surface to the state machine as a transient failure.

### 8.3 Bootstrap-UserOp special case

The very first UserOp from a fresh ClusterMember invokes `dstack_register`. At that moment the ClusterMember's `owner` field is still `address(0)` and validateUserOp uses the bootstrap path (contracts spec §9.1.2):

- The signature on `userOpHash` is from the binding key.
- The `messageSignature` inside the inner `dstack_register` proof is also from the binding key (signed over the EIP-191 message of the registration bind-hash; see contracts spec §6.3 step 4, *derived → message*).
- ClusterMember's validateUserOp recovers both and requires them to match.

The sidecar does not need to do anything different on its end — it constructs the UserOp the same way it constructs any other one. The chicken-and-egg is resolved entirely contract-side.

### 8.4 Direct RPC reads (current state only)

Direct RPC is retained for contract views:

- `member.cluster()` — finds the ClusterDiamond from the predicted ClusterMember address.
- `IndexerRegistry.current()` — finds the v1 Indexer endpoint + pubkey.
- `AttestFacet.memberOf(memberAddr)` — restart detection (§7.1 step 4).
- `AttestFacet.memberCount()` — CSK-role determination (§7.1 step 4a).
- `AttestFacet.cskCommitment()` — verifying a pulled CSK against the on-chain commitment (§13 onboardee path). Read when a pull response arrives rather than at startup, but uses the same direct-RPC path.
- `listMembers`, `memberById`, `meshIpOf`, and member key views — the periodic current-state reconciler uses these to configure peers and recover from missed wake-ups.
- EntryPoint nonce/view calls needed to construct outbound UserOperations.

The sidecar never uses direct RPC for event history: there is no `eth_getLogs`, lookback cursor, or Indexer-outage fallback. Signed Indexer envelopes are the sole source of `MessageSent` and state-change events; state-change events merely wake the direct-view reconciler early.

### 8.5 Tx-receipt failure handling

- **Registration UserOp reverts**: fatal. Exit non-zero. Common causes: not-allowlisted compose hash, bad KMS root, replay (member already exists — should have been caught by the §7.1 step 4 pre-flight read, but a race could land us here).
- **publishWgKey or send UserOp reverts**: log at warn, retry once with fresh gas estimation. If the second attempt also reverts, surface to the application as a `SendMessage` error (for `send`) or set a degraded-mode flag (for `publishWgKey`).
- **Bundler unavailable**: exponential-backoff retry against the same endpoint. Milestone B adds multi-provider failover (Pimlico, Stackup, etc.); v1 is single-provider.
- **Webhook denies sponsorship**: bundler returns `paymaster declined`. Treated as transient: log, wait 5 seconds, retry. (Persistent denial would mean the webhook config is wrong, which is operator-fixable; the sidecar's own behavior never changes.)

---

## 9. Indexer subscription

### 9.1 `.proto`

The canonical proto is in `sidecar/proto/indexer.proto`, shared with `indexer/`'s build. The full message set (including `RpcReproStub` and `IndexerAttestation`) is documented in **indexer.md §8.1**; the excerpt here covers only what's load-bearing for the sidecar's client view:

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
  bytes attestation = 3;    // reserved for milestone B re-verification; v1 indexer ignores (indexer.md §8.3)
  uint64 from_block = 4;    // 0 on first boot; last handled block on reconnect/backend change
  uint32 protocol_version = 5; // 2 = signed checkpoints + initialize missing cursor at indexed head
}

message Ack {
  uint64 block_number = 1;
  uint64 log_index = 2;
}

message PushEnvelope {
  bytes event_data = 1;             // RLP-encoded event topics + data (verifiable against the chain)
  bytes32 cluster_addr = 2;
  uint64 block_number = 3;
  bytes32 tx_hash = 4;
  uint64 log_index = 5;
  RpcReproStub rpc_repro = 6;       // see indexer.md §8.1 for full definition
  bytes indexer_signature = 7;      // Ed25519 over canonical-bytes(envelope minus this field minus indexer_attestation)
  IndexerAttestation indexer_attestation = 8;  // present on first push of a session only; see indexer.md §8.1
}
```

The proto file itself is canonical for codegen; both this spec and indexer.md must keep their illustrative excerpts in sync with it.

### 9.2 Client behavior

- Read `IndexerRegistry.current()` at boot. Cache `indexerPubKey` and `endpoint`.
- Open a single bidi stream and send protocol-v2 `Hello`. A first boot sends `from_block = 0`; reconnects send the last handled block, loaded from `SIDECAR_STATE_DIR/indexer-cursor.v1` when available. This lets a newly selected Indexer with no server cursor replay the boundary block instead of skipping to head. Verify every `indexer_signature` against the registry's `pubKey`; the signed `rpc_repro` remains diagnostic material and is not executed by default.
- On every event push: validate the cluster/RLP shape, decode and dispatch (`MessageSent` → decrypt/demux; membership/key/CSK events → wake the current-view reconciler). Cross-cluster or malformed signed data tears down the stream; undecryptable addressed messages are handled drops.
- Send `Ack(blockNumber, logIndex)` only after the handler completes. A signed empty checkpoint uses `(indexedThroughBlock, uint64::MAX)`, advances the server cursor across blocks with no relevant events, and is atomically persisted as the sidecar's cross-backend replay floor.
- On stream drop: mark the Indexer diagnostic disconnected, re-read `IndexerRegistry.current()`, and retry an established stream after 1 second. Failures before Subscribe opens back off `1s → 2s → 4s … 30s`. The server's exact cursor wins when present; a different backend replays from the sidecar-supplied block, including that whole boundary block. Registry read failures retry after 60 seconds, and an empty registry after 300 seconds.
- Preserve the signed `rpc_repro` for diagnostics/future sampling; protocol v2 does not execute it.

As wired in protocol v2, the Indexer is the sidecar's sole event source: there is no `eth_getLogs` boot scan or outage fallback. Direct RPC remains for current contract views (`listMembers`, member/key/CSK reads, and IndexerRegistry discovery). A missing/down Indexer pauses event delivery while reconnect runs; `MeshStatus` and `/healthz` report `indexer_connected`, `indexer_caught_up`, and the cursor block, but those diagnostics do not participate in the convergence+CSK health result. One gateway quirk remains: the tonic client must set `ClientTlsConfig::assume_http2(true)` for the dstack gateway.

---

## 10. Wireguard management

> **v1 transport reality (live).** This spec originally assumed direct UDP wireguard endpoints; dstack CVMs have **no inbound UDP**, so the v1 mesh bootstraps as **wireguard over length-prefixed UDP-over-TCP** through the dstack gateway's TLS-passthrough route (`<app_id>-<port>s.<GATEWAY_DOMAIN>`). The `transport` module bridges a loopback UDP socket per peer (which kernel wg uses as that peer's endpoint) to the peer's gateway ingress; `bringup` derives every peer's hostname from chain state + `GATEWAY_DOMAIN` — no off-chain config. The TLS on that leg is a throwaway self-signed cert (the gateway routes on SNI only); wireguard itself, with on-chain-pinned peer keys, remains the security layer. Two-sided simultaneous UDP hole-punching was live-verified on the prod5 fleet (including hairpin), so upgrading established links to pure punched UDP is deferred work, not a research risk.

### 10.1 Interface lifecycle

- Interface name: `attestmesh0` (single mesh per node in v1).
- Created at boot via netlink (`defguard_wireguard_rs`).
- Listen port: `WG_LISTEN_PORT` (default `51821` — the outer wg port; see §11.1 for why it differs from the heartbeat port). The endpoint peers actually dial is the gateway ingress hostname, advertised via PeerEndpoint.
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

UDP packet over the wg interface, port `51820` (cluster-wide convention; configurable in milestone B). Note this is the **in-mesh** heartbeat port, distinct from the wireguard **outer** listen port (`WG_LISTEN_PORT`, default `51821`): kernel wg owns its UDP socket, so the two cannot share a port.

```
struct Heartbeat {
  uint8  version;            // = 1
  uint64 sender_member_id_hi; // first 8 bytes of memberId
  uint64 sender_member_id_lo;
  uint32 sender_member_id_rest; // bytes 16..20 (memberId is 32 bytes; truncated for wire)
  // ... actually: send full 32-byte memberId
  bytes32 sender_member_id;
  uint64 timestamp_ms;       // monotonic clock since unix epoch in node
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

- Bind UDP on `0.0.0.0:51820` on the `attestmesh0` interface.
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
  bool indexer_connected = 5;        // diagnostic only
  bool indexer_caught_up = 6;        // signed checkpoint observed this session
  uint64 indexer_cursor_block = 7;
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
- No transport-level auth — the trust domain is the node. Anything inside the node is trusted.

### 12.3 Semantics

- `SendMessage`: sidecar encrypts payload with sealed-box to `recipient_member_id`'s `xPubKey` (read from local cache → fallback `AttestFacet.xPubKeyOf` via RPC), submits `MessageFacet.send(recipient, envelope_id, ciphertext)`. Returns when the tx is mined (Base ≈ 2s blocks; a few seconds typical). On revert (`DuplicateEnvelope`, `RecipientNotMember`), returns `FailedPrecondition` with a descriptive message.
- `SubscribeMessages`: stream every successfully-decrypted indexed message whose recipient is self and whose kind is *not* sidecar-internal. A plaintext `PeerEndpoint` carrying the reserved `peer-endpoint.v1` kind is consumed internally; **every other decrypted payload is forwarded verbatim** as `AppIncoming{sender_member_id, payload, block_number}`. Failed decryptions are handled drops and Ack'd so poison messages cannot wedge replay. Delivery is at-least-once; applications must dedup (the matrix-admin-agent keeps a `request_id` ledger), and the in-memory app queue remains non-durable across sidecar process restarts.
- `SubscribePeerEvents`: stream `PeerJoined` (on MemberRegistered + endpoint) and `PeerLiveness` (on heartbeat status changes).
- `GetClusterSharedKey`: returns the 32-byte CSK. Returns `Unavailable` before acquisition.
- `GetSelf` / `GetMeshStatus` / `ListPeers`: trivial reads of in-memory state.

### 12.4 Backpressure

- `SubscribeMessages` and `SubscribePeerEvents` use bounded channels (capacity 1024). On full channel, the sidecar logs at warn and drops the *oldest* unconsumed event. The app should consume promptly or lose history. Milestone B revisits with explicit backpressure.

### 12.5 Peer-control gRPC

Distinct from the app-facing façade (§12.1–§12.4). This is a second tonic server (`src/peer_grpc.rs`) bound to the node's **mesh IP** on the `attestmesh0` wireguard interface — not the app UDS — so the only callers are other cluster members reachable over the encrypted mesh. It carries the CSK peer-pull described in §13.

```proto
// proto/peer.proto — owned by this component
service PeerControl {
  rpc RequestClusterSharedKey(CskRequest) returns (SealedCsk);
}

message CskRequest {
  bytes32 requester_member_id = 1;
}
message SealedCsk {
  bytes sealed_csk = 1;        // CSK sealed-boxed (x25519) to the requester's on-chain xPubKey
}
```

Semantics:
- `RequestClusterSharedKey`: the responder verifies `requester_member_id` is a current member (its `xPubKey` exists in `AttestFacet`), then returns the CSK sealed-boxed to that `xPubKey`. If the responder does not hold the CSK yet, it returns gRPC `Unavailable`. See §13 for both the request (onboardee pull) and serve sides.

---

## 13. Cluster Shared Key handling

Per master spec §8.

- **Originator path** (§8.1): derive via `dstack.derive_key("attestmesh.cluster-shared.v1", "csk-v1") → [u8;32]`, publish `AttestFacet.setCskCommitment(keccak256(csk))`, persist the authenticated encrypted cache, and mark `csk_acquired = true`. The CSK itself never goes on chain — only the commitment does.
- **Onboardee path** (§8.3, peer pull): once the onboardee has ≥1 live wireguard tunnel to a member, it pulls the CSK over the mesh rather than waiting for any on-chain envelope:
  1. Order configured peers as originator → live peers → remaining peers and call `PeerControl.RequestClusterSharedKey(CskRequest { requester_member_id: self.memberId })` with at most eight probes in flight.
  2. Each probe has a 1s connect deadline and 2s RPC deadline. `Unavailable`, timeout, or connection failure advances the bounded queue; the first commitment-valid response cancels the rest.
  3. On a `SealedCsk { sealed_csk }` response: sealed-box open with this node's x25519 private key → verify `keccak256(csk) == AttestFacet.cskCommitment()` (a direct on-chain view read, like the `memberCount()` read in §8.4). On mismatch, discard and try another connected peer.
  4. On success: write `SIDECAR_STATE_DIR/csk.v1` atomically, cache in process memory, and mark `csk_acquired = true`. Failed durable writes are non-fatal.
- **Restart path** (§7.1 step 4): read the versioned envelope; require matching cluster, member contract, and current on-chain commitment; derive `GetKey("attestmesh.csk-cache.v1", "wrap-v1")`; authenticate/decrypt with XChaCha20-Poly1305; independently re-check `keccak256(csk)`. Missing, corrupt, stale, or undecryptable state falls back to originator re-derivation or peer pull.
- **Retry scheduling:** peer configured/live events wake the pull loop immediately. Otherwise retries use ±20% jittered exponential backoff from 250ms to 2s, avoiding both the old fixed 10s startup penalty and a cold-cluster busy loop.
- **Serving a peer's pull** (steady-state, master spec §8.2): a member that already holds the CSK answers `PeerControl.RequestClusterSharedKey` from a peer over the mesh:
  1. Verify the requester is a current cluster member — its `xPubKey` exists in `AttestFacet` for `requester_member_id`.
  2. Sealed-box-encrypt the CSK to that `xPubKey` and return it as `SealedCsk { sealed_csk }`. No `MessageFacet.send`, no on-chain transaction, no `[0,500]ms` backoff, no `DuplicateEnvelope` handling.
  3. If this node does not hold the CSK yet, return gRPC `Unavailable`.

---

## 14. Healthcheck & node HTTP surface

Two health surfaces, same semantics:

- **HTTP** at `HEALTH_HTTP_ADDR` (`/healthz`): returns `200 OK` once `first_converged && csk_acquired`, else `503 Service Unavailable` with a JSON body containing the current phase and which gates are open/closed. This is what docker-compose's `healthcheck:` directive curls.
- **gRPC** (`Agent.GetMeshStatus`): structured status (§12.1).

The same HTTP listener also serves:

- **`GET /metrics`** — Prometheus text form of the punch counters and per-peer link transport.
- **`GET /attestation`** — this member's public TEE attestation bundle, served node-locally so a UI or remote verifier can pull and verify each node **directly** rather than trusting a central API (the same node-local pattern TeeSQL uses for its `/attestation` route). It re-queries the dstack guest agent for a fresh quote on each call. Returns `200 OK` with the bundle when a quote is obtained, or `503 Service Unavailable` with `available:false` and an `errors` map when the guest agent is unreachable — it never fabricates a quote. The bundle is **public by design**: it is meant to be pulled and independently verified by anyone — a UI, a counterparty, or a third-party auditor — so remote attestation delivers its value without trusting any central API. It is safe to expose precisely because it carries **only public material** (see fields below): the no-secrets property, not a network wall, is what makes public exposure correct. The listener binds `HEALTH_HTTP_ADDR` (code default `127.0.0.1:9090`); the shipped deploy templates set `HEALTH_HTTP_ADDR=0.0.0.0:9090` and publish host port `9090`, so `/attestation` — alongside `/healthz` and `/metrics` — is reachable by external verifiers. Only **public** material is returned; never secret key material, the CSK, or env values.

  Body fields:
  - `attestor` — attestation method label (`"dstack"`); mirrors the on-chain attestor id and the mesh-state-api display label.
  - `member_id`, `member_contract`, `cluster`, `mesh_ip`, `phase` — this member's on-chain/mesh identity and current bring-up phase.
  - `identity` — the public keys already published on chain: `x_pub`, `ed25519_pub`, `wg_pub`.
  - `dstack` — guest-agent `/Info`: `app_id`, `compose_hash`, `instance_id`, `device_id`, `tcb_status`.
  - `code_id` — `bytes32(bytes20(app_id))`, the same value the on-chain `DstackProof` and the cluster boot gate use.
  - `report_data` — the 64-byte quote user-data; `report_data_binding` gives the recipe (`keccak256(cluster||memberContract||xPubKey||wgPubKey||ed25519PubKey)` in `report_data[0..32]`) so a verifier can independently recompute it from the public fields and confirm the quote is bound to this identity.
  - `quote` — `{ provider, format:"raw", len, bytes }`: the raw TEE-signed attestation blob over `report_data`.
  - `available` — `true` when both `/Info` and `/GetQuote` succeeded; otherwise `false` with an `errors` object (`info`/`quote`) and HTTP 503.

Phases reported (`MeshStatus.phase`):
- `booting` — pre key-derivation
- `registering` — pre-`dstack_register` tx
- `subscribing` — opening Indexer stream
- `waiting-peers` — no other current members yet, or awaiting peer configuration
- `pulling-csk` — onboardees only: pulling the CSK from a connected peer over the mesh
- `wg-configuring` — adding peers to wireguard
- `heartbeating` — peers added, awaiting first convergence
- `healthy` — both gates passed; the application container is now allowed to start

---

## 15. Failure handling

- **Registration revert** (any reason except `AlreadyRegistered` — see §7.1 step 4): exit non-zero. docker-compose restart policy applies; if the cause is allowlist-related, the loop continues until ops intervenes.
- **Indexer down**: reconnect with backoff; event delivery pauses and no direct-log fallback starts. Current-view peer reconciliation and the latched convergence+CSK health result continue, while Indexer diagnostics report disconnected.
- **CSK pull fails (no reachable peer holds it)**: phase stuck at `pulling-csk` (onboardees). Operational fix required.
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
- CSK candidate priority, eight-request concurrency cap, deadlines, first-valid cancellation, unavailable-peer retry, and bogus-response rejection.
- CSK cache round-trip/replacement, KMS/cluster/member/commitment binding, corruption/version rejection, optional-state behavior, and private atomic storage permissions.

### 16.2 Integration

`tests/integration/` runs a fake dstack socket + a fake Indexer + a foundry-anvil RPC, brings up three sidecar instances, asserts:

1. All three reach `healthy`.
2. Originator derives CSK; onboardees pull it; all three `GetClusterSharedKey` return identical bytes.
3. Each sidecar's `ListPeers` returns the other two.
4. Heartbeat liveness reflects connection state.
5. `SendMessage` from A to B is observed in B's `SubscribeMessages` stream as a decrypted payload; not observed in C's stream.
6. Killing the originator and bringing up a fourth onboardee still succeeds (the fourth pulls from one of the remaining two).
7. Killing the Indexer mid-flight only flips the diagnostic connection fields: existing health remains unchanged, event delivery pauses, and neither an existing nor a fresh sidecar starts an RPC log scan.
8. The focused fake-Indexer transport test delivers a signed `MessageSent`, proves its Ack happens only after handler completion, and verifies checkpoint Ack/diagnostics.
9. A rejecting JSON-RPC fixture allows current-view reconciliation but rejects every `eth_getLogs`; boot reconciliation succeeds with zero log calls.

No fuzz tests for v1. No mainnet-fork tests. No actual dstack hardware tests; those are milestone B.

---

## 17. Open questions

1. **`SubscribeMessages` catchup boundary.** Currently the sidecar's queue is "since process start." App restarts lose history. Should the sidecar persist incoming messages to a sealed disk store keyed by `(envelopeId, block_number)` so app restarts can replay? Adds storage; v1 leaves out. Application must dedup if it relies on idempotency.
2. **Heartbeat packet encoding stability.** v1 uses `serde_cbor` via `ciborium` with a pinned canonical encoder. CBOR has multiple valid encodings of the same logical value, and the heartbeat's Ed25519 signature is over the encoded bytes — the canonicalization is what guarantees verification works across encoder versions.
3. **Multiple cluster membership.** What if one node is a member of two AttestMesh clusters (e.g. a hypothetical Indexer dog-fooding scenario where the Indexer cluster's members are also subscribers to the customer clusters)? v1 sidecar is single-cluster only; this is a milestone B+ shape.
4. **Bundler provider failover.** v1 ships single-provider (Alchemy). If Alchemy's bundler is down, the node cannot submit any UserOps and stays unable to send messages until it recovers. Milestone B adds Pimlico / Stackup as fallback endpoints, with health-checked round-robin.
