# dstack Auth Extraction (the real fix — port dstackgres, don't reinvent)

**Status:** DRAFT
**Author:** LSDan
**Created:** 2026-06-03
**Last Updated:** 2026-06-03
**Supersedes:** `dstack-registration-rework.md` and `dstack-attested-quorum-membership.md` (both invented designs; killed by their own premortems)
**Source of truth for the port:** `TeeSQL/dstackgres` (cloned at `/tmp/dstackgres`) — `contracts/teesql-group-auth`, `crates/sqlx-ra-tls`, `crates/teesql-data-sidecar`, `services/gas-sponsorship-webhook`.

## Overview

The master spec always framed AttestMesh as **dstackgres's auth layer, extracted and rebranded** (§1, §10) — the part of dstackgres that has nothing to do with Postgres. The two prior fix attempts reinvented that layer from scratch and were killed by their own premortems (a self-asserted compose hash; a non-existent KMS app-key model; a Sybil-open quorum; a verifier assumed reusable that isn't). The mistake was reinvention. **dstackgres already contains the complete, working, on-chain-verified, owner-gated attestation membership system this project needs** — including the exact pieces the premortems flagged as missing.

This spec is a **porting/extraction plan**, not a new design. It maps dstackgres's proven components onto AttestMesh, applies the rebrand, and removes the Postgres/control-plane/leader-lease parts the master spec already excludes. The on-chain trust model it lands is exactly the one decided in review: an **owner-managed on-chain allowlist** (Sybil authority), the **dstack KMS as the boot gate** (`IBootGate`/`clusterBootPolicy`, out-of-the-box), and **on-chain verification of the dstack KMS sig chain at registration** (`dstack_kms_verifySigChain`) — defense in depth, no single process admits a node.

Concretely, `DstackKmsAdapterFacet._verifySigChain` already implements the real dstack format the reinvention got wrong:
- KMS root signs `keccak256("dstack-kms-issued:" ‖ bytes20(codeId) ‖ appCompressedPubkey)` → recovered signer must be in `allowedKmsRoots`;
- app key signs `keccak256(purpose ‖ ":" ‖ derivedPubkeyHex)`; derived key signs the EIP-191 message; the recovered addresses must chain.
This is the "lightly verify the KMS" path — and it's done, tested, and correct.

## Requirements

