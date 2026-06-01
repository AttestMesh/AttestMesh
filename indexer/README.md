# indexer/

Rust workspace for the AttestMesh Indexer — an attested off-chain service that watches every ClusterDiamond it has been asked to follow, pairs each event with its attestation-bound signed envelope and an RPC repro stub so members can verify independently, and pushes events only to the members of the cluster that emitted them.

One Indexer instance serves many clusters. Members subscribe over gRPC bidirectional streaming; subscriptions are stateful (per-member delivery cursors) so reconnects don't lose events.

**Spec**: [`docs/specs/indexer.md`](../docs/specs/indexer.md)
**Master spec**: [`docs/specs/attestmesh-coordination-layer.md`](../docs/specs/attestmesh-coordination-layer.md)

Code lands here when the indexer spec is generated.

## Quick reference

```bash
cargo build --release
cargo test
cargo clippy -- -D warnings
```

## v1 deployment

Single CVM instance on Base Sepolia, registered in the IndexerRegistry contract for chain id 84532. HA shape is a milestone B concern.
