# TeeMesh — Coordination Layer Master Spec

**Status**: Draft v0.1
**Authors**: LSDan (with Claude)
**Last updated**: 2026-05-30

---

## 1. Purpose

TeeMesh is the on-chain coordination layer for clusters of TEE-based confidential VMs (CVMs). It is the part of [dstackgres](https://github.com/TeeSQL/dstackgres) that has nothing to do with Postgres, extracted, generalized, and stripped of every Postgres- and dstack-specific assumption that does not belong in a generic mesh.

The goal: any application that wants to run a mesh of mutually-attested CVMs — Postgres, Redis, a Tendermint-style consensus net, a private inference cluster, anything — can deploy a TeeMesh cluster contract, drop the TeeMesh CVM compose package into their image, and get:

1. Provable membership: every peer is a CVM running an approved image on approved hardware, anchored to a TEE attestation chain that the cluster contract verifies.
2. Encrypted on-chain messaging: members can send each other arbitrary payloads through the chain, encrypted to TEE-derived keys, with no off-chain messaging broker required.
3. Self-bootstrapping wireguard mesh: each CVM derives its wireguard keys from its TEE, publishes them through the cluster contract, exchanges connection info over the encrypted message channel, and brings up a full mesh — without any application-level orchestrator.
4. Liveness consensus: every node heartbeats every peer; the CVM only proceeds past the mesh-bring-up gate once it has confirmed connections to every node the network agrees is live.

What this spec deliberately does **not** include:
- The application that runs inside the CVM (Postgres, etc.).
- Higher-level cluster semantics like leader election, sharding, or quorum protocols — those belong on top of TeeMesh, in the application layer.
- A control plane for cluster *operators* — administrative actions like adding attestation patterns are on-chain calls gated on the cluster owner; this spec does not define a dashboard, CLI, or SaaS service.

---

## 2. Architecture overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                            EVM L2                                    │
│                                                                      │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │           ClusterDiamond (ERC-2535 proxy)                    │  │
│   │                                                              │  │
│   │   ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌──────────────┐ │  │
│   │   │ Dstack   │ │ Attest   │ │  Message   │ │   Network    │ │  │
│   │   │ Facet    │ │ Facet    │ │  Facet     │ │   Facet      │ │  │
│   │   └──────────┘ └──────────┘ └────────────┘ └──────────────┘ │  │
│   │                                                              │  │
│   └──────────────────────────────────────────────────────────────┘  │
│                              ▲                                       │
│                              │ passthrough                           │
│                              │                                       │
│   ┌──────────────────────────┴──────────────────────────┐           │
│   │   ClusterMember proxies (one per CVM)               │           │
│   └──────────────────────────────────────────────────────┘           │
│                              ▲                                       │
└──────────────────────────────┼───────────────────────────────────────┘
                               │ register() / message / wg pubkey
                               │
                  ┌────────────┴────────────┐
                  │   CVM compose package    │
                  │   (Rust, runs as a       │
                  │    sidecar in each       │
                  │    confidential VM)      │
                  └─────────────────────────┘
```

The diamond is platform-agnostic: it knows about cluster membership, attestation policies, encrypted messages, and wireguard keys. It does **not** know about Postgres, dstack-specific KMS internals, or any particular TEE vendor's quote format outside of the optional DstackFacet.

Every CVM that participates in the cluster is represented on chain by a **ClusterMember** passthrough contract. The CVM's TEE attestation chain commits to the member contract's address (analogous to dstack's `app_id`). The member contract forwards a small set of calls into the diamond's facets, so the diamond can run cluster-wide logic while individual CVMs only ever interact with their own member address.

---

## 3. Diamond facets

The ClusterDiamond is constructed with a default facet cut consisting of the four facets below, plus the standard solidstate base (DiamondCut, loupe, ERC-165, SafeOwnable). Future facets may be added via diamondCut by the cluster owner; the spec does not constrain them.

### 3.1 DstackFacet

Implements the dstack `IAppAuthBasicManagement` interface (mirrored from `dstack/kms/auth-eth/contracts/IAppAuthBasicManagement.sol`). This is the admin surface for dstack-style compose-hash and device-id allowlists, plus the `requireTcbUpToDate` flag and the `allowAnyDevice` escape hatch.

This facet exists so that TeeMesh clusters can be driven by the same dstack tooling (phala-cli, the Phala dashboard, the dstack KMS) that already exists. New facets analogous to DstackFacet — IntelTdxFacet, AmdSnpFacet, NvidiaCcFacet, etc. — can be added later without disturbing the rest of the diamond.

**Key constraint**: every dstack-specific assumption (`AppBootInfo` field semantics, the `IAppAuth.isAppAllowed` signature, the dstack KMS signature chain shape) lives in this facet and the matching DstackMember passthrough impl. Nothing else in the diamond imports them.

### 3.2 AttestFacet

The platform-agnostic side of attestation. Holds:

- **Attestation patterns**: a set of admin-whitelisted patterns that an incoming attestation quote must match for the CVM to be admitted as a member. A "pattern" here is intentionally abstract — at minimum it is a tuple of (verifier address, code identifier, optional extra constraints). Each pattern is keyed by a `bytes32` patternId. The cluster owner can add or remove patterns.
- **Verifier registry**: addresses of contracts implementing the platform-specific quote-verification logic (e.g. a `DstackVerifier`, an Intel TDX direct verifier). The AttestFacet does not know how any verifier works internally; it only knows the `IVerifier` interface.
- **Member registry**: an indexed map of `memberId → MemberRecord`, where each record carries (a) the patternId the member was admitted under, (b) the member's TEE-derived public key (used by MessageFacet), and (c) any application-relevant metadata supplied at registration time.

`register(AttestArgs args)` is the entry point. It:

1. Looks up the verifier and pattern referenced by `args.patternId`.
2. Calls `verifier.verify(args.proof, args.boundData)`; the verifier returns either a code identifier extracted from the attestation (used as the CVM's passthrough address) or a revert.
3. Confirms the supplied passthrough address matches `args.member` and was issued by this diamond's member factory.
4. Records the member, indexed by the keccak256 of (cluster, member, patternId).
5. Emits `MemberRegistered(memberId, member, patternId, pubkey)`.

A member that fails registration is simply absent from the registry; the CVM can retry. There is no on-chain failure record.

### 3.3 MessageFacet

A per-member message inbox, gated on `isClusterMember(msg.sender)`.

- Each member has a logical "channel" identified by its memberId.
- `send(memberId recipient, bytes ciphertext, bytes32 envelopeId)` appends an encrypted payload to the recipient's channel. `envelopeId` is a sender-chosen identifier (typically a hash of the plaintext) so duplicates can be detected by the reader.
- Payloads are perma-stored on chain via event emission (`MessageSent(senderMemberId, recipientMemberId, envelopeId, ciphertext)`). The contract does not retain raw bytes in storage; readers reconstruct channel history by indexing events.
- Ciphertexts are encrypted by the sender to the recipient's registered public key (from AttestFacet). The MessageFacet does not validate this — it cannot, since it does not know the cipher — but any non-encrypted payload is a directive violation under the project's critical-directive rules.
- There is no message size limit at the contract level; gas is the only ceiling. Application-layer chunking is the caller's problem.

The message log is intentionally append-only and unbounded. Pruning, archival, and indexing are off-chain concerns.

### 3.4 NetworkFacet

The wireguard signalling surface, gated on `isClusterMember(msg.sender)`.

- `publishWgKey(bytes32 wgPublicKey)` stores the caller's wireguard public key (replacing any previous value) and emits `WgKeyPublished(memberId, wgPublicKey)`.
- `wgPublicKeyOf(memberId) → bytes32` returns the last-published key.
- `listMembers() → memberId[]` returns the current member set so a joining CVM can fetch every peer's wgPublicKey in one batch read.

The NetworkFacet does **not** store endpoint addresses (IP:port). Endpoint info is exchanged through MessageFacet, encrypted to the recipient — exposing endpoint addresses on chain leaks more than necessary about the deployment topology.

---

## 4. Member contracts

Each CVM is represented by a per-CVM **ClusterMember** proxy, deterministically deployed by a `ClusterMemberFactory` so the proxy address can be predicted before the CVM ever boots. (The proxy address is what the TEE attestation chain commits to.)

ClusterMember proxies are passthrough: they forward a small, fixed set of selectors (the dstack `IAppAuth` family on the DstackFacet, plus any future per-platform attestation interfaces) into the diamond. They do not hold cluster state. They are upgradeable via UUPS, gated on the diamond's owner — this lets us migrate per-CVM ABIs as new TEE platforms ship, without disturbing the diamond.

The Member abstraction is identical in shape to dstackgres's `TeeSqlClusterMember`. Nothing about it is Postgres-specific in dstackgres either; the rename to `ClusterMember` is the only change.

---

## 5. CVM compose package

A Rust binary (target: `cluster-mesh-agent`) shipped as an OCI image and included in CVM `docker-compose` files. Runs as a sidecar with elevated privileges (needs to configure wireguard) and a healthcheck that the application's main container can depend on.

### 5.1 Boot sequence

1. **Discover cluster address.** The compose package reads its own member contract address from a `MEMBER_CONTRACT` env var or a file mounted from the dstack runtime. It calls `member.cluster()` to get the ClusterDiamond address.
2. **Derive identity keys.** Using the TEE-derived seed (e.g. dstack's `derive_key` for a known purpose string like `teemesh.identity.v1`), produce: (a) a secp256k1 keypair whose address is the registration signer, (b) an x25519 keypair for MessageFacet payload decryption, (c) a wireguard keypair for the mesh. All keys are deterministic in the TEE state.
3. **Register.** Construct an `AttestArgs` for the cluster's expected pattern (e.g. the dstack KMS chain), sign the binding message with the secp256k1 key, and call `register()` on the cluster via the member proxy. On success, the diamond emits `MemberRegistered` and stores the x25519 pubkey for MessageFacet. On failure (verifier reverts, pattern not whitelisted), retry with backoff — but never silently proceed past this gate. Until registration succeeds, the package never reports healthy.
4. **Publish wireguard key.** Call `NetworkFacet.publishWgKey(wgPublicKey)` through the member proxy.
5. **Wait for peer endpoints.** Listen for `MessageSent` events on the member's channel. Each event whose decrypted payload is a `PeerEndpoint{ memberId, ip, port, wgPublicKey, expiresAt }` from an existing member is consumed: the package configures the wireguard interface with the peer.
6. **Send own endpoint.** For every other member the package learns about (via `listMembers()` plus inbound endpoint messages), encrypt a `PeerEndpoint` of self to the peer's x25519 pubkey and send it via MessageFacet.
7. **Heartbeat.** For every wireguard peer, run a lightweight heartbeat — a periodic UDP ping carrying (a) sender memberId, (b) timestamp, (c) the sender's view of which peers it currently considers connected. Heartbeats are signed with the secp256k1 key so they are not spoofable on the wire.
8. **Liveness consensus gate.** The package maintains a local view:
   - A node is **live** iff at least one other node's heartbeat reports it as connected.
   - The mesh is **converged** iff every live node reports the same connected-set, and that set equals the live set.
   - Until the mesh is converged, the package's healthcheck returns 503 and the application container does not start.
9. **Become healthy.** Once converged, healthy is reported. Heartbeat continues for the life of the process; on transient drops the package re-tries connection and the application's own logic decides whether to degrade.

### 5.2 Failure modes

- **Pattern revoked mid-flight.** If the cluster owner removes the attestation pattern between step 3 and the application coming up, no member already registered is forcibly removed (no on-chain eviction in v1); but new joiners cannot register, and a future re-register attempt (e.g. after a CVM restart) will fail. v1 punts cluster-driven eviction to a later spec.
- **Message channel poisoned.** A malicious member could spam another member's channel with garbage. Decryption failures are silently dropped; the package logs at debug only. Rate-limiting is not enforced on chain in v1.
- **Liveness deadlock.** If the network is partitioned at startup such that no convergence is possible, the package stays unhealthy indefinitely. This is intentional — degraded boot of an unmeshed mesh is worse than visible failure.

---

## 6. Trust model

The diamond's cluster owner (a Safe in production) controls:
- Attestation patterns (AttestFacet)
- DstackFacet allowlists (compose hashes, device IDs)
- Facet cuts (via solidstate's DiamondCut + SafeOwnable)
- Member factory address

The cluster owner can **not** decrypt messages, derive members' wireguard keys, or impersonate a member. All TEE-derived secrets stay in the TEEs. The chain only ever sees public commitments (pubkeys, ciphertexts, addresses).

A compromised cluster owner can rotate attestation patterns to admit malicious CVMs as members; this would let the attacker join the mesh and read messages addressed to itself. It would **not** let the attacker read messages addressed to honest members, since each member's inbox is encrypted to that member's TEE-derived x25519 pubkey.

A compromised TEE platform vendor (e.g. a malicious dstack KMS root) is out of scope: the dstack security model is taken as a given by the DstackFacet. Mitigations exist on the dstack side (KMS root rotation, multi-root patterns) and can be reflected here as additional patterns.

---

## 7. Differences from dstackgres

dstackgres is the codebase TeeMesh is being extracted from. The differences:

- **No Postgres anything.** dstackgres's `CoreFacet` mixes membership with `endpoint` strings, DNS labels, leader leases, signer authorization — all Postgres-cluster-specific. TeeMesh's AttestFacet keeps only the membership-registry shape.
- **No control-plane facet.** dstackgres has a `ControlPlaneFacet` for off-chain action authorization. TeeMesh omits it; if an application needs it, they can add a facet via diamondCut.
- **No leader lease.** Leader election is application-level; TeeMesh does not assume one is needed.
- **Generic verifier interface.** dstackgres's `DstackVerifier` is bolted into the boot path. In TeeMesh, every verifier is just an `IVerifier` referenced by an attestation pattern; the only dstack-specific verifier ships as the default but is no more privileged than any other.
- **MessageFacet is new.** dstackgres has nothing equivalent — endpoint exchange there happens via `signalEndpoint(bytes ciphertext, ...)` on `WgMeshFacet`. TeeMesh splits this cleanly: messaging is one facet, networking is another.
- **Wireguard signalling is decoupled from endpoint registry.** dstackgres stores endpoint blobs on chain; TeeMesh stores only wg public keys on chain and pushes endpoint info through MessageFacet so deployment topology is not leaked.

---

## 8. Out of scope (v1)

- On-chain eviction of misbehaving members
- Cluster-to-cluster federation
- Non-EVM target chains
- The CVM compose package's docker-compose authoring helpers (we will hand-roll those for the first integrations and standardize later)
- A reference application running on top of TeeMesh (dstackgres will become the first such application, post-extraction)

---

## 9. Open questions

These are tracked as open questions to resolve before the spec moves out of draft:

1. **Target chain.** dstackgres lives on Base mainnet. Does TeeMesh keep that as the canonical deployment target, or are we aiming for chain-agnosticism from day one?
2. **DstackFacet posture.** Is DstackFacet always part of the default facet cut, or is it opt-in per cluster?
3. **Member factory ownership.** Should the ClusterMemberFactory be diamond-owned (each cluster has its own factory) or shared across all clusters in the org?
4. **Encryption scheme for MessageFacet.** Sealed-box (NaCl-style x25519 + xsalsa20-poly1305), ECIES, or something else? Decision affects the compose-package crypto deps.
5. **Heartbeat transport.** UDP over wireguard (lightest), or a gossip protocol? Affects how `live` and `converged` are computed.
6. **Reorg handling for AttestFacet.** Do we wait for finality before treating a member as registered, or accept and let upstream prune?

---

## 10. References

- [dstack — IAppAuth / IAppAuthBasicManagement](https://github.com/Dstack-TEE/dstack/blob/master/kms/auth-eth/contracts/)
- [dstackgres contracts/teesql-group-auth](https://github.com/TeeSQL/dstackgres/tree/main/contracts/teesql-group-auth) — extraction source
- [EIP-2535 Diamonds](https://eips.ethereum.org/EIPS/eip-2535)
- [solidstate-solidity diamond base](https://github.com/solidstate-network/solidstate-solidity)
- [wireguard whitepaper](https://www.wireguard.com/papers/wireguard.pdf)
