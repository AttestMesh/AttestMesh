# Premortem Transcript — Deploying the dstack auth extraction (1780517097)

**Date:** 2026-06-03
**Target:** Deploying the `dstack-auth-extraction` fix (real KMS sig-chain verification preimages ported from TeeSQL/dstackgres + the F2 sponsor-then-sign paymaster fix) to a real owner-gated dstack cluster.
**Commits under review:** `1217c8c` (contracts), `c8635c7` (sidecar), `f316480` (gas-webhook), `58fe26f` (docs) on `goal-one-shot-prototype` (pushed).

## Context gathered

- **What it is:** On-chain `DstackFacet.dstack_register` now verifies dstack's real 3-link KMS chain (KMS root → app key → derived key → registration message), binds `codeId == bytes20(memberContract)`, pins the binding `messageHash`, sets the member's EIP-4337 owner to the derived key, and **drops** the self-asserted compose/device/TCB checks at registration (now boot-gate-only via `isAppAllowed`). The sidecar's F2 fix requests `alchemy_requestGasAndPaymasterAndData` before signing the userOpHash.
- **Who it's for:** an AttestMesh owner-gated cluster operator bringing up a mesh of mutually-attested dstack CVM nodes from zero members; downstream, anyone trusting that membership is genuinely TEE-attested.
- **Success:** a real dstack CVM's KMS proof verifies on-chain unchanged; members register; the wireguard mesh converges; sponsored UserOps land (no AA24); no unattested / wrong-software node joins.
- **Known soft spots going in:** preimages extracted from a third-party adaptation (not canonical dstack); `purpose` is a placeholder; tests are a closed loop (`MockKmsChain` signs with the same preimages `DstackSigChain` verifies); `isAppAllowed` appId-deadlock flagged-not-fixed; `BundlerClient` not wired into `run()`; alloy 0.8 vs reference alloy 1; no proof ever captured from a real KMS.

## Premortem frame

It is 3 months from now. This deployment has failed. Eight investigators each took one failure mode.

## Raw failure reasons

- **A.** Extracted preimages aren't canonical dstack (+ placeholder `purpose`) → real proofs revert `InvalidSigChain`; the closed-loop mock can't catch it.
- **B.** `app_id` ≠ member contract address → `CodeIdMismatch` on every real registration.
- **C.** Recovered KMS signer never matches the allowlisted root (representation/scope/rotation) → fails closed, or the operator allowlists the wrong address.
- **D.** `isAppAllowed` boot deadlock bricks first boot (needs prior registration) → no node ever registers.
- **E.** Compose-hash enforcement moved to a broken/bypassable gate → unapproved software registers (silent security failure).
- **F.** F2 lives in an unwired bundler + the live Alchemy flow (dummy sig, policy, response shape, non-LightAccount) never tested → no sponsored UserOp lands.
- **G.** alloy-0.8 ↔ solc ↔ viem ABI/selector drift → real calldata fails to decode / isn't sponsored.
- **H.** "IMPLEMENTED + green" treated as deploy-ready without ever touching a real KMS/CVM/bundler.

## Agent deep-dives

### A — Non-canonical preimages (closed loop relocated, not eliminated)

The break occurred at first contact between the on-chain verifier and a real KMS. The three preimages `DstackSigChain.verify` reconstructs were transcribed from `TeeSQL/dstackgres`, a Postgres-fused third-party adaptation, not canonical upstream dstack. A real CVM's KMS signed app-issuance over a different domain string/field layout (and the derived key over a different `purpose` than the placeholder `"app-key"`), so `ECDSA.recover` returned an address that was neither the allowlisted root nor `compressedToAddress(appCompressedPubkey)`. Every node hit `revert InvalidSigChain`. Zero members registered; the mesh never converged. CI was green because `MockKmsChain.buildProof` builds its preimages with the exact byte sequences `verify` checks — it imports the library's own `bytesToHex`. The "literal drift guard" only asserts the verifier contains `"dstack-kms-issued:"`; it pins the implementation to itself. The closed loop wasn't eliminated by porting "real" preimages — it was relocated up one level.

- **Assumption:** That a test suite whose proof producer and verifier derive from the same source can validate conformance to an external signing format neither was checked against.
- **Warning signs:** No proof fixture captured from a real dstack KMS exists — every vector is `vm.sign` over in-repo preimages, and `purpose` is still `"app-key"`. The cited source of truth is `/tmp/dstackgres` (a third-party fork).

### B — app_id ≠ member contract address (`CodeIdMismatch`)

