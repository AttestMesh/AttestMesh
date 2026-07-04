# Incident: PeerEndpoint resend storm (sponsored-gas / RPC / KV cost leak)

**Dates:** onset gradual (each node roll/rebuild since ~2026-06 worsened it); diagnosed 2026-07-03/04; containment deployed 2026-07-04.
**Status:** contained (webhook live); root-cause fix built + committed (`d484c8c`), fleet roll **pending**.
**Systems:** sidecar (`cluster-mesh-agent`), gas-sponsorship webhook (Cloudflare Worker), Alchemy account (Gas Manager + RPC CU), Cloudflare Workers KV.

---

## 1. Symptoms

- Alchemy bill spike (reported by operator; dashboard split Gas Manager vs CU not yet pulled).
- Cloudflare Workers KV daily operation limit exceeded (free tier: 1k writes/day).

## 2. Root cause

The sidecar's PeerEndpoint envelope exchange never converges under two conditions it was not designed for:

1. **Restart amnesia.** A peer's Ed25519 heartbeat key arrives only inside its encrypted PeerEndpoint envelope and was held **in memory only**. A restarted/rolled node forgot every learned key and resumed re-sending its own envelope to each peer every 600s (`ENVELOPE_RESEND`) — one **sponsored UserOp** per send. Peers only announce while *they* are missing *our* key (`!peer_ed_known` gate), so peers that already knew the restarted node's key never replied, and the restarted node had no way to ask. Result: unbounded 144 ops/day per stuck (sender, peer) pair.
2. **On-chain orphans.** C3 has no `removeMember`; decommissioned members (e.g. the old standalone postgres-node `0xFF52F36F…`, the pre-rebuild synclave) are registered forever, never answer, and every live node re-sent to them on the same 600s timer, forever.

### Cost cascade (one bug, three bills)

| Surface | Mechanism | Measured |
|---|---|---|
| Alchemy Gas Manager | each resend = paymaster-sponsored `MessageFacet.send` UserOp | **1,286 MessageSent/day** on C3 (24h window, head 48166489) |
| Alchemy RPC CU | webhook re-ran `eth_call` per storm request (see below); plus bundler submit/receipt traffic per op | proportional to op count |
| Workers KV | every C3 member is Path A, so `isOurMember(sender)` is **always negative** (10-min TTL). Policy checked it *first*, so a sender on a ~10-min send cadence re-missed and re-wrote the negative cache on nearly every request | ~1.5–3k writes/day vs 1k/day free tier |

### Measured distribution (24h, 2026-07-03, C3 = `0x5ab4706f…`, memberCount 18)

Senders: `0x3ee3f2…` = **synclave** (`0x2e5527Ac…`, rebuilt 07-01) 502/day; `0x7f3ea3…` = **ssh-node/hermes** (`0x02Cafb3c…`) 151/day; `0x678c89…` = **matrix** (`0xF847Dd18…`, rolled 6×) 151/day. Top 3 = 62% of volume; top 12 ≈ 99%; healthy baseline ≈ 8/day/node. Top orphan recipient: `0xfb9c33…` = old postgres-node, 375/day inbound. Both top spammers are recently-rebuilt nodes — the restart-amnesia signature.

## 3. What has been done

### 3.1 Webhook (deployed to production 2026-07-04, version `3f1a8ac7`)

- **Provenance check reorder** (`policy.ts` step 3): `isAllowlistedAppId` (positive for every live member, 24h TTL) now runs before `isOurMember`. Steady-state cost per request: one KV read, zero writes, zero eth_calls.
- **Per-sender daily cap** (`ratelimit.ts`, new policy step 8; closes security-audit finding **M2**): KV counter `cap:{chainId}:{sender}:{YYYY-MM-DD}` of *approved* ops; deny `sender-daily-cap` at `MAX_DAILY_OPS_PER_SENDER` (default **100**, `0` disables). Runs last (denials never consume quota or write); soft cap (KV eventual consistency); fails open on KV errors.
- Verified live: `/healthz` ok, `/check` on C3 ok, unknown-sender and bad-token deny paths correct, and real approvals confirmed flowing post-deploy (68 MessageSent in the first 10 min).
- Tests: 78 passing + tsc clean. Specs updated (`docs/specs/gas-webhook.md` §4, §5.1, §6, §8).

Effect: sponsored-gas and KV spend are now **hard-bounded** regardless of fleet behavior. Even un-patched spammers cost ≤100 ops/day each.

### 3.2 Sidecar (built, tested, committed `d484c8c` — **not yet rolled to any node**)

