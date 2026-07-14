# AttestMesh

On-chain coordination layer for meshes of mutually-attested nodes.

AttestMesh is what's left of [dstackgres](https://github.com/TeeSQL/dstackgres) once you remove everything Postgres-specific: a diamond-pattern cluster contract for mutually-attested node membership, encrypted member-to-member messaging, wireguard signalling, and a node-side sidecar that handles registration, key derivation, and mesh bring-up.

Any application that needs a mesh of attested peers — a database cluster, a private inference net, a Tendermint-style consensus group — can deploy an AttestMesh cluster contract, drop the AttestMesh sidecar into its node image, and get attestation, identity, and wireguard mesh for free.

**User documentation:** [attestmesh-docs.pages.dev](https://attestmesh-docs.pages.dev) ([source](https://github.com/AttestMesh/docs))

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
- **Indexer** — a shared attested off-chain service that watches all cluster contracts on a chain, pairs each event with a signed attestation and an RPC repro stub, and pushes events only to the members of the cluster that emitted them. ONE instance serves every cluster on the chains it watches — never one per cluster. Members trust the Indexer for liveness/completeness; correctness stays independently verifiable per event.
- **Cluster Shared Key (CSK)** — a single 32-byte symmetric key every member of a cluster holds. Derived deterministically by the first member to register (the "originator"); the only thing that touches the chain is a one-time `keccak256(CSK)` commitment. Subsequent members pull the CSK peer-to-peer over the wireguard mesh from a member that already holds it — sealed-boxed to the requester's key and verified against the on-chain commitment. The cluster contract never sees the plaintext. Exposed to the application container through the sidecar gRPC; application uses it for whatever cluster-wide encryption it needs.
- **Node sidecar** (Rust) — boots inside every node, derives identity / messaging / wireguard keys from an attestation-bound seed (x25519 for sealed-box, Ed25519 for heartbeat signatures), registers with the appropriate attestor facet via a sponsored UserOp, enumerates peers from chain state, brings up the wireguard mesh over the dstack gateway's TCP path (no STUN, no rendezvous), exchanges endpoint envelopes via MessageFacet, runs signed heartbeats, acquires the CSK, subscribes to the Indexer, and only reports healthy once the mesh is converged and the CSK is held.

See [`docs/specs/attestmesh-coordination-layer.md`](docs/specs/attestmesh-coordination-layer.md) for the full design.

## Status

**Live on Base mainnet (chain id 8453).** All four components — contracts, sidecar, indexer, and gas-webhook — are deployed and verified working end-to-end: real dstack CVMs self-register through sponsored UserOperations, form a wireguard mesh through the dstack gateway, exchange encrypted endpoint envelopes, distribute the CSK peer-to-peer, and hold verified subscriptions to the attested indexer. 222 tests green across the four suites (contracts at 94% line / 76% branch coverage).

- Live addresses: [`contracts/script/deployments/8453.json`](contracts/script/deployments/8453.json) (reference cluster `0xA46273adC86c772C7D8daE896a5fbfdDA2B6ccFA`)
- Deployment runbook + status log: [`docs/deployment.md`](docs/deployment.md)
- The ten live-only bugs it took to get there: [field notes](https://attestmesh-docs.pages.dev/reference/field-notes/)

Deferred to milestone B: the pure-UDP hole-punch transport upgrade (two-sided punch verified live; gateway-TCP bootstrap carries the mesh today), additional attestor facets, indexer HA, and security review.

## Repo layout

AttestMesh is a monorepo. The protocol, on-chain primitives, node sidecar, and Indexer service all live in one tree so they evolve atomically.

```
contracts/        Foundry workspace: diamond, core facets, attestor facets, ClusterMember (dstack + 4337), factories, IndexerRegistry
sidecar/          Rust workspace: cluster-mesh-agent (the per-node sidecar)
indexer/          Rust workspace: the attested event indexer (shared, one per chain-set)
services/
  gas-sponsorship-webhook/   Cloudflare Worker: validates EIP-4337 paymaster sponsorship for AttestMesh UserOps
deploy/           Idempotent bash routines + the smithers workflow that orchestrates them
docs/specs/       Specifications (start with attestmesh-coordination-layer.md)
docs/deployment.md  Live-deployment runbook and dated status log
docs/audits/      Project audit reports (latest only; git history holds previous)
.claude/commands/ Holodeck slash commands (/warmup, /spec, /generate, ...)
```

## Build

```bash
forge build                  # contracts
cargo build --release        # sidecar  (also: cargo build in indexer/)
npm --prefix services/gas-sponsorship-webhook ci   # gas-webhook
```

Container images for the sidecar and indexer are built and published to ghcr.io by GitHub Actions on push (`.github/workflows/build-{sidecar,indexer}.yml`).

## Test

```bash
( cd contracts && forge test -vvv )                       # 57 tests
( cd sidecar  && cargo test )                             # 50 tests
( cd indexer  && cargo test )                             # 42 tests
( cd services/gas-sponsorship-webhook && npm test )       # 73 tests
```

## Deploy

The full bring-up — contracts, webhook, shared indexer, two nodes, mesh verification — is one durable [smithers](https://smithers.sh) workflow:

```bash
source deploy/env.sh
bunx smithers-orchestrator up deploy/workflows/deploy.tsx
```

Each step shells out to an idempotent, logged routine in `deploy/` that can also be run standalone (`onchain.sh`, `webhook.sh`, `indexer.sh`, `node-pathA.sh` — including day-2 `update` / `restart` / `mesh-verify`). See [`docs/deployment.md`](docs/deployment.md) for the runbook and the [getting-started guide](https://attestmesh-docs.pages.dev/guides/getting-started/) for a walkthrough.

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

Install the repository's staged-only security hook once per clone:

```bash
deploy/install-precommit-security.sh
```

Every commit then rejects backup artifacts and high-confidence credential shapes locally before
running an ephemeral Smithers review with `gpt-5.6-sol` at Ultra reasoning. The model receives only
a locally redacted snapshot of added lines in Git's index; unstaged files, deleted text, and the
worktree are excluded. See [`docs/precommit-security.md`](docs/precommit-security.md).

See [`CLAUDE.md`](CLAUDE.md) for development guidelines.

## License

TBD.
