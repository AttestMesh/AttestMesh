# TeeMesh

On-chain coordination layer for clusters of TEE-based confidential VMs.

TeeMesh is what's left of [dstackgres](https://github.com/TeeSQL/dstackgres) once you remove everything Postgres-specific: a diamond-pattern cluster contract for mutually-attested CVM membership, encrypted member-to-member messaging, wireguard signalling, and a CVM-side sidecar that handles registration, key derivation, and mesh bring-up.

Any application that needs a mesh of TEE-attested peers — a database cluster, a private inference net, a Tendermint-style consensus group — can deploy a TeeMesh cluster contract, drop the TeeMesh sidecar into its CVM image, and get attestation, identity, and wireguard mesh for free.

## Architecture at a glance

- **ClusterDiamond** (ERC-2535 proxy) — the on-chain cluster contract. Surface is split into two layers:
  - **Core facets** (always installed, platform-agnostic):
    - **AttestFacet** — canonical "who is in this cluster" registry. Holds member records (TEE-derived x25519 pubkey, wg pubkey, metadata) and exposes `isClusterMember` to the rest of the diamond.
    - **MessageFacet** — gated by cluster membership. Members send sealed-box-encrypted messages to each other; payloads are perma-stored on chain via events.
    - **NetworkFacet** — gated by cluster membership. Members publish wireguard public keys and read peers' keys for mesh setup.
  - **Platform facets** (one per TEE platform; cluster picks which to install via `diamondCut`):
    - **DstackFacet** — ships in v1. Implements dstack's `IAppAuth` + `IAppAuthBasicManagement` so existing dstack tooling (phala-cli, dashboards, the dstack KMS) works unchanged. Verifies the dstack KMS signature chain and writes admitted members into AttestFacet's storage.
    - **IntelTdxFacet / AmdSnpFacet / NvidiaCcFacet** — future. Each adds a new TEE platform without touching the core facets.
- **ClusterMember** — per-CVM passthrough proxies, one per CVM. Address is deterministic and is what the TEE attestation chain commits to. Forwards a small fixed selector set into the diamond.
- **Indexer** — a shared TEE-attested off-chain service that watches all cluster contracts on a chain, pairs each event with a TEE-signed attestation and an RPC repro stub, and pushes events only to the members of the cluster that emitted them. Members trust the Indexer for liveness/completeness; correctness stays independently verifiable per event. Follows the dstackgres monitoring-hub pattern.
- **CVM sidecar** (Rust) — boots inside every CVM, derives identity / messaging / wireguard keys from a single Curve25519 TEE seed (x25519 for sealed-box, Ed25519 for heartbeat signatures), commits those pubkeys into the attestation quote's user-data slot, registers with the appropriate platform facet, subscribes to the Indexer, exchanges endpoint info via MessageFacet, brings up the wireguard mesh, runs heartbeats, and only reports healthy once the mesh is converged.

See [`docs/specs/teemesh-coordination-layer.md`](docs/specs/teemesh-coordination-layer.md) for the full design.

## Status

Pre-alpha. The contracts are being extracted from dstackgres; the sidecar is being built fresh. There are open design questions tracked at the end of the master spec.

## Repo layout

```
contracts/        Foundry workspace for the diamond, facets, and member contracts
sidecar/          Rust workspace for the CVM-side compose package
docs/specs/       Specifications (start with teemesh-coordination-layer.md)
docs/             Other design docs
.claude/commands/ Holodeck slash commands (/warmup, /spec, /generate, ...)
```

(`contracts/` and `sidecar/` will land as the first specs get generated.)

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
