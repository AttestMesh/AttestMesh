# dstack-Attested Quorum Membership (premortem fix #1, v2)

**Status:** SUPERSEDED — its premortem (`docs/premortems/premortem-transcript-1780495534.md`) found no-Sybil-resistance + verifier-not-reusable + bootstrap/TCB/F2 holes. Replaced by the extraction approach: [`dstack-auth-extraction.md`](./dstack-auth-extraction.md) (port dstackgres's proven auth code instead of reinventing).
**Author:** LSDan
**Created:** 2026-06-03
**Last Updated:** 2026-06-03
**Supersedes:** `dstack-registration-rework.md` (rejected by its own premortem — P1/P2/P6)

> **Not implemented.** This invented a >50% peer-quorum membership model whose premortem found it had no Sybil resistance (attested ≠ independent) and over-claimed a reusable off-chain verifier. The live design is `dstack-auth-extraction.md` — owner-gated allowlist + dstack KMS boot gate + on-chain `verifySigChain`, all **ported from dstackgres** which already implements it.
**Premortems:** `docs/premortems/premortem-transcript-1780448684.md` (original), `…-1780453627.md` (the rework that this replaces)
**Parent specs:** `attestmesh-coordination-layer.md` §1/§4/§9/§10, `contracts.md` §6, `sidecar.md` §6/§8/§13

## Overview

The first rework attempt ("adopt dstack's allowlist-gate model, verify one KMS signature on chain") was killed by its own premortem: the dstack KMS signs `app_id`+pubkey but **not** the compose hash (so an on-chain allowlist on a proof-supplied composeHash is self-asserted — P1); dstack's KMS only issues keys to apps registered in `DstackKms` and keyed per-`app_id`, so AttestMesh's per-member ClusterMember addresses get no key, or collide (P2); and the result reduces `DstackFacet` to dstack's own `DstackApp` + a registry, gutting the thesis (P6).

This v2 takes a different shape, decided in design review:

- **Policy on chain, evaluation off chain and mutual.** The chain holds the attestation **policy** (the allowlist) and the membership/governance state. The heavy attestation verification (DCAP quote verification) happens **off chain, in every node**, using dstack's own verifier libraries — each member independently verifies each peer against the on-chain policy. The chain verifies no quotes or signatures.
- **Attestation is the per-instance TDX quote**, not the dstack KMS signature chain. A node obtains a quote via dstack `GetQuote(report_data = keccak256(xPub‖wgPub))` — a local TDX operation that needs **no KMS app registration** and binds the node's on-chain pubkeys. This dissolves P1 (the compose hash is read from the *verified quote*, not self-asserted) and P2 (no per-`app_id` KMS key dependency).
- **Membership is a >50% multisig of mutually-attested members.** A newcomer is admitted when a strict majority of current members — each having DCAP-verified its quote off chain — cast on-chain admission votes. This restores meaning to on-chain membership (`membership ⟹ majority-attested`) and is the differentiator the rework lost (an on-chain policy + a self-governing quorum of attested nodes is emphatically not "just `DstackApp`").

Two planes, cleanly separated: the **control plane** (who is a member) is governed by a majority of the full member set; the **data plane** (who you tunnel with / serve the CSK to) is gated by the live set plus independent per-peer DCAP verification.

## Requirements

### Must Have
- [ ] **On-chain attestation policy.** An allowlist the cluster owner manages: approved compose hashes / aggregated MRs, device IDs, acceptable TCB status, and the DCAP roots / policy parameters peers must verify against. (Generalizes today's `DstackStorage`.)
- [ ] **Per-instance quote attestation.** Each node binds its identity into a TDX quote: `report_data = keccak256(abi.encode(xPubKey, wgPubKey))` (32 bytes, fits the 64-byte slot). Obtained via dstack `GetQuote`. The owner/voting key is **not** bound (self-asserted — see governance).
- [ ] **Off-chain DCAP verification in the sidecar**, reusing dstack's `ra-tls`/`verifier`: verify the quote's Intel signature chain + TCB, extract the measurements, check them against the on-chain policy, and check `report_data == keccak256(xPub‖wgPub)` for the peer's on-chain-recorded pubkeys.
- [ ] **Quorum admission.** A newcomer self-registers a *pending* entry (pubkeys, self-asserted owner key, quote hash). A member votes to admit only after DCAP-verifying that quote off chain; the vote carries the verified quote hash (accountability). At **> 50% of current `memberCount`**, the newcomer becomes a member (`MemberRegistered`).
- [ ] **Genesis.** The cluster-owner Safe directly admits member #1; pure quorum governs every join thereafter (N=1 → member #1 alone; N=2 → both; `floor(N/2)+1` for N≥3).
- [ ] **Quorum eviction.** `> 50%` vote removes a member (failed re-attestation, or detected-bad). The live majority **prunes apparently-dead members** through the same vote during normal operation, shrinking `memberCount`. The cluster-owner Safe has a **break-glass prune** for the catastrophic case where `> 50%` die permanently and simultaneously before pruning can occur.
- [ ] **Data-plane gating independent of the registry.** Before tunneling with or serving the CSK to any peer, a node independently DCAP-verifies that peer's current quote against the on-chain policy. On-chain membership is necessary but **not sufficient** for a trust action.
- [ ] **Sidecar dstack client over prpc/ra-rpc (HTTP/1, `POST /prpc/Tappd.<Method>`)** — `GetQuote`, `GetKey`, `Info` — **not** tonic gRPC. CSK persisted to the LUKS data disk (`/dstack`); originator re-derives the CSK deterministically via `GetKey`.
- [ ] **Remove on-chain attestation verification.** `DstackSigChain`, `dstack_register`'s sig-chain logic, and the KMS-root signature recovery are deleted. The contract verifies no quotes/signatures.
- [ ] **Re-attestation** at every peer handshake/reconnect and periodically; a peer that fails re-verification triggers an eviction vote.

### Should Have
- [ ] **Vote auditability.** Each admission/eviction vote records the quote hash + policy version the voter checked, so votes are auditable after the fact (mitigation for the self-asserted-voting-key trust assumption).
- [ ] **Policy versioning.** Policy updates (e.g. a TCB-recovery) bump a version; pending votes bound to an older version are invalidated.
- [ ] **RA-TLS mesh handshake.** The wireguard handshake (or a pre-tunnel control exchange) doubles as the attestation exchange (exchange quotes → verify → tunnel), reusing dstack's `ra-tls`.

### Must NOT Have
- On-chain TDX quote / DCAP verification (deferred — too heavy; Alternatives).
- Any dependency on the dstack KMS k256 signature chain or KMS app registration for membership.
- `> 50% of *live* nodes` as the governance quorum — rejected: it converts a partition / liveness-suppression attack into a cluster takeover (a minority that can silence the honest majority becomes the "live majority" and seizes admission + eviction). The control-plane quorum is always measured against the **full** member set.
- The cluster owner as an everyday governance actor (owner = policy-setter + genesis-admitter + break-glass pruner only).

## Non-Requirements

Not in this spec: the EIP-4337 paymaster sign-ordering fix (premortem F2), the Indexer event-decode fix (F5), or the convergence/originator-race fix (F6) — separate fixes. Not a general BFT consensus protocol: the chain is a single ordered log, so vote tallying needs no separate consensus (the chain serializes votes; there is no two-partition split-brain).

## Design

### Trust model

- **Source of truth:** the on-chain attestation policy (cluster owner) and the on-chain membership/governance state.
- **Verification:** mutual, off-chain, per-peer, via DCAP against the on-chain policy — N independent verifiers, not one contract and not one KMS.
- **Governance:** a `> 50%` multisig of members (signer-trust). Safety assumption: **> 50% of members are honest and diligent**. Safety holds under partition because the quorum is measured against the full configured set, not the live set.
- **Self-asserted voting key (accepted tradeoff):** a member's vote is a UserOp signed by its (self-asserted) ClusterMember owner key, so governance is a signer-trust multisig, not TEE-enforced. Two mitigations keep this sound: (1) the **data plane hard-gates** on independent DCAP verification, so a wrongly-admitted node still cannot enter the encrypted mesh or obtain the CSK; (2) votes carry the **verified quote hash** for accountability.
- **Owner Safe:** sets policy, admits member #1 at genesis, and holds the break-glass prune. Never an everyday admission/eviction voter.

### Attestation & binding

```
report_data = keccak256(abi.encode(xPubKey, wgPubKey))           // 32 bytes, fits the 64-byte TDX slot
quote       = dstack.GetQuote(RawQuoteArgs{ report_data })        // local TDX op; no KMS app registration

Off-chain verify(peer):  // sidecar, reusing dstack ra-tls/verifier (DCAP)
  1. DCAP-verify the quote (Intel PCS/PCK chain + TCB status)
  2. extract measurements (compose hash / mr_aggregated / device id) from the quote
  3. check each against the on-chain AttestationPolicy
  4. check quote.report_data == keccak256(peer.xPub ‖ peer.wgPub) from the on-chain record
  -> only on full pass: tunnel / serve CSK / cast an admit vote
```

### On-chain components

| Contract | Change |
|---|---|
| `DstackStorage` → `AttestationPolicyStorage` | allowlist (compose hashes, MRs, device ids, acceptable TCB, DCAP policy params) + a policy version; cluster-owner-managed admin selectors retained |
| `AttestFacet` | gains the quorum machinery: `proposeMember`, `voteAdmit`, `voteEvict`, tally + `> 50%` thresholds, live-majority prune, owner `genesisAdmit`/`breakGlassPrune`; `MemberRecord` gains `quoteHash`, `ownerKey`, `admittedAtBlock` |
| `DstackFacet` | **gutted**: `dstack_register`'s sig-chain verification, `DstackSigChain`, KMS-root recovery all removed. (Keeps the policy-admin surface if not moved to AttestFacet.) |
| `ClusterMember` | owner key is self-asserted at init/registration; the EIP-4337 wallet is otherwise unchanged |

The full quote bytes live **off chain** (fetched over the RA-TLS handshake / a side channel); the chain stores only the **quote hash** so a voter references a specific quote and the binding is auditable.

### Quorum mechanics (AttestFacet)

```
proposeMember(xPub, wgPub, ownerKey, quoteHash)   // newcomer self-registers a PendingMember
voteAdmit(memberId, quoteHash)                     // a member, having DCAP-verified, votes; reverts if quoteHash != pending's
    -> when votes > memberCount/2 : promote PendingMember -> MemberRecord, emit MemberRegistered
voteEvict(memberId)                                // > 50% removes a member (failed re-attest / detected-bad)
prune(memberId)                                    // live-majority vote for an apparently-dead member (same > 50% mechanic)
genesisAdmit(...)        // onlyOwner, only while memberCount == 0
breakGlassPrune(memberId[])  // onlyOwner; catastrophic-majority-loss recovery
```

Threshold is strictly `votes > memberCount / 2` evaluated at tally time; votes are bound to `(memberId, quoteHash, policyVersion)` and expire on a policy-version bump.

### Sidecar

- **dstack client:** prpc/ra-rpc over HTTP/1 (`POST /prpc/Tappd.GetQuote` etc.), generated against dstack's `agent_rpc.proto` *with dstack's prpc conventions* (not tonic). Methods: `GetQuote`, `GetKey`, `Info`.
- **Attestation verifier:** vendor/reuse dstack's `verifier` / `dcap-qvl` / `ra-tls` to DCAP-verify peer quotes off chain.
- **Mesh bring-up:** RA-TLS-style handshake — exchange quotes, verify against on-chain policy, then configure the wireguard peer. CSK serving gated on the same verification.
- **CSK persistence:** to `/dstack` (the LUKS data disk); originator re-derives via `GetKey`; onboardee persists the pulled CSK there, re-pulls if absent.
- **Voting:** after verifying a newcomer, the sidecar submits a `voteAdmit` UserOp; it runs re-attestation on a timer and on reconnect, submitting `voteEvict` on failure.

## Open Questions

- [ ] **DCAP collateral sourcing in the CVM** (PCCS endpoint for PCK certs / TCB info) — operational; dstack already configures one.
- [ ] **Quote availability off chain.** Confirm the RA-TLS handshake carries the full quote (it does in dstack's ra-tls) vs. needing a separate fetch; the chain holds only the hash.
- [ ] **Re-attestation period** and the **TCB-recovery policy-update flow** (how a policy bump forces re-verification + invalidates stale votes).
- [ ] **Large-cluster gas.** Admission costs O(memberCount) vote txs. Fine for v1's small clusters; revisit batching/aggregated signatures for scale.
- [ ] **Vote/quote freshness vs replay.** Bind votes to `(memberId, quoteHash, policyVersion)`; decide quote expiry (TCB recovery dates).

## Alternatives Considered

### The rejected rework (on-chain KMS-signature verification)
`dstack-registration-rework.md`. Killed by premortem P1 (composeHash self-asserted — the KMS sig binds `app_id`+pubkey only), P2 (KMS won't issue keys for ad-hoc `app_id`s, and per-`app_id` keys collide across instances), P6 (collapses to dstack's own gate). Superseded by this spec.

### On-chain DCAP quote verification
Verify the Intel quote on chain for a fully-trustless compose-hash binding. Far heavier (gas, PCS/cert management, complexity); deferred to a later milestone. The off-chain-mutual model here gets the same security property (verification against the on-chain policy) at the cost of trusting `> 50%` of members rather than the chain itself.

### `> 50%` of live nodes for governance
Rejected: measuring the governance quorum against the live set lets a faction that can partition/silence the honest majority become the "live majority" and seize admission + eviction — a takeover, strictly worse than the visible stall that `> 50%` of all members can suffer. The live set governs only the data plane.

### CVM-attested voting key
Binding the owner/voting key into `report_data` would make votes provably originate from the approved software (stronger quorum integrity). Considered and not chosen — the design uses a self-asserted signer-trust multisig (consistent with "treat the nodes like a 51% multisig"); the data-plane DCAP gate + vote-audit hash mitigate.

## Traceability

*Filled in during implementation.*

| Requirement | Implementation | Tests |
|---|---|---|

## Changelog

| Date | Author | Changes |
|---|---|---|
| 2026-06-03 | LSDan | Initial draft — on-chain policy + off-chain mutual DCAP attestation + `> 50%` multisig governance; supersedes `dstack-registration-rework.md`. |