The operator allowlists the KMS root, deploys a `ClusterMember` at `0xMEMBER`, and provisions a CVM "with the member address as its app ID." But dstack doesn't accept an arbitrary app_id — when the CVM is registered with the dstack KMS/registry, dstack *assigns* `app_id` (in real dstack a function of the compose hash and the app-registration tx), not a value the operator dictates. The CVM's `derive_key` path returns a `codeId = bytes20(app_id)` that is some KMS-assigned identifier, never `bytes20(0xMEMBER)`. On chain, `dstack_register` reverts `CodeIdMismatch` — every real node, every time. The closed-loop suite hid it: `MockKmsChain` sets `proof.codeId = bytes32(bytes20(memberContract))` by construction. The preimages came from dstackgres, where `DstackMember` *is* the AppAuth contract dstack knows about — a topology where codeId-equals-contract can hold — but AttestMesh's EIP-4337 `ClusterMember` is a different contract than whatever the operator registered as the dstack app. There is no operator knob to fix it; app_id is bound upstream of the member proxy.

- **Assumption:** That a dstack CVM's `app_id` is an operator-chosen value settable equal to the ClusterMember address, rather than an identifier dstack derives at app-registration time.
- **Warning signs:** The dstack app-registration step (or first `derive_key`) returns a `codeId` that doesn't equal `bytes20(memberContract)`. A test fixture that *defines* the property under test (`codeId` from `memberContract`) is the smell that no captured proof exists.

### C — KMS signer never matches the allowlisted root (with real precedent)

The operator seeds `allowedKmsRoots` with the address they compute from the published KMS pubkey. A real CVM boots, the sidecar assembles its proof, and `dstack_register` reverts `InvalidSigChain` — the `kmsSignature` step recovers a signer that isn't in the set. **This already happened in the source project:** the dstackgres tree contains a superseded Safe bundle `monitor-kms-fix-root-signer.safe.json` whose description reads: "added `0x7b7d98a9…` as the Phala KMS root signer. That address was derived incorrectly… yields `0x52d3CF51…`. The CVM cannot boot until `allowedKmsRoots` contains the real ecrecover-derived signer." They shipped the wrong root, bricked boot, and needed a corrective on-chain tx plus a `recover_root` CLI. AttestMesh inherited the math but not the scar tissue. The worse branch is silent: an operator who "fixes" the revert by allowlisting whatever address the first proof recovers — rather than the audited root — anchors cluster trust to an unverified key. If dstack's signer is app-scoped/intermediate or rotates, it becomes a recurring cluster-wide outage with no off-chain recovery path.

- **Assumption:** That the address `ecrecover` yields from a live KMS signature is a single, static, correctly-derivable value the operator can allowlist in advance — when its representation, scope, and rotation are dstack-internal facts the mock never tested.
- **Warning signs:** A real CVM's first `dstack_register` reverts `InvalidSigChain` while `forge test` is green. The runbook has a manual "derive the KMS root address from the pubkey" step with no on-chain assertion that the derived address equals the recovered signer.

### D — isAppAllowed boot deadlock bricks bring-up

The operator deploys the diamond, seeds the allowlists, powers on the first CVM. The KMS queries `DstackFacet.isAppAllowed` at boot; compose and device pass, then `memberIdOf[appId] == bytes32(0)` → returns `(false, "appId not a cluster member")`. The KMS refuses to release the derived key; the sidecar's `derive_all` blocks on `derive_key`, never builds a proof, never calls `dstack_register`. With no member registered, the next boot hits the identical refusal. Every node. The cluster never reaches one member. CI was green because the suite encodes the deadlock as expected behaviour: `dstack_register` is called directly (bypassing the gate), and the two gate tests use a phantom `appId` (`0xBEEF`) that only ever exercises the compose branch (checked first). `test_removeComposeHashBlocksBootGate` deliberately asserts only that the reason is *not* "compose hash not allowed" — it never asserts the gate returns `true`, because with an unregistered appId it never can. The suite was structurally incapable of exercising a successful cold-start boot. The team had it written down (Open Question #3) but classified it as a follow-up on a *test-coverage* argument, not a *can-a-real-node-boot* argument.

- **Assumption:** That `isAppAllowed`'s green tests meant the gate worked, when those tests only ever asserted rejection, never the cold-start accept path a real first node must traverse.
- **Warning signs:** No test asserts `isAppAllowed(...) == (true, "")` for a not-yet-registered appId. The first real CVM's KMS log shows `isAppAllowed → false: "appId not a cluster member"` while `memberIdOf[appId]` reads zero — observable on the very first boot, before any tx.

