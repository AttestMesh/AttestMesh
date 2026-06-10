# Premortem Transcript — the FIX (dstack-registration-rework)

**Run:** 2026-06-03 (ts 1780453627)
**Target:** the drafted spec `docs/specs/dstack-registration-rework.md` (premortem fix #1), assumed implemented-and-failed.
**Method:** 6 parallel investigators, each grounded in the spec + the real dstack source (`/tmp/dstack-real`) + the AttestMesh code.

> This premortem of the fix produced a strong negative result: **the fix as specced should not be built.** It fails on at least three *certain* technical dimensions and is strategically self-defeating. The architecture decision it encodes (adopt dstack's allowlist-gate model) needs revisiting.

---

## Context

- **What:** a rework that adopts dstack's allowlist-gate model — on chain, `dstack_register` verifies ONE real dstack KMS signature (`keccak256("dstack-kms-issued:"‖app_id‖sec1_pubkey)`) + an allowlist gate on proof-supplied composeHash/device/TCB + a pubkey binding; off chain it trusts the dstack KMS; the sidecar is rewritten to dstack's prpc API with encrypted-disk/re-derive CSK persistence. Covers both ends of the registration seam.
- **Who:** the team building the fix; the operators/nodes who need registration to work AND be secure.
- **Success:** a real dstack CVM registers by verifying a real proof; the security model is sound; the sidecar talks to the real guest agent; the CSK survives restarts.

## Raw failure modes
P1 composeHash is self-asserted · P2 dstack's app-key/app_id model doesn't map to per-member identity · P3 prpc ≠ gRPC (tonic can't talk to the guest agent) · P4 CSK persistence target wrong (ephemeral rootfs) + restart deadlock · P5 captured-proof fixture is an unowned dependency + single-version snapshot · P6 strategic: the fix guts AttestMesh's thesis and §9 security claims.

---

## Synthesis

**Most likely failure — P2 (certain, and it invalidates the spec's core mechanism).** dstack's `get_app_key` takes `app_id` from the *verified TDX quote's* `boot_info`, not from a caller-supplied value, and only issues keys to apps registered in `DstackKms.registeredApps` and gated by `allowedOsImages`. AttestMesh's plan to use arbitrary ClusterMember addresses as `app_id`s means **the KMS refuses to issue a key at all** ("App not registered") — registration can't even start. Run members under one real registered app instead, and `derive_k256_key(parent, app_id)` (per-`app_id`, KMS-signs the *app* key) hands every instance the **same** key → identical EIP-4337 owner → the second `dstack_register` reverts duplicate. Per-instance uniqueness lives in `instance_id` (used only for the disk key), never in the k256 app key the spec anchors identity on. The spec's central assumption — "the KMS will derive and sign a unique key for our chosen app_id" — is false.

**Most dangerous failure — P1 + P6 (the fix is both insecure and self-defeating).** dstack's `dstack-kms-issued` signature binds `app_id`+pubkey only — **not the compose hash** (`kms/src/crypto.rs`). The on-chain gate checks `proof.composeHash`, a self-asserted field welded to nothing: a holder of any KMS-issued app key registers while claiming an allowlisted composeHash but running unapproved software. That makes the §9 security model's "approved software" guarantee fictional — and P6 shows it simultaneously guts the thesis: §1's "anchored to an attestation chain *the cluster contract verifies*" and §10's "verifier as facet" both become false, leaving `DstackFacet` ≈ dstack's own `DstackApp.isAppAllowed` + a pubkey registry. The fatal review question — "why not just deploy `DstackApp` + a messaging contract?" — has no answer. The fix could pass every test and still lose.

**The hidden assumption.** That AttestMesh could "adopt dstack's model" cheaply — that the attestation-verification layer is swappable plumbing. It isn't: *the chain independently verifying attestation is the reason AttestMesh exists.* Decision B (trust the KMS off chain) negotiates away the anchor; the spec's own Open Question 2 and Non-Requirements already admit as much.

**The revised plan — revisit the architecture decision before writing any code.**
1. **Do not implement the spec as drafted.** The premortem invalidates Decision B (allowlist-gate / trust-KMS) on both security (P1) and strategy (P6), and P2 shows the mechanism doesn't even function.
2. **Stand up a real dstack CVM + KMS first** and answer empirically (these gate everything): can the KMS issue a key for an AttestMesh-controlled `app_id` at all, or must each member be a registered dstack app? What's actually in the TDX quote's `report_data` (the master spec §4.1 already intends the pubkey binding to live there)? Is the rootfs ephemeral (it is — dm-verity) and where is the LUKS data disk mounted?
3. **Reconsider per-instance, quote-based binding.** The master spec's *original* rule (§4.1) binds `xPub‖wgPub` into the TDX quote's `report_data` — which is per-instance, sidesteps the per-app-key collision (P2), and (if the quote is verified on chain or by an on-chain DCAP/verifier) preserves the "chain verifies attestation" thesis (P6). This points back toward the on-chain-quote path the draft deferred — re-evaluate it as the primary, not the fallback.
4. **Regardless of architecture, these implementation facts are now fixed:** the dstack client must speak **prpc/ra-rpc over HTTP/1** (Rocket, `POST /prpc/Service.Method`), not tonic gRPC (P3); CSK persistence must target the **LUKS data disk** (`/dstack`), not the verity rootfs, and the originator re-derives via the KMS (P4); the real-proof capture needs an **owner and provenance metadata** (dstack SHA, proto version, KMS root, TCB date), with ≥2 captures to diff (P5).

**Pre-implementation checklist.**
- ☐ A real dstack CVM+KMS exists and we've confirmed whether/how it issues a signed key for an AttestMesh `app_id` (P2).
- ☐ We've dumped a real `GetQuote` and inspected `report_data` + the `k256_signature_chain` element layout + where `tcb_status` lives (P5/P1).
- ☐ The architecture decision (on-chain quote/report_data binding vs trust-KMS) is re-made with those facts, with the security + thesis implications written down (P1/P6).
- ☐ A throwaway prpc round-trip from the sidecar to a real guest agent succeeds before any contract work (P3).
- ☐ A reboot test confirms the CSK survives on the data disk, or the originator re-derives without local state (P4).

---

## Agent deep-dives (verbatim)

### P1 — composeHash self-asserted
**Story.** The on-chain gate "passes": step 2 recovers a valid KMS-root signature over `keccak256("dstack-kms-issued" ":" app_id sec1_pubkey)`, step 3 finds `proof.composeHash` in `allowedComposeHashes`. But dstack's `sign_message` (`kms/src/crypto.rs`) hashes `[prefix, ":", appid, message]` where `message` is the SEC1 pubkey — the compose hash is **not in the signed bytes**. The `composeHash` field is an unauthenticated string the registrant typed. An operator who bumped their image without re-approval (or an attacker who can get the KMS to issue a key for their app_id — including via a misconfigured `allowAnyDevice`/dev mode) constructs a `DstackProof` with the genuine `kmsSig`+`appKeyPub` but pastes in any allowlisted composeHash, signs the binding with the same key, and clears every check. Nothing on chain saw the quote that measured the running software. The registry certifies "approved software" while only ever verifying "a KMS key exists for this address."
**Assumption.** That recovering one KMS signature over app_id+pubkey transitively authenticates the separately-supplied composeHash.
**Warnings.** A registration succeeds where `proof.composeHash` ≠ the composeHash in the registrant's own `Info→AppInfo` (never cross-checked on chain). The replay fixture is generated by `MockKmsChain` signing a synthetic preimage with a freely-chosen composeHash → green CI, zero binding.

### P2 — app-key/app_id identity model
**Story.** The captured-proof test passed against one dev CVM, so we deployed; the first real cluster never formed. dstack's KMS `get_app_key` does not take `app_id` from the request — it takes it from `boot_info`, extracted from the verified TDX quote's RTMRs. The CVM's real `app_id` is the `DstackApp` address baked into its compose at provisioning, registered in `DstackKms.registeredApps` and gated by `allowedOsImages`. Our ad-hoc ClusterMember addresses were never registered, so `is_app_allowed` returned "App not registered" and the KMS refused to issue a key — registration couldn't start. Working around that by running members under one registered app broke the opposite way: `derive_k256_key(parent, app_id)` is a pure KDF over `app_id` only, so every CVM of that app got the *same* K, the same `appKeyAddr`, a valid KMS sig — every member recovered to one identical owner; `_addMember` reverted duplicate on node 2. Per-instance uniqueness lives in `instance_id` (disk key only), never in the k256 app key.
**Assumption.** That the dstack KMS will derive and sign a *unique* k256 key for an arbitrary `app_id` we choose, when it only signs a deterministic per-registered-app key whose `app_id` comes from the attested quote.
**Warnings.** `DeriveK256Key` returns "App not allowed/registered" for members not in `DstackKms.registeredApps`. Two members on the same image yield byte-identical `k256_key`/`appKeyAddr`; the second register reverts.

### P3 — prpc ≠ gRPC
**Story.** dstack's guest agent is a **Rocket (HTTP/1.1)** server mounting services as `ra_rpc::prpc_routes!` under `/prpc/`. The wire format is dstack's **prpc/ra-rpc**: a single `POST /prpc/<Service>.<Method>` whose body is the protobuf request (or JSON with `?json`), `Content-Type: application/json`. There is no tonic/HTTP-2 anywhere in dstack; the `.proto`'s service/rpc blocks are consumed by dstack's `prpc_build`, not `tonic-build`. A tonic client speaks gRPC: HTTP/2 only, 5-byte length-prefixed frames, `content-type: application/grpc`, `grpc-status` trailers — none of which a Rocket HTTP/1 prpc endpoint understands. Against a real guest agent the tonic client's HTTP/2 preface gets no upgrade and the connection fails before any RPC. The team rediscovers, one layer down, the original "guessed the protocol" failure.
**Assumption.** That a `.proto` with service/rpc blocks implies a gRPC (HTTP/2) transport, so tonic codegen interoperates with any server built from it.
**Warnings.** dstack's `ra-rpc`/SDK imports `rocket`/`reqwest`/`prpc` and zero tonic/h2; every call is `POST /prpc/Service.Method`. The first live call dies with an h2/HTTP-2 framing error (or Rocket 404/400) while the same `.proto` messages round-trip fine against a tonic mock.

### P4 — CSK persistence target / restart deadlock
**Story.** The onboardee pulls the CSK and writes it to `${STATE_DIR}/csk`, believing it's on "the dstack-encrypted rootfs." It isn't: dstack's rootfs is `rootfs.img.verity` — a dm-verity, read-only image measured into RTMR2. Durable state lives on a *separate* LUKS volume (`PARTLABEL=dstack-data`/`/dev/vdb`), opened with the KMS `disk_crypt_key` and mounted at `/var/volatile/dstack/persistent` → bind-mounted as `/dstack`. Unless `${STATE_DIR}` is explicitly under that mount, the CSK lands on a volatile overlay. Every restart wipes it; the restart path always re-pulls; re-pull needs a live tunnel to a holder, which on a cold boot doesn't exist — the node loops at `pulling-csk` forever (premortem F6, now on every reboot). The originator's `seal_to_store`/`unseal_from_store` still call non-existent dstack seal/unseal, so that path doesn't even compile against the real prpc surface.
**Assumption.** That `${STATE_DIR}`/"the dstack-encrypted rootfs" is durable across restarts.
**Warnings.** Post-reboot, `${STATE_DIR}/csk` is missing and the agent re-pulls on every boot (never "loaded CSK from store"). `mount` shows `${STATE_DIR}`'s parent on an overlay/tmpfs upperdir, not under `/dstack`.

### P5 — captured-proof fixture
**Story.** Capturing a real `DeriveK256KeyResponse`+`Info` needs a live dstack CVM behind a deployed KMS that allowlisted the AttestMesh app_id — TDX hardware, a KMS instance, on-chain `DstackApp` wiring. Nobody owns standing that up; it's filed as "infra prerequisite" and slips. With the one gating test un-runnable, the contract engineer re-guesses Open Question #1 (compressed vs uncompressed SEC1, which `k256_signature_chain` element, the exact concat) and codes against the guess — reproducing the original F1 bug. The subtler death: someone captures *one* proof; the fixture freezes that CVM's bytes. But `k256_signature_chain` is a 2-element vector `[derived_sig, app_root_sig]` whose layout shifts if dstack reorders; `tcb_status` isn't a proto field — it's inside the `tcb_info` JSON; TCB status varies (`UpToDate`→`SWHardeningNeeded`) by platform/date; KMS roots rotate. The replay-test stays green against the frozen capture while a real CVM on newer dstack/different TCB emits a differently-shaped proof that `dstack_register` rejects on demo day.
**Assumption.** That one captured proof is a stable, representative spec of "a real dstack proof," not a single sample of a versioned, platform- and time-varying distribution.
**Warnings.** The fixture lands with no provenance (dstack SHA, proto version, KMS root, TCB date) and no second capture to diff. The replay-test is the only green dstack test while the "stand up a CVM/KMS" ticket has no assignee.

### P6 — strategic inversion
**Story.** AttestMesh demos cleanly, but the first adopter asks the fatal question and nobody can answer it: "If the chain trusts the dstack KMS to verify the quote and gate on `isAppAllowed`, and your `DstackFacet` is now basically `DstackApp.isAppAllowed` plus a pubkey registry — why not just deploy `DstackApp` and a messaging contract ourselves?" The rework silently invalidates the headline claims: §1's "anchored to an attestation chain *that the cluster contract verifies*" is false (it verifies one KMS signature and an allowlist, never the attestation); the Join invariant is hollow (composeHash/deviceId arrive as self-asserted fields); §10's "verifier as facet, not external contract" collapses — there is no verifier, just dstack's gate re-skinned. The project can demo but can't sell; serious adopters fork `DstackApp` + a sealed-box messaging contract directly and route around AttestMesh. The thesis dies not from a bug but from redundancy.
**Assumption.** That AttestMesh's value is the messaging/mesh/coordination surface, so the attestation-verification layer can be swapped for a trusted-KMS gate — when "the chain independently verifies attestation" *is* the reason to exist.
**Warnings.** The rework's own Open Question 2 and Non-Requirements explicitly punt independent verification — the spec is already negotiating away its anchor. A reviewer diffing `DstackFacet.dstack_register` against `DstackApp.sol` sees the same allowlist gate + registry.
