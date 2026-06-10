//! In-memory + persistent state management (spec §3, §7, §9).
//!
//! Ties together: the discovered-clusters set (spec §7.2), a cached map from a
//! cluster's members to their on-chain facts (populated from `MemberRegistered`
//! topics so subscribe-time membership checks can avoid an RPC round-trip per
//! connect), the per-(cluster, member) cursor store (spec §9), and the live
//! subscriber registry (spec §8.4).

pub mod cursor;
pub mod subscribers;

use alloy::primitives::{Address, B256};
use cursor::CursorStore;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use subscribers::SubscriberRegistry;
use tokio::sync::RwLock;

/// Cached membership facts learned from `MemberRegistered` (spec §8.2: "cached read of
/// AttestFacet.memberOf … at the time of MemberRegistered").
#[derive(Debug, Clone, Copy)]
pub struct MemberInfo {
    pub member_contract: Address,
    pub registered_at_block: u64,
}

/// The shared indexer state. Cheaply cloneable (`Arc` internals) so loops and the gRPC
/// service share one view.
#[derive(Clone)]
pub struct IndexerState {
    inner: Arc<Inner>,
}

struct Inner {
    /// Cluster diamonds we follow (spec §7.2).
    known_clusters: RwLock<HashSet<Address>>,
    /// Highest factory block scanned for `ClusterDeployed` (spec §7.2).
    last_factory_block: RwLock<u64>,
    /// Highest block scanned for cluster events (spec §7.1 catch-up cursor).
    last_indexed_block: RwLock<u64>,
    /// (cluster, memberId) -> member facts, cached from `MemberRegistered`.
    members: RwLock<HashMap<(Address, B256), MemberInfo>>,
    /// Live subscriptions.
    subscribers: SubscriberRegistry,
    /// Persistent delivery cursors.
    cursors: Arc<dyn CursorStore>,
}

impl IndexerState {
    pub fn new(cursors: Arc<dyn CursorStore>) -> Self {
        Self {
            inner: Arc::new(Inner {
                known_clusters: RwLock::new(HashSet::new()),
                last_factory_block: RwLock::new(0),
                last_indexed_block: RwLock::new(0),
                members: RwLock::new(HashMap::new()),
                subscribers: SubscriberRegistry::new(),
                cursors,
            }),
        }
    }

    pub fn subscribers(&self) -> &SubscriberRegistry {
        &self.inner.subscribers
    }

    pub fn cursors(&self) -> &Arc<dyn CursorStore> {
        &self.inner.cursors
    }

    // ── Cluster set ───────────────────────────────────────────────────────────

    pub async fn add_cluster(&self, cluster: Address) -> bool {
        self.inner.known_clusters.write().await.insert(cluster)
    }

    pub async fn known_clusters(&self) -> Vec<Address> {
        self.inner
            .known_clusters
            .read()
            .await
            .iter()
            .copied()
            .collect()
    }

    pub async fn is_known_cluster(&self, cluster: Address) -> bool {
        self.inner.known_clusters.read().await.contains(&cluster)
    }

    pub async fn cluster_count(&self) -> usize {
        self.inner.known_clusters.read().await.len()
    }

    // ── Block cursors for the watcher loops ─────────────────────────────────────

    pub async fn last_indexed_block(&self) -> u64 {
        *self.inner.last_indexed_block.read().await
    }

    pub async fn set_last_indexed_block(&self, block: u64) {
        *self.inner.last_indexed_block.write().await = block;
    }

    pub async fn last_factory_block(&self) -> u64 {
        *self.inner.last_factory_block.read().await
    }

    pub async fn set_last_factory_block(&self, block: u64) {
        *self.inner.last_factory_block.write().await = block;
    }

    // ── Member cache ────────────────────────────────────────────────────────────

    pub async fn record_member(
        &self,
        cluster: Address,
        member_id: B256,
        member_contract: Address,
        registered_at_block: u64,
    ) {
        self.inner.members.write().await.insert(
            (cluster, member_id),
            MemberInfo {
                member_contract,
                registered_at_block,
            },
        );
    }

    pub async fn member_info(&self, cluster: Address, member_id: B256) -> Option<MemberInfo> {
        self.inner
            .members
            .read()
            .await
            .get(&(cluster, member_id))
            .copied()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use cursor::{Cursor, SledCursorStore};
    use tempfile::TempDir;

    fn state() -> (IndexerState, TempDir) {
        let dir = TempDir::new().unwrap();
        let store = Arc::new(SledCursorStore::open(dir.path().to_str().unwrap()).unwrap());
        (IndexerState::new(store), dir)
    }

    #[tokio::test]
    async fn cluster_set_dedupes() {
        let (s, _d) = state();
        let c = Address::repeat_byte(0xc1);
        assert!(s.add_cluster(c).await);
        assert!(!s.add_cluster(c).await); // already present
        assert_eq!(s.cluster_count().await, 1);
        assert!(s.is_known_cluster(c).await);
    }

    #[tokio::test]
    async fn member_cache_round_trip() {
        let (s, _d) = state();
        let c = Address::repeat_byte(0xc1);
        let m = B256::repeat_byte(0xa1);
        let contract = Address::repeat_byte(0x11);
        assert!(s.member_info(c, m).await.is_none());
        s.record_member(c, m, contract, 1234).await;
        let info = s.member_info(c, m).await.unwrap();
        assert_eq!(info.member_contract, contract);
        assert_eq!(info.registered_at_block, 1234);
    }

    #[tokio::test]
    async fn cursor_store_is_shared() {
        let (s, _d) = state();
        let c = Address::repeat_byte(0xc1);
        let m = B256::repeat_byte(0xa1);
        s.cursors().advance(c, m, Cursor::new(5, 1)).unwrap();
        assert_eq!(s.cursors().load(c, m).unwrap(), Some(Cursor::new(5, 1)));
    }
}
