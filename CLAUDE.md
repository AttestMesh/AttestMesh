# TeeMesh Development Guide

## Claude Expertise Profile

Expert smart-contract engineer specializing in ERC-2535 diamond proxies, OpenZeppelin/Solidstate patterns, and TEE attestation chains. Equally fluent in Rust systems programming for confidential VMs, dstack/Phala attestation, KMS-derived key flows, and wireguard mesh networking. Pragmatic, security-minded, and ruthless about minimizing trust assumptions and avoiding dstack-specific coupling outside the DstackFacet.

---

## CRITICAL DIRECTIVES

- **No Claude attribution** in commits, PRs, or files.
- **No dstack-specific assumptions outside the DstackFacet** — the rest of the contract must be platform-agnostic so future facets (Intel TDX direct, AMD SEV-SNP, NVIDIA confidential GPU, etc.) can be added without touching core.
- **No off-chain coordination required for mesh bring-up** — the chain is the source of truth for membership, attestation policy, and peer endpoints.
- **No plaintext member-to-member messages on chain** — all `MessageFacet` payloads must be encrypted to the recipient's registered public key.
- **No secrets, keys, or `.env` files in source control.**
- **No breaking interface changes to deployed facets** without an explicit migration spec.

---

## Project Overview

**TeeMesh** - On-chain coordination layer for TEE-based confidential VM meshes — a diamond-pattern cluster contract, member registration via attestation, encrypted member-to-member messaging, and wireguard peer signalling, plus a CVM-side compose package that handles registration, key derivation, and mesh bring-up.

### Core Components

| Component | Description | Location | Spec |
|-----------|-------------|----------|------|
| Contracts | Foundry workspace: ClusterDiamond, core/platform facets, ClusterMember (dstack + EIP-4337), factories, IndexerRegistry | `contracts/` | [`docs/specs/contracts.md`](docs/specs/contracts.md) |
| Sidecar | Rust binary `cluster-mesh-agent` running inside every CVM: key derivation, registration, Indexer subscription, wireguard mesh, heartbeats, app-facing gRPC | `sidecar/` | [`docs/specs/sidecar.md`](docs/specs/sidecar.md) |
| Indexer | Rust service `teemesh-indexer` (TEE-attested): watches every cluster on the chain, pushes signed events with RPC-repro stubs to subscribed members | `indexer/` | [`docs/specs/indexer.md`](docs/specs/indexer.md) |
| Gas webhook | Cloudflare Worker (TypeScript) gating Alchemy's EIP-4337 paymaster sponsorship for TeeMesh UserOps | `services/gas-sponsorship-webhook/` | [`docs/specs/gas-webhook.md`](docs/specs/gas-webhook.md) |

---

## Build Commands

- Contracts: `forge build`
- CVM compose package: `cargo build --release`

---

## Test Commands

- Contracts: `forge test -vvv`
- CVM compose package: `cargo test`

---

## Lint Commands

- Contracts: `forge fmt --check`
- CVM compose package: `cargo clippy -- -D warnings`

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
