# Spec: on-chain Ed25519 heartbeat key

**Status:** proposed 2026-07-07. **Motivation:** the mesh distributes each node's Ed25519
heartbeat key via encrypted `MessageFacet` envelopes (`PeerEndpoint`), a sponsored write op
retried until delivery. That single static, public value is the *only* mesh-bring-up datum not
already on chain — everything else (wg key, mesh IP, membership, gateway host) is a chain read.
The envelope channel is the root of the 2026-07 resend storm, the restart-amnesia, the paymaster
fragility, and the per-op cost. Publishing the key on chain — exactly as `wgPubKey` already is —
makes mesh convergence pure chain reads with **zero sponsored writes** and no paymaster dependency.

## Design (mirrors `publishWgKey`)

The Ed25519 heartbeat key is a **public verification key**; on-chain publication leaks nothing
that heartbeat signatures don't already expose. Trust model is identical to `wgPubKey`: a member
may publish a wrong key, breaking only *its own* liveness (peers verify its heartbeats against the
published key), never a cross-member attack. Gate: `onlyClusterMember` — the member's ClusterMember
contract, authenticated by its registration-derived owner key on the sponsored UserOp.

### Contract
- **Storage** (`NetworkStorage.Layout`): append `mapping(bytes32 memberId => bytes32 ed25519Key) ed25519Keys;`
  after `wgPubKeys`. Same ERC-7201 lib/slot (`attestmesh.storage.Network`) — appending a mapping is
  slot-safe; no new namespace, no `Namespaces.t.sol` change.
- **`NetworkFacet.publishEd25519Key(bytes32)`** — `onlyClusterMember`; unconditional overwrite
  (rotation allowed), writes `ed25519Keys[_senderMemberId()]`, emits the event. No MemberStorage
  mirror (unlike wg — no one-shot MemberRecord read needs it).
- **`NetworkFacet.ed25519KeyOf(bytes32 memberId) view returns (bytes32)`**.
- **Event** `Ed25519KeyPublished(bytes32 indexed memberId, bytes32 ed25519Key)` in `INetwork`.
- **`ClusterCut._networkSelectors`** 3 → 5 (add `publishEd25519Key`, `ed25519KeyOf`).
- No change to `DstackFacet`/registration ABI — members publish once post-registration (a node's
  first bring-up submits one sponsored `publishEd25519Key`; new members do the same). Registration
  integration can fold in later without another storage change.

### Live-cluster migration (C3 `0x5ab4706f…`)
Owner-executed diamond cut, modeled on `script/UpgradeDstackFacetPathA.s.sol`: deploy a fresh
`NetworkFacet`, then one `diamondCut` that REPLACEs the 3 existing network selectors + ADDs the 2
new ones (a facet's selectors must all resolve to one target). No cluster/factory address change →
gas-webhook config untouched. New clusters get it automatically via `ClusterCut.buildFacetCuts`.
One-time backfill: each of the 18 members publishes its key once (~18 sponsored ops ≈ $0.09).

### Sidecar
- New `ChainClient::ed25519_key_of(cluster, memberId)` view read (pattern: `x_pubkey_of`).
- At bring-up, publish own key once: `NetworkFacet.publishEd25519Key(keys.ed25519_pub)` via a
  sponsored UserOp, idempotent (skip if `ed25519KeyOf(self)` already equals it). One op per node
  lifetime.
- Reconcile loop reads each peer's Ed25519 from chain and installs it into `PeerTable`
  (`set_ed25519`), replacing the envelope-absorb path.
- **PeerEndpoint envelope**: retire the key-carrying purpose. Transitional: keep sending/absorbing
  it as a fallback gated on `ed25519KeyOf(peer) == 0` (peer on an old sidecar), so a mixed fleet
  still converges; delete once the fleet is uniformly on the new facet + image. `MessageFacet`
  remains for genuine member-to-member application messages only.
- `sealed peer_cache` becomes redundant (chain is durable) — remove.

### Webhook / indexer
- Gas webhook: add `"publishEd25519Key(bytes32)"` to `ALLOWED_SIGNATURES`.
- Indexer (optional): add `Ed25519KeyPublished` to the `sol!` block + watcher for push; relevance =
  all members (like `WgKeyPublished`). Not required for correctness (sidecar reads on reconcile).

## Cost after
Steady-state mesh sponsored writes ≈ **zero**. Per node: registration (one-time, existing),
publish-key (one-time), `setCskCommitment` (originator only, one-time), plus real app messages and
rare key rotation. Far under the $1/CVM/month ceiling — pennies per node lifetime.

## Acceptance
- forge: new direct test (member publishes → `ed25519KeyOf` returns + event), `Selectors.t.sol`
  pin updated, `Namespaces.t.sol` unchanged (same slot), full suite green.
- Two-node bring-up on a cut cluster converges (heartbeats verify, `live_peers` > 0) with **zero
  PeerEndpoint envelopes** — `MessageSent` count = CSK/app traffic only.
