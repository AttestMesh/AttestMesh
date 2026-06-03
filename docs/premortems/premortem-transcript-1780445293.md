# Premortem Transcript — Deploying the AttestMesh v1 Cluster

**Run:** 2026-06-03 (ts 1780445293)
**Method:** Gary Klein prospective hindsight — assume the deployment already failed, work backward. 7 parallel deep-dive investigators, each grounded in the actual repository code.
**Companion report:** `premortem-report-1780445293.html`

---

## Context gathered

- **What:** Deploy the AttestMesh v1 three-node demo on Base Sepolia (master spec §11). Pipeline: org Safe runs `DeployInfra` (facets, factories, IndexerRegistry) → deploy the Indexer CVM + set its IndexerRegistry record → deploy the gas-sponsorship-webhook + point Alchemy's paymaster at it + fund it → `DeployCluster` (the ClusterDiamond, seeding KMS root, allowlists, mesh CIDR 10.13.0.0/16, memberFactory; ownership to the cluster Safe) → deploy 3 ClusterMembers (CREATE2) wired into dstack compose as `app_id` → boot 3 dstack CVMs running `cluster-mesh-agent`: derive keys, build a `DstackProof`, register via a gasless EIP-4337 UserOp (bundler → paymaster → webhook → `DstackFacet` verifies the secp256k1 KMS chain on chain), publish wg key, subscribe to the Indexer, exchange sealed-box PeerEndpoints, bring up wireguard, heartbeat to convergence, distribute the CSK (originator commits `keccak256(CSK)`; onboardees pull P2P), go healthy, start the app containers.
- **Who:** the AttestMesh org (infra + Indexer + paymaster) and the three node operators. The demo *is* the end-to-end proof.
- **Success:** all 3 register on chain, the wg mesh converges, the CSK reaches all 3, every healthcheck returns 200, app containers start.
- **Load-bearing build context:** built from specs, unit-tested per component (147 tests), but **never integration-tested end-to-end** (the §16.2/§14.2 harnesses are `#[ignore]`d stubs). The on-chain dstack verification was tested only against a mock KMS chain; the sidecar's wireguard and dstack-runtime clients are command/stub impls never run on real hardware; the EIP-4337/paymaster flow is structural; the Indexer and sidecar were built by separate agents.

## Raw premortem — 7 failure reasons

1. On-chain dstack verification was built against a *mock* KMS chain — the real proof shape differs, so `dstack_register` reverts and nobody registers.
2. The EIP-4337 + Alchemy paymaster + webhook flow is unproven — the bootstrap UserOp signs a hash Alchemy later mutates → signer mismatch → every registration op rejected.
3. Wireguard never establishes on real CVMs (command-based `MeshControl` never ran on a kernel; missing module/binaries/`NET_ADMIN`).
4. The dstack runtime client is a guessed UDS stub that doesn't match the real guest-agent API (keys/seal fail at boot; CSK silently lost on restart).
5. The sidecar verifies Indexer push signatures but never decodes the RLP `event_data` to act on events → nodes subscribe yet never learn peer endpoints.
6. Convergence + CSK distribution deadlock under real async timing (`active == live` requirement, racy originator selection, CSK-pull-before-mesh).
7. Never integration-tested + all single-points-of-failure infra → first real run hits unbudgeted seam bugs and silently hangs on 503s.

---

## Synthesis

**Most likely failure — F4 (dstack runtime stub), the earliest fatal seam.** The sidecar can't even derive keys against the real guest agent. But "most likely" undersells it: **F1, F2, F4, and F5 are each ~100% to fire on first contact with the real world.** The honest probability the demo works as-built is ≈0 — this is a certainty, not a risk profile.

**Most dangerous failure — silent, irreversible CSK loss (F4 insidious branch / F6 originator race).** An immutable on-chain `keccak256(CSK)` commitment plus a `seal`/`unseal` that doesn't truly round-trip (or an originator that races/crashes) = the CSK gone forever after the first restart, the cluster permanently bricked *after* appearing to pass.

**The hidden assumption — "147 green unit tests" was equated with "it works."** Every test runs against a mock/stub at exactly the four boundaries where the system meets reality (dstack runtime, dstack KMS proof, Alchemy bundler/paymaster, the live Indexer→sidecar wire). The mocks were authored alongside the code they validate, so they encode the same assumptions and cannot catch a wrong belief about an external contract.

