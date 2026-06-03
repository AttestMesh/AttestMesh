# AttestMesh Deployment Runbook

**Target:** Base **mainnet** (chain `8453`), interoperating with the Phala **dstack-base-prod5** KMS that the live dstack CVM fleet boots against. Mainnet (not Sepolia) because the real KMS — the one whose `isAppAllowed` gate and KMS-root signer we must match for a real node to register — lives there. This is the same chain + KMS the dstackgres fleet runs on.

> All secret values live in `~/.teesql` and are read at runtime by `deploy/env.sh`; nothing secret is committed. `source deploy/env.sh` to load the environment; `deploy/lib.sh` provides `run_step`/`log` that tee every step to `deploy/logs/` so failures are visible on re-runs.

## Resolved configuration

| Key | Value | Source |
|---|---|---|
| Chain | Base mainnet `8453` | — |
| RPC / bundler | `https://base-mainnet.g.alchemy.com/v2/<key>` | `~/.teesql/alchemy-api.key` |
| Gas Manager policy | `56444921-…` (`GAS_POLICY_ID`) | `~/.teesql/alchemy-policy.id` |
| Deployer | `0x60b174704AdAf2b0BF87B426B364D6EbD81818E1` (~0.0104 ETH) | `~/.teesql/global-deployer.{address,key}` |
| ORG_SAFE | deployer (bring-up); transfer to hub Safe `0xd97b…66f0` later | `~/.teesql/config.toml [hub_safe]` |
| **KMS root signer** | `0x52d3cf51c8a37a2ccfc79bbb98c7810d7dd4ce51` | `config.toml [kms].kms_roots` (the corrected root — see premortem 1780517097) |
| Compose hash (real) | `0x206a322e7ad0cfec0a2080822a4dfef10c4240a33e1dc553dd9e76b606b8ed0f` | `config.toml` (a live CVM) |
| KMS endpoint | `https://kms.dstack-base-prod5.phala.network` | `config.toml [kms].url` |
| Basescan verify key | set | `~/.teesql/hub.env` |

## Tracks (dependency order)

```
A. Contracts (on-chain)         ── ready ──►  unblocks B, C, D
   A1 DeployInfra  (facets, factories, IndexerRegistry)   →  script/deployments/8453.json
   A2 DeployCluster (one ClusterDiamond, allowlists seeded: KMS root + compose hash)
   A3 (verify on Basescan)

B. Gas-sponsorship webhook       ── ready ──►  needs A (factory addr)
   B1 wrangler.toml: chain 8453, factory addr, RPC; B2 wrangler deploy; B3 point the
      Alchemy Gas Manager policy at the webhook URL.

C. Indexer                       ── ready ──►  needs A (IndexerRegistry)
   C1 run attestmesh-indexer (RPC, registry addr, identity); C2 register endpoint+pubkey.

D. Node (dstack CVM via Phala)   ── MILESTONE-A WORK ──►  needs A,B,C
   The sidecar bring-up is built+unit-tested but NOT wired (docs/specs/sidecar.md §1.1).
   To register a real node, this track must:
   D1 [code] wire state::run() to drive registration→subscribe→mesh.
   D2 [code] add a DstackRuntime KMS-sig-chain request method (the real dstack guest-agent
            call) so dstack_register proof material can be sourced from the runtime.
   D3 [code] fix the isAppAllowed boot-gate bootstrap circularity (owner-seeded app_id
            allowlist / isOurMember(appId)) — else the KMS refuses first boot.
   D4 [build] sidecar OCI image + node compose (app + cluster-mesh-agent).
   D5 [deploy] `phala deploy` a CVM with the ClusterMember address as app_id; the KMS
            gate releases keys; the sidecar registers. **This is where the real KMS proof
            format is finally validated on-chain** (resolves premortem risk A/B/C).
   D6 repeat ×3 → mesh converges.
   Phala auth: `~/.teesql` has no obvious Phala Cloud API key; `npx phala` reports
   "not authenticated". Resolve before D5 (find the key or `phala login`).
```

## Standardized routines (smithers)

Order-sensitive / repeated routines are authored as durable `smithers` workflows under
`deploy/workflows/` so they survive crashes, can resume, and log every step:
- `onchain` — A1→A2→A3 in order, idempotent, writes receipts.
- `node` — D4→D5→D6 loop per node (build → push → create CVM → wait boot → verify
  on-chain registration), the most repetitive/ordering-sensitive routine.

Run: `source deploy/env.sh && smithers up deploy/workflows/onchain.tsx`.

## Deployed addresses — Base mainnet (8453)

Canonical receipt: `contracts/script/deployments/8453.json` (written by `DeployInfra`).

| Contract | Address |
|---|---|
| ClusterDiamondFactory | `0xB1fA7AD056A9Af1E3f4E1f8b6A147bE79CCfBd1d` |
| ClusterMemberFactory | `0xeE8159e67b07aA71b4938b5ea5a9ED63dC4f6e65` |
| ClusterMember impl | `0x7D8F186C1d3cC5bb92B014a5873A931eE8402713` |
| IndexerRegistry | `0x6579B19387f18AA9D9eb24a9Ce8De6b21c52708c` |
| DiamondInit | `0x6524c8aF358B739A32E315ed4530183d2d52339B` |
| AttestFacet | `0x437f66a2FF552ccE284bE5ADdF930506bF1bE641` |
| MessageFacet | `0x996F442669236E6843B6Bf063f7704D4aDeDd8c4` |
| NetworkFacet | `0x4eFf3b7A5b8b4888bf8C80855BCB1FaAa59D727F` |
| DstackFacet | `0xA5a9346665339369Cb9D78C622176a5cD14dd445` |
| **ClusterDiamond `attestmesh-1`** | **`0x624a5bcE50ffD6b950a2b03edee1a10E3DF0b712`** (owner=deployer; KMS root + compose hash seeded; allowAnyDevice; 10.13.0.0/16) |

## Status log

| Date | Step | Result |
|---|---|---|
| 2026-06-03 | recon + env bridge + preflight | ✔ key→deployer verified, chain 8453, balance OK |
| 2026-06-03 | **A1 DeployInfra (Base mainnet)** | ✔ 9 contracts live (~0.00006 ETH); bytecode verified on-chain |
| 2026-06-03 | **A2 DeployCluster** `attestmesh-1` | ✔ `0x624a…b712`; owner=deployer, allowedKmsRoots[real]=true, allowedComposeHashes[real]=true, allowAnyDevice, meshIp(1)=10.13.124.237 (matches sidecar vector) |
| 2026-06-03 | **B gas-webhook (Cloudflare)** | ✔ live at `attestmesh-gas-sponsorship-webhook.teesql-aa-webhook.workers.dev`; chain 8453 + real factory addrs, 2 KV namespaces, secrets set (RPC_URL, ALCHEMY_WEBHOOK_TOKEN→`~/.teesql/attestmesh-webhook-token`); `GET /healthz`=200. CF token: `~/.teesql/cloudflare-wrangler.toml` (the teebox-llc one lacks Workers-KV perms). |
| 2026-06-03 | B3 (todo, hardening) | point the Alchemy Gas Manager policy `56444921…` at the webhook URL + token (dashboard/Admin API). Sponsorship works via the policy's own rules without it; the webhook is the custom provenance gate. |
| 2026-06-03 | next | Track C (indexer), then Track D (node — milestone-A wiring) |
