# Fleet roll onto Pimlico paymaster + working RPC (2026-07-06)

## Goal
Get every C3 node (except frozen ssh-node) onto: our Base node for RPC (proxyd) + Pimlico
for 4337 sponsorship, running a sidecar image that has all three fixes. This heals the
mesh (peers can finally complete PeerEndpoint handshakes again) and ends the paymaster outage.

## Preconditions (done before any roll)
1. **Gate the `/pimlico-status` debug endpoint** behind the webhook token (was open). Keep it
   (not remove) — it's our only webhook-decision observability during the roll since TEE blocks
   container logs.
2. **Push** the webhook fix commit(s) to `origin/matrix-admin-agent`.
3. **Build one sidecar image** from branch HEAD carrying all fixes:
   - `0f6b996` Pimlico SponsorshipMode
   - `4f77e3c` getNonce routed to node RPC (not the bundler)
   - `6f2a31f` back off on send FAILURES (not just resends) — the anti-storm guard
   Fetch the digest from GHCR.

## Per-node roll procedure (ONE node at a time, user reviews logs between each)
For node N:
1. Re-pin `deploy/compose/<node>.yaml` sidecar digest → new image.
2. Roll: `RPC_URL=<deploy-tunnel> CVM_RPC_URL=<proxyd> CVM_BUNDLER_URL=<pimlico> GAS_POLICY_ID=sp_unusual_deadpool deploy/<node>-node.sh <node> update`
   (plus node-specific env, e.g. matrix agent creds).
3. **Watch (all free reads):**
   - `:9090/healthz` → phase climbs toward `heartbeating`/`healthy`, `live_peers` rises.
   - our node: node's `MessageSent` events landing on-chain (proves sponsorship→send→inclusion).
   - webhook `/pimlico-status` → `ok:true, sponsor:true`.
4. **User reviews Pimlico dashboard:** webhook requests = 200 (not 401), `pm_sponsorUserOperation` = Success, credit delta modest.
5. **STOP. Wait for user go before node N+1.**

## Roll order
1. **synclave** (re-roll; already up but stuck — canary for the full-fix image)
2. **matrix** (CSK originator, memberIds[0] — mesh-critical; needs agent env)
3. singles: r2-host, hindsight, runyard, telegram-sync, langfuse, webhost, postgres, fugu-router
4. HA trios last, one member each: pg-ha (replicas→leader), clickhouse, redis
5. 🔴 **ssh-node: NEVER** (frozen)

## Credit safety
- Each `pm_sponsorUserOperation` = **500 credits**; each `pimlico_getUserOperationGasPrice` = 10.
- The failure-backoff image means failed sends back off (600s→24h), so no storm.
- Pimlico policy per-user cap = 100 ops/day/sender → hard ceiling ≈ 50k cr/day/node.
- Convergence accelerates: early nodes send to mostly-not-yet-upgraded peers (more ops); as
  more nodes get the working bundler, peers reciprocate and handshakes complete → sends stop.
- Abort lever: stop the node's VM, or disable the Pimlico policy (dashboard) — instant.

## Post-roll cleanup
- Remove the `/pimlico-status` debug endpoint (or leave token-gated).
- Update memory + close the incident.
