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
//! It is `#[ignore]`d because it requires anvil + the deployed contracts + a mock
//! dstack runtime + spawning multiple wireguard-capable processes — out of scope for
//! the unit suite, which covers the protocol logic (keys, cidr, sealed-box,
//! heartbeat, liveness, CSK, userop, bind-hash, envelope-verify) inline. The flow
//! this harness would exercise has been validated live on Base mainnet
//! (docs/deployment.md); building the local reproduction harness remains open.

#[test]
#[ignore = "needs anvil + mock-dstack + multi-process wireguard harness; the live flow was validated on Base mainnet (docs/deployment.md)"]
fn three_node_bringup_end_to_end() {
    // See module docs for the full scenario this will assert.
}
