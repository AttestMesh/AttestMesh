# Paymaster provider selection (replacing Alchemy Gas Manager)

**Date:** 2026-07-06 · **Method:** 5 researchers + 14 adversarial verifiers (19 agents, all complete); all claims below verified against primary sources unless marked. **Context:** the Alchemy app (RPC + bundler + Gas Manager) is dark; RPC moved to our own Base node; the 4337 sponsorship path needs a bundler+paymaster-only provider (no node lock-in). Volume ≈ 1,500–3,000 small ops/month on Base 8453, EntryPoint v0.7. Hard requirements: raw JSON-RPC (Rust sidecar, no SDK), per-op authorization preserving our membership-provenance + per-sender-cap security model, fast emergency policy changes.

## Recommendation: Pimlico (all claims verified)

| Requirement | Pimlico |
|---|---|
| Base 8453 + EP v0.7, bundler+paymaster only | ✅ one URL: `api.pimlico.io/v2/8453/rpc?apikey=…` (v0.6/0.7/0.8) |
| Raw JSON-RPC | ✅ documented request/response; no SDK |
| Sponsorship call | `pm_sponsorUserOperation` ≈ 1:1 with `alchemy_requestGasAndPaymasterAndData` (est. + paymaster sign in one call; `{sponsorshipPolicyId}` context) |
| **Per-op webhook gating** | ✅ policy `webhook_endpoint`: POSTs full userOperation, expects `{"sponsor": true\|false}`; svix/Standard-Webhooks HMAC. Our Worker ports with a payload/signature adapter. |
| Native policy backstop | global/per-user/per-op USD caps + per-user op-count limits with daily resets (our webhook cap becomes belt-and-braces), REST-CRUDable (fast emergency caps) |
| Pricing @ our volume | $0/mo PAYG (card required), 10M credits/mo ≫ our use; **10% surcharge on sponsored gas** (cents-to-dollars/mo at Base prices) |
| Reliability / exit | status page 100%/90d incl. Base; 250M+ ops relayed; Alto bundler is open-source (self-host escape hatch) |

**Integration caveats (both manageable):**
1. **10-minute sponsorship expiry** — resends must re-request sponsorship, never replay a stale signed op. Our sidecar already builds + sponsors per submit attempt (no signed-op caching), so we're naturally compliant; noted as an invariant.
2. **Undocumented:** webhook timeout/retry + fail-open-vs-closed on webhook error; whether the webhook fires on the pure ERC-7677 path (docs tie it to `pm_sponsorUserOperation` — use that method). Test both before cutover; if fail-open, the native per-user caps are the backstop.

## Runner-up and fallback

- **ZeroDev** (verified): equivalent webhook (`{"proceed": bool}`) *plus* the best policy CRUD API (could API-sync the member allowlist as defense-in-depth). Cost: $69/mo floor + 8% — the subscription dominates at our volume. Must disable its "Policy Pass on Error" fail-open toggle. Choose if Pimlico's webhook tests badly.
- **CDP (Coinbase)**: cheapest fees (flat 7%, invoiced) and v0.7 is very likely fine (the "v0.6 only" doc line was verified stale against their v0.7 verifying-paymaster repo) — but the claimed recurring free tier was **refuted** (no monthly gas allowance; only the application-gated Base Gasless Campaign credits), there is **no webhook gating** (Coinbase itself recommends a backend proxy), and policy changes propagate "in a few minutes" (bad for emergency caps). Usable only via the **ERC-7677 proxy pattern** below.
- **Biconomy**: webhook gating verified **dead** (legacy SmartAccountsV2 docs 404; current MEE stack has no per-op authorization webhook). Skip. **thirdweb**: capable (webhook on Growth plan) but pricing claims didn't verify cleanly; no advantage over Pimlico.

## The no-lock-in insurance: ERC-7677 proxy pattern

ERC-7677 (`pm_getPaymasterStubData`/`pm_getPaymasterData`) is implemented by Pimlico, Alchemy, CDP, ZeroDev, thirdweb, Candide. Fronting any of them with our own `pm_*` proxy (our Worker: provenance via our node's data + KV caps, then forward) makes provider switching a one-URL change — Pimlico even ships an official Workers-compatible `erc7677-proxy` template. Not needed for the Pimlico path (their webhook does the gating), but it is the universal fallback and the CDP-enabler. The Rust delta for 7677 (if ever needed) is ~120-150 lines behind the same seam.

## Integration plan (Pimlico)

1. **Account (operator, ~10 min):** dashboard.pimlico.io → API key + sponsorship policy (global USD cap, per-user daily op-count ≈ our webhook cap value, `webhook_endpoint` → our Worker URL, webhook secret into Worker env). Card required for mainnet.
2. **Worker adapter (~1-2h):** new route parsing `user_operation.sponsorship.requested` (svix HMAC verify) → existing `evaluatePolicy` (provenance reads already on PublicNode, KV caps live) → `{"sponsor": bool}`. Keep the Alchemy route for rollback.
3. **Sidecar (~2-4h + tests):** `SponsorshipMode` in `chain/bundler.rs` behind `apply_sponsorship`: `pimlico_getUserOperationGasPrice` → `pm_sponsorUserOperation` → `eth_sendUserOperation` → receipt. Env: `BUNDLER_URL` → Pimlico URL, policy id reuses the `GAS_POLICY_ID` slot. New image via CI.
4. **Cutover rides the remaining rolls:** re-seal `CVM_BUNDLER_URL` per node (plumbing already in every deploy script). Synclave first as the canary: its envelope sends resume → peers' keys re-learn → `live_peers` recovers on rolled nodes.
5. **Verify:** sponsored op end-to-end on synclave; webhook deny path (non-member sender); expiry behavior; then fleet.

## Cost picture at steady state

Post-storm-fix volume (~50 ops/day): platform $0, surcharge ≈ 10% of a few dollars of Base gas per month. Even a full resend-storm relapse (1,286 ops/day) stays inside free credits with the surcharge in the low dollars — and the webhook + native caps now bound it twice.