### Must Have
- [ ] **Port the on-chain auth diamond** from `contracts/teesql-group-auth/src` into `contracts/src`, rebranded (`teesql.*`→`attestmesh.*`; vocab per the rename: *attestor facet / attestorId / node*). Specifically: `CoreFacet` (register/onboarding/endpoints), `BootGateFacet` (the `IAppAuth`/`clusterBootPolicy` boot gate), `WgMeshFacet` (attested wg-pubkey signalling), `AdminFacet`, `ViewFacet`, the `DstackKmsAdapterFacet`/`DstackAttestationAdapterFacet` verification, the `libraries/DstackSigChain.sol` (the **real** `Proof` shape + `recover`/`compressedToAddress`), `members/DstackMember.sol` (→ `ClusterMember`), and the storage layouts (`KmsDstackStorage`, `AllowlistsStorage`, `MemberStorage`, `WgMeshStorage`, `LifecycleStorage`, `CoreStorage`, factory storage).
- [ ] **Replace AttestMesh's invented `DstackFacet`/`DstackSigChain`/`dstack_register`** with the ported `CoreFacet.register(RegisterArgs)` + `dstack_kms_verifySigChain` flow. Delete the reinvented sig-chain code (`contracts/src/libraries/DstackSigChain.sol`, `facets/attestor/DstackFacet.sol`'s `dstack_register`).
- [ ] **Fold the verifier into the attestor facet** (master §10: "verifier as facet, not external contract; no adapter registry"). Port `_verifySigChain` directly into the AttestMesh attestor facet rather than via the `AdapterRegistry` indirection.
- [ ] **Port the off-chain verifier crate** `crates/sqlx-ra-tls` → `sidecar` (or a shared crate): the reusable `DcapVerifier` (`verify_with_collateral`, `with_pccs_url`), `AttestationVerifier`, `VerifiedAttestation`, and `compute_report_data`. This is the peer/RA-TLS DCAP verification for the data-plane handshake — and it refutes the premortem's "no reusable verifier" finding.
- [ ] **Port the sidecar's gas-payment** `crates/teesql-data-sidecar/.../gas_payment/{alchemy.rs,userop.rs}` to replace `sidecar/src/chain/bundler.rs` — this is the **F2 fix**: it calls `alchemy_requestGasAndPaymasterAndData` to get the gas + paymaster fields **before** computing the userOpHash and signing (LightAccount v2 sig-type byte), so the signed hash matches the on-chain `validateUserOp`.
- [ ] **Port the dstack runtime client** `teesql-data-sidecar/.../dstack.rs` (the real prpc/ra-rpc guest-agent client) → replace AttestMesh's guessed `UnixSocketDstack`.
- [ ] **Port the onboarding/registration + admission + attestation modules** (`group_auth.rs`, `onboarding.rs`, `admission.rs`, `attestation.rs`, `client_verify.rs`) → AttestMesh sidecar registration + the peer-verification handshake.
- [ ] **Port the CSK** from `cluster_secrets.rs`/`cluster_secrets_writer.rs` → replace AttestMesh's `csk.rs` (and confirm the persistence target = the LUKS data disk, per the earlier finding).
- [ ] **Port originator/genesis** from `cold_start_election.rs` + `cold_start_witness*.rs` → resolve the originator-race + genesis-bootstrap (premortem F6) with dstackgres's proven election.
- [ ] **Port the encrypted endpoint rendezvous** `endpoint_crypto.rs` (+ `wg_relay.rs`) → the pre-membership rendezvous (decision #2: a freshly-registered member encrypts its endpoint to each peer over the message channel; peers dial out).
- [ ] **Eviction via owner action + on-chain event**: port the `LifecycleStorage`/lifecycle handling so an owner blacklist/removal emits an event members watch (via the Indexer) and tear down the offending wg peer (decision #5).

### Should Have
- [ ] **Port the dstackgres test suites** that cover this surface (`tests/integration/multisig/*`, `crates/sqlx-ra-tls/tests/{dcap_roundtrip,pubkey_binding}.rs`) and the data-sidecar registration tests, adapted to AttestMesh.
- [ ] **Reconcile the Indexer** with dstackgres's `monitoring-hub` (its event-distribution analog) rather than maintaining a divergent design.
- [ ] **Reconcile the gas-webhook**: the already-built `services/gas-sponsorship-webhook` was a partial port; diff it against dstackgres's `services/gas-sponsorship-webhook` and align.

### Must NOT Have (per master §10 — what AttestMesh deliberately drops from dstackgres)
- `ControlPlaneFacet` / the off-chain control-plane action-authorization (master §10 omits it).
- Leader lease / `LeaderClaimed` (master §10: leader election is application-level).
- Any Postgres/DNS specifics (`dnsLabel`, WAL/backup modules, `pg_up_monitor`, `postgres.rs`, `backup_*`, `wal_*`).
- The `AdapterRegistry` indirection (fold the verifier into the attestor facet).
- The peer **quorum as a hard gate** — per decision #1 it is dropped (or reduced to per-peer RA-TLS verification at the mesh handshake). The gates are: owner allowlist + dstack KMS boot gate + on-chain `verifySigChain`. The peer handshake verifies, it does not vote.

## Non-Requirements

Not re-litigating the architecture (decided: owner-gated, IAppAuth + KMS, defense-in-depth). Not porting dstackgres's Postgres product. Not a new on-chain DCAP-quote verifier (the `verifySigChain` light path + off-chain `sqlx-ra-tls` cover it). The EIP-4337 paymaster fix (F2), the Indexer event-decode (sidecar §9), and the heartbeat/convergence remain AttestMesh-side, informed by the corresponding dstackgres modules.

## Design — component map

| AttestMesh target | dstackgres source | Adaptation |
|---|---|---|
| `contracts/src/facets/.../CoreFacet` (register/onboard/endpoints) | `contracts/teesql-group-auth/src/facets/CoreFacet.sol` | rebrand; drop leader-lease + dnsLabel; keep `register(RegisterArgs)`→`verifySigChain`→`_addMember` |
| attestor facet (verifier) | `facets/dstack/DstackKmsAdapterFacet.sol` + `DstackAttestationAdapterFacet.sol` | fold in directly (no AdapterRegistry); keep `_verifySigChain` + `allowedKmsRoots`/`addRoot`/`registerApp` |
| `libraries/DstackSigChain.sol` (real) | `contracts/.../libraries/DstackSigChain.sol` | **replace** AttestMesh's invented one; this `Proof` shape + preimages are correct |
| `BootGateFacet` (`IAppAuth`/`clusterBootPolicy`) | `facets/BootGateFacet.sol` + `interfaces/IBootGate.sol` | rebrand; this is the dstack KMS boot gate |
| `WgMeshFacet` (attested wg signalling) | `facets/WgMeshFacet.sol` | keep `setMemberWgPubkeyAttested(memberId, quoteHash, tdxQuote)` + relayed variant; drop deprecated plain setter |
| `AdminFacet`/`ViewFacet` | `facets/{AdminFacet,ViewFacet}.sol` | rebrand; admin = owner allowlist management (compose hashes, devices, KMS roots) |
| `ClusterMember` | `members/DstackMember.sol` | rebrand; owner = dstack-derived key (decision #3) |
| storage | `storage/{KmsDstack,Allowlists,Member,WgMesh,Lifecycle,Core,Factory}Storage.sol` | rebrand; ERC-7201 namespaces under `attestmesh.storage.*` |
| `sidecar` off-chain verifier | `crates/sqlx-ra-tls` (`DcapVerifier`, `AttestationVerifier`, `compute_report_data`) | port as a crate; drop sqlx/postgres specifics |
| `sidecar/src/chain/bundler.rs` | `crates/teesql-data-sidecar/.../gas_payment/{alchemy,userop}.rs` | **the F2 fix** — sponsor-then-sign, LightAccount v2 sig byte |
| `sidecar/src/dstack.rs` | `.../sidecar/src/dstack.rs` | real prpc guest-agent client (replaces guessed JSON) |
| registration/onboarding | `group_auth.rs`, `onboarding.rs`, `admission.rs` | the proof-building + register + admit flow |
| peer verification | `attestation.rs`, `client_verify.rs` (+ `sqlx-ra-tls`) | the mesh-handshake DCAP/KMS verification |
| `sidecar/src/csk.rs` | `cluster_secrets.rs`, `cluster_secrets_writer.rs` | the CSK; persist to the LUKS data disk |
| originator/genesis | `cold_start_election.rs`, `cold_start_witness*.rs` | replaces the racy `memberCount()` originator (premortem F6) |
| endpoint rendezvous | `endpoint_crypto.rs`, `wg_relay.rs` | decision #2 (encrypted endpoint over the message channel) |
| eviction reaction | `LifecycleStorage` + lifecycle facet/events | decision #5 (owner blacklist event → members drop the peer) |
| `services/gas-sponsorship-webhook` | `services/gas-sponsorship-webhook` | diff + align the already-built port |

### What of the already-built AttestMesh v1 survives
The diamond/factory scaffolding, the rebrand vocab, the ERC-7201 discipline, `MessageFacet` (sealed-box messaging — AttestMesh's own, no dstackgres equivalent needed beyond endpoint_crypto), `NetworkFacet` (subsumed by `WgMeshFacet`), the Indexer (reconcile with monitoring-hub), the heartbeat/convergence (AttestMesh-specific; cross-check `pairing_check.rs`/`fence_watchdog.rs`/`degraded_mode.rs`). The mesh-IP derivation, sealed-box, and heartbeat unit tests already pass and stay.

## Open Questions

- [ ] **Peer-verification independence (decision #1 follow-through).** Light = on-chain `verifySigChain` only (trusts the KMS). Heavy = also off-chain `sqlx-ra-tls` DCAP at the mesh handshake (independent of the KMS). Recommend: on-chain `verifySigChain` for registration + `sqlx-ra-tls` at the wg handshake as defense-in-depth, since the code for both already exists.
- [ ] **WgMesh stores a `quoteHash` + `tdxQuote` on chain** (dstackgres's `setMemberWgPubkeyAttested`). Adopt as-is, or keep the quote off-chain (hash only)? dstackgres puts it on chain; default to matching it.
- [ ] **How much of the diamond to keep vs replace.** AttestMesh already shipped a diamond with passing tests; decide whether to graft the dstackgres facets onto it or replace the facet set wholesale. Recommend: replace the attestor + core registration surface (where the reinvention lives), keep the solidstate base + Message/mesh-IP.
- [ ] **Indexer vs monitoring-hub** convergence — separate reconcile task.
- [ ] **Licensing/provenance.** dstackgres → AttestMesh is intra-org extraction (the stated project origin); preserve SPDX headers and attribution as required.

## Alternatives Considered

- **Reinvent (the two prior specs).** Rejected: both were killed by premortems for problems dstackgres already solved. Reinvention re-derived (badly) what exists.
- **On-chain DCAP quote verification.** Unnecessary given the proven `verifySigChain` light path + off-chain `sqlx-ra-tls`; remains a far-future option.

## Validation

1. Port `DstackSigChain` + the attestor facet, then a forge test replaying a **real** dstackgres-format proof (the dstackgres tests already carry fixtures) passes against the ported facet.
2. The ported `sqlx-ra-tls` `dcap_roundtrip`/`pubkey_binding` tests pass in the AttestMesh tree.
3. The ported `gas_payment/alchemy.rs` lands one sponsored UserOp on Sepolia (closes F2).
4. The ported `cold_start_election` + `endpoint_crypto` bring up a local multi-node mesh (closes the originator-race + rendezvous gaps).

## Traceability

*Filled in during implementation.*

| Requirement | Implementation | Tests |
|---|---|---|

## Changelog

| Date | Author | Changes |
|---|---|---|
| 2026-06-03 | LSDan | Initial draft — extract/port dstackgres's `teesql-group-auth` + `sqlx-ra-tls` + data-sidecar + gas-payment instead of reinventing; supersedes both invented specs. |