- **`peer_cache` module (new):** learned peer Ed25519 keys sealed to the dstack store (label `attestmesh.peer_ed25519.v1`), reloaded at bring-up, applied in the reconcile pass. Restarts no longer forget the fleet.
- **Resend backoff:** 600s doubling per attempt, capped at 24h. An orphan now costs each live node 1 envelope/day instead of 144.
- **Reply-on-receive:** an inbound announce triggers one reply even when the sender's key is already known (suppressed if we sent to them within 600s, preventing ping-pong). An announce is thereby an implicit "I lost your key" request — this heals *un-upgraded* restarted nodes as soon as their **peers** are upgraded.
- Tests: 57 passing + clippy clean. Spec updated (`docs/specs/sidecar.md` §7.1 resend rules).

### 3.3 Diagnostics established

- Storm measurement: `cast logs` for `MessageSent(bytes32,bytes32,bytes32,bytes)` on the cluster, ≤9k-block chunks; group by `topics[1]` (sender) / `topics[2]` (recipient).
- Sender→node mapping: `memberById(bytes32)` → `memberContract` → grep `deploy/logs/*.state`.

## 4. What has NOT been done (open items)

| # | Item | Notes |
|---|---|---|
| 1 | **Sidecar fleet roll** (the actual root-cause fix in prod) | Needs image rebuild from `d484c8c` + staged roll: **synclave first** (502/day), then **matrix** (151/day), remainder folded into routine updates. Rolls preserve membership/mesh IPs. 🔴 **ssh-node/hermes is frozen — never roll it**; the webhook cap bounds it now and reply-on-receive converges it once its peers are upgraded. Awaiting operator GO. |
| 2 | **fugu-router restart loop** (separate, active issue) | Observed 2026-07-04 ~00:15 UTC: 4 boots/10 min, each boot announces to all peers → 60 sends/10 min (member `0x78d2ea6d…` / `0xD3e18376…`, registered 07-03 23:18). It will hit the daily cap and see `sender-daily-cap` paymaster declines until UTC midnight — expected containment, not a new bug. In the operator's court (in-flight deploy). |
| 3 | **Ed25519 key on-chain** (design simplification, milestone-B) | The heartbeat key is public; publishing it at registration (member record field or `WgKeyPublished`-style event) would make bring-up need **zero** member-to-member messages and delete this entire failure class. Contract-facing change → needs a spec + migration plan. The committed fix should be considered transitional. |
| 4 | Reconcile-interval stretch | Sidecar polls chain every 15s (`listMembers` + `blockNumber` + `getLogs`) even while indexer-subscribed; wake channel already exists. 15s→120s+ when the subscription is healthy ≈ 8× cut on the largest RPC CU line (~280M CU/mo fleet-wide). Not implemented. |
| 5 | Self-hosted Base RPC node | Pruned op-reth full node (receipts pruned before block 46,868,742), hosted L1 inputs, ~2TB NVMe / 32GB / 8 cores, bare-metal (not in a CVM). Moves indexer+sidecar read traffic off Alchemy; bundler/paymaster stays. Researched, not started. |
| 6 | Per-service Alchemy API keys | Everything shares one key (`cbplChE…`) — no dashboard attribution. |
| 7 | Alchemy dashboard confirmation | Pull Gas Manager vs CU split to confirm the cost ranking empirically. |
| 8 | Orphan cleanup / `removeMember` tombstones on C3 | Exists in milestone-B contracts (indexer-ha PR); C3 predates it. Orphans stay until then; backoff makes them cheap. |
| 9 | Push `d484c8c` | Committed on `matrix-admin-agent`, not pushed. |
| 10 | KV plan headroom (optional) | Post-fix traffic fits the free tier; $5 Workers plan is the fallback if not. |

## 5. Verification / monitoring

- **Success metric:** MessageSent/day on C3 should fall from ~1,286 toward the ~50/day healthy baseline as the roll proceeds (measure with the `cast logs` recipe above).
- Webhook logs: `sender-daily-cap` denials identify remaining runaway senders by address.
- Cloudflare KV write graph should flatten immediately (reorder shipped); Gas Manager spend should step down with the cap and again with each rolled node.

## 6. Lessons

1. **Fire-and-forget + in-memory state = divergence.** Any "send until acknowledged" protocol needs its learned state persisted, a backoff, and a recovery path for asymmetric knowledge — or better, no handshake at all (see open item 3).
2. **Sponsorship without a rate cap turns any retry loop into a billing incident** (audit M2 was exactly this; it is now closed).
3. **Cache polarity matters:** checking the always-negative predicate first turned a 24h cache into a 10-min cache with a write per request. Order membership checks so the common case is a positive hit.
4. One protocol bug can surface as three unrelated bills (gas, RPC, KV). Cross-correlate before optimizing any single surface.
