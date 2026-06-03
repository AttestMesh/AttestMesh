# Premortem Transcript v2 — Deploying the AttestMesh v1 Cluster (dstack-grounded)

**Run:** 2026-06-03 (ts 1780448684) · **supersedes** the initial run (ts 1780445293)
**What changed:** the first run reasoned about dstack from the AttestMesh specs + assumptions. This run re-grounds every dstack-touching failure mode in the **actual dstack source** (`github.com/dstack-tee/dstack`, read locally). The result sharpens F1 and F4 from "likely differs" to "documented exact mismatch," and **downgrades F3** (dstack CVMs do ship wireguard). F2/F5/F6/F7 are dstack-independent and unchanged.

---

## Ground truth read from dstack-tee/dstack

1. **dstack does NO on-chain signature verification.** `kms/auth-eth/contracts/DstackApp.sol::isAppAllowed` is a pure allowlist gate: TCB-up-to-date + `allowedComposeHashes[composeHash]` + (`allowAnyDevice || allowedDeviceIds[deviceId]`). The TDX quote is verified **off-chain by the KMS** (itself a TEE); the on-chain contract only checks allowlists and stores the KMS-root/app registry.
2. **dstack's `IAppAuth.AppBootInfo` is byte-identical to AttestMesh's mirror** (`{address appId; bytes32 composeHash; address instanceId; bytes32 deviceId; bytes32 mrAggregated; bytes32 mrSystem; bytes32 osImageHash; string tcbStatus; string[] advisoryIds;}`). AttestMesh got the boot-gate side right.
3. **The real k256 key + signature** (`kms/src/crypto.rs`): `derive_k256_key(parent, app_id)` derives via `kdf::derive_key(parent, [app_id, "app-key"])` and signs with `sign_message(parent, b"dstack-kms-issued", app_id, sec1_pubkey)`, where `sign_message` computes `Keccak256(prefix ‖ ":" ‖ app_id ‖ message)` and returns a 65-byte recoverable ECDSA sig. So the chain is **recoverable ECDSA over `keccak256("dstack-kms-issued" ‖ ":" ‖ app_id ‖ sec1_pubkey)`**, rooted in the KMS's k256 key (whose authenticity comes from a **TDX quote** binding `"dstack-kms-genereted-keys-v1:{p256};{k256};"`, `kms/src/main_service.rs:377`).
4. **The guest-agent API is prpc/protobuf** (`guest-agent/rpc/proto/agent_rpc.proto`): `GetKey(GetKeyArgs{path,purpose,algorithm}) → {key, signature_chain}`, `DeriveK256Key(GetKeyArgs) → {k256_key, k256_signature_chain}`, `GetQuote(RawQuoteArgs{report_data}) → {quote, event_log, report_data, vm_config}`, `GetTlsKey`, `Info → AppInfo{app_id, instance_id, device_id, compose_hash, mr_aggregated, …}`. **There is no `Seal`/`Unseal` RPC.**
5. **dstack persistence = encrypted rootfs.** The app data volume is encrypted with a KMS-derived disk key; "sealing" means writing to that disk, not an RPC. dstack key derivation (`GetKey`/`DeriveK256Key`) is **deterministic per app_id**, so an app re-derives its keys across restarts.
6. **dstack CVMs ship wireguard.** `basefiles` rootfs references `/etc/wireguard/$IFNAME.conf`; the gateway uses `wg.conf` templates and wireguard for CVM↔gateway connectivity.

---

## Re-graded failure modes

