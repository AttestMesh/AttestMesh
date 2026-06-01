# gas-sponsorship-webhook

Cloudflare Worker (TypeScript) that gates Alchemy's paymaster sponsorship for AttestMesh UserOperations.

When the sidecar submits an EIP-4337 UserOp via Alchemy's bundler, the bundler asks this webhook whether to sponsor the gas. The webhook validates against a fixed policy (chain id, outer selector is `ClusterMember.execute`, inner selector is in an allowlist of cluster operations, value is 0, target is an AttestMesh diamond deployed by our canonical `ClusterDiamondFactory`) and returns `{approved: true|false}`. Alchemy's paymaster signs the sponsorship fields on approve; the bundler bundles; gas is paid by Alchemy and billed to the AttestMesh org account.

**Spec**: [`docs/specs/gas-webhook.md`](../../docs/specs/gas-webhook.md)
**Master spec**: [`docs/specs/attestmesh-coordination-layer.md`](../../docs/specs/attestmesh-coordination-layer.md) (§13 item 18)
**Source pattern**: ported and repurposed from dstackgres's `services/gas-sponsorship-webhook/`.

Code lands here when the spec is generated.

## Quick reference

```bash
npm install
npm run dev               # wrangler dev
npm run deploy            # wrangler deploy
npm test                  # vitest
```
