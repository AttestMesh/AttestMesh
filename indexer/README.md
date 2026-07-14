# indexer/

Rust workspace for the AttestMesh Indexer — an attested off-chain service that watches every ClusterDiamond it has been asked to follow, pairs each event with a signed envelope and an RPC repro stub, and pushes events only to the members of the cluster that emitted them.

Each Indexer worker serves many workload clusters. Members subscribe over bidirectional gRPC. Protocol v3 makes the sidecar's exact, crash-durable `(blockNumber, logIndex)` cursor authoritative across reconnects, including when the stable LB selects a different worker. Protocol-v2 checkpoint and instance-mode behavior remain supported.

**Spec**: [`docs/specs/indexer.md`](../docs/specs/indexer.md)
**Master spec**: [`docs/specs/attestmesh-coordination-layer.md`](../docs/specs/attestmesh-coordination-layer.md)

Implemented: block-watcher (`eth_getLogs` polling + per-member relevance filtering), signed-envelope gRPC with checkpoints and exact resume cursors, sled-backed replica-local cursors, sidecar-owned durable resume state, dual identity modes, registry-gated shared serving, and health/read-model HTTP endpoints.

Identity modes:

- `INDEXER_IDENTITY=instance` is the default and preserves the deployed per-CVM signing key and single-worker blue/green flow.
- `INDEXER_IDENTITY=cluster-shared` is the Stage A worker-HA mode. It requires a fresh, dedicated `INDEXER_CLUSTER_ADDR` and a co-located sidecar `AGENT_GRPC_ADDR` UDS, derives one generation key with `HKDF-SHA512(CSK, info="attestmesh.indexer.signing.v2")`, obtains the measured code ID from dstack `/Info`, and keeps gRPC closed until the unchanged v1 registry matches both key and code ID.

Shared-mode replica quotes are diagnostic, not per-replica authentication: a quote binding the shared key cannot prove which physical worker served a stream. Stage A also leaves the stable LB as a front-door single point of failure.

## Quick reference

```bash
cargo build --release
cargo test
cargo clippy --all-targets -- -D warnings
cargo fmt --all -- --check
```

## Deployment status

The instance-mode single CVM is **live on Base mainnet**, registered in the IndexerRegistry contract for chain id 8453 (see [`docs/deployment.md`](../docs/deployment.md)). It is shared infrastructure: one worker serves every workload cluster on the chain, never one worker per cluster.

Protocol v3 and the Stage A cluster-shared worker pool are implemented in code, but their production rollout is pending. Protocol-v3 sidecars must roll out before the shared pool is enabled. The pool reuses the stable registry/LB endpoint and independent worker stores; sidecar exact cursors provide continuity across workers without a shared cursor database.
