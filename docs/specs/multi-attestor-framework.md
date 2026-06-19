# Multi-Attestor Framework + OperatorFacet

**Status:** IMPLEMENTED
**Author:** LSDan
**Created:** 2026-06-10
**Last Updated:** 2026-06-10
**Parent spec:** [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) §5/§7/§9, [`contracts.md`](./contracts.md) §8/§10
**Components:** `contracts/`, `sidecar/`, `services/gas-sponsorship-webhook/`

## Overview

The core diamond is already attestor-agnostic where it counts: `MemberRecord` carries
an opaque `attestorId`, `memberId = keccak256(abi.encode(cluster, memberContract,
attestorId))`, and `_addMember` / `_setWgPubKey` are method-blind internal selectors
(`AttestFacet.sol:106-122`). But everything *around* that seam is hardwired to dstack:
`ClusterCut.buildFacetCuts` takes exactly one attestor facet with hardcoded selectors,
the factory bakes the facet set into immutables, `DiamondInit.InitArgs` is dstack-shaped,
the sidecar's registration path imports `dstack_facet::` directly, and the gas-webhook
allowlist hardcodes `dstack_register`.

This spec (a) generalizes those seams so attestor facets are pluggable — the
prerequisite for IntelTdxFacet / AmdSnpFacet / NvidiaCcFacet — and (b) proves the
abstraction end-to-end with a deliberately simple second method: **OperatorFacet**, an
operator-signature attestor. A cluster owner allowlists operator signer addresses; a
node is admitted by presenting an operator's ECDSA signature over the standard bind
preimage. This exercises every seam (cut, init, register, member bootstrap, sidecar
provider, webhook policy) with zero new cryptographic dependencies, and is genuinely
useful for dev/test clusters and non-TEE nodes admitted by operator fiat. TEE-direct
facets (DCAP et al.) become follow-on specs that slot into this framework.

**Trust disclosure (load-bearing):** installing OperatorFacet on a cluster changes that
cluster's trust model — members admitted by it are vouched for by a key, not by
hardware attestation, and `isClusterMember` treats all members identically. This is per
master spec §5.1 by design (the cluster picks its attestation policy by picking its
facets), but every artifact this spec touches must say it out loud.

## Requirements

### Must Have

- [x] An `IAttestorFacet` convention every attestor facet implements on its stateless
      implementation contract: `attestorId() → bytes32`, `selectorManifest() → bytes4[]`
      (the selectors to cut into the diamond), and `initAttestor(bytes)` (delegatecall-
      only initializer for its own ERC-7201 namespace).
- [x] `ClusterCut` v2: core cuts stay fixed; attestor cuts are built dynamically from
      each configured facet's `selectorManifest()` (staticcall against the
      implementation address, not the diamond).
- [x] `ClusterDiamondFactory` v2: deploy takes `AttestorConfig[] { address facet,
      bytes initData }`; the factory only accepts facet addresses from an owner-managed
      **approved-attestor registry** (vetted implementations), and `DiamondInit` v2
      delegatecalls each facet's `initAttestor(initData)` after seeding core storage.
- [x] `OperatorFacet`: `operator_register(OperatorProof, address memberContract,
      bytes32 xPubKey, bytes32 wgPubKey) → bytes32 memberId` with
      `OPERATOR_ATTESTOR_ID = keccak256("attestmesh.attestor.operator")`, its own
      `OperatorStorage` namespace (allowlisted signer set), and owner-managed
      `addOperatorSigner` / `removeOperatorSigner` / `operatorSigners` admin selectors.
- [x] OperatorProof binds exactly what dstack binds: signature by an allowlisted
      signer over `keccak256(abi.encode(OPERATOR_BIND_DOMAIN, cluster, memberContract,
      xPubKey, wgPubKey, ownerKey, expiry))`, with `block.timestamp <= expiry` and
      member provenance checked identically to `DstackFacet.sol:169-173`
      (factory-deployed or owner-allowlisted). Registration sets the member's 4337
      owner to `ownerKey` via `__setOwnerFromCluster` — same atomicity as dstack.
      *(Implementation note: the signature is EIP-191 personal-sign over the bind
      hash, exactly like dstack's binding signature; the provenance check is the
      factory anchor only — the owner app_id allowlist is dstack-KMS Path A state in
      DstackStorage, which no other facet may read, and operator members have no KMS
      constraint forcing a non-factory address.)*
- [x] Replay safety: a `(memberContract, attestorId)` pair registers at most once
      (already enforced by memberId uniqueness in `_addMember`); the `expiry` bounds
      voucher lifetime.
