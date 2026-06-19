# AttestMesh Development Guide

## Claude Expertise Profile

Expert smart-contract engineer specializing in ERC-2535 diamond proxies, OpenZeppelin/Solidstate patterns, and attestation chains (TEE today via dstack/Phala, other methods later). Equally fluent in Rust systems programming for confidential VMs, dstack/Phala attestation, KMS-derived key flows, and wireguard mesh networking. Pragmatic, security-minded, and ruthless about minimizing trust assumptions and avoiding attestation-method-specific coupling outside the relevant attestor facet.

---

## CRITICAL DIRECTIVES

- **No Claude attribution** in commits, PRs, or files.
- **No attestation-method-specific assumptions outside that method's facet** — on-chain, only the relevant attestor facet may know a method's details; sidecar-side, only that method's provider module may. The rest of the system stays attestation-method-agnostic so future attestor facets (Intel TDX direct, AMD SEV-SNP, NVIDIA CC, and non-TEE attestation methods) can be added without touching core.
- **No off-chain coordination required for mesh bring-up** — the chain is the source of truth for membership, attestation policy, and peer endpoints.
- **No plaintext member-to-member messages on chain** — all `MessageFacet` payloads must be encrypted to the recipient's registered public key.
- **No secrets, keys, or `.env` files in source control.**
- **No breaking interface changes to deployed facets** without an explicit migration spec.

---

## Project Overview

**AttestMesh** - On-chain coordination layer for meshes of mutually-attested nodes — a diamond-pattern cluster contract, member registration via attestation, encrypted member-to-member messaging, and wireguard peer signalling, plus a node-side compose package that handles registration, key derivation, and mesh bring-up.

### Core Components

| Component | Description | Location | Spec |
|-----------|-------------|----------|------|
| Contracts | Foundry workspace: ClusterDiamond, core/attestor facets, ClusterMember (dstack + EIP-4337), factories, IndexerRegistry | `contracts/` | [`docs/specs/contracts.md`](docs/specs/contracts.md) |
| Sidecar | Rust binary `cluster-mesh-agent` running inside every node: key derivation, registration, Indexer subscription, wireguard mesh, heartbeats, app-facing gRPC | `sidecar/` | [`docs/specs/sidecar.md`](docs/specs/sidecar.md) |
| Indexer | Rust service `attestmesh-indexer` (attested): watches every cluster on the chain, pushes signed events with RPC-repro stubs to subscribed members | `indexer/` | [`docs/specs/indexer.md`](docs/specs/indexer.md) |
| Gas webhook | Cloudflare Worker (TypeScript) gating Alchemy's EIP-4337 paymaster sponsorship for AttestMesh UserOps | `services/gas-sponsorship-webhook/` | [`docs/specs/gas-webhook.md`](docs/specs/gas-webhook.md) |
| Matrix-admin agent | Python agent co-located on Matrix-node CVMs: executes the full Synapse admin surface from on-chain member commands (via the sidecar app gRPC) and from an LLM bot in Matrix; deny-all egress except a single pinned LLM | own repo `AttestMesh/matrix-admin-agent` + `deploy/compose/matrix-node.yaml` | [`docs/specs/matrix-admin-agent.md`](docs/specs/matrix-admin-agent.md) |

---

## Build Commands

- Contracts: `forge build`
- Node compose package: `cargo build --release`

---

## Test Commands

- Contracts: `forge test -vvv`
- Node compose package: `cargo test`

---

## Lint Commands

- Contracts: `forge fmt --check`
- Node compose package: `cargo clippy -- -D warnings`

---

## Slash Commands

| Command | Description |
|---------|-------------|
| `/warmup` | Initialize session context |
| `/spec` | Create and refine specifications |
| `/generate` | Generate code from specifications |
| `/doc` | Edit documents with collaborative refinement |
| `/reconcile` | Sync specs, code, and documentation |
| `/audit` | Project health check |

---

## Development Workflow

1. Create specification: `/spec create <feature>`
2. Refine until approved: `/spec refine docs/specs/<feature>.md`
3. Generate implementation: `/generate docs/specs/<feature>.md`
4. Document: `/doc README.md` or `/doc docs/<file>.md`
5. Verify consistency: `/reconcile all`
6. Health check: `/audit`
