# AttestMesh Mesh-State API — Component Spec

**Status**: IMPLEMENTED — live-verified against C3 on Base mainnet (2026-07-01)
**Parent spec**: [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) (membership §5–§8), [`contracts.md`](./contracts.md) (AttestFacet / NetworkFacet)
**Component**: `services/mesh-state-api/`
**Author**: AttestMesh (owned in `teemesh`)
**Created**: 2026-07-01
**Last Updated**: 2026-07-01

---

## 1. Purpose

A **stateless, read-through HTTP API** that turns the on-chain cluster state into clean JSON so that *multiple* consumers share one implementation of the facet reads / mesh-IP derivation / event decoding instead of each re-implementing chain plumbing.

Two known consumers, one contract:

1. **User-facing console** (public) — renders the mesh members / topology to end users.
2. **Box Admin "Mesh" page** (operator, on `myserver01`) — renders the same, plus per-node VM status and liveness.

The service serves **only the public/on-chain tier**. It never touches the dstack host API and never probes the tailnet. Operator-tier fields (`vm`, `health`) are always present as keys but always `null` from this service; the Box Admin sidecar merges them locally (it already runs as root on the host with VMM-Status + tailnet access). This keeps the API pure, stateless, and deployable anywhere — including a public CVM behind `nginx-gw`.

### 1.1 Non-goals / scope boundary

