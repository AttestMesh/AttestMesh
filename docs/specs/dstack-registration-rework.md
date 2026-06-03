# dstack Registration Rework (premortem fix #1)

**Status:** DRAFT
**Author:** LSDan
**Created:** 2026-06-03
**Last Updated:** 2026-06-03
**Parent specs:** [`contracts.md`](./contracts.md) §6, [`sidecar.md`](./sidecar.md) §6/§8/§13, [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) §4.2/§7.1
**Premortem:** `docs/premortems/premortem-transcript-1780448684.md` (F1, F4)

## Overview

The dstack-grounded premortem found that AttestMesh's dstack registration path was designed without reference to dstack's actual code, and cannot work against a real dstack node:

- **F1 (on-chain verifier).** `DstackFacet.dstack_register` verifies an *invented* 3-level secp256k1 chain over `keccak256(abi.encode("dstack.app", appKey, composeHash))`-style preimages. dstack does **no** on-chain signature verification — its on-chain contract (`DstackApp.isAppAllowed`) is a pure allowlist gate. dstack's KMS verifies the TDX quote *off chain*, calls `isAppAllowed` to gate on the allowlist (`kms/src/main_service/upgrade_authority.rs::is_app_allowed`), and signs keys with a single recoverable `keccak256("dstack-kms-issued" ‖ ":" ‖ app_id ‖ sec1_pubkey)` (`kms/src/crypto.rs`). A real proof can never verify against AttestMesh's invented format.
- **F4 (sidecar client).** `UnixSocketDstack` POSTs JSON `/DeriveKey` with scrambled fields. The real guest agent is **prpc/protobuf** (`GetKey{path,purpose,algorithm}`, `DeriveK256Key`, `GetQuote`, `Info`; `guest-agent/rpc/proto/agent_rpc.proto`) and has **no `Seal`/`Unseal`** — persistence is the encrypted rootfs and key derivation is deterministic.

**Decision (this spec adopts dstack's native model).** AttestMesh treats the dstack KMS as the off-chain attestor: the KMS verifies the quote and gates on the on-chain allowlist before issuing keys. On chain, AttestMesh keeps dstack's `isAppAllowed` allowlist gate, verifies dstack's **real** single KMS signature as the soundness anchor (proving the registrant holds a KMS-issued key for *this* member's `app_id`), binds the member's x25519/wireguard pubkeys, and records membership. AttestMesh does **not** re-derive or re-verify a multi-level chain or the TDX quote on chain. This spec covers **both ends** of the seam (the contract verifier and the sidecar's dstack client + proof construction) so the proof format is pinned byte-for-byte on both sides — the divergence that caused the failure.

## Requirements

