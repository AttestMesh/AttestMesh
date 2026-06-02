//! End-to-end integration harness (sidecar spec §16.2).
//!
//! This drives three real sidecar instances against a fake dstack socket, a fake
//! Indexer, and a foundry-anvil RPC, asserting: all three reach `healthy`; the
//! originator derives the CSK and onboardees pull it (identical `GetClusterSharedKey`
//! bytes); `ListPeers` is symmetric; heartbeat liveness reflects connectivity; an
//! A→B `SendMessage` appears decrypted in B's stream but not C's; killing the
//! originator and adding a fourth onboardee still succeeds; and killing the Indexer
//! mid-flight keeps healthy sidecars healthy while a fresh one stalls at `subscribing`.
//!
//! It is `#[ignore]`d because it requires anvil + the deployed contracts + spawning
//! multiple wireguard-capable processes — out of scope for the v1 unit suite, which
//! covers the protocol logic (keys, cidr, sealed-box, heartbeat, liveness, CSK,
//! userop, bind-hash, envelope-verify) inline. The component-level unit tests are the
//! v1 deliverable; this end-to-end harness lands with the milestone-A demo build-out.

#[test]
#[ignore = "requires anvil + deployed contracts + multi-process wireguard harness (milestone A)"]
fn three_node_bringup_end_to_end() {
    // See module docs for the full scenario this will assert.
}
