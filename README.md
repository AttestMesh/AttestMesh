# AttestMesh

On-chain coordination layer for meshes of mutually-attested nodes.

AttestMesh is what's left of [dstackgres](https://github.com/TeeSQL/dstackgres) once you remove everything Postgres-specific: a diamond-pattern cluster contract for mutually-attested node membership, encrypted member-to-member messaging, wireguard signalling, and a node-side sidecar that handles registration, key derivation, and mesh bring-up.

Any application that needs a mesh of attested peers — a database cluster, a private inference net, a Tendermint-style consensus group — can deploy an AttestMesh cluster contract, drop the AttestMesh sidecar into its node image, and get attestation, identity, and wireguard mesh for free.

## Architecture at a glance

- **ClusterDiamond** (ERC-2535 proxy) — the on-chain cluster contract. Surface is split into two layers:
  - **Core facets** (always installed, attestation-method-agnostic):
    - **AttestFacet** — canonical "who is in this cluster" registry. Holds member records (attestation-bound x25519 pubkey, wg pubkey, metadata) and exposes `isClusterMember` to the rest of the diamond.
    - **MessageFacet** — gated by cluster membership. Members send sealed-box-encrypted messages to each other; payloads are perma-stored on chain via events.
    - **NetworkFacet** — gated by cluster membership. Members publish wireguard public keys and read peers' keys for mesh setup.
  - **Attestor facets** (one per attestation method; cluster picks which to install via `diamondCut`):
    - **DstackFacet** — ships in v1. Implements dstack's `IAppAuth` + `IAppAuthBasicManagement` so existing dstack tooling (phala-cli, dashboards, the dstack KMS) works unchanged. Verifies the dstack KMS signature chain and writes admitted members into AttestFacet's storage.
    - **IntelTdxFacet / AmdSnpFacet / NvidiaCcFacet** — future. Each adds a new attestation method without touching the core facets.
- **ClusterMember** — one per node, combining two roles on a single deterministic address: a dstack-style app proxy (so dstack tooling and the KMS recognize the node via `IAppAuth`) and an EIP-4337 smart wallet (so the sidecar submits gasless, paymaster-sponsored UserOps). The address is what the attestation chain commits to and what the diamond sees as `msg.sender`.
- **Indexer** — a shared attested off-chain service that watches all cluster contracts on a chain, pairs each event with a signed attestation and an RPC repro stub, and pushes events only to the members of the cluster that emitted them. Members trust the Indexer for liveness/completeness; correctness stays independently verifiable per event. Follows the dstackgres monitoring-hub pattern.
- **Cluster Shared Key (CSK)** — a single 32-byte symmetric key every member of a cluster holds. Derived deterministically by the first member to register (the "originator"); the only thing that touches the chain is a one-time `keccak256(CSK)` commitment. Subsequent members pull the CSK peer-to-peer over the wireguard mesh from a member that already holds it — sealed-boxed to the requester's key and verified against the on-chain commitment. The cluster contract never sees the plaintext. Exposed to the application container through the sidecar gRPC; application uses it for whatever cluster-wide encryption it needs.
- **Node sidecar** (Rust) — boots inside every node, derives identity / messaging / wireguard keys from a single Curve25519 attestation-bound seed (x25519 for sealed-box, Ed25519 for heartbeat signatures), commits those pubkeys into the attestation quote's user-data slot, registers with the appropriate attestor facet, subscribes to the Indexer, exchanges endpoint info via MessageFacet, brings up the wireguard mesh, runs heartbeats, and only reports healthy once the mesh is converged.

See [`docs/specs/attestmesh-coordination-layer.md`](docs/specs/attestmesh-coordination-layer.md) for the full design.

## Status

Pre-alpha. The contracts are being extracted from dstackgres; the sidecar is being built fresh. Resolved design decisions (and any remaining open questions) are tracked at the end of the master spec.

## Repo layout

AttestMesh is a monorepo. The protocol, on-chain primitives, node sidecar, and Indexer service all live in one tree so they evolve atomically.

```
contracts/        Foundry workspace: diamond, core facets, attestor facets, ClusterMember (dstack + 4337), factories, IndexerRegistry
sidecar/          Rust workspace: cluster-mesh-agent (the per-node sidecar)
indexer/          Rust workspace: the attested event indexer
services/
  gas-sponsorship-webhook/   Cloudflare Worker: validates EIP-4337 paymaster sponsorship for AttestMesh UserOps
docs/specs/       Specifications (start with attestmesh-coordination-layer.md)
docs/audits/      Project audit reports (latest only; git history holds previous)
.claude/commands/ Holodeck slash commands (/warmup, /spec, /generate, ...)
```

(Component directories land as the matching specs get generated.)

## Build

```bash
forge build                  # contracts
cargo build --release        # sidecar
```

## Test

```bash
forge test -vvv              # contracts
cargo test                   # sidecar
```

## Development workflow

This project uses Holodeck methodology — work flows through specs that live in `docs/specs/` and are generated, refined, and reconciled with code via slash commands:

```bash
/warmup                       # establish session context
/spec create <feature>        # draft a new spec
/spec refine docs/specs/<f>.md
/generate docs/specs/<f>.md   # generate implementation
/reconcile all                # sync spec / code / docs
/audit                        # health check
```

See [`CLAUDE.md`](CLAUDE.md) for development guidelines.

## License

TBD.
