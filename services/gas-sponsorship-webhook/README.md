# gas-sponsorship-webhook

Cloudflare Worker (TypeScript) that gates Alchemy's paymaster sponsorship for AttestMesh UserOperations.

When the sidecar submits an EIP-4337 UserOp via Alchemy's bundler, the bundler asks this webhook whether to sponsor the gas. The webhook validates against a fixed policy (chain id, outer selector is `ClusterMember.execute`, inner selector is in an allowlist of cluster operations, value is 0, target is an AttestMesh diamond deployed by our canonical `ClusterDiamondFactory`) and returns `{approved: true|false}`. Alchemy's paymaster signs the sponsorship fields on approve; the bundler bundles; gas is paid by Alchemy and billed to the AttestMesh org account.

**Spec**: [`docs/specs/gas-webhook.md`](../../docs/specs/gas-webhook.md)
**Master spec**: [`docs/specs/attestmesh-coordination-layer.md`](../../docs/specs/attestmesh-coordination-layer.md) (§13 item 18)
**Source pattern**: ported and repurposed from dstackgres's `services/gas-sponsorship-webhook/`.

Implemented: the policy validator (chain id, `ClusterMember.execute` outer selector, inner-selector allowlist, value-0, and cluster-factory provenance checks) over a viem RPC client (82 unit tests).

## Multi-attestor policy (multi-attestor spec)

The inner-selector allowlist is partitioned per attestation method: `CORE_SELECTORS` (method-agnostic cluster ops), `DSTACK_SELECTORS` (dstack registration + allowlist admin), and `OPERATOR_SELECTORS` (`operator_register` + OperatorFacet signer admin).

- `SPONSOR_OPERATOR_METHOD=true|false` (default **false**) gates the operator set independently. **Trust note:** members admitted via OperatorFacet are vouched for by an allowlisted operator key, NOT hardware attestation — paying their gas is an explicit opt-in.
- `CANONICAL_CLUSTER_FACTORY_V2` (optional) — the multi-attestor ClusterDiamondFactoryV2. v1 and v2 factories coexist; `isDeployedCluster` trusts either. Unset means v1-only (pre-v2 deployments unchanged).

## Quick reference

```bash
npm install
npm run dev               # wrangler dev
npm run deploy            # wrangler deploy
npm test                  # vitest
```
