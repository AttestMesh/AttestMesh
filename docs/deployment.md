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

## Standardized routines (smithers + logged bash)

Standardized two layers deep:
- **Logged bash routines** hold the actual, idempotent commands; every step tees to
  `deploy/logs/` via `deploy/lib.sh::run_step`, so on a re-run you see exactly what failed:
  - `deploy/onchain.sh` — `{preflight | infra | cluster | patha-upgrade <cluster> | seed-appid}`.
    `infra` no-ops if already deployed (`FORCE=1` to redeploy); `patha-upgrade` diamond-cuts the
    Path A DstackFacet + deploys the upgrade-target impl.
  - `deploy/webhook.sh` — `{deploy | route}`. `wrangler deploy` + ensures the custom-domain
    route `gas-webhook.teesql.com/*` points at the AttestMesh worker (a stale route → bundler 401).
  - `deploy/node-pathA.sh <node>` — `{env-file | deploy | prime | upgrade | setup | verify | all}`.
    The Path A node bring-up: builds the sealed env (ghcr pull-creds; MEMBER_CONTRACT omitted →
    self-discovered), phala-deploys a stock DstackApp CVM, primes the cluster gate, upgrades the
    proxy to ClusterMember, then polls for the sidecar's self-registration. Gated on a `phala
    login` session; persists CVM_ID/app_id in a state file so every subcommand is re-entrant.
  - `deploy/node.sh` — the legacy `--custom-app-id` flow (unsupported on base KMS; kept for
    reference / a future custom-app-id KMS).
- **A durable smithers workflow** (`deploy/workflows/deploy.tsx`) sequences those routines as
  crash-recoverable, resumable compute steps:
  `preflight → infra → cluster → pathaUpgrade → webhook → node{env-file → deploy → prime →
  upgrade → verify}`. The node sub-steps are individually durable — a `verify` timeout (the
  sponsored registration UserOp is slow) resumes from `verify` without re-deploying the CVM.
  Validated with `smithers graph` (renders the ordered 10-task plan). `deploy/package.json`
  pins `smithers-orchestrator@0.22.0` + `zod@^4`.

Run:
```bash
source deploy/env.sh                                   # load ~/.teesql creds
( cd deploy && bun install )                           # once — deduped smithers deps
( cd deploy && bunx smithers-orchestrator up workflows/deploy.tsx --input '{"node":"attestmesh-node-1"}' )
# resume from the failed step after fixing it:  … up workflows/deploy.tsx --run-id <id> --resume true
# or a single routine directly:  deploy/onchain.sh all  /  deploy/webhook.sh deploy  /
#                                CLUSTER=… MEMBER_IMPL=… deploy/node-pathA.sh attestmesh-node-1 setup
```

## Deployed addresses — Base mainnet (8453)

Canonical receipt: `contracts/script/deployments/8453.json` (written by `DeployInfra`).
**Current = the fixed-facet redeploy** (DstackFacet with the cold-start deadlock fix +
the `allowedAppIds` allowlist). The earlier set (`0xB1fA7AD…`/`0x624a5bcE…`) is superseded
and orphaned on-chain.

| Contract | Address |
|---|---|
| ClusterDiamondFactory | `0xf6E85fD138E3208d3AAE63ce4E2A33f20e82b9fb` |
| ClusterMemberFactory | `0xFf9f438EFdAa197f4ae59D1d711C4f09640b8417` |
| ClusterMember impl | `0xd05223da04B4E73AC02ECA9638D490f45f765843` |
| IndexerRegistry | `0xbC003686943fB957100E517D3CEf66c52B5CDdBf` |
| DiamondInit | `0xe3C9CE59b6c164c7b4c81f686C876EB85198cFC3` |
| AttestFacet | `0x10532164ca3BdaCf1dAd13Fb534262DEc5aAA9AA` |
| MessageFacet | `0x0F67cd8c1D8A2F71d2bb8091B2eb166E6b6bB564` |
| NetworkFacet | `0x6AB2b7D506c85C7A9eA22f2ECcE0cc9191006159` |
| DstackFacet (infra bundle) | `0xe9d463974c6E833DC38794d7f2DC5AB692352968` — superseded on the live cluster by the Path A cut below |
| **ClusterDiamond `attestmesh-1`** | **`0xA46273adC86c772C7D8daE896a5fbfdDA2B6ccFA`** (owner=deployer; KMS root + compose hash seeded; allowAnyDevice; 10.13.0.0/16; **cold-start gate verified live**; DstackFacet diamond-cut to the Path A build) |

### Path A (dstack base KMS) — live cluster overrides

Base KMS only mints app_ids it provisions, so a member cannot be a factory-predicted address — it must BE the provisioned app_id. The cluster's DstackFacet was diamond-cut to a Path A build, and a dedicated ClusterMember impl is the UUPS upgrade target for the stock DstackApp proxy `phala deploy` mints.

| Item | Address / value |
|---|---|
| DstackFacet (Path A, cut into the cluster) | `0xC631793fB80d3Bc18435aAD3788B8b31B44bE255` (`dstack_register` also accepts owner-allowlisted app_ids) |
| ClusterMember impl (Path A upgrade target) | `0xBe579F0B8A971d0F8b083Eb3E8bB241985A3C5C4` (`reinitializeFromDstackApp`) |
| Node-1 app_id — **registered member** | `0x54e63929b4d8d09d3c9e3019d54bd20e289ed985` (memberId `0x6c576be9…`, mesh IP `10.13.46.241`, owner = KMS-derived key `0x6EB37a6B…`; registered via sponsored tx `0x577cd15d…`; CVM `7c3234af…`) — CSK originator (memberIds[0]) |
| Node-2 app_id — **registered member** | `0xa87128971070f41c26871ce361d3eded7ecf909b` (memberId `0xdf63f08d…`, mesh IP `10.13.120.109`; CVM `b2d97057…`) |
| Mesh transport | wg over gateway TCP: ingress `<app_id>-51900s.dstack-base-prod5.phala.network`, wg outer port 51821 (in-CVM), heartbeats 51820 in-mesh |
| Sponsorship webhook custom domain | `https://gas-webhook.teesql.com/?token=<~/.teesql/attestmesh-webhook-token>` → CF worker route → `attestmesh-gas-sponsorship-webhook` |

