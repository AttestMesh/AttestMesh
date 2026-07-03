# CSK Rotation

**Status**: DEFERRED (intentionally — this document records the intent and the reasoning, not a design commitment)
**Related**: [`attestmesh-coordination-layer.md`](./attestmesh-coordination-layer.md) §8 (CSK primitive, P2P distribution), [`contracts.md`](./contracts.md) (AttestFacet `setCskCommitment`), [`indexer-ha.md`](./indexer-ha.md) (`removeMember` tombstones), [`dstack-attested-quorum-membership.md`](./dstack-attested-quorum-membership.md)

## 1. What we want (eventually)

A mechanism to **rotate the Cluster Shared Key**: derive a new CSK among the *live* member set, redistribute it peer-to-peer, publish a new commitment, and retire the old key — so that possession of a previous CSK stops mattering.

Today the CSK is derived once by the originator, committed on chain exactly once (`setCskCommitment` is originator-gated and set-once), and pulled P2P by every onboardee for the life of the cluster. There is no second generation.

## 2. Why we want it

- **`removeMember` is eviction, not revocation.** The tombstone (indexer-ha spec) cuts a member out of coordination — peers drop it, it can't send, it's not in the member set — but the evicted node **still holds the CSK** and can decrypt anything encrypted under it. Rotation is the missing half of removal.
- **Compromise recovery.** If a member CVM (or the TEE guarantees behind it) is suspected compromised, the only full remedy for shared-key exposure is a new key the compromised party never sees.
- **Hygiene for long-lived clusters.** Clusters now accumulate members over months (agent nodes join permanently today); a key whose holder set only ever grows is a liability that compounds.

## 3. Rough shape (sketch only — not designed)

A **cluster-generation** scheme is the expected direction:

1. A generation counter and per-generation commitment on chain (`cskCommitment(gen)`), replacing the set-once single slot.
2. An owner-triggered (or policy-triggered) rotation event; a designated live member — not necessarily the original originator — derives `CSK[gen+1]` and publishes the new commitment.
3. Redistribution reuses the existing P2P pull machinery (§8.2–8.3 of the master spec), with serving filtered to **live** (non-tombstoned) members and verification against the new generation's commitment.
4. A bounded dual-key window in the sidecar so application-layer consumers can re-encrypt shared state from `CSK[gen]` to `CSK[gen+1]`; old-generation ciphertext remains readable only until the window closes.
5. The application-layer re-encryption story (what data exists under the old key and who rewrites it) is app-specific and the hardest open question.

## 4. Why it is deferred — on purpose

- **The current threat model doesn't demand it.** Removal today targets decommissioned nodes whose CVMs we also destroy; the CSK plaintext lives only inside TEEs (sidecar memory + sealed store) and is never exportable by the application. An evicted-and-destroyed node holds nothing.
- **It is a breaking interface change.** Versioning `cskCommitment` breaks the set-once invariant that registration, onboarding, and restart-recovery logic (originator re-derive + commitment check) all lean on. Per CLAUDE.md, that requires an explicit migration spec — real work we don't want to spend before the requirements are firm.
- **It interacts with the deferred break-glass escrow research.** Quantum-safe escrow / quorum recovery (RecoveryFacet direction) and rotation shape each other (an escrowed key that rotates must re-escrow; a rotation mechanism is also the recovery path's re-keying primitive). Designing one without the other risks doing both twice. The escrow research is itself deferred.
- **Operational fallback exists.** The nuclear option — stand up a fresh cluster (new diamond, new CSK generation by construction) and migrate members — is proven tooling we already exercise. Costly, but honest, and strictly safer than a half-designed in-place rotation.

## 5. Triggers to un-defer

Any of the following should reopen this as a real spec (`/spec create csk-rotation`):

- First actual member-compromise event, or a `removeMember` of a node whose CVM cannot be verified destroyed.
- Application data encrypted under the CSK whose confidentiality horizon exceeds the life of the current member set.
- Adversarial or third-party-operated members joining a cluster (trust boundary shifts from code-integrity to counterparty).
- The break-glass escrow research concluding, so both mechanisms can be designed together.

## 6. Interim posture (what we do today)

- Treat `removeMember` + CVM destruction as the eviction story; document loudly (as the indexer-ha spec does) that removal ≠ revocation.
- On suspected CSK exposure: fresh cluster generation by redeployment, not in-place rotation.
- Keep the CSK app-layer usage thin (sub-key derivation, sealing) so a future rotation has a small re-encryption surface.
