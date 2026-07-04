//! Learned-peer key persistence (sidecar spec §10.2).
//!
//! A peer's Ed25519 key arrives only inside its PeerEndpoint envelope. Held only in
//! memory, a restart forgot every learned key and the reconcile loop resumed
//! re-sending our PeerEndpoint envelope — one sponsored UserOp per peer per resend
//! tick — while peers that already knew OUR key never replied, so the resends never
//! converged (live-found 2026-07: ~1.3k sponsored UserOps/day across the fleet).
//! Sealing the map makes learned keys survive restarts. The entries are peers'
//! PUBLIC keys; the sealed store is used as the sidecar's one durable surface, not
//! for secrecy.

use crate::dstack::DstackRuntime;
use anyhow::Result;
use std::collections::HashMap;

const SEAL_LABEL: &str = "attestmesh.peer_ed25519.v1";

/// memberId → Ed25519 public key, as learned from PeerEndpoint envelopes.
pub type LearnedKeys = HashMap<[u8; 32], [u8; 32]>;

/// Fixed-width records (memberId ‖ ed25519_pub), sorted by memberId so identical
/// contents always seal to identical bytes.
fn encode(map: &LearnedKeys) -> Vec<u8> {
    let mut ids: Vec<&[u8; 32]> = map.keys().collect();
    ids.sort();
    let mut out = Vec::with_capacity(map.len() * 64);
    for id in ids {
        out.extend_from_slice(id);
        out.extend_from_slice(&map[id]);
    }
    out
}

/// A truncated/corrupt tail is dropped silently — worst case those peers are
/// re-learned through one more envelope exchange.
fn decode(bytes: &[u8]) -> LearnedKeys {
    bytes
        .chunks_exact(64)
        .map(|c| {
            let mut id = [0u8; 32];
            let mut ed = [0u8; 32];
            id.copy_from_slice(&c[..32]);
            ed.copy_from_slice(&c[32..]);
            (id, ed)
        })
        .collect()
}

/// Load the learned-key map on boot. Any failure degrades to an empty map (the
/// pre-persistence behavior), never blocks bring-up.
pub async fn load(dstack: &dyn DstackRuntime) -> LearnedKeys {
    match dstack.unseal(SEAL_LABEL).await {
        Ok(Some(bytes)) => decode(&bytes),
        Ok(None) => LearnedKeys::new(),
        Err(e) => {
            tracing::warn!(error = ?e, "peer-key unseal failed; keys will be re-learned");
            LearnedKeys::new()
        }
    }
}

/// Seal the full map (it is small: 64 bytes per cluster peer).
pub async fn store(dstack: &dyn DstackRuntime, map: &LearnedKeys) -> Result<()> {
    dstack.seal(SEAL_LABEL, &encode(map)).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dstack::MockDstack;

    #[tokio::test]
    async fn roundtrips_through_the_sealed_store() {
        let d = MockDstack::new([7u8; 32]);
        let mut m = LearnedKeys::new();
        m.insert([1u8; 32], [0xaa; 32]);
        m.insert([2u8; 32], [0xbb; 32]);
        store(&d, &m).await.unwrap();
        assert_eq!(load(&d).await, m);
    }

    #[tokio::test]
    async fn empty_store_loads_empty() {
        let d = MockDstack::new([7u8; 32]);
        assert!(load(&d).await.is_empty());
    }

    #[test]
    fn corrupt_tail_is_dropped_not_fatal() {
        let mut m = LearnedKeys::new();
        m.insert([3u8; 32], [0xcc; 32]);
        let mut bytes = encode(&m);
        bytes.extend_from_slice(&[0xde, 0xad]); // partial record
        assert_eq!(decode(&bytes), m);
    }

    #[test]
    fn encoding_is_deterministic_across_insertion_order() {
        let mut a = LearnedKeys::new();
        a.insert([1u8; 32], [0xaa; 32]);
        a.insert([2u8; 32], [0xbb; 32]);
        let mut b = LearnedKeys::new();
        b.insert([2u8; 32], [0xbb; 32]);
        b.insert([1u8; 32], [0xaa; 32]);
        assert_eq!(encode(&a), encode(&b));
    }
}