### F1 — CONFIRMED, now exact (the clearest showstopper) · severity: CERTAIN
AttestMesh's `DstackFacet.dstack_register` verifies an **invented** 3-level secp256k1 chain on chain with preimages `keccak256(abi.encode("dstack.app", appKey, composeHash))`, `keccak256(abi.encode("dstack.instance", derivedPubKey, instanceId, deviceId))`, and a binding over `abi.encode("attestmesh.bind.v1", …)`. dstack's real signature is `keccak256("dstack-kms-issued" ‖ ":" ‖ app_id ‖ sec1_pubkey)` — **different domain string, different encoding (raw concat vs `abi.encode`), different bound fields (app_id+SEC1-pubkey vs composeHash+appKey-bytes), and dstack has only one such KMS→app-key link, not a 3-level chain.** A real `k256_signature_chain` cannot recover against AttestMesh's `hApp`/`hDerived`, so `dstack_register` reverts (`KmsAppKeySigInvalid`) for every real CVM. Deeper: dstack does **no** on-chain sig verification at all — AttestMesh invented an architecture (on-chain verifier, master spec §10 "verifier as facet") whose proof format it never reconciled with dstack's actual key code. The mock encoded AttestMesh's own invention, so 17 contract tests pass while a real proof would fail.
**Underlying assumption:** that dstack emits an `abi.encode`-shaped, compose-hash-committing, 3-level secp256k1 chain verifiable on chain — when dstack signs `keccak256("dstack-kms-issued:" ‖ app_id ‖ sec1_pubkey)` off chain and does only an allowlist check on chain.
**Early warning signs:** a forge fork-test replaying a real `DeriveK256KeyResponse.k256_signature_chain` reverts; `DstackSigChain`'s preimage builders contain string literals (`"dstack.app"`) that appear nowhere in `dstack/kms/src/crypto.rs`.

### F3 — DOWNGRADED (was CERTAIN) · severity: LOW/UNCERTAIN
dstack CVMs ship wireguard (rootfs `/etc/wireguard/*.conf`; gateway `wg.conf`), so the "no wireguard module/binaries" premise is **false** — the tooling exists in the dstack image. Residual risk: AttestMesh's second `attestmesh0` interface still needs the `wg`/`ip` tools reachable and `NET_ADMIN` inside the sidecar's container context, and `state::run` still swallows `create_interface` errors with `.ok()`, so a real failure would still be silent. Verify capability + de-swallow; but this is no longer a guaranteed showstopper.
**Underlying assumption:** that the dstack CVM has no wireguard (it does).
**Early warning signs:** `wg`/`ip link` present inside the CVM; the residual risk is now only the `.ok()` swallow + container `NET_ADMIN`.

### F4 — CONFIRMED, now exact · severity: CERTAIN
`UnixSocketDstack` POSTs JSON to `/DeriveKey` with `{"path": purpose, "purpose": subkey}`. Real dstack is **prpc/protobuf** with `GetKey(GetKeyArgs{path, purpose, algorithm})` / `DeriveK256Key` / `GetQuote` — wrong protocol, wrong method, scrambled field mapping; the first call fails to parse. And **`seal`/`unseal` don't exist** — the sidecar's `dstack.seal("attestmesh.csk.v1", csk)` calls a nonexistent RPC. The originator can simply **re-derive** the CSK (deterministic `GetKey`); but an onboardee that pulled the CSK must persist it to the **encrypted rootfs** (or re-pull on restart) — the current restart path (`unseal` → `LostState`) is built on a primitive dstack doesn't provide.
**Underlying assumption:** dstack exposes a JSON HTTP `/DeriveKey` + a `seal`/`unseal` RPC; in fact it's prpc and persistence is the encrypted disk.
**Early warning signs:** no test instantiates `UnixSocketDstack`; a prpc client generated from `agent_rpc.proto` immediately shows the method/field mismatch; `grep -r Seal dstack/guest-agent` returns nothing.

### F2 — unchanged (dstack-independent), now coupled to F1 · severity: CERTAIN
The bootstrap UserOp signs a userOpHash before Alchemy inserts `paymasterAndData`, so `validateUserOp` recovers a different signer and rejects every registration op. Coupling note: the "binding key" is a dstack-derived k256 key whose on-chain provenance proof is the same (broken) F1 chain — so F2 and F1 fail at the same step.

### F5 — unchanged · severity: CERTAIN
`indexer_client::connect_and_run` verifies push signatures then `tracing::debug!`s — the RLP `event_data` decode/dispatch is a no-op, so nodes never learn peer endpoints. The mesh never forms even with everything else working.

