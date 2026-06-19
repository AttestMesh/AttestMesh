# sidecar/

Rust workspace for `cluster-mesh-agent` — the per-node sidecar that runs inside every AttestMesh node.

Boots, derives attestation-bound identity keys (x25519 for sealed-box messaging, Ed25519 for off-chain heartbeat signatures, plus a wireguard keypair), registers with the cluster via the appropriate attestor facet, subscribes to the Indexer over gRPC, exchanges peer endpoints via MessageFacet, brings up the wireguard mesh, runs heartbeats, and reports healthy only when the mesh is converged.

**Spec**: [`docs/specs/sidecar.md`](../docs/specs/sidecar.md)
**Master spec**: [`docs/specs/attestmesh-coordination-layer.md`](../docs/specs/attestmesh-coordination-layer.md)

## Attestation methods & trust model

Everything attestation-method-specific sits behind the `AttestationProvider` trait (`src/attestor/`), selected by `ATTESTOR=dstack|operator` (default `dstack`, multi-attestor spec):

- **dstack** (`src/attestor/dstack.rs`) — wraps the existing dstack runtime flow verbatim: TEE-attested key derivation, KMS sig-chain proof, sealed store, CSK origination.
- **operator** (`src/attestor/operator.rs`) — **NOT hardware attestation.** Keys derive from a local seed file (`ATTESTOR_SEED_PATH`, generated on first boot, mode 0600) and admission rests on an allowlisted operator's signature (`OPERATOR_VOUCHER`, minted by the `mesh-voucher` bin — one provisioning run yields seed + voucher together). The provider logs a prominent warning at boot. Operator nodes can be CSK **onboardees** but never the **originator** (v1 gates origination to dstack), and have no sealed store (the CSK is re-pulled after a restart).

## Layout

- `src/attestor/` — the `AttestationProvider` seam (`dstack` / `operator` providers); `src/bin/mesh_voucher.rs` — voucher-minting CLI.
- `src/keys.rs`, `src/dstack.rs` — identity derivation + the dstack runtime trait (UDS client + mock).
- `src/envelopes.rs`, `src/csk.rs` — libsodium sealed boxes, PeerEndpoint, CSK origination/pull/serve.
- `src/wg/` — deterministic mesh-IP allocation (cross-checked against the on-chain `meshIpOf`) + a swappable `MeshControl`.
- `src/heartbeat/` — signed UDP heartbeats + the first-convergence liveness calc.
- `src/chain/` — alloy read provider, EIP-4337 UserOp construction/hashing, bundler client, facet calldata builders, binding-hash (cross-checked against Solidity).
- `src/indexer_client.rs` — gRPC subscription + envelope-signature verification.
- `src/agent_grpc.rs`, `src/peer_grpc.rs`, `src/health.rs`, `src/state/` — the app façade (UDS), CSK peer-control, healthcheck, and bring-up state machine.
- `src/bringup.rs`, `src/transport/` — mesh bring-up orchestration (peers from chain, envelope exchange, heartbeats, CSK, gRPC servers) + the wireguard-over-TCP gateway transport.
- `proto/` — `indexer.proto` (shared with the indexer), `agent.proto`, `peer.proto`.

The `MeshControl` trait abstracts wireguard (command-based impl + mock) so the crate builds and unit-tests without a kernel; swap in a netlink impl for production.

## Quick reference

```bash
cargo build --release
cargo test                    # 60 unit tests (live on Base mainnet — see docs/deployment.md + docs/specs/sidecar.md §1.1)
cargo clippy -- -D warnings
cargo fmt --check
```
