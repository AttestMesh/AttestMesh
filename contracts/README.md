# contracts/

Foundry workspace for the on-chain layer of TeeMesh: the ClusterDiamond, its core facets (Attest / Message / Network), the DstackFacet platform facet, the ClusterMember passthrough, factories, and the IndexerRegistry.

**Spec**: [`docs/specs/contracts.md`](../docs/specs/contracts.md)
**Master spec**: [`docs/specs/teemesh-coordination-layer.md`](../docs/specs/teemesh-coordination-layer.md)

Code lands here when the contracts spec is generated.

## Quick reference

```bash
forge build              # compile
forge test -vvv          # run tests
forge fmt --check        # lint
```

## Targets

- v1: Base Sepolia
- Milestone B: Base mainnet (behind a Safe)