- **NOT an indexer.** No DB, no event-follower, no persistence. The existing `attestmesh-indexer` (`indexer/`) is an attested gRPC event *relay* to members — different shape, different trust model — and is deliberately not reused here.
- **NOT operator-tier.** No VM status, no `/healthz` probing, no host/tailnet reachability. That lives in the Box Admin sidecar.
- **NOT a write surface.** Read-only. No transactions, ever. No key material.
- **NOT multi-cluster (v1).** One configured cluster (C3). Multi-cluster is a trivial future extension (§8) but out of scope now.
- **NOT attestation-method-aware.** It reads `attestorId` and maps to a display label from a static table; it encodes no dstack/TDX/SEV specifics (per the project's attestation-agnostic directive).

---

## 2. Toolchain

- **TypeScript 5.x** (strict), **Node ≥ 20** runtime.
- **`viem`** for Base RPC reads (`eth_call`, `eth_getLogs`) and ABI decoding.
- HTTP via `node:http` (no framework) or a minimal router — keep dependencies lean.
- Run via **`tsx`** (bundled dep) — no build step, portable across Node builds. **Do not use `node src/server.ts`**: native TS type-stripping is flag-gated on 22.x and can be compiled out of a build (`ERR_NO_TYPESCRIPT`); `tsx` avoids that everywhere. `npm start`, `npm test`, and the `Dockerfile` all use it.
- Ships as: (a) `npm start` bound to `127.0.0.1:<port>` for the Box Admin host, and (b) an OCI image for the public deployment behind `nginx-gw`. Same code, config-only difference.
- Lint/test: `npm run typecheck` / `npm test`.

---

## 3. Configuration (env)

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `RPC_URL` | yes | — | Base mainnet RPC. Public reader OK for view calls; **archive/Alchemy recommended for the first historical `getLogs` sweep** (see §6). |
| `CLUSTER_ADDR` | no | `0x5ab4706fCa998A0792E5c06432b13c73c54E4557` | Cluster diamond (C3). |
| `CHAIN_ID` | no | `8453` | Base mainnet. |
| `DEPLOY_BLOCK` | no | `47527215` | `getLogs` floor. Verified via `getCode` binary search; first `MemberRegistered` at 47527311. |
| `GATEWAY_DOMAIN` | no | *(unset)* | If set, `members[].endpoint` is derived as `https://<appId>-51900s.<GATEWAY_DOMAIN>`; else `null`. |
| `LISTEN_ADDR` | no | `127.0.0.1:8787` | Bind address. Public deployment overrides to `0.0.0.0:<port>` behind nginx. |
| `CACHE_TTL_MS` | no | `10000` | Read-through cache TTL for member/topology reads. |
| `TIMELINE_CACHE_TTL_MS` | no | `60000` | TTL for the (bounded) event timeline. |

No config files, no secrets beyond `RPC_URL`.

---

## 4. Architecture

```
                        ┌──────────────────────────────┐
   Base mainnet RPC ◄───┤  mesh-state-api (stateless)   │
   (view calls +        │  - facet eth_calls            │
    bounded getLogs)    │  - mesh-IP + endpoint derive  │
                        │  - short read-through cache   │
                        └───────┬───────────────┬───────┘
                                │ JSON (vm/health = null)
              ┌─────────────────┘               └─────────────────┐
   public CVM behind nginx-gw              127.0.0.1:8787 on myserver01
              │                                       │
     user-facing console                     Box Admin sidecar (server.mjs)
     (renders public tier)                    merges vm (VMM Status, keyed by
                                              appId==memberContract) + health
                                              (tailnet /_sidecar/healthz,
                                              /_agent/healthz) → Mesh page
```

The service holds a single in-memory cache. Every request either returns a cached snapshot or refreshes it via a batch of `eth_call`s at a pinned block. No cross-request state, no disk.

---

## 5. Interfaces (the frozen v1.0 contract)

All responses `application/json`. Hex casing is pinned:
- **`bytes32` fields** (`memberId`, `attestorId`, `xPubKey`, `wgPubKey`, `cskCommitment`, `envelopeId`): `0x`-prefixed **lowercase**.
- **`address` fields** (`memberContract`, `cluster`): **EIP-55 checksummed** (viem `getAddress` default — e.g. `0xF847Dd18…`). Examples in this spec reflect that.
- **`appId`**: always the `memberContract` **lowercased, no `0x`** (e.g. `f847dd18…`) — the operator join key; casing-stable regardless of the above.

`vm` and `health` keys are **always present** and **always `null`** from this service (they are the operator tier — see §5.1 classification).

### 5.1 `GET /mesh/members`

```jsonc
{
  "cluster": "0x5ab4706fca998a0792e5c06432b13c73c54e4557",
  "chainId": 8453,
  "atBlock": 48079256,                 // block the reads were pinned to
  "meshCidr": "10.18.0.0/16",
  "cskCommitment": "0x3ac453f5…",      // keccak256(CSK), or null if unset
  "originatorMemberId": "0x678c897c…", // == listMembers()[0]
  "memberCount": 6,
  "members": [
    {
      "memberId": "0x678c897c…",
      "memberContract": "0xF847Dd18…",  // ClusterMember addr == dstack app_id
      "appId": "f847dd18…",             // memberContract, lowercased, no 0x — the operator join key
      "attestorId": "0x…",
      "attestor": "dstack",             // decoded label; "unknown:<id-prefix>" if unmapped
      "xPubKey": "0x…",                 // x25519 (sealed-box)
      "wgPubKey": "0x…",                // wireguard
      "meshIp": "10.18.196.231",       // decoded from meshIpOf()
      "registeredAt": 1781843777,       // unix seconds (block.timestamp at registration)
      "isOriginator": true,
      "endpoint": "https://f847dd18…-51900s.<gatewayDomain>", // derived, or null
      "vm": null,                       // operator-tier — Box Admin fills
      "health": null                    // operator-tier — Box Admin fills
    }
  ]
}
```

**Ordering:** `members` preserves on-chain `listMembers()` order (registration order); `[0]` is the originator.

**Operator-tier shape (documented here so the Box Admin codes to it; this service always emits `null`):**

```jsonc
"vm":     { "id": "4fcbdabf…", "name": "matrix-node", "status": "<any dstack status string>", "uptime": "…" },
"health": { "phase": "healthy", "livePeers": 1, "firstConverged": true,
            "cskAcquired": true, "source": "tailnet", "checkedAt": 1750000000 }
```

`vm.status` is **any dstack status string** verbatim (`running`, `stopped`, `booting`, `exited`, …) — the Box Admin emits the real value; consumers must not hardcode a two-value enum. `running` is the only value that means "up."

**Node classification is TIER-SCOPED — do not apply the orphan/liveness rules on the public tier.** On the public tier `vm`/`health` are `null` for *every* member (live ones included), so a naive `vm == null → orphan` rule would mislabel the whole mesh. Explicitly:

- **Public tier** (`vm`/`health` always `null`): render **on-chain membership only** — memberId, keys, mesh IP, registration. **No liveness or orphan distinction is possible from this API alone.** Do not infer status from `null`.
- **Operator tier** (Box Admin populates `vm` from VMM Status keyed by `appId == memberContract`) — four states:
  1. `vm == null` → **orphan / decommissioned** (on-chain member, no VM; render distinctly, not "down").
  2. `vm.status != "running"` → **down** (stopped/exited/etc.).
  3. `vm.status == "running"` **&&** `health.phase == "healthy"` → **live**.
  4. `vm.status == "running"` **&&** `health == null` → **live, liveness unknown** — a running node that simply doesn't expose a health proxy (today only matrix-node does; postgres/ssh/synclave/Hindsight/RunYard legitimately return `health: null` while healthy — Hindsight is mesh-only with no tailnet at all). **Must NOT read as "down."** This is the common case.

**On-chain reality (why orphans can exist):** membership only grows — no `removeMember` in v1 — so `memberCount` *can* exceed live VMs. Verified 2026-07-01: **6 on-chain members = matrix-node, postgres-node, ssh-node, synclave, Hindsight (`10.18.78.76`, mesh-only), RunYard — all currently live; zero orphans right now.** Orphans will appear only when a node is torn down (its member record persists). Note the mesh-only nodes (Hindsight has no Tailscale/gateway): they are live and have a VM, but expose **no health proxy**, so they land in operator-state (4) "live, liveness unknown" — `vm` populated, `health: null`. This makes state (4) the common case, not the exception.

### 5.2 `GET /mesh/topology`

Full mesh — every member peers with every other. Nodes + all-pairs edges.

```jsonc
{
  "cluster": "0x5ab4706f…",
  "atBlock": 48079256,
  "nodes": [ { "memberId": "0x…", "appId": "f847dd18…", "label": "0xF847Dd18…", "meshIp": "10.18.196.231", "isOriginator": true } ],
  "edges": [ { "a": "0x<memberId>", "b": "0x<memberId>", "state": "unknown" } ]  // state always "unknown" from this tier
}
```

`label` is the `memberContract` here (this tier has no VM name). Edge `state` is `"unknown"` from the API; the Box Admin may upgrade to `"up"/"down"` from its liveness merge. Edges are the complete graph over `nodes` (n·(n-1)/2 entries).

### 5.3 `GET /mesh/health`

On-chain-derivable summary only (this tier cannot see liveness):

```jsonc
{
  "cluster": "0x5ab4706f…",
  "atBlock": 48079256,
  "memberCount": 6,
  "cskCommitted": true,            // cskCommitment() != 0
  "originatorMemberId": "0x678c897c…",
  "note": "liveness (phase/live_peers) is operator-tier; null here"
}
```

### 5.4 `GET /mesh/timeline` (membership events)

Bounded, cached event scan from `DEPLOY_BLOCK` → head. v1 events: `MemberRegistered`, `CskCommitmentSet`, `WgKeyPublished`, ownership transfers. No `MemberRemoved` exists in v1.

```jsonc
{
  "cluster": "0x5ab4706f…",
  "fromBlock": 47527215,
  "toBlock": 48079256,
  "events": [
    { "type": "MemberRegistered", "block": 47527311, "txHash": "0x…", "logIndex": 12,
      "memberId": "0x…", "memberContract": "0x…", "attestorId": "0x…" },
    { "type": "CskCommitmentSet", "block": 47600000, "txHash": "0x…", "commitment": "0x3ac453f5…" }
  ]
}
```

### 5.5 `GET /healthz`

`200 {"ok": true, "rpcReachable": true, "atBlock": 48079256, "cacheAgeMs": 4200}` — for the container/host probe. `503` if the RPC is unreachable and no cache is warm.

### 5.6 Errors

Uniform: `{ "error": "<code>", "message": "<detail>" }` with `502` on RPC failure (stale cache served with a `stale: true` flag if available), `404` unknown route.

---

## 6. On-chain read design

All against `CLUSTER_ADDR` on Base 8453. Signatures verbatim from `contracts/src/facets/core/AttestFacet.sol` + `NetworkFacet.sol` (mirrored in `sidecar/src/chain/attest.rs`, `network_facet.rs`).

**Snapshot (one refresh):**
1. `blockNumber` → pin `atBlock`.
2. `listMembers() → bytes32[]` (all reads below pinned to `atBlock`).
3. Per member (batched/multicall where possible): `memberById(bytes32) → (attestorId,memberContract,xPubKey,wgPubKey,registeredAt)`, `meshIpOf(bytes32) → uint32`.
4. `meshCidr() → (uint32,uint8)`, `cskCommitment() → bytes32`.

**mesh-IP decode:** `meshIpOf` returns a packed big-endian `uint32`; render `(ip>>24)&255 . (ip>>16)&255 . (ip>>8)&255 . ip&255`. `meshCidr` network `168951808 = 0x0A120000 = 10.18.0.0`, prefix `16`.

**attestor label:** static map `keccak256("attestmesh.attestor.dstack") → "dstack"`; unmapped → `"unknown:0x<first4bytes>"`.

**endpoint derive:** `GATEWAY_DOMAIN` set → `https://<appId>-51900s.<GATEWAY_DOMAIN>` (the wg-over-gateway-TCP leg the sidecar uses), else `null`.

**timeline (§5.4):** `eth_getLogs` on `CLUSTER_ADDR`, `fromBlock = DEPLOY_BLOCK`, chunked (≤ provider cap; default 9000-block windows to stay under the ~10k public cap), topics = the v1 event set. Cached `TIMELINE_CACHE_TTL_MS`; incremental `fromBlock = lastSeen+1` after the first sweep.

**RPC guidance:** view calls (§5.1–§5.3) are cheap `eth_call`s at latest/pinned — **public reader (`base-rpc.publicnode.com`) is fine**. The **from-`DEPLOY_BLOCK` timeline sweep** must be chunked; if you point it at a public RPC it works within the 9000-block windows but may rate-limit — Alchemy is smoother for the one-time historical sweep. Never a from-genesis scan (that was the indexer's original 47M-block bug; `DEPLOY_BLOCK` is the floor).

---

## 7. Constants (baked defaults)

```
CLUSTER_ADDR       0x5ab4706fCa998A0792E5c06432b13c73c54E4557   (C3, Base 8453)
DEPLOY_BLOCK       47527215        (first MemberRegistered @ 47527311)
meshCidr           10.18.0.0/16    (meshCidr() = 168951808, 16)
originatorMemberId 0x678c897c0c228950b5e1f342b79d8db80e179ff305daef54d84ba30261f4b0bf
MemberRegistered   topic0 0x037e8a34abcb145eb3b9d4e5a283866981a1fc2f024495dd30af21164b64c79a
```

Live at spec time: `memberCount() == 6`, `cskCommitment()` set (`0x3ac453f5…`).

---

## 8. Requirements

### Must Have
- [ ] `GET /mesh/members` per §5.1 frozen schema, `vm`/`health` always `null`.
- [ ] `GET /mesh/topology` per §5.2 (full-mesh edges).
- [ ] `GET /mesh/health` per §5.3.
- [ ] `GET /healthz` per §5.5.
- [ ] Correct mesh-IP decode, attestor label map, `endpoint` derivation.
- [ ] Read-through cache (`CACHE_TTL_MS`); serve last-good on transient RPC failure with `stale` flag.
- [ ] Reads pinned to a single block per snapshot (no torn reads across members).
- [ ] Public-reader RPC works for view calls; no key material required.
- [ ] Binds `127.0.0.1:8787` by default (Box Admin), configurable for public deploy.

### Should Have
- [ ] `GET /mesh/timeline` per §5.4 (bounded, chunked, cached).
- [ ] Multicall batching of per-member reads.
- [ ] Prometheus-free lightweight counters on `/healthz` (cache age, last RPC error).

### Must NOT Have
- No DB / persistence. No writes. No VM/tailnet/host access. No secrets beyond `RPC_URL`. No per-cluster deployment (one shared instance).

## 9. Open Questions

- [ ] **Public hostname/route** behind `nginx-gw` for the console deployment (coordinate with lsdan). Box Admin uses `127.0.0.1:8787` regardless.
- [ ] **`GATEWAY_DOMAIN` value** for this dstack box's wg-over-gateway leg (endpoint derivation). Left `null` until confirmed — the mesh IP is the primary addressing field consumers need.
- [ ] Multicall contract address on Base to batch per-member reads, or accept N sequential `eth_call`s (fine at n=6).

## 10. Alternatives Considered

- **(a) Each consumer does direct eth_calls** — rejected: duplicates ABI/mesh-IP/decoding logic across the console and Box Admin; no shared timeline.
- **(c) Full indexer into a DB** — rejected for v1: only buys long-range membership analytics; at n≈6 a bounded cached `getLogs` serves the timeline without stateful infra. Revisit if analytics are needed.
- **Reuse `attestmesh-indexer`** — rejected: gRPC push to *attested members* only; wrong shape and trust model for a public reader.
- **Cloudflare Worker (like gas-webhook)** — viable for the public tier, but the Box Admin needs a `127.0.0.1` listener on the host; one portable Node service covers both. Worker adapter is a possible future packaging.

## 11. Traceability

| Requirement | Implementation | Tests |
|-------------|----------------|-------|
| `/mesh/members` frozen schema, `vm`/`health` null | `src/chain.ts` `fetchSnapshot`, `src/server.ts` | live-verified; `test/format.test.ts` (decode/shape) |
| `/mesh/topology` full-mesh edges | `src/server.ts` `buildTopology` | `test/format.test.ts` (6 nodes → 15 edges) |
| `/mesh/health` | `src/server.ts` `buildHealth` | `test/format.test.ts` |
| `/mesh/timeline` bounded chunked scan | `src/chain.ts` `fetchTimeline` | live-verified (13 events from deployBlock) |
| `/healthz` | `src/server.ts` | live-verified |
| mesh-IP decode / attestor label / endpoint / casing | `src/format.ts` | `test/format.test.ts` (9 units green) |
| read-through cache + stale-on-error | `src/cache.ts` | live-verified |
| block-pinned snapshot (no torn reads) | `src/chain.ts` `fetchSnapshot` (all reads at `atBlock`) | live-verified |
| config / defaults | `src/config.ts` | — |

Deployed shape: Node ≥ 20 native-TS (no build step), `viem`, `Dockerfile` for the public
CVM behind `nginx-gw`. Live at `127.0.0.1:8787` on `myserver01` at verification time.

## 12. Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-07-01 | AttestMesh | Initial draft → REVIEW; schema frozen v1.0, constants grounded against live C3 reads. |
| 2026-07-01 | AttestMesh | Consumer sign-off → APPROVED. Folded in: tier-scoped node classification (public = no orphan/liveness inference; operator 4-state incl. "live, liveness unknown" for running+health-null); pinned hex casing (bytes32 lowercase, address EIP-55 checksummed, appId lowercased no-0x); `vm.status` documented as any dstack status string. No schema change (still v1.0). |
| 2026-07-01 | AttestMesh | Implemented `services/mesh-state-api/` (Node/TS + viem) and live-verified all 5 routes against C3. **Corrected mesh CIDR: C3 is `10.18.0.0/16` (members on `10.18.x.x`), not `10.13` — that was the retired milestone-B cluster; a hand-decode error, caught by the unit test.** → IMPLEMENTED. |
| 2026-07-01 | AttestMesh | Box Admin Mesh page shipped consuming the API. Switched runtime to `tsx` (from `node --strip-types`) after the box's Node 22.22 was built without TS support (`ERR_NO_TYPESCRIPT`) — `npm start`/`test`/Dockerfile now all use tsx. Operator instance runs on `myserver01` (`/opt/mesh-state-api`, systemd `mesh-state-api.service`, `RPC_URL=base-rpc.publicnode.com`), operated by the Box Admin owner; code stays owned here (ping-on-rev). Confirmed live: all 6 C3 members running, zero orphans. |