Procedure: `source deploy/env.sh && CLUSTER=… MEMBER_IMPL=… ENV_FILE=… COMPOSE=deploy/compose/node-1.yaml deploy/node-pathA.sh attestmesh-node-1 setup` (deploy stock CVM → prime gate → upgrade proxy), then `… verify`.

## Status log

| Date | Step | Result |
|---|---|---|
| 2026-06-03 | recon + env bridge + preflight | ✔ key→deployer verified, chain 8453, balance OK |
| 2026-06-03 | **A1 DeployInfra (Base mainnet)** | ✔ 9 contracts live (~0.00006 ETH); bytecode verified on-chain |
| 2026-06-03 | **A2 DeployCluster** `attestmesh-1` | ✔ `0x624a…b712`; owner=deployer, allowedKmsRoots[real]=true, allowedComposeHashes[real]=true, allowAnyDevice, meshIp(1)=10.13.124.237 (matches sidecar vector) |
| 2026-06-03 | **B gas-webhook (Cloudflare)** | ✔ live at `attestmesh-gas-sponsorship-webhook.teesql-aa-webhook.workers.dev`; chain 8453 + real factory addrs, 2 KV namespaces, secrets set (RPC_URL, ALCHEMY_WEBHOOK_TOKEN→`~/.teesql/attestmesh-webhook-token`); `GET /healthz`=200. CF token: `~/.teesql/cloudflare-wrangler.toml` (the teebox-llc one lacks Workers-KV perms). |
| 2026-06-03 | B3 (todo, hardening) | point the Alchemy Gas Manager policy `56444921…` at the webhook URL + token (dashboard/Admin API). Sponsorship works via the policy's own rules without it; the webhook is the custom provenance gate. |
| 2026-06-03 | Phala auth | ⚠ not found: no `phala` on PATH, no `~/.phala*` store, no `PHALA_CLOUD_API_KEY` in env/`~/.teesql`; `npx phala` says "not authenticated". Needs `PHALA_CLOUD_API_KEY` exported (dstackgres CLI reads that env var) or `phala login`. Blocks only D5 (CVM boot). |
| 2026-06-03 | **milestone-A code** | ✔ done + tested: cold-start deadlock fix; `DstackRuntime` `/GetKey`+`/Info`; `build_kms_material` (app-pubkey recovery) + `build_proof` — e2e test proves a sidecar-built proof passes the on-chain `DstackSigChain.verify` (37 sidecar tests). |
| 2026-06-03 | **fixed-facet redeploy (final on-chain)** | ✔ infra+cluster redeployed with the deadlock fix; `0xA46273…ccFA`; **`isAppAllowed(allowlisted, unregistered app_id) = (true,"")` verified live** (cold-start works); webhook redeployed to the new factories, `/healthz`=200. |
| 2026-06-03 | (was) remaining = Phala-gated | wire `build_proof` into `state::run()` + the live submit, then `phala deploy` a CVM. |
| 2026-06-09 | **Phala auth ✔** | device-flow `phala login` succeeded (user authorized); `phala status` = logged in as `lsdan`. Track D unblocked. |
| 2026-06-09 | **registration wired** | `state::run()` now derives keys → `build_proof_from_runtime` → sponsored bootstrap `dstack_register` UserOp, heavily logged (commit `3d275a3`). |
| 2026-06-09 | **base KMS reality** | `--custom-app-id` is unsupported on base KMS (proved via a stock DstackApp control deploy); the member must BE a phala-minted app_id → **Path A**: upgrade the stock DstackApp proxy to ClusterMember. |
| 2026-06-09 | **Path A contracts + webhook** | `ClusterMember.reinitializeFromDstackApp` + `dstack_register` accepts owner-allowlisted app_ids; **diamond-cut live** into `0xA46273…` (new DstackFacet `0xC631793f…`, impl `0xBe579F0B…`, 26 contract tests). Webhook gains a Path A branch (cluster-allowlisted app_ids, 73 tests) + Cloudflare redeploy (also fixed a stale factory var). |
| 2026-06-09 | **live CVM bring-up** | `phala deploy` (base KMS, ghcr pull-creds in sealed env) → upgrade proxy → sidecar self-registers. Fixed 3 live-only bugs: `/DeriveKey`→`/GetKey` (dstack 0.5.x removed it), odd-length dummy sig (Alchemy rejected), proof sig recovery-ids 0/1→27/28 (OZ ECDSA reverts on v<27). Codified in `deploy/node-pathA.sh`. |
| 2026-06-09 | **webhook routing bug (B3 root cause)** | Alchemy calls the policy's webhook at the custom domain `gas-webhook.teesql.com`, whose Cloudflare worker route pointed at the OLD `teesql-gas-webhook-prod` worker (→ HTTP 401), not `attestmesh-gas-sponsorship-webhook`. Repointed the route via the CF API (zone `teesql.com`, route `69066fb7…`). The dummy `A 192.0.2.1` placeholder record is fine (proxied; the worker route does the routing). NB: this moved `gas-webhook.teesql.com` away from the dstackgres worker. |
| 2026-06-09 | **✅ NODE REGISTERED (milestone)** | A real dstack CVM (Phala base-KMS prod5) self-registered: `phala deploy` → upgrade proxy → sidecar derived KMS keys, self-discovered app_id `0x54e63929…`, built proof, submitted a **sponsored** EIP-4337 `dstack_register` UserOp → tx `0x577cd15d…` (success). `memberCount=1`, `isClusterMember=true`, `member.owner()=0x6EB37a6B…` (KMS-derived key installed), gas paid by the paymaster. End-to-end Path A proven on Base mainnet. |
| 2026-06-10 | **milestone-B de-risk (live probes)** | Two probe CVMs on prod5 proved: outbound UDP ✓; NAT mapping endpoint-independent ✓; **two-sided simultaneous UDP punch works incl. hairpin** (one-sided does not — filtering is restricted); gateway-domain DNS == egress IP (STUN-free self-discovery); gateway forwards raw (non-HTTP) TCP **only** via the `s` TLS-passthrough suffix (`<app_id>-<port>s.<domain>`); punch coordination done fully P2P over gateway TCP. Probes deleted after. |
| 2026-06-10 | **mesh bring-up wired** (`734838c`) | Sidecar `bringup` + `transport` modules: wg-over-TCP bridges through the gateway TLS-passthrough (self-signed ingress TLS — SNI routing only, wireguard is the security layer), peers enumerated from chain (`listMembers`/`memberById`/`meshIpOf`), PeerEndpoint envelopes via `MessageFacet.send` + `MessageSent` log polling (no Indexer dependency), heartbeats, CSK originate-or-pull, peer gRPC (mesh-only) + agent gRPC (UDS). wg outer port moved to 51821 (51820 collided with the heartbeat recv socket). Compose exposes ingress 51900 + `GATEWAY_DOMAIN` sealed-env var. |
| 2026-06-10 | **live-CVM update procedure** | Compose changes need the new compose hash allowlisted before reboot: deployer `cast send CLUSTER addComposeHash(0xhash)` (the `phala deploy --prepare-only/--commit` token flow rejected every CVM-id form; a plain `phala deploy --cvm-id … --compose … -e …` works once the hash is already on-chain — state-only verification). `phala cvms restart` re-pulls `:latest`. |
| 2026-06-10 | **2 more live-only bugs** | (4) 4337 nonce: `eth_getUserOperationNonce` is not a real bundler method and the error was swallowed → every post-registration UserOp replayed nonce 0 → AA25. Fixed: `EntryPoint.getNonce` via `eth_call` (`8db4fce`). (5) heartbeat connected-view omitted self → a 2-node mesh could never satisfy `view == live_set`, `first_converged` never latched. Fixed (`527dc54`). |
| 2026-06-10 | **✅ MESH CONNECTED (milestone-B core)** | Both nodes formed the wireguard mesh over the gateway TCP leg: sponsored PeerEndpoint envelopes landed (`0x4da2ac5c…`, `0xb1dc6b22…`), Ed25519 keys absorbed from `MessageSent` logs, verified heartbeats → `live_peers=1` on both; node-1 (memberIds[0]) **originated the CSK + set the on-chain commitment** (`0xabf10640…`); node-2 **pulled the CSK over the mesh** (`http://10.13.46.241:50051` — node-1's mesh IP through the tunnel) and verified it against the commitment. |
| 2026-06-10 | **bug 6: CSK restart deadlock** | After a CVM restart the guest-agent `/Seal` data did not survive container recreation; with the commitment already on-chain, the originator joined the pull path and all nodes waited on each other (`pulling-csk` deadlock). Fixed (`b9ba052`): the originator's CSK is deterministically KMS-derived, so on restart it re-derives and verifies against the on-chain commitment before falling back to peer pull; store writes best-effort. **Caveat: dstack `/Seal`/`/Unseal` persistence is unverified — onboardees re-pull on every restart (fine while ≥1 originator-derivable node is up).** |
| 2026-06-10 | **✅ MESH HEALTHY (milestone-B complete)** | Both nodes `GET /healthz` → **200** `{phase:"healthy", live_peers:1, first_converged:true, csk_acquired:true}`. Restart-resilient: node-1 re-derived CSK (log: "CSK re-derived + verified against on-chain commitment"), node-2 re-pulled over the mesh, convergence latched on both. Chain remained the sole coordination layer end-to-end: membership + wg keys + mesh IPs from facet reads, endpoints derived from app_id + `GATEWAY_DOMAIN`, envelopes via `MessageFacet`. **No STUN, no Indexer, no off-chain config.** |

## Milestone-A reference: the real dstack guest-agent API

(From dstackgres `crates/teesql-data-sidecar/.../dstack.rs` — the `DstackClient`.)
The dstack guest agent listens on a unix socket (`/var/run/dstack.sock`) or
`http://localhost:8090`, prpc-style JSON POST:
- **`/Info`** → `{ app_id, instance_id, compose_hash, app_name, device_id, app_cert, tcb_info, key_provider_info }`
- **`/GetKey`** (path, purpose) → **`{ key: hex, signature_chain: Vec<String> }`** ← the KMS sig chain
- `/GetQuote` (report_data) → quote; `/Sign` (algorithm, data); `/GetTlsKey`

So the `DstackRuntime` trait (sidecar/src/dstack.rs) needs `get_key(path, purpose) -> { key, signature_chain }`
and `info() -> { app_id, compose_hash, instance_id, device_id, ... }`. The on-chain
`DstackProof` (codeId, app/kms signatures, app/derived compressed pubkeys, messageHash)
is assembled from `signature_chain` + the derived key. **Next:** read dstackgres
`group_auth.rs` for the exact `signature_chain` → proof mapping — this is the
ground-truth check on AttestMesh's on-chain `DstackSigChain` preimages (premortem risk A/B/C).
NOTE: dstack's real `app_id` (from `/Info`) is what `codeId` must equal — confirm it is the
ClusterMember address in the AttestMesh model (it is set as the CVM's app_id at deploy).

## Phala auth (action needed for D5)

To deploy a real CVM, export the Phala Cloud API key the way the dstackgres CLI expects:
`export PHALA_CLOUD_API_KEY=<key>` (or `npx phala login`). Everything up to the CVM boot
proceeds without it.