### F6 — unchanged · severity: HIGH
Racy `memberCount()==1` originator selection + strict `active == live` convergence gate deadlock under real async boot timing; onboardees can't pull the CSK before a tunnel exists.

### F7 — unchanged · severity: HIGH
Never integration-tested; single-everything fail-closed infra; three nodes wedge in different phases with no signal of which seam broke.

---

## Synthesis v2

**Most likely / clearest failure — F1.** Now precisely diagnosed: AttestMesh's on-chain registration verifier checks a signature format that **dstack does not produce and never verifies on chain**. This isn't a tuning bug; the entire `DstackSigChain` + `dstack_register` preimage design was authored without reading `dstack/kms/src/crypto.rs`. Combined with F4 (the client can't even talk to the guest agent) and F2 (the UserOp can't validate), **three independent certain showstoppers gate registration alone** — the cluster never gets a single member.

**Most dangerous failure — CSK persistence built on a nonexistent primitive (F4) against an immutable on-chain commitment.** Because `seal`/`unseal` don't exist, an onboardee that pulls the CSK has nowhere durable to put it under the current design; meanwhile the originator's `keccak256(CSK)` commitment is immutable on chain. A naive "it derived something, commit it" path can strand the cluster with a committed-but-unrecoverable key. Unrecoverable beats merely-broken.

**The hidden assumption (sharpened).** AttestMesh mirrored the parts of dstack that were visible in *contracts* (`IAppAuth.AppBootInfo`, `isAppAllowed` — and got them exactly right) but **invented** the parts that live in dstack's *Rust* (the guest-agent prpc API and the KMS signature format), then validated the inventions with mocks that encode the same inventions. The boundary of what the team actually read from dstack is exactly the boundary between what works and what is fictional.

**Revised plan (dstack-concrete).**
1. **dstack client → prpc.** Generate from `guest-agent/rpc/proto/agent_rpc.proto` (or use dstack's SDK): `GetKey{path,purpose,algorithm}`, `DeriveK256Key`, `GetQuote{report_data}`, `Info`. Map AttestMesh purposes to `GetKey` paths. Drop `seal`/`unseal`: re-derive the originator CSK deterministically; persist the onboardee CSK to the encrypted rootfs (or re-pull on restart).
2. **Re-architect the on-chain verifier (F1) — pick a model deliberately:**
   - (a) *Match dstack on chain:* rewrite `DstackSigChain`/`dstack_register` to recover `keccak256("dstack-kms-issued" ‖ ":" ‖ app_id ‖ sec1_pubkey)` against an allowlisted KMS k256 **address**, and establish compose-hash→app_id→member via dstack's app registry / the quote — not an invented `abi.encode` preimage; or
   - (b) *Adopt dstack's model:* off-chain KMS verifies the quote, on-chain you only run `isAppAllowed` — simpler, but reintroduces a trusted off-chain attestor (a real architectural decision for "the chain is the source of truth").
3. **Capture a real proof.** Pull one live `DeriveK256KeyResponse` + `GetQuote` from a real dstack CVM and add a forge fork-test replaying it against `DstackFacet`. Highest-value single validation; blocks deploy until green.
4. **F2:** reorder `bundler.rs::submit` to sponsor-then-sign; land one real UserOp on Sepolia.
5. **F5:** implement the Indexer `event_data` RLP decode + dispatch; anvil test A→B peer config.
6. **F3:** confirm the sidecar container has `NET_ADMIN` + wg/ip; remove the `.ok()` swallow (downgraded, not dismissed).
7. **F6/F7:** fix the originator race (order by `MemberRegistered` block+logIndex), rehearse 3-node on anvil, add per-phase health detail + runbook.

**Pre-deploy checklist:** real-proof forge fork-test passes (F1) · a prpc `GetKey`/`DeriveK256Key` round-trips against a real guest agent (F4) · one sponsored UserOp lands on Sepolia (F2) · A's PeerEndpoint configures a wg peer on B (F5) · 3-node all-healthy once on a local harness with the CSK persisted/re-derived across a restart (F4/F6).