- [x] ClusterMember bootstrap validation generalized: in owner-unset mode,
      `validateUserOp` recognizes the inner register selector per method and validates
      the UserOp signature against that method's bootstrap signer (dstack: binding
      signer recovered from the proof, as today, `ClusterMember.sol:110-172`;
      operator: the `ownerKey` named inside the signed OperatorProof).
- [x] Sidecar `AttestationProvider` trait with two implementations (dstack wraps the
      existing `DstackRuntime` path unchanged; operator consumes a pre-signed
      registration voucher from config) selected by `ATTESTOR=dstack|operator`.
- [x] Gas-webhook allowlist partitioned per attestor (selector sets keyed by method),
      `operator_register` and the OperatorFacet admin selectors added, with a config
      flag to disable sponsorship of operator-method traffic independently.
- [x] v1 dstack-only clusters keep working unchanged; adding OperatorFacet to an
      existing cluster is an explicit owner `diamondCut` + `initAttestor` runbook step,
      never automatic.
- [x] Every user-facing artifact (specs, READMEs, docs site page) that mentions
      OperatorFacet states the trust-model difference explicitly. *(Docs site lives in
      AttestMesh/docs — its OperatorFacet page is a follow-up there.)*

### Should Have

- [x] A `mesh-voucher` helper (CLI or deploy/ routine) for operators to mint
      OperatorProof vouchers — agent-friendly, scriptable, no wallet GUI required.