**The revised plan** (don't deploy the demo yet — climb the validation ladder; full detail in the HTML report): validate each real-world seam in isolation, cheapest first — (1) capture the real dstack guest-agent API and rewrite `UnixSocketDstack`; (2) fork-test a real `DstackProof` against `DstackFacet`; (3) reorder `bundler.rs::submit` to sponsor-then-sign and land one real UserOp on Sepolia; (4) implement the Indexer `event_data` decode/dispatch + an anvil test that A's PeerEndpoint becomes a wg peer on B; (5) confirm the CVM image has wireguard + remove the `.ok()` so failures are loud; (6) fix the originator race (order by `MemberRegistered` block+logIndex) and rehearse on local anvil; (7) add per-phase health detail + a runbook/smoke test.

**Pre-deploy checklist:** real-proof fork-test passes · one sponsored registration UserOp lands on Sepolia · `attestmesh0` up inside the real CVM image with a wg handshake · integration test asserts A→B peer config · 3-node all-healthy at least once on a local harness.

---

## Agent deep-dives (verbatim)

### F1 — On-chain dstack verifier built against a mock KMS chain

**Failure story.** The operator boots node 1. The sidecar derives keys, computes `bind_hash = keccak256(abi.encode("attestmesh.bind.v1", cluster, member, xPub, wgPub))`, EIP-191-signs it, and assembles a `DstackProof`. But `KmsChainMaterial` was only ever populated by `MockDstack`: real dstack does not hand back a 3-level secp256k1 ecrecover chain with preimages `keccak256(abi.encode("dstack.app", appKey, composeHash))`. Real dstack KMS returns a TDX quote + a KMS-signed cert chain over its own canonical preimages/encodings. The gasless UserOp lands, the diamond delegatecalls `DstackFacet.dstack_register`, and `ecrecover(hApp, appKeySig)` returns an address that isn't `rootAddr`, reverting `KmsAppKeySigInvalid`. The sidecar retries 10× and exits non-zero. Nodes 2–3 fail identically — structural, not transient. `_addMember` never runs; the cluster is born empty and stays empty.
**Underlying assumption.** Real dstack KMS output is shaped exactly like the hand-authored mock chain (ecrecover-able secp256k1 over AttestMesh-chosen `abi.encode` preimages), not a TDX quote over dstack's own encodings.
**Early warning signs.** A `forge`-fork test replaying one recorded *real* `DstackProof` against the deployed facet reverts `KmsAppKeySigInvalid`/`KmsRootNotAllowed`. `UnixSocketDstack` produces a `kms_root_pubkey` whose `compressedToAddress` is absent from `allowedKmsRoots`.

### F2 — EIP-4337 signature dies once Alchemy inserts paymaster data

**Failure story.** In `bundler.rs::submit`, the sidecar estimates gas, then computes `user_op_hash` and signs it while `op.paymaster == None` — so the hash commits to `keccak256("")` for `paymasterAndData`. The op goes to Alchemy, the paymaster POSTs the webhook, gets `{approved:true}`, and Alchemy *now* fills the paymaster fields. On-chain, the EntryPoint recomputes the hash over the populated `paymasterAndData`, `ClusterMember.validateUserOp` recovers a different address than the bootstrap binding signer, returns `SIG_VALIDATION_FAILED`, and the op reverts. Fails 100% of the time; never caught because it was never run against a live bundler. Layered on: webhook provenance checks may deny a freshly counterfactual member, and the sidecar treats denial as transient ("retry in 5s") — an infinite loop, not a surfaced error. An unfunded paymaster yields the same silent spin.
**Underlying assumption.** A signature computed before Alchemy populates `paymasterAndData` will still validate after — i.e. the v0.7 userOpHash is stable across the paymaster handshake, when it commits to those fields.
**Early warning signs.** Bundler emits `AA24 signature error` on every registration while the log shows a benign "webhook declined, retrying" loop. No `userop.rs` test asserts `user_op_hash` equality between a `paymaster=None` op and the same op with paymaster fields filled.

### F3 — Wireguard never establishes on the real CVMs

**Failure story.** All three CVMs reach `wg_ctl.create_interface(...)` in `state/mod.rs`. The first line of `CommandWg::create_interface` — `ip link add attestmesh0 type wireguard` — fails on every node (no module, no iproute2/`wg`, no `NET_ADMIN`), but is terminated with `.ok()`; the outer `create_interface(...).await.ok()` also swallows it. Boot proceeds into `Phase::Heartbeating`. Heartbeats to peers' mesh /32s drop at the host; `liveness.is_converged` never returns true, `latch_first_converged()` never fires, and `health.rs` serves 503 forever (`wg-configuring`/`heartbeating`). The app container's startup gate never opens — a perpetual, silent, green-logs-but-dead loop.
**Underlying assumption.** A `MeshControl` impl that passed only against `MockWg` behaves the same on a real dstack kernel, and would fail loudly rather than be swallowed by `.ok()`.
**Early warning signs.** `Phase::Heartbeating` with zero received-heartbeat counters and `first_converged: false` on every node simultaneously. `ip link show attestmesh0` returns "device does not exist" despite a clean boot log.

### F4 — The dstack runtime client is a guessed UDS stub

**Failure story.** All three CVMs open `/var/run/dstack.sock`, and `UnixSocketDstack::request` fires `POST /DeriveKey` with `{"path": <purpose>, "purpose": <subkey>}`. The real guest agent doesn't serve that route — it speaks Tappd-style prpc (e.g. `/prpc/Tappd.DeriveKey`) with a different envelope and returns a TLS keypair/cert, not `{"key":"0x…"}`. Node 1 dies at boot: `missing key`, or half the runs never parse a response (`malformed dstack response`). The keccak `MockDstack` that every test used passed perfectly, so CI was green up to real hardware. The insidious branch: if `DeriveKey` happens to return 32 decodable bytes, the originator commits `keccak256(CSK)` on chain — but real dstack may have no `Seal`/`Unseal` at all; those routes 404. On the first restart `unseal_from_store` returns `None`, the sidecar takes `LostState`, and the CSK — its commitment already immutable on chain — is gone forever.
**Underlying assumption.** A reverse-engineered guess at dstack's paths, JSON shapes, and primitive set (incl. a `seal`/`unseal` that may not exist) equals the real protocol because the mock round-tripped.
**Early warning signs.** No test ever instantiates `UnixSocketDstack` (it appears only in `dstack.rs`; every `#[tokio::test]` uses `MockDstack`). A `curl --unix-socket` against a real CVM 404s `/DeriveKey` and `/Seal`.

### F5 — Sidecar verifies Indexer pushes but never decodes/acts on them

**Failure story.** All three nodes register; `MemberRegistered` lands on Sepolia; the explorer shows three members. Each sidecar opens the bidi gRPC stream, sends `Hello`, and the Indexer pushes; logs scroll `verified indexer push block=…` and every signature verifies (the CBOR signing view matches byte-for-byte). The subscription phase self-reports SUCCESS. Then `ListPeers` on any node returns **empty** — no handshakes, no data plane. The cause is one dead wire: `indexer_client::connect_and_run` verifies the signature, emits `tracing::debug!`, and ends with `let _ = &shared;`. The RLP `[address,[topics],data]` decode + dispatch is a TODO, so `MessageSent` is never sealed-box-decrypted into a `PeerEndpoint`, `wg::add_peer` is never called, and `MemberRegistered` never triggers an endpoint reply. Every downstream handler exists and passes its own unit tests, but nothing on the live stream calls them.
**Underlying assumption.** "Signature-verified and logged" was equated with "consumed and acted upon," so a healthy stream was assumed to mean a converging mesh.
**Early warning signs.** `add_peer`/handshake counters stay at zero while `verified indexer push` climbs. No integration test drives `connect_and_run` across the gRPC boundary.

### F6 — Convergence + CSK distribution deadlock under real async timing

**Failure story.** Three nodes boot within ~400ms. Two read `memberCount()` in the same block before either tx confirms — both see `0`, both register, and `memberCount()==1` is never observed (it jumps 0→…→3 as txs land out of order). No node believes it's the originator; the CSK is never derived; `keccak256(CSK)` is never committed; all sit at `pulling-csk` forever. Even when the race resolves and one node derives the CSK, the strict `active == live` gate never latches: with 2s/3-miss timing there's a long stretch where A hears B but not C while C already reports all three; the active and live sets disagree every tick, and the window slides faster than the last node joins. Onboardees that did pick an originator still can't pull — the pull needs a live tunnel, but tunnel bring-up and convergence are mutually blocking under staggered timing.
**Underlying assumption.** A system validated only against a synchronized logical clock with all peers present at t=0 behaves identically when three real nodes boot, register, and tunnel at independent wall-clock times.
**Early warning signs.** Two+ nodes log the originator/`memberCount` read before any `MemberRegistered` confirms; `cskCommitment()` stays `0x0` after all three register. A `pulling-csk`/`heartbeating` phase that never advances, `is_converged` flapping false by exactly the last-joined node each tick.

### F7 — Never integration-tested; every dependency a fail-closed SPOF

**Failure story.** Demo day: three terminals, three sidecars, all 503, none advancing. The "147 green tests" confidence evaporates — every component passed in isolation precisely because the seams were never exercised (mocked dstack socket, stubbed Indexer feed, faked bundler; the §16.2/§14.2 harnesses `#[ignore]`d). The diagnostic nightmare: the three nodes wedge in *different* phases — A pre-registration (webhook policy mismatch), B subscribed-but-empty (IndexerRegistry endpoint / cert-pinning mismatch), C blocked on B's wireguard signal — each silently at 503 with no indication of which dependency it's waiting on, no cross-node correlation, no orchestrator. With single-everything, one SPOF down wedges the whole mesh identically, and no fallback exists.
**Underlying assumption.** Components that each pass their own unit tests compose correctly across their seams on first contact, so end-to-end testing is a formality that can be skipped.
**Early warning signs.** The integration harnesses are committed as `#[ignore]`d stubs; CI has never produced a passing multi-process or anvil-backed run. The gas-webhook, ClusterMember, sidecar, and Indexer were authored by different agents with no shared seam contract or interop test.
