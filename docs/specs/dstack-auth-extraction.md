# dstack Auth Extraction — SURGICAL scope (verification primitive only)

**Status:** DRAFT
**Author:** LSDan
**Created:** 2026-06-03
**Last Updated:** 2026-06-03
**Supersedes:** `dstack-registration-rework.md`, `dstack-attested-quorum-membership.md` (both invented; killed by premortem)
**Premortems:** `…-1780448684`, `…-1780453627`, `…-1780495534`, `…-1780498753` (the last one narrowed this spec from a wholesale port to a surgical extraction)
**Port reference (read-only):** `TeeSQL/dstackgres` at `/tmp/dstackgres` — `contracts/teesql-group-auth/.../DstackSigChain` + `DstackKmsAdapterFacet`, and `crates/teesql-data-sidecar/.../gas_payment/alchemy.rs`.

## Overview

The premortem of the wholesale port (`…-1780498753`) was decisive: dstackgres's auth is a 35k-line/46-module subsystem fused to Postgres, on a **mutually-exclusive** diamond (different storage namespaces, `memberId` algebra, and a UUPS-forwarder `DstackMember` vs AttestMesh's EIP-4337 `ClusterMember`), pinned to **alloy 1 vs AttestMesh's 0.8**. Grafting it produces an unbuildable chimera; importing it discards AttestMesh v1's passing tests.

The premortem also showed that v1 mostly **works** (17 contract + 30 sidecar tests pass). Only two things are actually *wrong*: (1) the on-chain dstack **verification preimages** are invented (`keccak256(abi.encode("dstack.app", …))`) instead of dstack's real `keccak256(abi.encodePacked("dstack-kms-issued:", bytes20(codeId), appCompressedPubkey))`, and the mock was built to mirror the wrong code (a closed loop that hides it); and (2) the EIP-4337 **paymaster sign-ordering** (premortem F2).

This spec is therefore narrowed to a **surgical extraction**: bring the *correct verification preimages* (≈200 lines) into v1, fix the mock + add a real-format proof fixture, and fix F2 in v1's own bundler. Everything else in v1 is kept. The remaining v1 gaps (CSK persistence target, originator selection, the pre-membership rendezvous) are **separate, AttestMesh-native follow-ups**, informed by dstackgres patterns but not in this spec.

## Requirements

### Must Have
- [ ] **Port the verification primitive.** Replace `contracts/src/libraries/DstackSigChain.sol` with the real dstackgres logic, rebranded but preserving the exact preimages/encodings:
  - KMS step: recover `keccak256(abi.encodePacked("dstack-kms-issued:", bytes20(codeId), appCompressedPubkey))` → signer must be an allowlisted KMS root.
  - app step: recover `keccak256(abi.encodePacked(purpose, ":", hex(derivedCompressedPubkey)))` → must equal `compressedToAddress(appCompressedPubkey)`.
  - derived/binding step: recover EIP-191 of `messageHash` → must equal `compressedToAddress(derivedCompressedPubkey)`.
  - keep the existing `compressedToAddress` (secp256k1 decompression via modexp) — it is already correct and unit-tested.
- [ ] **Rewrite the attestor facet's registration** (`DstackFacet.dstack_register` / `IDstackFacet.DstackProof`) to the real `Proof` shape + the `verifySigChain` flow, while **keeping v1's diamond**: v1's `MemberStorage`, `memberId = keccak256(abi.encode(cluster, member, attestorId))`, `MessageFacet`, `NetworkFacet`, the EIP-4337 `ClusterMember`, the factories — all unchanged. Bind `codeId == bytes20(memberContract)` (the dstack `app_id` is the ClusterMember address) and set the ClusterMember owner to the derived key (decision #3). The binding `messageHash` continues to commit to `(cluster, member, xPubKey, wgPubKey)`.
- [ ] **Drop the self-asserted composeHash check at registration** (premortem P1). Approved-software is enforced by the dstack KMS boot gate (`isAppAllowed`, which the KMS queries against the on-chain allowlist) + the KMS-sig-chain verification (which proves the node passed that gate). The on-chain `allowedComposeHashes`/`allowedDeviceIds`/`allowedKmsRoots` allowlist stays — it is the boot-gate policy and the KMS-root set.
- [ ] **Fix the mock + add a real-format fixture.** Rewrite `test/helpers/MockKmsChain.sol` to produce proofs in the **real** format (real preimages, the real `Proof` struct), and add a test that asserts on the literal `"dstack-kms-issued:"` so the preimage can never silently drift. Add a checked-in fixture proof (real-format; a genuinely captured one from a live CVM is a follow-up once a dstack node exists).
- [ ] **Fix F2** in `sidecar/src/chain/bundler.rs`: request gas + paymaster fields **before** computing the userOpHash and signing (sponsor-then-sign), so the signed hash matches the on-chain `validateUserOp`. Use dstackgres's `gas_payment/alchemy.rs` as a **read-only reference** (do not port across alloy 0.8→1).
- [ ] **`forge test` green** against the real-format proof; **`cargo build`/`cargo test` green** for the sidecar.

