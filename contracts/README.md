# contracts/

Foundry workspace for the on-chain layer of AttestMesh: the ClusterDiamond, its core facets (Attest / Message / Network), the DstackFacet attestor facet, the ClusterMember passthrough, factories, and the IndexerRegistry.

**Spec**: [`docs/specs/contracts.md`](../docs/specs/contracts.md)
**Master spec**: [`docs/specs/attestmesh-coordination-layer.md`](../docs/specs/attestmesh-coordination-layer.md)

## Layout

- `src/facets/core/` — AttestFacet (member registry + CSK commitment + owner rotation), MessageFacet (encrypted messaging), NetworkFacet (wg pubkeys).
- `src/facets/attestor/DstackFacet.sol` — dstack KMS sig-chain verification + `IAppAuth` boot gate.
- `src/members/` — ClusterMember (dstack proxy + EIP-4337 v0.7 wallet) + its CREATE2 factory.
- `src/factory/`, `src/registry/` — ClusterDiamondFactory, IndexerRegistry.
- `src/libraries/DstackSigChain.sol` — secp256k1 recovery + on-chain point decompression (modexp precompile).
- `script/` — DeployInfra / DeployCluster / DeployMember.

Dependencies are cloned into `lib/` (solidstate v0.0.61, OZ 5.1, account-abstraction v0.7, forge-std). Run `git clone` of those into `lib/` or `forge install` before building.

## Quick reference

```bash
forge build              # compile
forge test -vvv          # run tests (22 passing)
forge fmt --check        # lint
```

## Targets

- v1: Base Sepolia
- Milestone B: Base mainnet (behind a Safe)
