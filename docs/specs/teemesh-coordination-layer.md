# TeeMesh — Coordination Layer Master Spec

**Status**: Draft v0.2
**Authors**: LSDan (with Claude)
**Last updated**: 2026-05-30

---

## 1. Purpose

TeeMesh is the on-chain coordination layer for clusters of TEE-based confidential VMs (CVMs). It is the part of [dstackgres](https://github.com/TeeSQL/dstackgres) that has nothing to do with Postgres, extracted, generalized, and stripped of every Postgres- and dstack-specific assumption that does not belong in a generic mesh.

The goal: any application that wants to run a mesh of mutually-attested CVMs — Postgres, Redis, a Tendermint-style consensus net, a private inference cluster, anything — can deploy a TeeMesh cluster contract, drop the TeeMesh sidecar into their CVM image, and get:

1. Provable membership: every peer is a CVM running an approved image on approved hardware, anchored to a TEE attestation chain that the cluster contract verifies.
2. Encrypted on-chain messaging: members can send each other arbitrary payloads through the chain, encrypted to TEE-derived keys, with no off-chain messaging broker required.
3. Self-bootstrapping wireguard mesh: each CVM derives its wireguard keys from its TEE, publishes them through the cluster contract, exchanges connection info over the encrypted message channel, and brings up a full mesh — without any application-level orchestrator.
4. Liveness consensus: every node heartbeats every peer over the wireguard mesh; the CVM only proceeds past the mesh-bring-up gate once it has confirmed connections to every node the network agrees is live.

What this spec deliberately does **not** include:
- The application that runs inside the CVM (Postgres, etc.).
- Higher-level cluster semantics like leader election, sharding, or quorum protocols — those belong on top of TeeMesh, in the application layer.
- A control plane for cluster *operators* — administrative actions like adding attestation patterns are on-chain calls gated on the cluster owner; this spec does not define a dashboard, CLI, or SaaS service.

---

## 2. Architecture overview

```
┌────────────────────────────────────────────────────────────────────┐
│                              EVM L2                                 │
│                                                                     │
│   ┌─────────────────────────────────────────────────────────────┐  │
│   │         ClusterDiamond (ERC-2535 proxy)                     │  │
│   │                                                             │  │
│   │   ── Core facets (always present) ─────────────────────     │  │
│   │   ┌──────────┐ ┌───────────┐ ┌──────────────┐               │  │
│   │   │ Attest   │ │ Message   │ │  Network     │               │  │
│   │   │ Facet    │ │ Facet     │ │  Facet       │               │  │
│   │   └──────────┘ └───────────┘ └──────────────┘               │  │
│   │                                                             │  │
│   │   ── Platform facets (cluster opts in per platform) ───     │  │
│   │   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐    │  │
│   │   │ Dstack   │ │ IntelTdx │ │ AmdSnp   │ │ NvidiaCc   │    │  │
│   │   │ Facet    │ │ Facet*   │ │ Facet*   │ │ Facet*     │    │  │
│   │   └──────────┘ └──────────┘ └──────────┘ └────────────┘    │  │
│   │                          * = future                         │  │
│   └─────────────────────────────────────────────────────────────┘  │
│                              ▲                                      │
│                              │ passthrough                          │
│                              │                                      │
│   ┌──────────────────────────┴──────────────────────────┐          │
│   │   ClusterMember proxies (one per CVM)               │          │
│   └─────────────────────────────────────────────────────┘          │
│                              ▲                                      │
│                              │ event subscription (eth_getLogs)     │
│                              │                                      │
│   ┌──────────────────────────┴──────────────────────────┐           │
│   │   Indexer (TEE service, watches many clusters)      │           │
│   └────┬───────────────────────────────────────────────┘            │
└────────┼─────────────────────┼──────────────────────────────────────┘
         │                     │
         │ signed event push   │ register() / publishWgKey() / send()
         │ (+ RPC-repro stub)  │ via ClusterMember passthrough
         ▼                     ▼
        ┌─────────────────────────┐
        │   CVM sidecar           │
        │   (Rust, runs in each   │
        │   confidential VM)      │
        └─────────────────────────┘
```

The diamond's surface area is split into two layers:

- **Core facets** know about cluster membership, encrypted messaging, and wireguard signalling. They are platform-agnostic, always installed, and define the contract's stable API.
- **Platform facets** know about one specific TEE platform. They verify that platform's attestation quote shape, hold that platform's allowlists, and on successful registration write into the shared member storage that the core facets read from. Each cluster's deployer chooses which platform facets to install based on the TEE platforms they want to admit.

Every CVM that participates in the cluster is represented on chain by a **ClusterMember** passthrough contract. The CVM's TEE attestation chain commits to the member contract's address (analogous to dstack's `app_id`). The member contract forwards a small set of calls into the diamond's facets, so the diamond can run cluster-wide logic while individual CVMs only ever interact with their own member address.

---

## 3. Core facets

The ClusterDiamond is constructed with these three facets always installed, plus the standard solidstate base (DiamondCut, loupe, ERC-165, SafeOwnable). They define the cluster's stable API surface: anything an application built on TeeMesh relies on lives here.

### 3.1 AttestFacet

The platform-agnostic member registry — the canonical "who is in this cluster" surface. AttestFacet owns the shared `MemberStorage` ERC-7201 namespace that every platform facet writes into on successful registration, and exposes platform-independent membership queries that the other core facets gate against.

Storage:

- **Members**: an indexed map of `memberId → MemberRecord`, where each record carries (a) the platformId the member was admitted under, (b) the member's TEE-derived x25519 public key (used by MessageFacet), (c) the wireguard public key (mirrored from NetworkFacet for one-shot reads), (d) the member-contract address, and (e) any application-relevant metadata supplied at registration time.
- **Indices**: `memberByAddress[address] → memberId`, `members[]` for enumeration, `memberCount`.

External surface (all view):

- `isClusterMember(address) → bool`
- `memberOf(address) → MemberRecord`
- `xPubKeyOf(memberId) → bytes32`
- `wgPubKeyOf(memberId) → bytes32` (convenience mirror of NetworkFacet)
- `listMembers() → memberId[]`
- `memberCount() → uint256`

Internal surface (callable only by other facets in the same diamond, gated on `address(this) == msg.sender` or equivalent):

- `_addMember(MemberRecord) → memberId`
- `_setWgPubKey(memberId, bytes32)`

AttestFacet does **not** verify attestation quotes itself. Verification lives in platform facets. AttestFacet is purely the member-registry side of the bookkeeping.

### 3.2 MessageFacet

A per-member message inbox, gated on `AttestFacet.isClusterMember(msg.sender)`.

- Each member has a logical "channel" identified by its memberId.
- `send(memberId recipient, bytes ciphertext, bytes32 envelopeId)` appends an encrypted payload to the recipient's channel. `envelopeId` is a sender-chosen identifier (typically a hash of the plaintext) so duplicates can be detected by the reader.
- Payloads are perma-stored on chain via event emission (`MessageSent(senderMemberId, recipientMemberId, envelopeId, ciphertext)`). The contract does not retain raw bytes in storage; readers reconstruct channel history by indexing events.
- Ciphertexts are encrypted by the sender to the recipient's x25519 public key (read from AttestFacet) using libsodium-style sealed boxes (XSalsa20-Poly1305 over X25519 ECDH with an ephemeral sender key). The MessageFacet does not validate this — it cannot, since it does not know the cipher — but any non-encrypted payload is a directive violation under the project's critical-directive rules.
- There is no message size limit at the contract level; gas is the only ceiling. Application-layer chunking is the caller's problem.

The message log is intentionally append-only and unbounded. Pruning, archival, and indexing are off-chain concerns.

### 3.3 NetworkFacet

The wireguard signalling surface, gated on `AttestFacet.isClusterMember(msg.sender)`.

- `publishWgKey(bytes32 wgPublicKey)` stores the caller's wireguard public key (replacing any previous value), updates the mirror in AttestFacet, and emits `WgKeyPublished(memberId, wgPublicKey)`.
- `wgPublicKeyOf(memberId) → bytes32` returns the last-published key.
- `listMembers() → memberId[]` is a convenience pass-through to AttestFacet so a joining CVM can fetch every peer's wgPublicKey in one batch read.

NetworkFacet does **not** store endpoint addresses (IP:port). Endpoint info is exchanged through MessageFacet, encrypted to the recipient — exposing endpoint addresses on chain leaks more than necessary about the deployment topology.

---

## 4. Platform facets

Each TEE platform TeeMesh supports is represented by exactly one **platform facet** on the diamond. A cluster's deployer installs whichever platform facets they want to admit. A cluster can install zero, one, or many platform facets:

- **Zero**: nothing can register. Useful for a paused / not-yet-bootstrapped cluster.
- **One**: the common case. A single-platform cluster (e.g. dstack-only).
- **Many**: a heterogeneous cluster admitting CVMs from multiple TEE families. All members share the same MessageFacet / NetworkFacet surface regardless of which platform admitted them; the only thing platform-specific is the registration path and the allowlist semantics.

Platform facets are added or removed via the diamond's `diamondCut`, gated on the diamond's solidstate owner (the cluster Safe).

### 4.1 The platform facet contract

Every platform facet:

1. **Owns its platform's allowlist storage** in its own ERC-7201 namespace. Compose hashes for dstack. MR_TD / RTMR for Intel TDX. Launch-measurement / policy bits for AMD SEV-SNP. NVIDIA CC report measurements. Each platform's shape is whatever that platform's attestation report exposes.
2. **Exposes platform-specific admin selectors** for the cluster owner to manage its allowlists. For DstackFacet these are the `IAppAuthBasicManagement` set (`addComposeHash`, `addDevice`, `setRequireTcbUpToDate`, …); for future platform facets they are whatever the platform needs.
3. **Exposes a single platform-specific `register` selector** that a CVM (via its ClusterMember proxy) calls to join the cluster. The selector name is namespaced: `dstack_register(...)`, `tdx_register(...)`, `snp_register(...)`, etc.
4. **Performs three checks** inside that `register`:
   - **Quote validity**: verify the attestation quote per the platform's rules (signature chain, freshness, etc.).
   - **Allowlist match**: the quote's measurements must satisfy the facet's allowlist.
   - **Key binding**: the quote's user-data slot (`report_data` on TDX/SGX, `user_data` on SEV-SNP, equivalent on others — every supported platform exposes at least 64 bytes here) must equal `keccak256(abi.encodePacked(xPubKey, wgPubKey))` or, where the slot is wide enough, the raw concatenation `xPubKey || wgPubKey`. The exact binding rule is documented in each platform facet's spec; the goal is the same — the on-chain pubkeys cannot have been substituted by anyone outside the TEE.
5. **On success, writes the member into AttestFacet's shared storage** via the internal `_addMember` selector, stamping `platformId = <thisPlatform>`. Also calls `NetworkFacet._setWgPubKey` so the wg pubkey is registered in the same transaction.
6. **Implements any platform-required external interfaces** that off-chain tooling expects. DstackFacet, specifically, implements dstack's `IAppAuth` + `IAppAuthBasicManagement` so the dstack KMS and phala-cli interact with a TeeMesh cluster the same way they interact with a stock dstack app contract. Other platform facets may have their own equivalent.

The platform facet's `register` is what makes a TeeMesh cluster heterogeneous-capable: every facet writes into the same MemberStorage shape, every member ends up indistinguishable from the perspective of MessageFacet and NetworkFacet.

### 4.2 DstackFacet (default-shipped)

The only platform facet that ships in the initial TeeMesh release. Mirrors the existing dstackgres dstack-attestation flow:

- **Allowlists**: `allowedComposeHashes`, `allowedDeviceIds`, `allowAnyDevice`, `requireTcbUpToDate`, `allowedKmsRoots` — all manageable via the `IAppAuthBasicManagement` interface so existing dstack tooling works unchanged.
- **`isAppAllowed(AppBootInfo)`**: implements dstack's `IAppAuth` boot-gate interface. Called by the dstack KMS at CVM boot via the ClusterMember passthrough. Returns `(true, "")` iff the boot info's composeHash and deviceId are in the allowlists and the appId is one of this diamond's passthrough members.
- **`dstack_register(DstackProof proof, bytes32 xPubKey, bytes32 wgPubKey)`**: verifies the dstack KMS 3-level signature chain (KMS root → app key → derived key), checks the derived key's bound report_data commits to the supplied pubkeys, checks the resulting code identifier is one of our passthroughs, and writes the member.
- **`IAppAuth`-flavored boot path** stays intact: a dstack CVM still boots via `DstackKms.registerApp(appId = passthrough)` → `ClusterMember.isAppAllowed(bootInfo)` → `DstackFacet.isAppAllowed(bootInfo)`. This is what makes a TeeMesh cluster a drop-in replacement for a stock dstack app contract.

### 4.3 Future platform facets

These are not part of v1; they are sketched here so the platform-facet design can be evaluated against the platforms it will eventually need to host.

- **IntelTdxFacet** — Intel TDX direct (no dstack wrapper). Allowlists MR_TD, RTMRs, and policy bits. Verifies TDX quotes against Intel's PCS root (or a pinned signer set). Useful for non-Phala TDX deployments.
- **AmdSnpFacet** — AMD SEV-SNP. Allowlists launch measurement + ID block + policy. Verifies the attestation report against AMD's VCEK chain.
- **NvidiaCcFacet** — NVIDIA confidential GPU. Allowlists the GPU measurement report. Verifies against NVIDIA's attestation service signer.
- **GenericReportDataFacet** — a permissive "if it has a 64-byte user-data slot and you trust the verifier I'm pointed at, admit it" facet. Useful for prototyping a platform before writing a dedicated facet for it.

Each is a discrete diamondCut addition; none requires touching the core facets.

---

## 5. Member contracts

Each CVM is represented by a per-CVM **ClusterMember** proxy, deterministically deployed by a `ClusterMemberFactory` so the proxy address can be predicted before the CVM ever boots. (The proxy address is what the TEE attestation chain commits to.)

ClusterMember proxies are passthrough: they forward a small, fixed set of selectors (the dstack `IAppAuth` family on the DstackFacet, plus any future per-platform attestation interfaces) into the diamond. They do not hold cluster state. They are upgradeable via UUPS, gated on the diamond's owner — this lets us migrate per-CVM ABIs as new TEE platforms ship, without disturbing the diamond.

The Member abstraction is identical in shape to dstackgres's `TeeSqlClusterMember`. Nothing about it is Postgres-specific in dstackgres either; the rename to `ClusterMember` is the only change.

If a cluster supports multiple platforms (multiple platform facets installed), each ClusterMember can specialize for one platform — typically by being deployed via a per-platform ClusterMember impl that forwards the platform's specific external interfaces. The diamond does not care which impl is behind a given member, only that the member's address is registered as one of its passthroughs.

---

## 6. Indexer

The cluster contract emits events for every state change: `MemberRegistered`, `WgKeyPublished`, `MessageSent`, allowlist mutations, owner transfers, facet swaps. The CVM sidecar (§7) needs to react to most of them — most obviously, a member must consume `MessageSent` events addressed to its memberId or it cannot bring up the wireguard mesh.

Requiring every CVM to maintain its own chain RPC subscription is bad: it scales linearly with cluster size against a paid RPC, websocket subscriptions drop in CVMs that migrate or hibernate, and polling wastes work when a cluster is quiet. TeeMesh ships a **shared Indexer** that solves this for every cluster at once, following the same pattern dstackgres established with its monitoring-hub.

### 6.1 Role

The Indexer is a TEE-attested off-chain service that:

1. Watches the chain (one RPC subscription, shared across every cluster it serves) for events emitted by any ClusterDiamond it has been asked to follow.
2. For each event, identifies which cluster it belongs to and which members of that cluster have subscribed.
3. Pushes the event to those members — and only those members.
4. Pairs each push with two artefacts that let the member verify the push independently:
   - the Indexer's **TEE attestation signature** over the pushed bytes (the Indexer's signing key is itself attested by its TEE; the cluster knows the Indexer's pubkey from on-chain discovery), and
   - an **RPC repro stub** — the exact `eth_getLogs` / `eth_getTransactionReceipt` call (contract address, block range, topic filter) that, if a member runs it against any RPC provider, returns the same event bytes. The repro stub means the Indexer's claim is independently checkable, not just trust-the-signature.

The Indexer covers many clusters but each push only goes to members of the specific cluster that emitted the event. There is no cross-cluster leak — a member of cluster A is not subscribed to and never receives events from cluster B.

### 6.2 Trust posture

The Indexer's TEE attestation commits to its code. Members trust the Indexer for:

- **Liveness** of event delivery (the Indexer is online and pushing).
- **Completeness** of event delivery within its subscription window (no event is silently dropped).

Members do **not** have to trust the Indexer for:

- **Correctness** of event data (the RPC repro stub lets them verify any push against any RPC provider).
- **Confidentiality** of message contents (`MessageSent` ciphertext is sealed-boxed to the recipient; the Indexer sees the ciphertext but cannot decrypt).

A member sidecar may sample pushes — issuing the repro stub against an independent RPC provider on (say) 1 in N events — without changing its steady-state cost much. The Indexer can therefore be operated by a third party with no loss of trust-minimization on data correctness.

### 6.3 Discovery

A member sidecar discovers the Indexer at startup by reading a known **IndexerRegistry** contract — a tiny on-chain registry mapping `chainId → (indexerEndpoint, indexerCodeId, indexerPubKey)`. The registry is owned by the TeeMesh org Safe and exists per chain we deploy on (Base mainnet for v1).

The CVM sidecar reads the IndexerRegistry directly via RPC at startup — this is one of the only direct RPC reads the sidecar does. After Indexer subscription is established, all subsequent event ingestion goes through the Indexer.

A cluster may override the default indexer by storing its own indexer reference in a cluster-scoped namespace (a later spec). v1 ships only the default-discovery path.

### 6.4 Subscription protocol

Member sidecar → Indexer over a long-lived bidirectional connection (transport TBD — see §11):

1. Member opens a connection and presents `(memberId, clusterAddress, attestationProof)`.
2. Indexer verifies that `memberId` exists in `clusterAddress`'s AttestFacet `MemberStorage` and that the attestation matches the recorded TEE pubkeys. (The Indexer is essentially re-running the same verification the platform facet did at registration time — but it can do so as an off-chain read since the cluster diamond is authoritative.)
3. On success, Indexer adds the member to the cluster's subscriber set, records the highest delivered `blockNumber` for that member, and begins streaming events.
4. Each event is delivered as a signed envelope: `{event, clusterAddress, blockNumber, txHash, logIndex, rpcReproStub, indexerAttestation, indexerSignature}`.
5. Member verifies the signature against the Indexer's pubkey from IndexerRegistry. On signature mismatch (or attestation mismatch on the Indexer's first push of the session), the member tears down the subscription and re-discovers.

Subscriptions are stateful: the Indexer remembers per-member delivery cursors so a reconnecting member catches up cleanly rather than losing events.

### 6.5 Indexer infrastructure (v1)

For v1 the Indexer ships as a single CVM image, run by TeeMesh-org, with whatever HA shape is operationally appropriate (load balancer + N replicas; each replica is independently attested). A future spec will treat the Indexer itself as a TeeMesh cluster — eating our own dog food, with members of the Indexer cluster mutually attesting each other through the same primitives this spec defines. v1 punts that recursion.

---

## 7. CVM sidecar

A Rust binary (target: `cluster-mesh-agent`) shipped as an OCI image and included in CVM `docker-compose` files. Runs as a sidecar with elevated privileges (needs to configure wireguard) and a healthcheck that the application's main container can depend on.

### 7.1 Boot sequence

1. **Discover cluster address.** The sidecar reads its own member contract address from a `MEMBER_CONTRACT` env var or a file mounted from the dstack runtime. It calls `member.cluster()` to get the ClusterDiamond address.
2. **Derive identity keys.** Using the TEE-derived seed, produce a single Curve25519 root from a known purpose string (e.g. `teemesh.identity.v1`), then derive:
   - an **x25519 keypair** for sealed-box decryption of MessageFacet payloads, and
   - an **Ed25519 keypair** for signing off-chain heartbeats and any in-network attestations.
   Same underlying curve, two operations; on dstack this comes out of `derive_key` with a deterministic purpose string. A separate derivation produces the wireguard keypair (`teemesh.wireguard.v1`).
3. **Construct the attestation request.** The sidecar asks the TEE to produce an attestation quote whose user-data slot (`report_data` / equivalent) commits to `xPubKey || wgPubKey`. On dstack this is a `RawQuote` request with that 64-byte payload. The TEE will only emit such a quote if it actually controls those pubkeys, so the binding is forgery-resistant.
4. **Register.** Bundle the quote into a platform-specific `Proof` shape and call the appropriate platform-facet selector — `dstack_register(proof, xPubKey, wgPubKey)` on a dstack CVM, `tdx_register(...)` on an Intel TDX CVM, etc. The CVM only knows its own platform; it does not need to enumerate the diamond's installed facets. On failure (verifier reverts, pattern not whitelisted), retry with backoff — but never silently proceed past this gate. Until registration succeeds, the sidecar never reports healthy.
5. **Publish wireguard key.** Call `NetworkFacet.publishWgKey(wgPubKey)` through the member proxy. (In v1 we can fold this into `register` since registration already binds the wg pubkey; we keep the separate selector for rotation and resilience.)
6. **Subscribe to the Indexer.** Read the IndexerRegistry contract for this chain to discover the Indexer endpoint and pubkey. Open a subscription (§6.4), presenting `(memberId, clusterAddress, attestationProof)`. From this point on, every `MessageSent`, `MemberRegistered`, `WgKeyPublished`, or other cluster event the sidecar cares about arrives as a signed Indexer push — the sidecar does not poll the chain directly for events.
7. **Wait for peer endpoints.** Each Indexer push of a `MessageSent` event on the member's channel is decrypted (sealed-box, x25519). Payloads that parse as `PeerEndpoint{ memberId, ip, port, wgPublicKey, expiresAt }` from existing members are consumed: the sidecar configures the wireguard interface with the peer.
8. **Send own endpoint.** For every other member the sidecar learns about (via `AttestFacet.listMembers()` plus inbound endpoint messages), encrypt a `PeerEndpoint` of self to the peer's x25519 pubkey via sealed-box and send it via MessageFacet.
9. **Heartbeat.** For every wireguard peer, run a lightweight heartbeat — a periodic UDP packet over wireguard carrying (a) sender memberId, (b) timestamp, (c) the sender's view of which peers it currently considers connected. Heartbeats are signed with the Ed25519 key so they are not spoofable on the wire.
10. **Liveness consensus gate.** The sidecar maintains a local view:
    - A node is **live** iff at least one other node's heartbeat reports it as connected.
    - The mesh is **converged** iff every live node reports the same connected-set, and that set equals the live set.
    - Until the mesh is converged, the sidecar's healthcheck returns 503 and the application container does not start.
11. **Become healthy.** Once converged, healthy is reported. Heartbeat continues for the life of the process; on transient drops the sidecar re-tries connection and the application's own logic decides whether to degrade. The Indexer subscription stays open for the life of the process; if it drops, the sidecar reconnects and resumes from its last-delivered cursor.

### 7.2 Failure modes

- **Pattern revoked mid-flight.** If the cluster owner removes the attestation pattern between step 4 and the application coming up, no member already registered is forcibly removed (no on-chain eviction in v1); but new joiners cannot register, and a future re-register attempt (e.g. after a CVM restart) will fail. v1 punts cluster-driven eviction to a later spec.
- **Indexer down.** If the Indexer is unreachable at boot, the sidecar stays in step 6 and reports unhealthy. The application container does not start. There is no v1 fallback to direct chain polling — operationally, the Indexer's HA shape is what guarantees liveness.
- **Indexer signature/attestation mismatch.** Treated as adversarial: the sidecar tears down the subscription, re-reads IndexerRegistry, and retries. If the pubkey on chain has been rotated (legitimate operator action), the new subscription succeeds. If not, the sidecar fails closed and stays unhealthy.
- **Message channel poisoned.** A malicious member could spam another member's channel with garbage. Decryption failures are silently dropped; the sidecar logs at debug only. Rate-limiting is not enforced on chain in v1.
- **Liveness deadlock.** If the network is partitioned at startup such that no convergence is possible, the sidecar stays unhealthy indefinitely. This is intentional — degraded boot of an unmeshed mesh is worse than visible failure.

---

## 8. Trust model

The diamond's cluster owner (a Safe in production) controls:
- Which platform facets are installed (via diamondCut).
- Each platform facet's allowlists (compose hashes, MR_TDs, launch measurements, etc.).
- Diamond-level admin (DiamondCut, SafeOwnable transfer).
- Member factory address.

The cluster owner can **not** decrypt messages, derive members' wireguard keys, or impersonate a member. All TEE-derived secrets stay in the TEEs. The chain only ever sees public commitments (pubkeys, ciphertexts, addresses).

A compromised cluster owner can install a malicious platform facet or relax an existing facet's allowlists to admit malicious CVMs as members. That attacker would then be able to read messages addressed to themselves. They would **not** be able to read messages addressed to honest members, since each member's inbox is sealed-boxed to that member's TEE-derived x25519 pubkey.

A compromised TEE platform vendor (e.g. a malicious dstack KMS root, a leaked Intel PCS signer key) is out of scope: each platform's security model is taken as a given by that platform's facet. Mitigations exist platform-side (root rotation, multi-root patterns) and can be reflected here by updating the facet's allowlists or pinned signer set.

Critically, **registration binding does not rely on per-CVM ECDSA**. The x25519 + wg public keys are bound to the CVM by being committed into the attestation quote's user-data slot, which the TEE will only sign over data that genuinely lives inside it. There is no per-CVM secp256k1 signing key. The only ECDSA on chain is the platform-specific verifier checking the platform's own root signer chain — secp256k1 stays inside the platform facet, never on the per-member key path.

---

## 9. Differences from dstackgres

dstackgres is the codebase TeeMesh is being extracted from. The differences:

- **No Postgres anything.** dstackgres's `CoreFacet` mixes membership with `endpoint` strings, DNS labels, leader leases, signer authorization — all Postgres-cluster-specific. TeeMesh's AttestFacet keeps only the membership-registry shape.
- **No control-plane facet.** dstackgres has a `ControlPlaneFacet` for off-chain action authorization. TeeMesh omits it; if an application needs it, they can add a facet via diamondCut.
- **No leader lease.** Leader election is application-level; TeeMesh does not assume one is needed.
- **Verifier as facet, not external contract.** dstackgres has a separate `DstackVerifier` UUPS contract registered with `TEEBridge` via an adapter registry. TeeMesh folds that role directly into the platform facet: there is no separate verifier contract, no adapter registry, no `IVerifier` interface needed cross-platform. Each platform's verification logic is library code linked into its facet.
- **MessageFacet is new.** dstackgres has nothing equivalent — endpoint exchange there happens via `signalEndpoint(bytes ciphertext, ...)` on `WgMeshFacet`. TeeMesh splits this cleanly: messaging is one facet, networking is another.
- **Wireguard signalling is decoupled from endpoint registry.** dstackgres stores endpoint blobs on chain; TeeMesh stores only wg public keys on chain and pushes endpoint info through MessageFacet so deployment topology is not leaked.
- **Curve25519 only on the member key path.** dstackgres derives a per-CVM secp256k1 key and binds it via `ecrecover` on a signed registration message. TeeMesh derives x25519 + Ed25519 from a Curve25519 seed and binds them via the attestation quote's user-data commitment — no per-CVM ECDSA touches the chain.

---

## 10. Out of scope (v1)

- On-chain eviction of misbehaving members
- Cluster-to-cluster federation
- Non-EVM target chains
- The CVM sidecar's docker-compose authoring helpers (we will hand-roll those for the first integrations and standardize later)
- A reference application running on top of TeeMesh (dstackgres will become the first such application, post-extraction)
- Platform facets other than DstackFacet

---

## 11. Open questions

These are tracked as open questions to resolve before the spec moves out of draft:

1. **Member factory ownership.** Should the ClusterMemberFactory be diamond-owned (each cluster has its own factory) or shared across all clusters in the org? Per-platform member impls add a wrinkle here.
2. **Reorg handling for registration.** Do we wait for finality before treating a member as registered, or accept and let upstream prune? The Indexer's per-member cursor needs an answer here too.
3. **DstackFacet bootstrapping.** On a fresh cluster, the cluster owner needs to seed dstack's allowedKmsRoots before any CVM can register. Does this happen via the diamond constructor (init contract), or as a post-deploy admin call?
4. **Indexer push transport.** gRPC streaming? HTTP/2 server-sent events? A custom protocol over libp2p? Each has different ops/observability/firewall trade-offs.
5. **Indexer-side member-attestation cache.** On subscribe, the Indexer re-verifies the member's attestation. Do we cache the result with a TTL, or re-verify on every reconnect? Affects p99 reconnect latency vs freshness against on-chain eviction (if eviction lands later).
6. **Member-side sampling cadence for Indexer pushes.** What fraction of pushes does a member spot-check via the RPC repro stub? Always (defeats the purpose), never (max trust in Indexer), or 1-in-N with N adjustable?

---

## 12. Resolved design decisions

(Decisions called during the spec's drafting that may otherwise look load-bearing without context.)

1. **Heartbeat transport: UDP-over-wireguard, gossip-computed convergence** (not on chain via MessageFacet). Cheap, fast, no per-heartbeat gas. Off-chain observers wanting "is the mesh healthy" must consume from a member.
2. **Curve25519-only on the per-CVM key path** (sealed-box on x25519 for messaging, Ed25519 for heartbeat signatures). Per-CVM keys are bound via attestation quote user-data commitments, not on-chain ECDSA. Per-CVM secp256k1 is gone.
3. **Platform support = installed facet.** Each TEE platform is a facet on the diamond. Clusters install whichever platform facets they want to admit; the core facets (Attest / Message / Network) are platform-agnostic and never need to change as new platforms ship.
4. **Target chain: Base mainnet.** Same chain as dstackgres. Chain-agnosticism is a v2+ concern; v1 deployment scripts, the IndexerRegistry instance, and the org Safe-owned addresses are all Base-specific.
5. **Event delivery via a shared TEE-attested Indexer**, not direct chain polling from each CVM. Members trust the Indexer for liveness and completeness only; each push carries an RPC repro stub so correctness is independently verifiable per event. Follows the dstackgres monitoring-hub pattern.
6. **Monorepo.** Contracts, CVM sidecar, and Indexer service all live in `TeeSQL/TeeMesh`. The protocol and its reference implementations evolve together; the spec in this repo is authoritative for the deployed Indexer it ships alongside.
7. **No dstackgres compatibility, Postgres deferred.** TeeMesh is the generic mesh primitive. The existing TeeSQL Postgres-as-a-Service product is on hold and the existing dstackgres deployments on Base mainnet are not migration targets. dstackgres is referenced in §9 strictly as the *extraction source* — useful for understanding which moving parts were ripped out and why — not as a system we owe ABI compatibility to. A future Postgres application on top of TeeMesh is plausible but explicitly out of scope for v1.

---

## 13. References

- [dstack — IAppAuth / IAppAuthBasicManagement](https://github.com/Dstack-TEE/dstack/blob/master/kms/auth-eth/contracts/)
- [dstackgres contracts/teesql-group-auth](https://github.com/TeeSQL/dstackgres/tree/main/contracts/teesql-group-auth) — extraction source
- [EIP-2535 Diamonds](https://eips.ethereum.org/EIPS/eip-2535)
- [solidstate-solidity diamond base](https://github.com/solidstate-network/solidstate-solidity)
- [libsodium sealed-box](https://doc.libsodium.org/public-key_cryptography/sealed_boxes)
- [wireguard whitepaper](https://www.wireguard.com/papers/wireguard.pdf)