### E — Compose-hash enforcement regression (silent security failure)

An operator adds their approved compose hash via `addComposeHash` and trusts the P1 claim: the KMS won't boot anything else and the sig chain "proves the node passed that gate." It doesn't. `DstackProof` carries no `composeHash`/`deviceId`/`tcbStatus` field — registration is *structurally* incapable of constraining which image ran. `verify` checks only that an allowlisted KMS root signed `"dstack-kms-issued:" ‖ codeId ‖ appPubkey`, that the app key authorised a derived key, and that the derived key signed the binding. A KMS root issues keys under a `codeId`; it does not, in the signed material, attest the compose hash. The exploit needs no crypto break: an attacker (or a careless operator running a patched build, or one who flipped `allowAnyDevice` to get past the deadlock) boots a tampered image under the same `codeId`, obtains a normally KMS-issued derived key, signs the binding, registers — joins, pulls the CSK, receives plaintext peer traffic. The gate meant to bind compose-to-boot is a separate, deadlocked view function never invoked at registration.

- **Assumption:** That an allowlisted KMS root having issued a key under a `codeId` cryptographically implies the approved compose hash booted — when the signed chain binds neither.
- **Warning signs:** A live member's running image hash doesn't match any `allowedComposeHashes` entry yet its `memberId` is live. `isAppAllowed` is observed false at real boot (the deadlock) while registrations still succeed — proof the gate isn't actually gating.

### F — F2 fix unwired + untested against a live bundler

The F2 fix shipped green: `bundler.rs` correctly sponsors-then-signs and a unit test proved the invariant — but only against a hand-written `sample_sponsorship()` JSON the author also wrote. **`BundlerClient::new` is invoked nowhere in the tree**, and its only consumer (`AgentService::new`) is itself never called. `state::run()` derives keys, discovers the cluster, sets `Phase::Registering`, then calls `health::serve` and returns — `build_register_calldata` is dead. So a real zero-ETH node sat in "registering" forever and joined no mesh; the F2 fix was irrelevant because the bundler it lived in was unreachable. Once hand-wired to debug, the live Alchemy path broke as suspected: modelled on dstackgres's LightAccount v2 flow, but AttestMesh's `ClusterMember` has a different `validateUserOp`, so the hardcoded 65-byte dummy signature recovered a mismatched verification-gas profile, the response field names didn't line up with the exact-key parser (which `bail!`s on any missing key), and `GAS_POLICY_ID` empty-by-default sent `policyId: ""`.

- **Assumption:** A unit test passing on self-authored fixture JSON proves an integration works, and code that compiles is code that runs in the bring-up path.
- **Warning signs:** A node logs `phase=registering` indefinitely with no `eth_sendUserOperation` ever leaving the box. `BundlerClient::new` / `build_register_calldata` have zero call sites reachable from `run()`.

### G — Cross-language ABI / selector drift

Three independent ABI definitions of `DstackProof` shipped green. The webhook hard-codes `0x537d491c` (viem over the tuple); the contract's selector came from solc; the sidecar's from alloy 0.8's `sol!` macro. Nothing compared the three byte-for-byte. The sidecar's only relevant test asserts `bind_hash` (a keccak over five *static* params), nothing about the `dstack_register` selector or full proof calldata. "Green across all three" never meant "the three agree" — each agreed with itself. The trap sprang at first real registration because `build_register_calldata`/`assemble_proof` are unwired, so no encode→decode ever ran end-to-end. With five dynamic `bytes` + a trailing `string`, the head/tail layout is fragile: an off-by-32 head offset or differing field order between alloy 0.8 and solc yields a 4-byte prefix the webhook never allowlisted (sponsorship silently denied), or — selector matching but tail differing — the facet ABI-decodes garbage and reverts inside `verify` as `CodeIdMismatch`/`BindingMismatch` after the node paid gas.

- **Assumption:** That three per-language suites each passing in isolation proves the three ABIs agree — when no test compares one component's actual bytes/selector against another's.
- **Warning signs:** `0x537d491c` appears only in webhook code; grep returns zero hits in `sidecar/` and `contracts/`. `build_register_calldata`/`assemble_proof` have no caller reachable from `run()`.

### H — "IMPLEMENTED + green" mistaken for deploy-ready

