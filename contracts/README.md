# contracts/

Foundry workspace for the on-chain layer of AttestMesh: the ClusterDiamond, its core facets (Attest / Message / Network), the pluggable attestor facets (Dstack / Operator), the ClusterMember passthrough, factories, and the IndexerRegistry.

**Spec**: [`docs/specs/contracts.md`](../docs/specs/contracts.md)
**Master spec**: [`docs/specs/attestmesh-coordination-layer.md`](../docs/specs/attestmesh-coordination-layer.md)
**Multi-attestor framework**: [`docs/specs/multi-attestor-framework.md`](../docs/specs/multi-attestor-framework.md)

## Layout

- `src/facets/core/` — AttestFacet (member registry + CSK commitment + owner rotation), MessageFacet (encrypted messaging), NetworkFacet (wg pubkeys).
- `src/facets/attestor/DstackFacet.sol` — dstack KMS sig-chain verification + `IAppAuth` boot gate.
- `src/facets/attestor/OperatorFacet.sol` — operator-signature admission (see trust note below).
- `src/interfaces/IAttestorFacet.sol` — the convention every attestor facet implements (`attestorId` / `selectorManifest` / `initAttestor`); cuts are built from each facet's own manifest.
- `src/members/` — ClusterMember (dstack proxy + EIP-4337 v0.7 wallet) + its CREATE2 factory.
- `src/factory/` — ClusterDiamondFactoryV2 (owner-managed approved-attestor set; current) and the deprecated v1 factory (kept for the live deployment's verification).
- `src/registry/` — IndexerRegistry.
- `src/libraries/DstackSigChain.sol` — secp256k1 recovery + on-chain point decompression (modexp precompile).
- `script/` — DeployInfra / DeployCluster (v2 factory lineage) / DeployMember.

Dependencies are cloned into `lib/` (solidstate v0.0.61, OZ 5.1, account-abstraction v0.7, forge-std). Run `git clone` of those into `lib/` or `forge install` before building.

## Attestation methods & trust model

A cluster picks its attestation policy by picking its attestor facets (master spec §5.1). The framework keeps every method's details inside its own facet:

- **DstackFacet** — members prove a dstack KMS signature chain rooted in TEE hardware attestation.
- **OperatorFacet** — **members are vouched for by an allowlisted operator key, NOT hardware attestation.** Installing it changes the cluster's trust model: `isClusterMember` treats all members identically once admitted, so an operator-admitted member has the same standing as a TEE-attested one. Intended for dev/test clusters and operator-vouched non-TEE nodes. Admission method is observable per member via `MemberRecord.attestorId`.

Adding OperatorFacet to an existing cluster is an explicit owner `diamondCut` + `initAttestor` runbook step, never automatic.

## Quick reference

```bash
forge build              # compile
forge test -vvv          # run tests (83 passing)
forge fmt --check        # lint
```

## Targets

- v1: Base mainnet (chain id 8453) — **live**; addresses in [`docs/deployment.md`](../docs/deployment.md)
- Milestone B: ownership transfer to a Safe