- [ ] `AttestFacet.memberAttestorOf(memberId)` view (it's already in storage) so
      off-chain consumers can filter members by admission method. *(Deferred:
      `memberById(memberId).attestorId` already serves the read, and AttestFacet is a
      deployed v1 facet — an additive selector belongs to a facet-upgrade runbook,
      not this deploy-new-contracts spec.)*

### Must NOT Have

- No TEE-direct facets in this spec (IntelTdxFacet/AmdSnpFacet/NvidiaCcFacet are
  follow-on specs consuming this framework).
- No changes to `MemberStorage`, memberId computation, MessageFacet, NetworkFacet, or
  any deployed v1 facet interface (CRITICAL directive: no breaking interface changes
  without a migration spec). v2 factory/init are **new deployments**, not upgrades of
  the live ones.
- No attestation-method knowledge outside the method's facet (on-chain) or provider
  module (sidecar) — the framework exists to enforce this, not relax it.

## Non-Requirements

- Per-message or per-action policy differentiating members by attestor (all members
  are equal once admitted; finer-grained policy is a future spec if ever needed).
- Sybil-resistance for operator-admitted members beyond the signer allowlist + expiry
  (the operator is trusted by definition of installing the facet).
- Multi-method *sidecars* (one node registers via exactly one method; a cluster may
  mix methods across nodes).

## Design

### Architecture

```
ClusterDiamondFactory v2 ──deploys──► ClusterDiamond
  │ approvedAttestors: set<address>      ├── core cuts (fixed): AttestFacet, MessageFacet, NetworkFacet
  │ deployCluster(initArgs,              ├── attestor cuts (dynamic): for each AttestorConfig,
  │   AttestorConfig[], salt)            │     selectors = staticcall facet.selectorManifest()
  └──────────────────────────────────►  └── DiamondInit v2: core seed, then per-facet
                                              delegatecall facet.initAttestor(initData)
```

A facet's `selectorManifest()` is `pure` on the implementation contract, so the cut is
self-describing and a manifest/selector drift between facet code and cut code is
impossible by construction (replacing the hand-maintained list in
`ClusterCut.sol:90-113`).

### Components

**Contracts**

1. `interfaces/IAttestorFacet.sol` — the convention above. `initAttestor` must revert
   unless `address(this)` is a diamond (delegatecall guard via a storage sentinel),
   mirroring the existing `DiamondInit` pattern.
2. `libraries/ClusterCut.sol` v2 — `buildCoreCuts(...)` + `buildAttestorCuts(
   AttestorConfig[])`. DstackFacet gains `selectorManifest()` returning exactly its
   current selector set (bit-for-bit — covered by a regression test against the v1
   hardcoded list).
3. `factory/ClusterDiamondFactory.sol` v2 — `approvedAttestors` (owner-managed
   EnumerableSet), `deployCluster(CoreInitArgs, AttestorConfig[], bytes32 salt)`.
   Core facet addresses stay immutable; attestor facets come from the approved set.
4. `facets/attestor/OperatorFacet.sol` + `storage/OperatorStorage.sol`
   (`erc7201:attestmesh.storage.operator`, appended to `_namespaces.txt` and the
   `Namespaces.t.sol` collision test):

   ```solidity
   struct OperatorProof {
       address signer;        // must be in OperatorStorage.signers
       address ownerKey;      // becomes the member's 4337 owner
       uint64  expiry;        // voucher deadline (block.timestamp)
       bytes   signature;     // ECDSA(signer, bindHash)
   }
   // bindHash = keccak256(abi.encode(
   //   keccak256("attestmesh.operator.bind.v1"),
   //   address(this), memberContract, xPubKey, wgPubKey, ownerKey, expiry))
   ```

   Registration mirrors `DstackFacet.dstack_register` step-for-step: provenance check
   → bind verification → `IAttest._addMember` → `INetwork._setWgPubKey` →
   `IClusterMember.__setOwnerFromCluster(ownerKey)`.
5. `members/ClusterMember.sol` — bootstrap extraction recognizes
   `operator_register` inner calldata and validates the UserOp signature against
   `proof.ownerKey`. (Per master spec §9 a per-method member impl is also legal; we
   extend the canonical impl because the operator case is two branches, and a fleet of
   member impls is worse for the webhook's provenance checks.)

**Sidecar**

6. `attestor.rs` (new) — the provider seam:

   ```rust
   #[async_trait]
   pub trait AttestationProvider: Send + Sync {
       fn attestor_id(&self) -> [u8; 32];
       async fn derive_keys(&self) -> Result<KeyMaterial>;
       /// ABI-encoded inner call for this method's register selector,
       /// plus the signer the bundler/member will bootstrap-validate against.
       async fn build_register_call(
           &self, cluster: Address, member: Address,
           x_pub: [u8; 32], wg_pub: [u8; 32],
       ) -> Result<RegisterCall>;
   }
   ```

   `DstackProvider` wraps the existing `dstack.rs` + `chain/dstack_facet.rs` flow
   verbatim. `OperatorProvider` derives keys from a local seed file
   (`ATTESTOR_SEED_PATH`, generated on first boot, mode 0600 — explicitly weaker than
   TEE-derived keys; the provider logs a prominent non-TEE warning at boot) and reads
   the operator voucher from `OPERATOR_VOUCHER` (hex CBOR or JSON). `bringup.rs`
   depends only on the trait; `keys.rs` purpose strings are unchanged (they're
   AttestMesh-scoped, not dstack-scoped).
7. CSK note: the CSK originator path derives the CSK from the dstack KMS today
   (`csk.rs`). An operator-admitted node can be an **onboardee** (P2P pull is
   method-agnostic) but must not be the **originator** in v1 — gate originator role on
   `attestor_id == dstack` until CSK derivation is provider-generalized.

**Gas-webhook**

8. `selectors.ts` → per-attestor sets: `CORE_SELECTORS` (publishWgKey, send,
   setCskCommitment, transfers), `DSTACK_SELECTORS`, `OPERATOR_SELECTORS`; env flag
   `SPONSOR_OPERATOR_METHOD=true|false` gates the operator set. Provenance checks
   (`provenance.ts`) are already method-blind (factory membership / Path A allowlist /
   cluster check) and need no change.

**Indexer** — no changes: `MemberRegistered` carries `attestorId` and the
membership check at subscribe (`verify_member.rs`) is method-blind. ABI bindings
regenerate; that's all.

### Interfaces

New/changed external surface (additive only):

| Surface | Change |
|---|---|
| Factory v2 | `deployCluster(CoreInitArgs, AttestorConfig[], salt)`, `addApprovedAttestor`, `removeApprovedAttestor` |
| OperatorFacet | `operator_register(...)`, `addOperatorSigner`, `removeOperatorSigner`, `operatorSigners()`, `attestorId()` |
| All attestor facets | `selectorManifest()`, `initAttestor(bytes)` (impl-level, not cut into diamonds except `initAttestor` via init delegatecall) |
| Sidecar config | `ATTESTOR=dstack\|operator`, `OPERATOR_VOUCHER`, `ATTESTOR_SEED_PATH` |
| Webhook config | `SPONSOR_OPERATOR_METHOD` |

### Data Model

- `OperatorStorage`: `EnumerableSet.AddressSet signers`.
- `DiamondInit` v2 `CoreInitArgs`: today's `InitArgs` minus the dstack fields
  (`kmsRootSigner`, compose hashes, devices, policy bits), which move into
  DstackFacet's `initAttestor` blob — this is the §8 "per-attestor-facet init blobs"
  extension contracts.md:507 already promised.

## Open Questions

- [x] ~~Does the live v1 cluster migrate to factory-v2 lineage, or do v1 and v2
      factories coexist?~~ **Resolved (2026-06-10): coexist, v1 deprecated for new
      deploys.** The webhook trusts both factories (`isDeployedCluster` against
      either); v1 clusters keep working untouched; deploy scripts and docs target v2
      only and mark v1 deprecated. No migration of the live cluster.
- [x] ~~Approved-attestor registry on the factory or standalone?~~ **Resolved
      (2026-06-10): on the factory** (owner-managed EnumerableSet on
      ClusterDiamondFactory v2). The factory is already the provenance anchor; a
      future factory generation re-approves its set explicitly.
- [x] ~~`__setOwnerFromCluster` is set-once (`OwnerAlreadySet`); how should a second-
      method registration's owner assignment behave?~~ **Resolved (2026-06-10):
      skip-if-set.** Registration via a second attestor succeeds and leaves the
      existing owner untouched (the node already controls its wallet; the second
      method only adds an admission record). The second proof's `ownerKey` is ignored
      when an owner exists — registration emits an event noting the skip so the
      mismatch is observable.
- [x] ~~Voucher delivery UX for CVM-less operator nodes?~~ **Resolved (2026-06-10):
      env/config var** (`OPERATOR_VOUCHER`, hex CBOR), minted by the `mesh-voucher`
      CLI. The voucher is not secret (useless without the node's seed). Because the
      voucher signs over the node's pubkeys, the CLI handles the ordering: it
      generates the seed file *and* the voucher together at provision time (or
      accepts pubkeys from a node's first-boot output), so a single provisioning
      step yields both `ATTESTOR_SEED_PATH` and `OPERATOR_VOUCHER`.

## Alternatives Considered

### Framework only, no concrete second facet
Rejected (design decision 2026-06-10): an abstraction with one consumer is untested by
definition; the historical pattern in this repo (quorum-membership spec killed by its
premortem) says unproven designs die on contact.

### Intel TDX direct as the proving facet
Deferred: on-chain DCAP verification means either trusting Automata's verifier
deployment or hand-rolling quote parsing — a large external dependency and its own
threat model. It deserves its own spec once the framework exists; nothing in this
design precludes it (TDX binds `xPubKey || wgPubKey` via report_data per master spec
§5.1 instead of a signature).

### Per-method ClusterMember implementations
Master spec §9 permits this, but for the operator method it would double the member
audit surface and complicate webhook provenance for two branches of calldata
extraction. Kept as the documented escape hatch for methods whose bootstrap genuinely
can't fit the canonical member (likely some TEE-direct flows).

## Traceability

| Requirement | Implementation | Tests |
|-------------|----------------|-------|
| IAttestorFacet convention | `contracts/src/interfaces/IAttestorFacet.sol`; implemented by `DstackFacet` (attestorId/selectorManifest/initAttestor) and `OperatorFacet` | `test/unit/AttestorManifest.t.sol` (convention selectors excluded from manifests, attestorIds pinned); `test/integration/MultiAttestorBringup.t.sol::test_initAttestorRevertsOutsideDiamondContext` |
| ClusterCut v2 (fixed core, dynamic attestor cuts) | `contracts/src/libraries/ClusterCut.sol` `buildCoreCuts`/`buildAttestorCuts` (v1 `buildFacetCuts` kept for the deployed lineage) | `AttestorManifest.t.sol::test_coreCutsMatchV1`, `::test_buildAttestorCutsMirrorsManifests` |
| Dstack manifest == v1 cut, bit-for-bit | `DstackFacet.selectorManifest()` | `AttestorManifest.t.sol::test_dstackManifestMatchesV1CutBitForBit` |
| Factory v2 + approved-attestor registry | `contracts/src/factory/ClusterDiamondFactoryV2.sol` (owner-managed EnumerableSet; v1 factory marked deprecated) | `MultiAttestorBringup.t.sol::test_approvedAttestorSetIsOwnerManaged`, `::test_deployRevertsOnUnapprovedAttestor`, `::test_predictClusterAddressMatchesDeploy` |
| DiamondInit v2 (CoreInitArgs + per-facet blobs) | `contracts/src/DiamondInitV2.sol`; dstack fields moved into `DstackFacet.DstackInitArgs`; operator blob = `abi.encode(address[])` | `MultiAttestorBringup.t.sol::test_initSeededCoreAndBothAttestorNamespaces`, `::test_dstackRegisterWorksOnV2Cluster` |
| OperatorFacet + OperatorStorage | `contracts/src/facets/attestor/OperatorFacet.sol`, `contracts/src/storage/OperatorStorage.sol` (`erc7201:attestmesh.storage.Operator`), `contracts/src/interfaces/IOperatorFacet.sol` | `MultiAttestorBringup.t.sol` (happy path, unlisted signer, expiry, tampered binding, non-factory member, replay, signer admin); `test/unit/Namespaces.t.sol`; `test/unit/Selectors.t.sol::test_operatorRegisterSelectorIsPinned` (0x57977c40) |
| ClusterMember bootstrap generalization + skip-if-set owner | `contracts/src/members/ClusterMember.sol` `_bootstrapSigner` (dstack + operator branches), `__setOwnerFromCluster` skip-if-set + `OwnerSetSkipped` event (`IClusterMember.sol`) | `test/unit/ClusterMemberUserOp.t.sol` (operator bootstrap accept/reject/foreign-member), `test/unit/ClusterMemberAuth.t.sol::test_setOwnerSkipIfSet` |
| Sidecar AttestationProvider seam | `sidecar/src/attestor/mod.rs` (trait + `ATTESTOR` selection), `attestor/dstack.rs` (wraps existing flow verbatim), `attestor/operator.rs` (seed file 0600 + voucher + boot warning); `state::run`/`bringup` depend only on the trait | `attestor/dstack.rs` tests (verbatim-wrap equality), `attestor/operator.rs` tests (seed perms/determinism, voucher JSON+CBOR, bind-hash Solidity vector, register-call validation, CSK/self-discovery gates), `config.rs::attestor_selection_parses`, `chain/abi.rs::operator_register_selector_is_pinned` |
| CSK originator gated to dstack | `AttestationProvider::supports_csk_origination` / `derive_csk_originator`; `bringup.rs::csk_once` gates originate + restart-rederive paths | `attestor/operator.rs::operator_provider_gates_csk_and_self_discovery` |
| mesh-voucher helper | `sidecar/src/bin/mesh_voucher.rs` (seed+voucher in one run, or `--x-pub/--wg-pub/--owner` from first-boot output) | voucher encode/parse + bind-hash covered in `attestor/operator.rs` tests (the bin is a thin flag-parsing shell over those functions) |
| Webhook per-method selector sets + flag | `services/gas-sponsorship-webhook/src/selectors.ts` (CORE/DSTACK/OPERATOR), `env.ts` (`SPONSOR_OPERATOR_METHOD` default false, `CANONICAL_CLUSTER_FACTORY_V2`), `policy.ts` step 6, `provenance.ts` dual-factory `isDeployedCluster` | `test/selectors.spec.ts` (partition, operator pin, gate), `test/policy.spec.ts` (operator branch on/off), `test/provenance.spec.ts` (v1-only / v2 / neither) |
| v1 clusters unchanged; runbook cut for live clusters | v1 factory/init sources untouched (deprecation note only); webhook v2 factory optional | `MultiAttestorBringup.t.sol::test_runbookAddOperatorFacetToDstackOnlyCluster`; full v1 suite still green (83 contract tests) |
| Trust disclosure everywhere | `IOperatorFacet.sol`/`OperatorFacet.sol` natspec, `OperatorStorage.sol`, `attestor/operator.rs` module banner + boot warning, `mesh-voucher` stderr note, `contracts/README.md`, `sidecar/README.md`, webhook `README.md` + `wrangler.toml`, `DeployCluster.s.sol` console note | — |

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-06-10 | LSDan | Initial draft |
| 2026-06-10 | LSDan | APPROVED → IMPLEMENTING; implementation started on `milestone-b-multi-attestor-framework` |
| 2026-06-10 | LSDan | IMPLEMENTING → IMPLEMENTED: contracts (IAttestorFacet, ClusterCut v2, ClusterDiamondFactoryV2, DiamondInitV2, OperatorFacet/Storage, ClusterMember bootstrap + skip-if-set), sidecar (AttestationProvider, Dstack/Operator providers, mesh-voucher), webhook (selector partition, SPONSOR_OPERATOR_METHOD, dual-factory provenance); traceability filled; `memberAttestorOf` view deferred (served by `memberById().attestorId`; AttestFacet is a deployed v1 facet) |