The status line — "IMPLEMENTED, 22+33+65 tests green, fmt/clippy clean" — is what traveled to standup, the demo, the partner deck. Nobody re-read the Open Questions three scrolls down, where the spec itself confessed that no captured proof, no confirmed `purpose`, and no working boot gate existed. The deferred items lived in prose; the green checkmarks lived in the status line, and stakeholders read the status line. A go-live date got committed on the strength of a number that only ever described a closed-loop mock signing and verifying against itself. First boot bricked on the `isAppAllowed` deadlock; the operator patching it open (`allowAnyDevice`) then surfaced the never-captured-proof and placeholder-`purpose` risks — or admitted a node never validated against a real KMS. These were never code bugs found late; they were the actual gating work, mislabeled as follow-ups.

- **Assumption:** That "all tests pass" measures real-world readiness, when the tests only verified the system against a mock the team authored and the spec had already named the three unknowns it couldn't cover.
- **Warning signs:** The "IMPLEMENTED" status and the Open Questions contradict each other in the same document, and no issue/gate/checklist tracks the three deferrals. Zero CI/test artifacts reference a real dstack KMS, a testnet CVM, or a live Alchemy call.

## Synthesis

**Most likely failure — the cluster never reaches one member.** Bring-up dies at the first boot on the confirmed `isAppAllowed` deadlock (D); and even patched past it, the never-validated `codeId`/preimage/signer assumptions (A/B/C) revert registration next. Every one of these is near-certain precisely because nothing was ever validated against a real KMS — they're not bugs, they're untested interfaces.

**Most dangerous failure — silent admission of unattested software (E).** Compose-hash enforcement was removed from registration and moved to a gate that is deadlocked and structurally absent from the signed proof. The dangerous branch is the operator who flips `allowAnyDevice` (or otherwise loosens the gate) to escape the bring-up deadlock — now nothing on-chain binds "approved image" to "registered member," CI stays green, the mesh forms, and the one guarantee the product exists to provide is void. The C-variant (allowlisting whatever address the first proof recovers, to clear the revert) is the same failure by a different door.

**The hidden assumption (named by nearly every investigator):** *Internal self-consistency was mistaken for external truth.* The proof producer (`MockKmsChain`) and the verifier (`DstackSigChain`) are built from the same ported source, so "all green" proves only that the implementation agrees with itself — not that it conforms to a real dstack KMS, a real `app_id`, a real bundler, or a real boot gate. No byte ever crossed the trust boundary. The dstackgres root-signer incident is proof this exact class of error bites in production.

### Revised plan (concrete)

1. **Capture one real proof first — it adjudicates A, B, and C at once.** Stand up a single real dstack CVM on testnet, capture its KMS sig-chain material, real `app_id`, and real KMS root pubkey, check it in as a fixture, and add a test that runs *that* fixture through `DstackSigChain.verify`. Until this passes, treat the verifier as unverified.
2. **Fix the `isAppAllowed` deadlock now, not as a follow-up.** Change the appId branch to check an owner-seeded `allowedAppIds` (the operator's flow step 1) or `isOurMember(appId)` instead of prior registration, and add a positive-path test: `isAppAllowed(allowlisted-but-unregistered appId) == (true, "")`.
3. **Wire the bundler into `run()` and land one real sponsored `dstack_register` on testnet** (real Alchemy policy, real `ClusterMember` dummy-sig, real response parsing). One included UserOp with no AA24 is the only thing that validates F2.
4. **Add a cross-language ABI conformance test.** Emit the sidecar's actual `dstack_register` calldata (alloy 0.8) for a known proof, assert its selector `== 0x537d491c` in Rust, and decode those exact bytes back into the same proof in a Solidity test.
5. **Settle the compose-hash trust model explicitly.** Either re-add an on-chain compose binding at registration (defense in depth), or document the *observed* dstack KMS behaviour (it calls `isAppAllowed` and refuses on false) that makes gate-only enforcement safe — never an unobserved assumption. Decide what happens if an operator sets `allowAnyDevice`.

### Pre-launch checklist

- [ ] A **real captured** dstack proof fixture verifies through `DstackSigChain.verify` in CI (not a synthesized one).
- [ ] `isAppAllowed` returns `(true,"")` for an allowlisted, not-yet-registered appId in a test, and a real CVM boots past the gate on a cold cluster.
- [ ] One real sponsored `dstack_register` UserOp is included on testnet (no AA24), with `BundlerClient` wired into `run()`.
- [ ] The sidecar-emitted `dstack_register` selector is asserted `== 0x537d491c` in Rust and round-trip-decoded in Solidity.
- [ ] Documented + observed evidence that an unapproved compose hash cannot obtain a KMS-issued key for the `codeId` — or registration re-adds a compose binding. Define the `allowAnyDevice` posture.
