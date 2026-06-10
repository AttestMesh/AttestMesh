# sidecar/

Rust workspace for `cluster-mesh-agent` — the per-node sidecar that runs inside every AttestMesh node.

Boots, derives attestation-bound identity keys (x25519 for sealed-box messaging, Ed25519 for off-chain heartbeat signatures, plus a wireguard keypair), registers with the cluster via the appropriate attestor facet, subscribes to the Indexer over gRPC, exchanges peer endpoints via MessageFacet, brings up the wireguard mesh, runs heartbeats, and reports healthy only when the mesh is converged.

**Spec**: [`docs/specs/sidecar.md`](../docs/specs/sidecar.md)
**Master spec**: [`docs/specs/attestmesh-coordination-layer.md`](../docs/specs/attestmesh-coordination-layer.md)

## Layout

- `src/keys.rs`, `src/dstack.rs` — identity derivation + the dstack runtime trait (UDS client + mock).
- `src/envelopes.rs`, `src/csk.rs` — libsodium sealed boxes, PeerEndpoint, CSK origination/pull/serve.
- `src/wg/` — deterministic mesh-IP allocation (cross-checked against the on-chain `meshIpOf`) + a swappable `MeshControl`.
- `src/heartbeat/` — signed UDP heartbeats + the first-convergence liveness calc.
- `src/chain/` — alloy read provider, EIP-4337 UserOp construction/hashing, bundler client, facet calldata builders, binding-hash (cross-checked against Solidity).
- `src/indexer_client.rs` — gRPC subscription + envelope-signature verification.
- `src/agent_grpc.rs`, `src/peer_grpc.rs`, `src/health.rs`, `src/state/` — the app façade (UDS), CSK peer-control, healthcheck, and bring-up state machine.
- `src/bringup.rs`, `src/transport/` — mesh bring-up orchestration (peers from chain, envelope exchange, heartbeats, CSK, gRPC servers) + the wireguard-over-TCP gateway transport.
- `src/transport/punch.rs` — UDP hole-punch link upgrader ([`docs/specs/udp-transport-upgrade.md`](../docs/specs/udp-transport-upgrade.md)): per-peer `tcp → punching → udp` state machine with automatic revert, peer-to-peer negotiation over `PeerControl`, and the UDP-path watchdog. Gateway TCP stays the bootstrap path and permanent fallback. Knobs: `WG_UDP_PUNCH` (default `true`), `PUNCH_TIMEOUT_SECS` (10), `PUNCH_RETRY_BACKOFF_SECS` (30, doubling, cap 3600).
- `proto/` — `indexer.proto` (shared with the indexer), `agent.proto`, `peer.proto`.

The `MeshControl` trait abstracts wireguard (command-based impl + mock) so the crate builds and unit-tests without a kernel; swap in a netlink impl for production.

## Quick reference

```bash
cargo build --release
cargo test                    # 75 unit tests (live on Base mainnet — see docs/deployment.md + docs/specs/sidecar.md §1.1)
cargo clippy -- -D warnings
cargo fmt --check
```
