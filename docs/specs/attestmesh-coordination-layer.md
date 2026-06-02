# AttestMesh — Coordination Layer Master Spec

**Status**: Draft v0.3
**Authors**: LSDan
**Last updated**: 2026-06-01

---

## 1. Purpose

AttestMesh is the on-chain coordination layer for clusters of mutually-attested nodes. It is the part of [dstackgres](https://github.com/TeeSQL/dstackgres) that has nothing to do with Postgres, extracted, generalized, and stripped of every Postgres- and dstack-specific assumption that does not belong in a generic mesh.

The goal: any application that wants to run a mesh of mutually-attested nodes — Postgres, Redis, a Tendermint-style consensus net, a private inference cluster, anything — can deploy an AttestMesh cluster contract, drop the AttestMesh sidecar into their node image, and get:

1. Provable membership: every peer is a node running approved software, anchored to an attestation chain that the cluster contract verifies.
2. Encrypted on-chain messaging: members can send each other arbitrary payloads through the chain, encrypted to attestation-bound keys, with no off-chain messaging broker required.
3. Self-bootstrapping wireguard mesh: each node derives its wireguard keys from its attestation, publishes them through the cluster contract, exchanges connection info over the encrypted message channel, and brings up a full mesh — without any application-level orchestrator.
4. Liveness consensus: every node heartbeats every peer over the wireguard mesh; the node only proceeds past the mesh-bring-up gate once it has confirmed connections to every node the network agrees is live.

What this spec deliberately does **not** include:
- The application that runs inside the node (Postgres, etc.).
- Higher-level cluster semantics like leader election, sharding, or quorum protocols — those belong on top of AttestMesh, in the application layer.
- A control plane for cluster *operators* — administrative actions like adding attestation patterns are on-chain calls gated on the cluster owner; this spec does not define a dashboard, CLI, or SaaS service.

**Join invariant.** Any client may join an AttestMesh cluster iff it can present, to an installed attestor facet, a valid proof that (a) it runs software the facet's allowlist approves and (b) that proof is cryptographically bound to the x25519/wireguard public keys it publishes on chain. Only the nature of the proof varies between attestation methods; everything downstream of registration is method-agnostic.

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
│   │   ── Attestor facets (cluster opts in per method) ───       │  │
│   │   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐    │  │
│   │   │ Dstack   │ │ IntelTdx │ │ AmdSnp   │ │ NvidiaCc   │    │  │
│   │   │ Facet    │ │ Facet*   │ │ Facet*   │ │ Facet*     │    │  │
│   │   └──────────┘ └──────────┘ └──────────┘ └────────────┘    │  │
│   │                          * = future                         │  │
│   └─────────────────────────────────────────────────────────────┘  │
│                              ▲                                      │
│                              │ execute()  (EIP-4337 entry path)     │
│                              │  + dstack IAppAuth                   │
│                              │                                      │
│   ┌──────────────────────────┴──────────────────────────┐          │
│   │  ClusterMember (one per node):                      │          │
│   │    dstack app proxy + EIP-4337 smart wallet         │          │
│   └─────────────────────────────────────────────────────┘          │
│                              ▲                                      │
│                              │ event subscription (eth_getLogs)     │
│                              │                                      │
│   ┌──────────────────────────┴──────────────────────────┐           │
│   │   Indexer (TEE service, watches many clusters)      │           │
│   └────┬───────────────────────────────────────────────┘            │
└────────┼─────────────────────┼──────────────────────────────────────┘
         │                     ▲
         │ signed event push   │ EntryPoint.handleOps(userOps) — sent by Alchemy bundler
         │ (+ RPC-repro stub)  │
         │                     │     ┌──────────────────────────────────┐
         │                     │     │ Alchemy bundler + paymaster      │
         │                     ├─────┤ (asks gas-webhook to approve)    │
         │                     │     └────────────▲─────────────────────┘
         │                     │                  │ approve/deny
         │                     │     ┌────────────┴─────────────────────┐
         │                     │     │ AttestMesh org Cloudflare Worker │
         │                     │     │ (gas-sponsorship-webhook)        │
         │                     │     └──────────────────────────────────┘
         │                     │
         │                     │ eth_sendUserOperation
         ▼                     │
        ┌─────────────────────────┐
        │   node sidecar          │
        │   (Rust, runs in each   │
        │   node)                 │
        │   — holds zero ETH      │
        └─────────────────────────┘
```

The diamond's surface area is split into two layers:

- **Core facets** know about cluster membership, encrypted messaging, and wireguard signalling. They are attestation-method-agnostic, always installed, and define the contract's stable API.
- **Attestor facets** know about one attestation method. They verify that method's attestation proof shape, hold that method's allowlists, and on successful registration write into the shared member storage that the core facets read from. Each cluster's deployer chooses which attestor facets to install based on the attestation methods they want to admit.

Every node that participates in the cluster is represented on chain by a **ClusterMember** contract that combines two responsibilities on a single address: a dstack-style app proxy (so dstack's KMS recognizes the CVM at boot via `IAppAuth`) AND an EIP-4337 v0.7 smart wallet (so the sidecar can submit gasless UserOps via Alchemy's bundler with paymaster-sponsored gas — §13 item 18). The node's attestation commits to this address; the diamond sees this address as `msg.sender` on every member-originated call. Details in §5.

---

## 3. Core facets

The ClusterDiamond is constructed with these three facets always installed, plus the standard solidstate base (DiamondCut, loupe, ERC-165, SafeOwnable). They define the cluster's stable API surface: anything an application built on AttestMesh relies on lives here.

### 3.1 AttestFacet

The attestation-method-agnostic member registry — the canonical "who is in this cluster" surface. AttestFacet owns the shared `MemberStorage` ERC-7201 namespace that every attestor facet writes into on successful registration, and exposes method-independent membership queries that the other core facets gate against.

Storage:

- **Members**: an indexed map of `memberId → MemberRecord`, where each record carries (a) the attestorId the member was admitted under, (b) the member's attestation-bound x25519 public key (used by MessageFacet), (c) the wireguard public key (mirrored from NetworkFacet for one-shot reads), (d) the member-contract address.
- **Indices**: `memberIdOf[address] → memberId`, `memberIds` for enumeration, `memberCount`.
- **Cluster-wide config** (consolidated from the cluster's `DiamondInit`): `clusterOwner`, `pendingClusterOwner`, `meshCidrIp`, `meshCidrPrefix`. See contracts spec §4.1 for the canonical layout.

External surface (all view):

- `isClusterMember(address) → bool`
- `memberOf(address) → MemberRecord`
- `xPubKeyOf(memberId) → bytes32`
- `wgPubKeyOf(memberId) → bytes32` (convenience mirror of NetworkFacet)
- `listMembers() → memberId[]`
- `memberCount() → uint256`
- `clusterOwner() → address`, `pendingClusterOwner() → address` (cluster-wide config readers)
- `meshCidr() → (uint32 ip, uint8 prefix)`, `meshIpOf(bytes32 memberId) → uint32` (deterministic IP derivation per §7.3)

Events:

- `MemberRegistered(bytes32 indexed memberId, address indexed memberContract, bytes32 indexed attestorId, bytes32 xPubKey, bytes32 wgPubKey)`

Internal surface (callable only by other facets in the same diamond, gated on `address(this) == msg.sender` or equivalent):

- `_addMember(MemberRecord) → memberId`
- `_setWgPubKey(memberId, bytes32)`

AttestFacet does **not** verify attestation proofs itself. Verification lives in attestor facets. AttestFacet is purely the member-registry side of the bookkeeping.

### 3.2 MessageFacet

A per-member message inbox, gated on `AttestFacet.isClusterMember(msg.sender)`.

- Each member has a logical "channel" identified by its memberId.
- `send(bytes32 recipientMemberId, bytes32 envelopeId, bytes ciphertext)` appends an encrypted payload to the recipient's channel. `envelopeId` is a sender-chosen identifier (typically a hash of the plaintext); the contract reverts with `DuplicateEnvelope` if the same `envelopeId` has already been sent to that recipient. This on-chain dedup is what makes the CSK-onboarding race (§8.2) free of protocol damage — racers' second send reverts at the cost of one wasted tx and no protocol state is mutated.
- Payloads are perma-stored on chain via event emission (`MessageSent(senderMemberId, recipientMemberId, envelopeId, ciphertext)`). The contract does not retain raw bytes in storage; readers reconstruct channel history by indexing events.
- Ciphertexts are encrypted by the sender to the recipient's x25519 public key (read from AttestFacet) using libsodium-style sealed boxes (XSalsa20-Poly1305 over X25519 ECDH with an ephemeral sender key). The MessageFacet does not validate this — it cannot, since it does not know the cipher — but any non-encrypted payload is a directive violation under the project's critical-directive rules.
- There is no message size limit at the contract level; gas is the only ceiling. Application-layer chunking is the caller's problem.

The message log is intentionally append-only and unbounded. Pruning, archival, and indexing are off-chain concerns.

### 3.3 NetworkFacet

The wireguard signalling surface, gated on `AttestFacet.isClusterMember(msg.sender)`.

- `publishWgKey(bytes32 wgPubKey)` stores the caller's wireguard public key (replacing any previous value), updates the mirror in AttestFacet, and emits `WgKeyPublished(memberId, wgPubKey)`.
- `wgPubKeyOf(memberId) → bytes32` returns the last-published key. (AttestFacet exposes the same selector as a convenience mirror; the underlying value lives in NetworkStorage.)

Members enumerating the cluster call `AttestFacet.listMembers()` directly — NetworkFacet does not declare its own `listMembers` selector to avoid a diamond-cut selector clash.

NetworkFacet does **not** store endpoint addresses (IP:port). Endpoint info is exchanged through MessageFacet, encrypted to the recipient — exposing endpoint addresses on chain leaks more than necessary about the deployment topology.

---

## 4. Attestor facets

Each attestation method AttestMesh supports is represented by exactly one **attestor facet** on the diamond. A cluster's deployer installs whichever attestor facets they want to admit. A cluster can install zero, one, or many attestor facets:

- **Zero**: nothing can register. Useful for a paused / not-yet-bootstrapped cluster.
- **One**: the common case. A single-method cluster (e.g. dstack-only).
- **Many**: a heterogeneous cluster admitting nodes from multiple attestation methods. All members share the same MessageFacet / NetworkFacet surface regardless of which method admitted them; the only thing attestation-method-specific is the registration path and the allowlist semantics.

Attestor facets are added or removed via the diamond's `diamondCut`, gated on the diamond's solidstate owner (the cluster Safe).

### 4.1 The attestor facet contract

Every attestor facet:

1. **Owns its method's allowlist storage** in its own ERC-7201 namespace. Compose hashes for dstack. MR_TD / RTMR for Intel TDX. Launch-measurement / policy bits for AMD SEV-SNP. NVIDIA CC report measurements. Each method's shape is whatever that method's attestation report exposes.
2. **Exposes attestation-method-specific admin selectors** for the cluster owner to manage its allowlists. For DstackFacet these are the `IAppAuthBasicManagement` set (`addComposeHash`, `addDevice`, `setRequireTcbUpToDate`, …); for future attestor facets they are whatever the method needs.
3. **Exposes a single attestation-method-specific `register` selector** that a node (via its ClusterMember proxy) calls to join the cluster. The selector name is namespaced: `dstack_register(...)`, `tdx_register(...)`, `snp_register(...)`, etc.
4. **Performs three checks** inside that `register`:
   - **Proof validity**: verify the attestation proof per the method's rules (signature chain, freshness, etc.).
   - **Allowlist match**: the proof's measurements must satisfy the facet's allowlist.
   - **Key binding**: the attestation must cryptographically bind to the published `xPubKey` / `wgPubKey` so they cannot be substituted by anyone outside the attested software — this is the general rule for every method. For TEE methods, the binding lives in the quote's user-data slot (`report_data` on TDX/SGX, `user_data` on SEV-SNP, equivalent on others — every supported TEE method exposes at least 64 bytes here) and must equal `keccak256(abi.encodePacked(xPubKey, wgPubKey))` or, where the slot is wide enough, the raw concatenation `xPubKey || wgPubKey`. The exact binding rule is documented in each attestor facet's spec; the goal is the same — the on-chain pubkeys cannot have been substituted by anyone outside the attested software.
5. **On success, writes the member into AttestFacet's shared storage** via the internal `_addMember` selector, stamping `attestorId = <thisMethod>`. Also calls `NetworkFacet._setWgPubKey` so the wg pubkey is registered in the same transaction.
6. **Implements any method-required external interfaces** that off-chain tooling expects. DstackFacet, specifically, implements dstack's `IAppAuth` + `IAppAuthBasicManagement` so the dstack KMS and phala-cli interact with an AttestMesh cluster the same way they interact with a stock dstack app contract. Other attestor facets may have their own equivalent.

The attestor facet's `register` is what makes an AttestMesh cluster heterogeneous-capable: every facet writes into the same MemberStorage shape, every member ends up indistinguishable from the perspective of MessageFacet and NetworkFacet.

### 4.2 DstackFacet (default-shipped)

The only attestor facet that ships in the initial AttestMesh release. Mirrors the existing dstackgres dstack-attestation flow:

- **Allowlists**: `allowedComposeHashes`, `allowedDeviceIds`, `allowAnyDevice`, `requireTcbUpToDate`, `allowedKmsRoots` — all manageable via the `IAppAuthBasicManagement` interface so existing dstack tooling works unchanged.
- **`isAppAllowed(AppBootInfo)`**: implements dstack's `IAppAuth` boot-gate interface. Called by the dstack KMS at CVM boot via the ClusterMember passthrough. Returns `(true, "")` iff the boot info's composeHash and deviceId are in the allowlists and the appId is one of this diamond's passthrough members.
- **`dstack_register(DstackProof proof, address memberContract, bytes32 xPubKey, bytes32 wgPubKey)`**: verifies the dstack KMS 3-level signature chain (KMS root → app key → derived key, all secp256k1), then ecrecovers a binding-message signature from the derived key over `keccak256(abi.encode("attestmesh.bind.v1", clusterAddr, memberAddr, xPubKey, wgPubKey))` — `"attestmesh.bind.v1"` is the EIP-712-style **message domain string**, distinct from the dstack **derive-key purpose** `attestmesh.binding.v1` used by the sidecar to obtain the derived key. The chain proves the derived key was granted to a CVM running an approved compose hash on an approved instance; the binding signature proves that specific derived key controls the supplied pubkeys. The derived key's address becomes the ClusterMember smart wallet's owner (§13 item 18) via a diamond callback in this same transaction; from then on the same key signs every subsequent EIP-4337 UserOpHash. Other attestor facets (e.g. a future IntelTdxFacet) can use raw quote `report_data` commitments instead.
- **`IAppAuth`-flavored boot path** stays intact: a dstack CVM still boots via `DstackKms.registerApp(appId = passthrough)` → `ClusterMember.isAppAllowed(bootInfo)` → `DstackFacet.isAppAllowed(bootInfo)`. This is what makes an AttestMesh cluster a drop-in replacement for a stock dstack app contract.

### 4.3 Future attestor facets

These are not part of v1; they are sketched here so the attestor-facet design can be evaluated against the attestation methods it will eventually need to host.

- **IntelTdxFacet** — Intel TDX direct (no dstack wrapper). Allowlists MR_TD, RTMRs, and policy bits. Verifies TDX quotes against Intel's PCS root (or a pinned signer set). Useful for non-Phala TDX deployments.
- **AmdSnpFacet** — AMD SEV-SNP. Allowlists launch measurement + ID block + policy. Verifies the attestation report against AMD's VCEK chain.
- **NvidiaCcFacet** — NVIDIA confidential GPU. Allowlists the GPU measurement report. Verifies against NVIDIA's attestation service signer.
- **GenericReportDataFacet** — a permissive "if it has a 64-byte user-data slot and you trust the verifier I'm pointed at, admit it" facet. Useful for prototyping an attestation method before writing a dedicated facet for it.

Each is a discrete diamondCut addition; none requires touching the core facets.

---

## 5. Member contracts

Each node is represented by a per-node **ClusterMember** contract that combines two responsibilities on a single address: (1) dstack-style app proxy, so dstack's KMS recognizes the CVM via `IAppAuth` / `IAppAuthBasicManagement` at boot; (2) EIP-4337 v0.7 smart wallet, so the sidecar can submit gasless UserOps via Alchemy's bundler with the webhook-gated paymaster (§13 item 18). Doing both on one address means the dstack `app_id`, the diamond's `msg.sender`, and the gas-webhook's `userOperation.sender` are all the same value — no extra mappings, no per-node "wallet vs identity" split.

ClusterMember is deployed by a singleton per-chain `ClusterMemberFactory` via CREATE2, keyed only by `(factory, impl, salt, clusterAddr)` — not by the owner key. The address is therefore predictable before the CVM boots, which is what lets the operator wire dstack's compose config to point `app_id` at the ClusterMember address before the CVM has derived any TEE-state-dependent keys.

`owner` is set lazily, during the very first `dstack_register` call, via an atomic callback from the diamond into `ClusterMember.__setOwnerFromCluster(bindingKeyAddress)`. Before that, `validateUserOp` runs in a bootstrap mode that recovers the binding signer from the inner `dstack_register` calldata; after, it runs in standard LightAccount-style mode against the stored owner. Contracts spec §9 has the full mechanics.

ClusterMember is UUPS-upgradeable, gated on `IAttest(cluster).clusterOwner()` — this lets per-node ABI evolutions happen as new attestation methods ship, without disturbing the diamond. The dstack KMS / phala-cli ABI surface is preserved across upgrades; the EIP-4337 surface evolves with the EIP itself.

If a cluster supports multiple attestation methods (multiple attestor facets installed), each ClusterMember can specialize for one method — typically by being deployed via a per-method ClusterMember impl that forwards the method's specific external interfaces. The diamond does not care which impl is behind a given member, only that the member's address was minted by the canonical `ClusterMemberFactory` (which the webhook + DstackFacet both verify via `isOurMember`).

---

## 6. Indexer

The cluster contract emits events for every state change: `MemberRegistered`, `WgKeyPublished`, `MessageSent`, allowlist mutations, owner transfers, facet swaps. The node sidecar (§7) needs to react to most of them — most obviously, a member must consume `MessageSent` events addressed to its memberId or it cannot bring up the wireguard mesh.

Requiring every node to maintain its own chain RPC subscription is bad: it scales linearly with cluster size against a paid RPC, websocket subscriptions drop in nodes that migrate or hibernate, and polling wastes work when a cluster is quiet. AttestMesh ships a **shared Indexer** that solves this for every cluster at once, following the same pattern dstackgres established with its monitoring-hub.

### 6.1 Role

The Indexer is an attested off-chain service that:

1. Watches the chain (one RPC subscription, shared across every cluster it serves) for events emitted by any ClusterDiamond it has been asked to follow.
2. For each event, identifies which cluster it belongs to and which members of that cluster have subscribed.
3. Pushes the event to those members — and only those members.
4. Pairs each push with two artefacts that let the member verify the push independently:
   - the Indexer's **attestation signature** over the pushed bytes (the Indexer's signing key is itself covered by its attestation; the cluster knows the Indexer's pubkey from on-chain discovery), and
   - an **RPC repro stub** — the exact `eth_getLogs` / `eth_getTransactionReceipt` call (contract address, block range, topic filter) that, if a member runs it against any RPC provider, returns the same event bytes. The repro stub means the Indexer's claim is independently checkable, not just trust-the-signature.

The Indexer covers many clusters but each push only goes to members of the specific cluster that emitted the event. There is no cross-cluster leak — a member of cluster A is not subscribed to and never receives events from cluster B.

### 6.2 Trust posture

The Indexer's attestation commits to its code. Members trust the Indexer for:

- **Liveness** of event delivery (the Indexer is online and pushing).
- **Completeness** of event delivery within its subscription window (no event is silently dropped).

Members do **not** have to trust the Indexer for:

- **Correctness** of event data (the RPC repro stub lets them verify any push against any RPC provider).
- **Confidentiality** of message contents (`MessageSent` ciphertext is sealed-boxed to the recipient; the Indexer sees the ciphertext but cannot decrypt).

A member sidecar may sample pushes — issuing the repro stub against an independent RPC provider on (say) 1 in N events — without changing its steady-state cost much. The repro stub is generated on every push so this verification path is always available; v1's default sidecar policy is to skip sampling (always trust the Indexer signature), with sampling cadence tunable in milestone B. The Indexer can therefore be operated by a third party with no loss of trust-minimization on data correctness.

### 6.3 Discovery

A member sidecar discovers the Indexer at startup by reading a known **IndexerRegistry** contract — a tiny on-chain registry mapping `chainId → (indexerEndpoint, indexerCodeId, indexerPubKey)`. The registry is owned by the AttestMesh org Safe and exists per chain we deploy on (Base Sepolia for v1, Base mainnet for milestone B).

The node sidecar reads the IndexerRegistry directly via RPC at startup — this is one of the only direct RPC reads the sidecar does. After Indexer subscription is established, all subsequent event ingestion goes through the Indexer.

A cluster may override the default indexer by storing its own indexer reference in a cluster-scoped namespace (a later spec). v1 ships only the default-discovery path.

### 6.4 Subscription protocol

Member sidecar → Indexer over a long-lived bidirectional connection (gRPC bidi streaming, see §13 item 9):

1. Member opens a connection and presents `(memberId, clusterAddress, attestationProof)`.
2. Indexer verifies that `memberId` exists in `clusterAddress`'s AttestFacet `MemberStorage` and that the attestation matches the recorded attestation-bound pubkeys. (The Indexer is essentially re-running the same verification the attestor facet did at registration time — but it can do so as an off-chain read since the cluster diamond is authoritative.)
3. On success, Indexer adds the member to the cluster's subscriber set, records the highest delivered `blockNumber` for that member, and begins streaming events.
4. Each event is delivered as a signed envelope: `{event_data, cluster_addr, block_number, tx_hash, log_index, rpc_repro, indexer_signature, indexer_attestation}` (proto field names — see indexer spec §8.1 and sidecar spec §9.1 for the full message definition).
5. Member verifies the signature against the Indexer's pubkey from IndexerRegistry. On signature mismatch (or attestation mismatch on the Indexer's first push of the session), the member tears down the subscription and re-discovers.

Subscriptions are stateful: the Indexer remembers per-member delivery cursors so a reconnecting member catches up cleanly rather than losing events.

### 6.5 Indexer infrastructure (v1)

For v1 the Indexer ships as a single node image, run by AttestMesh org. Whether milestone B's HA Indexer eats its own dog food (Indexer replicas as members of an AttestMesh cluster, coordinating cursor leadership via MessageFacet) or runs as a standalone primitive is deferred — both are open; dog-fooding is the preferred direction but neither shape is committed. v1's single-instance Indexer is built to be portable into either model.

---

## 7. Node sidecar

A Rust binary (target: `cluster-mesh-agent`) shipped as an OCI image and included in node `docker-compose` files. Runs as a sidecar with elevated privileges (needs to configure wireguard) and a healthcheck that the application's main container can depend on.

### 7.1 Boot sequence

1. **Discover cluster address.** The sidecar reads its own member contract address from a `MEMBER_CONTRACT` env var or a file mounted from the dstack runtime. It calls `member.cluster()` to get the ClusterDiamond address.
2. **Derive identity keys.** Using the attestation-bound seed, produce a single Curve25519 root from a known purpose string (e.g. `attestmesh.identity.v1`), then derive:
   - an **x25519 keypair** for sealed-box decryption of MessageFacet payloads (the public half is what the diamond stores in MemberRecord and what other members encrypt sealed-box payloads to), and
   - an **Ed25519 keypair** for signing off-chain heartbeats. The Ed25519 *public* key is **not** stored on chain — it is exchanged peer-to-peer as part of the `PeerEndpoint` envelope (§7.1 step 7); a member learns each peer's Ed25519 pubkey at the same time it learns the peer's wireguard endpoint.
   Same underlying curve, two operations; on dstack this comes out of `derive_key` with a deterministic purpose string. A separate derivation produces the wireguard keypair (`attestmesh.wireguard.v1`).
3. **Construct the registration proof.** Attestation-method-specific. On dstack: ask the dstack runtime for a `derive_key("attestmesh.binding.v1", k256)` derived secp256k1 key, then sign a binding message that commits to `("attestmesh.bind.v1", clusterAddr, memberAddr, xPubKey, wgPubKey)` with that derived key (the "attestmesh.bind.v1" string is the EIP-712-style message-domain tag, distinct from the derive-key purpose `attestmesh.binding.v1`). Bundle the dstack KMS sig chain (KMS root → app key → derived key) plus the binding signature into a `DstackProof`. On future methods that expose raw attestation quotes, instead request a quote whose user-data slot commits to `keccak256(xPubKey || wgPubKey)` and bundle that with the method's verifier chain. The attested environment will only sign / emit either shape if it actually controls those pubkeys, so the binding is forgery-resistant either way.
4. **Register (or recognize that we already have).** First, read `AttestFacet.memberOf(memberContractAddress)`:
   - If a record exists and its `xPubKey` and `wgPubKey` match the derived ones: this is a CVM restart with persisted TEE state, *not* a first-time join. The CSK is in the sealed store (§8.5); unseal it. Skip directly to step 5. No transaction needed.
   - If a record exists but the keys do *not* match: the TEE state has been lost and the sidecar derived different keys. Fail closed — log loudly and exit with a non-zero code. Ops needs to either restore the TEE state or replace the ClusterMember (different address, different member entry). v1 does not attempt automated recovery.
   - If no record exists: call the appropriate attestor-facet selector — `dstack_register(proof, memberContract, xPubKey, wgPubKey)` on a dstack CVM, `tdx_register(...)` on a future Intel TDX direct CVM, etc. The node only knows its own attestation method; it does not need to enumerate the diamond's installed facets. On failure (verifier reverts, allowlist mismatch), retry with exponential backoff capped at 60 seconds for up to 10 attempts. After 10 consecutive failures, fail closed (exit non-zero) — this is almost always an allowlist / configuration mismatch that an operator must resolve, and continuing to spin is worse than visibly stopping. Until registration succeeds, the sidecar never reports healthy.
4a. **Determine CSK role.** Immediately after registration confirms, read `AttestFacet.memberCount()`. If `1`, this member is the CSK **originator** — derive and seal the CSK per §8.1. If `>1`, this member is an **onboardee** and will receive the CSK via an Indexer-pushed envelope during step 6/7; mark "awaiting CSK" as one of the gates blocking step 11.
5. **Publish wireguard key.** Call `NetworkFacet.publishWgKey(wgPubKey)` through the member proxy. (In v1 we can fold this into `register` since registration already binds the wg pubkey; we keep the separate selector for rotation and resilience.)
6. **Subscribe to the Indexer.** Read the IndexerRegistry contract for this chain to discover the Indexer endpoint and pubkey. Open a subscription (§6.4), presenting `(memberId, clusterAddress, attestationProof)`. From this point on, every `MessageSent`, `MemberRegistered`, `WgKeyPublished`, or other cluster event the sidecar cares about arrives as a signed Indexer push — the sidecar does not poll the chain directly for events.
7. **Wait for peer endpoints and (if onboardee) CSK envelope.** Each Indexer push of a `MessageSent` event on the member's channel is decrypted (sealed-box, x25519). Payloads are demultiplexed by envelope id and inner `kind` field:
   - `keccak256("attestmesh.peer-endpoint.v1")` → parse as `PeerEndpoint{ memberId, host, port, wgPubKey, ed25519PubKey }` from an existing member. The sidecar computes the peer's mesh IP from the cluster CIDR and the peer's `memberId` (§7.3), configures the wireguard interface with the peer (assigning that IP, AllowedIPs to that /32, endpoint = `host:port`), and stores the peer's `ed25519PubKey` for heartbeat verification.
   - `keccak256("attestmesh.csk.onboarding.v1")` (onboardees only) → consume per §8.3 and mark "CSK acquired."
   - Anything else → log at debug and ignore; the application layer never sees these.

   The trust chain on Ed25519 pubkey (and other claimed-sender fields) is: Indexer-signed `MessageSent` event names the sender, payload claims "I am sender X here are my keys," sidecar checks the claim's `memberId` matches the signed sender.
8. **Send own endpoint.** For every other member the sidecar learns about (via `AttestFacet.listMembers()` plus inbound endpoint messages), encrypt a `PeerEndpoint` of self (including own Ed25519 pubkey) to the peer's x25519 pubkey via sealed-box and send it via MessageFacet.
9. **Heartbeat.** For every wireguard peer, run a lightweight heartbeat — a periodic UDP packet over wireguard carrying (a) sender memberId, (b) timestamp, (c) the sender's view of which peers it currently considers connected. Heartbeats are signed with the Ed25519 key so they are not spoofable on the wire. Receivers verify against the peer's `ed25519PubKey` learned in step 7. A heartbeat from a peer whose Ed25519 has not yet been learned (`PeerEndpoint` not yet arrived) is buffered briefly and discarded if the corresponding `PeerEndpoint` never lands.
10. **First-convergence gate.** The sidecar maintains a local view:
    - A node is **live** iff at least one other node's heartbeat reports it as connected.
    - The mesh is **converged** iff every live node reports the same connected-set, and that set equals the live set.
    - Until first convergence is observed, the sidecar's healthcheck returns 503 and the application container does not start.
    - **The gate fires once.** Once a sidecar observes first convergence, it never re-gates on convergence again — subsequent member joins/departures may temporarily break the cluster-wide property, but a node that has already crossed the gate stays healthy.
11. **Become healthy; steady-state mesh maintenance.** Once *both* first-convergence is observed *and* the CSK is acquired (originator: derived in step 4a; onboardee: received and sealed in steps 6/7 per §8.3), healthy is reported. From here on the sidecar:
    - Continues heartbeating its current peers.
    - On Indexer push of `MemberRegistered` for a new member, sends a `PeerEndpoint` to them and adds them as a wireguard peer once their own `PeerEndpoint` arrives — but does *not* re-evaluate convergence or change its healthcheck while the new node integrates. *Additionally*, after a uniform `[0, 500]` ms backoff and a dedup check, the existing member may onboard the new node with the CSK per §8.2.
    - On peer connection loss, retries connection and continues heartbeating; the application's own logic decides whether to degrade based on the heartbeat liveness report.
    - The Indexer subscription stays open for the life of the process; if it drops, the sidecar reconnects and resumes from its last-delivered cursor.

### 7.2 Failure modes

- **Pattern revoked mid-flight.** If the cluster owner removes the attestation pattern between step 4 and the application coming up, no member already registered is forcibly removed (no on-chain eviction in v1); but new joiners cannot register, and a future re-register attempt (e.g. after a CVM restart) will fail. v1 punts cluster-driven eviction to a later spec.
- **Indexer down.** If the Indexer is unreachable at boot, the sidecar stays in step 6 and reports unhealthy. The application container does not start. There is no v1 fallback to direct chain polling — operationally, the Indexer's HA shape is what guarantees liveness.
- **Indexer signature/attestation mismatch.** Treated as adversarial: the sidecar tears down the subscription, re-reads IndexerRegistry, and retries. If the pubkey on chain has been rotated (legitimate operator action), the new subscription succeeds. If not, the sidecar fails closed and stays unhealthy.
- **Message channel poisoned.** A malicious member could spam another member's channel with garbage. Decryption failures are silently dropped; the sidecar logs at debug only. Rate-limiting is not enforced on chain in v1.
- **First-convergence deadlock.** If the network is partitioned at startup such that no convergence is possible, a *joining* sidecar stays unhealthy indefinitely. This is intentional — degraded boot of an unmeshed mesh is worse than visible failure. Already-healthy sidecars elsewhere in the cluster are unaffected; the gate fires once per process.
- **CSK acquisition deadlock.** An onboardee that never receives a `csk-onboarding-v1` envelope stays unhealthy indefinitely. Same surface as first-convergence deadlock — the application container does not start. Recovery is operational (verify the Indexer is delivering events to that member; verify at least one existing member sees the new `MemberRegistered` and is actually sending). The originator-lost case (§8.6) is permanent.

### 7.3 Mesh IP allocation

Each cluster has a CIDR (configured by the `(meshCidrIp, meshCidrPrefix)` pair in `DiamondInit.InitArgs`; default `10.13.0.0/16` for v1). Every member's mesh IPv4 is derived deterministically from its `memberId` and the cluster CIDR:

```
ip = cidr.network_address() | ((uint32(keccak256(memberId)) mod (cidr.host_count() - 2)) + 1)
```

Any sidecar can compute any peer's mesh IP from the on-chain MemberRecord alone — no off-chain coordination, no on-chain IP storage. The `.0` (network) and final-host (broadcast on a /24, equivalent on larger CIDRs) addresses are excluded by the `+ 1` and `- 2`.

For a /16, collision probability between any two members is `2^-16 ≈ 1.5e-5` per pair, which is comfortably below any cluster size AttestMesh expects to host. A collision would manifest as two members claiming the same IP and wireguard refusing to bring up the second peer; if it ever happens, mitigation is either (a) redeploying the affected member behind a different ClusterMember address (different memberId, different derived IP) or (b) widening the CIDR in a future cluster. v1 does not include on-chain disambiguation.

Different clusters can use overlapping CIDRs because each cluster's mesh is a separate wireguard interface. The CIDR is per-cluster, not per-chain.

---

## 8. Cluster Shared Key

The **Cluster Shared Key (CSK)** is a 32-byte AES-256 key that every member of an AttestMesh cluster holds. The cluster contract never sees it; the Indexer never sees it. AttestMesh treats its plaintext as opaque — the application layer decides what to use it for (typically: encrypting cluster-wide shared state, deriving sub-keys for specific app concerns, sealing artifacts at rest).

This is the one AttestMesh primitive that lets the application bootstrap symmetric-key crypto across the cluster without rolling its own key-exchange protocol.

### 8.1 Origination

The first member to register derives the CSK locally via dstack's key derivation:

```
csk = dstack.derive_key("attestmesh.cluster-shared.v1", "csk-v1")
```

Deterministic for *that specific* member's TEE state — across the originator's own restarts, the same call gives the same key. It is **not** deterministic across different CVMs: dstack keys by app_id, and every ClusterMember has a different address. Only the originator can derive the CSK; everyone else gets it via onboarding (§8.2).

The originator detects its role by reading `AttestFacet.memberCount()` immediately after its `dstack_register` tx confirms. If `memberCount == 1`, it is the originator. The check is racey across simultaneous registrations, but the chain serializes — exactly one tx confirms first; any node whose tx confirms second sees `memberCount >= 2` and recognizes itself as an onboardee.

The originator seals the CSK to dstack's local sealed store after deriving it. The originator could skip sealing and rely on re-derivation across its own restarts, but matching the onboardee storage path keeps the sidecar state machine uniform.

### 8.2 Distribution

When any existing cluster member sees `MemberRegistered` for a new member (via Indexer push), it may onboard them:

1. **Randomized backoff**, uniform on `[0, 500]` ms. Spreads the moment-of-decision across existing members so they do not all race to onboard simultaneously and burn redundant gas. Backoff is hard-capped at 500 ms so onboardees do not wait noticeably.
2. **Check for prior onboarding** by looking at the recipient's MessageFacet channel for any prior envelope with `envelopeId == keccak256("attestmesh.csk.onboarding.v1")` — a canonical, well-known envelope id used only for CSK onboarding. If one already exists, no-op. Someone else got there first.
3. **Send**, otherwise. Construct `payload = { kind: "csk-onboarding-v1", csk: <32 bytes>, originatorMemberId: <originator's memberId> }`, sealed-box-encrypt with the new member's `xPubKey` from AttestFacet, call `MessageFacet.send(newMemberId, envelopeId=keccak256("attestmesh.csk.onboarding.v1"), ciphertext)`.

The well-known `envelopeId` is what enables the step-2 dedup via MessageFacet's built-in duplicate-envelope check — a racing second sender simply reverts with `DuplicateEnvelope`, costing them one tx but doing no protocol damage.

### 8.3 Onboardee receipt

A new member that recognizes itself as an onboardee (post-register `memberCount > 1`) does the following in parallel with mesh bring-up:

1. Subscribe to the Indexer (already required for peer-endpoint exchange — §7.1 step 6).
2. Listen for a `MessageSent` event on its own channel with `envelopeId == keccak256("attestmesh.csk.onboarding.v1")`.
3. Decrypt with its x25519 private key (sealed-box open). Validate `payload.kind == "csk-onboarding-v1"`.
4. Verify the sender is a cluster member (implicit in MessageFacet's send gate, but the sidecar may double-check the sender's `memberId` from the Indexer-signed envelope).
5. Seal the CSK to its own dstack store.
6. Mark "CSK acquired" — one of the gates before reporting healthy (§7.1 step 11).

### 8.4 Sidecar app exposure

The sidecar's app-facing gRPC (§13 item 13) adds one method:

```
rpc GetClusterSharedKey(Empty) returns (Bytes32);
```

Available only after CSK acquisition; before then, the call returns `Unavailable`.

### 8.5 Storage in the node

- **In memory** in the sidecar process, zeroed on process exit.
- **At rest** sealed via `dstack.seal("attestmesh.csk.v1", csk_bytes)`. Unsealed at boot.
- **Never** on disk in plaintext, never in a shared tmpfs volume, never exposed to the application except through the sidecar gRPC.

### 8.6 Failure modes

- **Originator dies before onboarding any other member.** The CSK is lost forever (only the originator's TEE could re-derive it). The cluster cannot bootstrap. This is accepted: a cluster that never starts up is never used. v1 and milestone B both ship this behavior. Out-of-scope mitigations (key escrow, quorum recovery) are not on the roadmap.
- **All members die simultaneously.** Same outcome — CSK is lost, accepted.
- **Onboarding race burns gas.** Two existing members both pass the dedup check inside the same racy window and both send. The first send confirms; the second reverts with `DuplicateEnvelope`. Cost: one wasted tx. No protocol damage.
- **Onboardee is partitioned from the Indexer.** Onboardee blocks at the "CSK acquired" gate. Same failure surface as first-convergence deadlock; healthcheck stays at 503.

---

## 9. Trust model

The diamond's cluster owner (a Safe in production) controls:
- Which attestor facets are installed (via diamondCut).
- Each attestor facet's allowlists (compose hashes, MR_TDs, launch measurements, etc.).
- Diamond-level admin (DiamondCut, SafeOwnable transfer).
- Member factory address.

The cluster owner can **not** decrypt messages, derive members' wireguard keys, or impersonate a member. All attestation-bound secrets stay inside the attested environments. The chain only ever sees public commitments (pubkeys, ciphertexts, addresses).

A compromised cluster owner can install a malicious attestor facet or relax an existing facet's allowlists to admit malicious nodes as members. That attacker would then be able to read messages addressed to themselves. They would **not** be able to read messages addressed to honest members, since each member's inbox is sealed-boxed to that member's attestation-bound x25519 pubkey.

A compromised attestation-method provider (e.g. a malicious dstack KMS root, a leaked Intel PCS signer key) is out of scope: each method's security model is taken as a given by that method's facet. Mitigations exist method-side (root rotation, multi-root patterns) and can be reflected here by updating the facet's allowlists or pinned signer set.

**Registration binding signature plus EIP-4337 UserOp authentication share one node-derived secp256k1 key.** On dstack, the binding signature is a single secp256k1 signature from a key derived via the dstack KMS at registration time. The *same* derived key's address is then installed as the ClusterMember smart wallet's `owner` (atomically, via a diamond callback inside `dstack_register` — see §13 item 18). Every subsequent EIP-4337 UserOp from that node is signed by the same key and validated on chain by `ClusterMember.validateUserOp` against the stored owner. The node's other operational keys are all Curve25519 (x25519 for sealed-box, Ed25519 for heartbeat signatures, plus its wireguard keypair). On future methods that expose raw attestation quotes, the *attestation* binding mechanism is thinner — the pubkeys live in the quote's user-data slot, no per-node ECDSA needed for the registration proof — but those methods still need a per-node signing key for EIP-4337 UserOps (or, equivalently, an alternative paymaster integration that doesn't require k256 sigs). The only ECDSA key the node holds is the one bound by attestation at registration; there is no operator-provisioned key.

---

## 10. Differences from dstackgres

dstackgres is the codebase AttestMesh is being extracted from. The differences:

- **No Postgres anything.** dstackgres's `CoreFacet` mixes membership with `endpoint` strings, DNS labels, leader leases, signer authorization — all Postgres-cluster-specific. AttestMesh's AttestFacet keeps only the membership-registry shape.
- **No control-plane facet.** dstackgres has a `ControlPlaneFacet` for off-chain action authorization. AttestMesh omits it; if an application needs it, they can add a facet via diamondCut.
- **No leader lease.** Leader election is application-level; AttestMesh does not assume one is needed.
- **Verifier as facet, not external contract.** dstackgres has a separate `DstackVerifier` UUPS contract registered with `TEEBridge` via an adapter registry. AttestMesh folds that role directly into the attestor facet: there is no separate verifier contract, no adapter registry, no `IVerifier` interface needed cross-method. Each method's verification logic is library code linked into its facet.
- **MessageFacet is new.** dstackgres has nothing equivalent — endpoint exchange there happens via `signalEndpoint(bytes ciphertext, ...)` on `WgMeshFacet`. AttestMesh splits this cleanly: messaging is one facet, networking is another.
- **Wireguard signalling is decoupled from endpoint registry.** dstackgres stores endpoint blobs on chain; AttestMesh stores only wg public keys on chain and pushes endpoint info through MessageFacet so deployment topology is not leaked.
- **Curve25519 for the node's operational keys.** dstackgres derives a per-CVM secp256k1 key and uses it for cluster-side operations via the dstackgres control plane. AttestMesh derives x25519 + Ed25519 + wireguard from Curve25519 for the operational surface; the per-CVM secp256k1 key from the dstack KMS is reused only for (a) the one-shot registration binding signature and (b) signing EIP-4337 UserOpHashes for sponsored paymaster submission. No long-lived per-CVM ECDSA key exists beyond what attestation produces.

---

## 11. v1 scope

v1 is a **working three-node demo on Base Sepolia** (milestone "A"). The goal is to prove out the protocol end-to-end with the smallest possible surface — production hardening, second attestor facets, and security review are deferred to milestone "B".

**In v1:**

- DstackFacet only (no second attestation method).
- A single ClusterDiamond deployed on Base Sepolia.
- Three dstack CVMs registering, publishing wg pubkeys, exchanging endpoints via MessageFacet, and converging the wireguard mesh.
- A single-instance Indexer (one node, no HA) watching that one cluster and pushing events to the three members.
- IndexerRegistry contract deployed on Sepolia with the v1 Indexer's endpoint and pubkey.
- One end-to-end test sealed-box message exchanged between two members.
- Liveness gate working: the application container does not start until the mesh is converged.

**Deferred to milestone B (production-shaped single-method release):**

- Deployment to Base mainnet behind a Safe.
- HA Indexer (multiple replicas, load balancer, monitoring).
- Production sidecar packaging (signed OCI images, dstack runtime integration).
- Formal threat model review.
- Member-side sampling cadence policy (v1 default: always trust the Indexer signature; the repro stub is always generated so sampling is *available*, just not exercised by default).
- Indexer-side attestation cache TTL (v1 re-verifies on every reconnect; B introduces a cache).
- Member factory shape settles in milestone B if per-method impls force a different shape; v1 ships a singleton per-chain `ClusterMemberFactory` (contracts spec §9.2) shared across every cluster on the chain.
- Reorg handling policy (v1 treats Sepolia confirmations as final; B picks a finality depth).

**Deferred indefinitely (not on the B path):**

- On-chain eviction of misbehaving members.
- Cluster-to-cluster federation.
- Non-EVM target chains.
- Attestor facets other than DstackFacet (milestone C territory).
- A reference application on top of AttestMesh.

---

## 12. Open questions

No remaining open questions block v1. (Component-level specs may surface new ones as they get written; those will be tracked in their respective specs, not here.)

---

## 13. Resolved design decisions

(Decisions called during the spec's drafting that may otherwise look load-bearing without context.)

1. **Heartbeat transport: UDP-over-wireguard, gossip-computed convergence** (not on chain via MessageFacet). Cheap, fast, no per-heartbeat gas. Off-chain observers wanting "is the mesh healthy" must consume from a member.
2. **Curve25519 for the node's operational key path** (sealed-box on x25519 for messaging, Ed25519 for heartbeat signatures). The per-CVM secp256k1 key the dstack KMS derives is used both for the registration binding signature AND, after the diamond callback installs it as the ClusterMember's owner, for signing every subsequent EIP-4337 UserOpHash (item 18). One key, two recoverable signing surfaces. Future methods that expose raw attestation quotes can use the user-data slot for the *attestation* binding, but still need a signing key for paymaster-sponsored UserOps (unless an alternative gas-sponsorship integration removes that requirement).
3. **Attestation-method support = installed facet.** Each attestation method is a facet on the diamond. Clusters install whichever attestor facets they want to admit; the core facets (Attest / Message / Network) are attestation-method-agnostic and never need to change as new attestation methods ship.
4. **Target chain: Base** (Sepolia for v1, mainnet for milestone B and beyond). Same EVM family as dstackgres. Chain-agnosticism is a milestone-C+ concern; v1 deployment scripts, the IndexerRegistry instance, and the org Safe-owned addresses are all Base-specific.
5. **Event delivery via a shared attested Indexer**, not direct chain polling from each node. Members trust the Indexer for liveness and completeness only; each push carries an RPC repro stub so correctness is independently verifiable per event. Follows the dstackgres monitoring-hub pattern.
6. **Monorepo.** Contracts, node sidecar, and Indexer service all live in `AttestMesh/AttestMesh`. The protocol and its reference implementations evolve together; the spec in this repo is authoritative for the deployed Indexer it ships alongside.
7. **No dstackgres compatibility, Postgres deferred.** AttestMesh is the generic mesh primitive. The existing TeeSQL Postgres-as-a-Service product is on hold and the existing dstackgres deployments on Base mainnet are not migration targets. dstackgres is referenced in §10 strictly as the *extraction source* — useful for understanding which moving parts were ripped out and why — not as a system we owe ABI compatibility to. A future Postgres application on top of AttestMesh is plausible but explicitly out of scope for v1.
8. **Atomic constructor bootstrapping.** ClusterDiamond's constructor takes `(facetCuts, initContract, initCalldata)` and delegatecalls the init contract into its own storage on construction, seeding the dstack KMS root allowlist, the initial compose-hash / device-id allowlists, the cluster owner Safe, and any other per-attestor-facet config in one transaction. The diamond is never reachable in a "deployed but unconfigured" state. Same pattern dstackgres's `DiamondInit` uses; the constructor-arg encoding burden is real but worth it for atomicity.
9. **Indexer push transport: gRPC bidirectional streaming over HTTP/2 (via `tonic`).** One `.proto` for the subscription envelope generates code on both ends; eliminates schema drift between sidecar and Indexer. HTTP/2 plays well with load balancers when the Indexer goes HA in milestone B. OpenTelemetry instrumentation is first-class.
10. **Heartbeat defaults (v1).** 2-second interval, 3-miss threshold (a peer is considered down after 6 seconds of silence). Convergence calc tolerates a single missed heartbeat without breaking the converged signal — only a full miss-threshold flips a peer to down. These are tunable in milestone B; v1 picks defaults and we adjust during the demo build-out.
11. **First-convergence gate fires once per sidecar process, not continuously.** Bringing a node up gates its application container behind the first cluster-wide convergence it observes. Subsequent member joins or peer drops may temporarily break cluster-wide convergence; already-healthy sidecars do not re-gate or report unhealthy. Joining nodes still integrate (new peer is added to wireguard, heartbeats start), they just don't push existing nodes back through the healthcheck.
12. **CVM restart is sidecar-detected, not a contract concern.** Before calling `dstack_register` (or any other attestor-facet register selector), the sidecar checks `AttestFacet.memberOf(memberAddr)` and skips the registration tx if a matching record already exists. Pubkey mismatch on the existing record means lost TEE state — sidecar fails closed in v1; future eviction / rotation specs cover automated recovery. Contracts treat re-registration as an error (`AlreadyRegistered`) — the sidecar is responsible for not getting there.
13. **Full sidecar-as-app-facade.** The sidecar exposes a gRPC API to the application container running alongside it (over a unix domain socket in the same node). Surface includes mesh status, peer listing, message send, decrypted message subscription, peer-event subscription, and `GetClusterSharedKey`. The app never holds AttestMesh keys, never sees ciphertexts, and never talks to the chain or the Indexer directly. **Decryption-filtered:** the sidecar receives every `MessageSent` event from the Indexer but only forwards to the application those whose payloads decrypt successfully against the member's x25519 private key. Failed decryptions (not addressed to us, malformed, key mismatch) are silently dropped — the app's message stream contains only verified, decrypted, addressed-to-it traffic. Sidecar-internal coordination messages (`PeerEndpoint`, `csk-onboarding-v1`) are consumed before the app sees them.
14. **Cluster Shared Key (CSK) primitive.** A single 32-byte symmetric key, derived deterministically by the first member to register via `dstack.derive_key("attestmesh.cluster-shared.v1", "csk-v1")`, distributed to subsequent members via sealed-box-encrypted `MessageFacet.send` with a canonical well-known `envelopeId == keccak256("attestmesh.csk.onboarding.v1")`. Senders apply a uniform `[0, 500]` ms randomized backoff and dedup against the canonical envelopeId so only one onboarding tx wins; racers revert with `DuplicateEnvelope`. The CSK is sealed to dstack's per-CVM sealed store at rest, held in sidecar memory at runtime, exposed to the application via `GetClusterSharedKey` gRPC. Never on chain in plaintext; never visible to the diamond, Indexer, or application container outside the sidecar gRPC. Originator-permanent-loss before first onboarding bricks the cluster — accepted as a non-issue ("a cluster that never starts up is never used"). See §8 for the full mechanism.
15. **Ed25519 pubkeys are exchanged off-chain in `PeerEndpoint` envelopes, not stored in MemberStorage.** Heartbeats are signed with the sender's Ed25519 key; receivers learn each peer's Ed25519 pubkey at the same moment they learn the peer's wireguard endpoint (both ride together in the sealed-box `PeerEndpoint` payload). The trust chain on the Ed25519 pubkey holds via the Indexer-signed `senderMemberId` on the carrying `MessageSent` event plus the payload's self-claim of identity. Keeps MemberStorage at two pubkeys (x25519 + wg) and the on-chain registration binding at 64 bytes; the cost is that non-members cannot verify heartbeats they happen to capture, which doesn't matter in practice since heartbeats are UDP-over-wireguard.
16. **Mesh IP allocation: deterministic from `memberId` against a per-cluster CIDR.** Each cluster's `DiamondInit.InitArgs` carries `(meshCidrIp, meshCidrPrefix)` (default `10.13.0.0/16`). Every member's wireguard IP is `cidr.network() | (keccak256(memberId) mod (cidr.host_count() - 2)) + 1` — any sidecar can compute any peer's IP from the on-chain memberId. No off-chain coordination, no on-chain IP storage. Collision probability is ~1.5e-5 per pair for a /16; mitigation if it ever bites is to redeploy the affected member behind a different ClusterMember address. See §7.3.
17. **Fused two-owner rotation helper.** The diamond has two owners by design — solidstate's owner (DiamondCut authority) and `MemberStorage.clusterOwner` (allowlist authority) — which can diverge if an operator wants distinct council vs ops Safes. For the common case where one Safe holds both roles and needs to rotate to a new Safe, AttestFacet ships `transferBothOwners(newOwner)` / `acceptBothOwners()` that propose + atomically apply both transitions in two transactions. Independent `transferClusterOwnership` / `acceptClusterOwnership` remain for the diverging case. Inconsistency window for the typical case collapses to zero.
18. **Gas: EIP-4337 + Alchemy paymaster, gated by an AttestMesh org Cloudflare Worker.** Nodes hold zero ETH. The sidecar submits every state-mutating call as an EIP-4337 v0.7 UserOperation against an Alchemy bundler endpoint. Alchemy's paymaster service POSTs each UserOp to an AttestMesh org Cloudflare Worker for policy validation (chain id, outer selector is `ClusterMember.execute`, inner selector is in an allowlist of cluster operations, value is 0, sender is a known ClusterMember per `ClusterMemberFactory.isOurMember`, target is a known cluster per `ClusterDiamondFactory.isDeployedCluster`); on approve, the paymaster signs the sponsorship fields and Alchemy bills the AttestMesh org account for the gas. To make this work, ClusterMember is an EIP-4337 smart wallet (implements `IAccount` + `IAppAuth` + `IAppAuthBasicManagement` on the same address) and the dstack-derived `attestmesh.binding.v1` k256 key is set as its owner during `dstack_register` via a diamond callback. Ported and narrowed from dstackgres's existing `gas-sponsorship-webhook` Cloudflare Worker — see `docs/specs/gas-webhook.md` and contracts spec §9.1 (ClusterMember as smart wallet) + §10 (ClusterDiamondFactory provenance for the webhook check).

---

## 14. References

- [dstack — IAppAuth / IAppAuthBasicManagement](https://github.com/Dstack-TEE/dstack/blob/master/kms/auth-eth/contracts/)
- [dstackgres contracts/teesql-group-auth](https://github.com/TeeSQL/dstackgres/tree/main/contracts/teesql-group-auth) — extraction source
- [EIP-2535 Diamonds](https://eips.ethereum.org/EIPS/eip-2535)
- [solidstate-solidity diamond base](https://github.com/solidstate-network/solidstate-solidity)
- [libsodium sealed-box](https://doc.libsodium.org/public-key_cryptography/sealed_boxes)
- [wireguard whitepaper](https://www.wireguard.com/papers/wireguard.pdf)
