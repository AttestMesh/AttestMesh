//! Integration test placeholder (spec §14.2).
//!
//! The full integration harness is OUT OF SCOPE for this prototype: it requires an
//! anvil node, the AttestMesh contracts deployed via forge scripts, and in-process
//! mock subscribers. This file documents what that harness would assert so the
//! `tests/` surface is present and the contract is recorded. Run with
//! `cargo test -- --ignored` once the harness lands.

/// What the §14.2 harness does, once implemented:
///
/// 1. Deploy AttestMesh infra + a single cluster on anvil (forge scripts compiled
///    into the fixture).
/// 2. Boot the indexer pointed at anvil's RPC.
/// 3. Spin up three in-process gRPC mock subscribers with different
///    `(cluster, member_id)` tuples.
/// 4. Assert:
///    - all three receive `MemberRegistered` for the other two,
///    - a `MessageSent` to member A is received by A only (never B or C),
///    - reconnect with `from_block = 0` resumes from the persisted cursor with no
///      duplicate Acks,
///    - kill + restart preserves cursors and subscribers resume,
///    - a subscriber that stalls for 5s then receives 2048 events sees the oldest
///      1024 dropped (with `indexer_events_dropped_total` incremented).
/// 5. Cleanup.
#[test]
#[ignore = "integration harness (anvil + forge + mock subscribers) is out of scope for the prototype"]
fn indexer_end_to_end() {
    // Intentionally unimplemented — see the doc comment above for the spec'd shape.
    unimplemented!("see docs/specs/indexer.md §14.2");
}