### Must Have
- [ ] `DstackFacet.dstack_register` accepts a proof shaped to dstack's real output and, on chain, performs only: (1) member-factory provenance, (2) recovery of dstack's real KMS signature → allowlisted KMS root + `app_id == memberContract`, (3) the `isAppAllowed`-equivalent allowlist gate (composeHash/device/TCB), (4) the member-pubkey binding signature, (5) member record + EIP-4337 owner set. No invented multi-level chain.
- [ ] `DstackFacet.isAppAllowed(AppBootInfo)` retained unchanged (it already matches dstack's `IAppAuth.AppBootInfo` byte-for-byte and is the gate the dstack KMS calls).
- [ ] `DstackSigChain` reduced to: `recover(digest, sig)` (recoverable ECDSA) and `sec1PubKeyToAddress(bytes)` (reusing the existing modexp point-decompression for compressed SEC1). The 3-level chain logic is removed.
- [ ] The exact KMS-signature preimage and the binding preimage are specified once and implemented identically in the contract and the sidecar (a single shared definition in this spec is the source of truth).
- [ ] The sidecar's dstack client speaks dstack guest-agent **prpc** (generated from `agent_rpc.proto`): `GetKey`, `DeriveK256Key`, `GetQuote`, `Info`. The JSON `UnixSocketDstack` is removed.
- [ ] The sidecar derives the EIP-4337 binding/owner key via `DeriveK256Key` and obtains its KMS signature from `DeriveK256KeyResponse.k256_signature_chain`; it derives x25519/wireguard keys via `GetKey` with the appropriate `algorithm`; it reads `app_id`/`instance_id`/`device_id`/`compose_hash` from `Info → AppInfo`.
- [ ] CSK persistence no longer uses `seal`/`unseal`. The originator re-derives the CSK deterministically (`GetKey`, deterministic per app_id). An onboardee persists the pulled CSK to a file on the dstack-encrypted rootfs and re-pulls if it is missing on restart.
- [ ] A forge fork-/replay-test verifies `dstack_register` against a **real captured** dstack proof (`DeriveK256KeyResponse` + `Info`) from a live dstack CVM. This test gates any deploy.

### Should Have
- [ ] The contract's allowlist gate in `dstack_register` is configurable (cluster owner may keep it as defense-in-depth or set `allowAnyDevice` / rely on the KMS). Default: enforce composeHash + device, matching `isAppAllowed`.
- [ ] A captured-proof fixture checked into `contracts/test/fixtures/` so the replay-test runs in CI without a live CVM.

### Must NOT Have
- On-chain TDX quote verification (DCAP). Deferred (see Alternatives).
- Changes to the EIP-4337 paymaster sign-ordering (premortem F2), the Indexer event-decode (F5), or the convergence/originator logic (F6) — separate fixes, separate specs.
- Any change to MessageFacet / NetworkFacet / the core membership storage shape.

## Non-Requirements

This spec does not redesign AttestMesh's trust model beyond adopting dstack's: it explicitly accepts trusting the dstack KMS root (already allowlisted) to have verified the quote and gated on `isAppAllowed`. It does not attempt to make on-chain registration independently verify "runs approved software" without the KMS — that is the on-chain-quote path, out of scope.

## Design

### Trust model & flow

```
CVM boots ──> dstack KMS verifies TDX quote (off chain)
                 └─> KMS builds BootInfo, calls isAppAllowed(bootInfo) on chain (allowlist gate)
                 └─> if allowed: KMS issues app key K + sig = ECDSA_recoverable(
                        keccak256("dstack-kms-issued" ":" app_id sec1(K.pub)) ) by the KMS root key
Sidecar ──> DeriveK256Key(app_id) -> { k256_key=K, k256_signature_chain=[sig,...] }
        ──> Info -> AppInfo{ app_id, compose_hash, device_id, instance_id, ... }
        ──> sign binding: bindSig = K.sign(keccak256("attestmesh.bind.v1" cluster member xPub wgPub))
        ──> submit dstack_register(proof) via EIP-4337
Chain (DstackFacet.dstack_register):
   1. ClusterMemberFactory.isOurMember(member)                       (NotOurMember)
   2. kmsRoot = recover(kmsSigDigest, proof.kmsSig); require allowedKmsRoots[kmsRoot]  (KmsRootNotAllowed/KmsSigInvalid)
      where kmsSigDigest = keccak256("dstack-kms-issued" ":" member proof.appKeyPub)   // app_id == member
   3. allowlist gate: allowedComposeHashes[composeHash] && (allowAnyDevice||allowedDeviceIds[deviceId])
      && (!requireTcbUpToDate || tcbStatus=="UpToDate")              (ComposeHashNotAllowed/DeviceNotAllowed/TcbStale)
   4. appKeyAddr = sec1PubKeyToAddress(proof.appKeyPub);
      require recover(toEthSignedMessageHash(bindHash), proof.bindingSig) == appKeyAddr   (BindingSigInvalid)
   5. _addMember(...); INetwork._setWgPubKey(...); ClusterMember.__setOwnerFromCluster(appKeyAddr)
```

The single on-chain signature recovery (step 2) is the soundness anchor: it proves the registrant possesses a key the KMS issued for `app_id == memberContract`. Everything heavier (quote verification, compose-hash extraction) is trusted to the KMS, which already did it off chain. The allowlist gate (step 3) is retained as the cluster owner's policy surface and mirrors `isAppAllowed`.

### Shared proof format (source of truth — both ends implement THIS)

- **app_id** = the ClusterMember contract address (the dstack `app_id`; AttestMesh already sets `app_id` to the member address).
- **KMS signature preimage**: `keccak256( bytes("dstack-kms-issued") ‖ bytes(":") ‖ bytes20(app_id) ‖ sec1_pubkey_bytes )`, recovered with raw-digest `ecrecover` (dstack uses `sign_digest_recoverable`, 65-byte r‖s‖v with `v = recid`). The exact concatenation and the SEC1 encoding (compressed vs uncompressed) MUST be confirmed against a captured proof (Open Question 1) and then frozen here.
- **Binding preimage** (unchanged domain): `keccak256(abi.encode("attestmesh.bind.v1", clusterAddr, memberContract, xPubKey, wgPubKey))`, signed EIP-191 by the dstack app key K. (The sidecar signs with K; the contract recovers to `appKeyAddr`.)

### Interfaces & data model

New `DstackProof` (contracts §6.3 replacement):

```solidity
struct DstackProof {
    bytes appKeyPub;      // SEC1 public key of the dstack-issued app key K (per DeriveK256KeyResponse.k256_key)
    bytes kmsSig;         // KMS recoverable signature over the dstack-kms-issued preimage (from k256_signature_chain)
    bytes32 composeHash;  // from AppInfo.compose_hash
    bytes32 deviceId;     // from AppInfo.device_id
    string tcbStatus;     // from AppInfo / quote tcb status
    bytes bindingSig;     // K's EIP-191 signature over the binding preimage
}
function dstack_register(DstackProof calldata proof, address memberContract, bytes32 xPubKey, bytes32 wgPubKey)
    external returns (bytes32 memberId);
```

Sidecar (F4) — `src/dstack.rs` + `src/chain/dstack_facet.rs`:
- New `DstackRuntime` trait: `get_key(path, purpose, algorithm)`, `derive_k256_key(app_id)→(key, sig_chain)`, `get_quote(report_data)`, `info()→AppInfo`. Remove `seal`/`unseal`.
- A prpc transport generated from `agent-rpc/agent_rpc.proto` (vendored), replacing the JSON `UnixSocketDstack`. `MockDstack` keeps the same trait for tests.
- Key derivation (sidecar §6) maps AttestMesh purposes onto `GetKey`/`DeriveK256Key`; x25519 + wireguard keys come from `GetKey` with the curve algorithm; the binding/owner key from `DeriveK256Key`.
- CSK (sidecar §13): originator `GetKey("attestmesh.cluster-shared.v1","csk-v1", aes-256)`; persistence to `${STATE_DIR}/csk` on the encrypted rootfs; restart path reads that file, else re-pulls.

### Components touched

| Component | Change |
|---|---|
| `contracts/src/facets/attestor/DstackFacet.sol` | rewrite `dstack_register`; keep `isAppAllowed`/allowlist admin |
| `contracts/src/libraries/DstackSigChain.sol` | drop 3-level logic; keep `recover` + `sec1PubKeyToAddress` (modexp decompression reused) |
| `contracts/src/interfaces/IDstackFacet.sol` | new `DstackProof` |
| `contracts/test/helpers/MockKmsChain.sol` + new fixture | produce the dstack-real format; add the captured-proof replay test |
| `sidecar/src/dstack.rs`, `src/chain/dstack_facet.rs`, `src/keys.rs`, `src/csk.rs`, `src/state/mod.rs` | prpc client; new proof construction; CSK persistence |
| `sidecar/proto/` (+ vendored `agent_rpc.proto`) | dstack guest-agent prpc codegen |

## Open Questions

- [ ] **1. Exact KMS signature format.** Confirm against a captured real proof: the SEC1 encoding dstack returns (compressed 33B vs uncompressed 65B), the precise concatenation/separators in `sign_message` (`prefix ":" app_id message`), and how many elements `k256_signature_chain` carries (is the app-key sig element [0]? is there an intermediate?). Freeze the preimage here once confirmed.
- [ ] **2. Is even the single KMS-signature check wanted, or fully trust an off-chain registration path?** This spec keeps the one recovery as the anti-forgery anchor. If the team wants the chain to be thinner still, registration would instead be authorized by an attested off-chain service — a larger change. Recommend keeping the single check.
- [ ] **3. app_id ↔ member binding.** dstack derives the app key from `app_id`; confirm `app_id` is the 20-byte member address (it is in AttestMesh's compose wiring) and that the KMS preimage uses that exact value.
- [ ] **4. Quote freshness / replay.** dstack offers `sign_message_with_timestamp` for replay protection. Decide whether registration needs the timestamped variant (likely not — registration is one-shot and `_addMember` reverts on replay, but document it).

## Alternatives Considered

### A. Verify dstack's full signature chain on chain (rejected for the first fix)
Recover dstack's real chain on chain but keep AttestMesh's heavier verifier-as-facet stance. Rejected: dstack's chain still bottoms out at the KMS root and does not put the compose-hash binding on chain anyway, so the extra verification buys little over the single KMS-sig anchor while costing more. The chosen model is strictly simpler and dstack-native.

### C. On-chain TDX quote verification (deferred)
Verify the Intel DCAP quote on chain for a truly trustless compose-hash binding. Far heavier (gas, complexity, PCS/cert management); a milestone-C concern, not the first fix.

## Traceability

*Filled in during implementation.*

| Requirement | Implementation | Tests |
|---|---|---|
| | | |

## Changelog

| Date | Author | Changes |
|---|---|---|
| 2026-06-03 | LSDan | Initial draft — adopts dstack's allowlist-gate model; covers both ends of the registration seam (F1 + F4). |