### Should Have
- [ ] A `forge fmt`/`clippy`-clean result preserved.
- [ ] A short note in the contracts README pointing at dstackgres as the verification reference.

### Must NOT Have (the chimera guards)
- No `teesql.storage.*` namespace, no second `memberId` formula, no second member contract (`DstackMember`), no second CIDR (`10.42.0.0/24`) — ever, in the tree.
- No import of dstackgres's data-sidecar modules or its diamond facets. Patterns are *reimplemented* AttestMesh-native, not copied across.
- No alloy bump to 1.x to accommodate a port (stay on 0.8; reference dstackgres's flow, port the *idea*).
- No CSK/originator/rendezvous rework in this spec (separate follow-ups).

## Non-Requirements

Not the wholesale port (rejected). Not the CSK-persistence / originator-selection / bootstrap-rendezvous fixes (separate AttestMesh-native specs, informed by `cluster_secrets.rs` / `cold_start_election.rs` / `endpoint_crypto.rs` as references). Not on-chain DCAP. Not the Indexer/monitoring-hub reconcile.

## Design — what changes, precisely

| File | Change |
|---|---|
| `contracts/src/libraries/DstackSigChain.sol` | **replace** the invented logic with the real preimages (KMS/app/derived steps); keep `compressedToAddress` + `recover` |
| `contracts/src/interfaces/IDstackFacet.sol` | `DstackProof` → the real `Proof` shape (`codeId, purpose, appCompressedPubkey, appSignature, derivedCompressedPubkey, kmsSignature, messageHash, messageSignature`) |
| `contracts/src/facets/attestor/DstackFacet.sol` | `dstack_register` verifies via the real chain; binds `codeId==memberContract`; sets owner = derived key; **drops** the self-asserted `composeHash` gate; keeps `_addMember` into v1 `MemberStorage` + `NetworkFacet._setWgPubKey` + the owner callback |
| `test/helpers/MockKmsChain.sol` | produce real-format proofs (real preimages); expose the derived/app/kms keys for tests |
| `test/integration/ClusterBringup.t.sol`, `test/unit/ClusterMemberUserOp.t.sol` | update to the new proof shape + the real bind `messageHash`; add the `"dstack-kms-issued:"` literal assertion + a fixture |
| `sidecar/src/chain/bundler.rs` | F2: sponsor-then-sign ordering |

Everything else in `contracts/src` and `sidecar/src` is untouched.

## Open Questions
- [ ] A genuinely **captured** real dstack proof (vs a real-format synthesized one) requires a live dstack CVM + KMS; until then the fixture is real-*format*. Flag where the format must still be confirmed against a node.
- [ ] Confirm the exact `purpose` string dstack uses for the app→derived signature (read it from the real `DstackKmsAdapterFacet._verifySigChain` + a captured proof).

## Validation
1. `forge test` passes with `MockKmsChain` producing real-format proofs, and a test asserting `"dstack-kms-issued:"` is the KMS preimage prefix.
2. `forge fmt --check` clean.
3. `cargo test` (sidecar) passes; a unit test asserts the userOp is signed *after* paymaster fields are populated (F2).

## Traceability

| Requirement | Implementation | Tests |
|---|---|---|

## Changelog

| Date | Author | Changes |
|---|---|---|
| 2026-06-03 | LSDan | Narrowed from a wholesale dstackgres port to a surgical extraction of the verification primitive + the F2 fix (per premortem `…-1780498753`). |
