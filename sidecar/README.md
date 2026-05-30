# sidecar/

Rust workspace for `cluster-mesh-agent` — the per-CVM sidecar that runs inside every TeeMesh CVM.

Boots, derives TEE-attested identity keys (x25519 for sealed-box messaging, Ed25519 for off-chain heartbeat signatures, plus a wireguard keypair), registers with the cluster via the appropriate platform facet, subscribes to the Indexer over gRPC, exchanges peer endpoints via MessageFacet, brings up the wireguard mesh, runs heartbeats, and reports healthy only when the mesh is converged.

**Spec**: [`docs/specs/sidecar.md`](../docs/specs/sidecar.md)
**Master spec**: [`docs/specs/teemesh-coordination-layer.md`](../docs/specs/teemesh-coordination-layer.md)

Code lands here when the sidecar spec is generated.

## Quick reference

```bash
cargo build --release
cargo test
cargo clippy -- -D warnings
```
