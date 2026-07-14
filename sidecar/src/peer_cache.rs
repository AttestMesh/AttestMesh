//! Learned-peer public-key persistence (sidecar spec §10.2).
//!
//! New peers publish Ed25519 heartbeat keys on chain, but the cache still supports
//! mixed-version fleets without re-entering the sponsored envelope resend loop.
//! These values are public, so they are stored directly (and atomically) in the
//! sidecar-only durable state directory; unlike the retired dstack Seal API, this
//! survives an in-place CVM restart.

use anyhow::Result;
use std::collections::HashMap;
use std::path::Path;

const CACHE_FILE: &str = "peer-ed25519.v1";
const MAX_CACHE_BYTES: u64 = 1024 * 1024;

/// memberId → Ed25519 public key, as learned from PeerEndpoint envelopes.
pub type LearnedKeys = HashMap<[u8; 32], [u8; 32]>;

/// Fixed-width records (memberId ‖ ed25519_pub), sorted by memberId so identical
/// contents always persist to identical bytes.
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
pub async fn load(state_dir: Option<&Path>) -> LearnedKeys {
    match crate::storage::read_optional(state_dir, CACHE_FILE, MAX_CACHE_BYTES).await {
        Ok(Some(bytes)) => decode(&bytes),
        Ok(None) => LearnedKeys::new(),
        Err(e) => {
            tracing::warn!(error = ?e, "peer-key cache read failed; keys will be re-learned");
            LearnedKeys::new()
        }
    }
}

/// Persist the full map. Unconfigured deployments deliberately remain memory-only.
pub async fn store(state_dir: Option<&Path>, map: &LearnedKeys) -> Result<()> {
    let Some(state_dir) = state_dir else {
        return Ok(());
    };
    crate::storage::atomic_write(state_dir, CACHE_FILE, &encode(map)).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn roundtrips_through_the_durable_store() {
        let temp = tempfile::tempdir().unwrap();
        let mut m = LearnedKeys::new();
        m.insert([1u8; 32], [0xaa; 32]);
        m.insert([2u8; 32], [0xbb; 32]);
        store(Some(temp.path()), &m).await.unwrap();
        assert_eq!(load(Some(temp.path())).await, m);
    }

    #[tokio::test]
    async fn unset_or_empty_store_loads_empty() {
        let temp = tempfile::tempdir().unwrap();
        assert!(load(None).await.is_empty());
        assert!(load(Some(temp.path())).await.is_empty());
        let m = LearnedKeys::new();
        store(None, &m).await.unwrap();
    }

    #[test]
    fn corrupt_tail_is_dropped_not_fatal() {
        let mut m = LearnedKeys::new();
        m.insert([3u8; 32], [0xcc; 32]);
        let mut bytes = encode(&m);
        bytes.extend_from_slice(&[0xde, 0xad]);
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
