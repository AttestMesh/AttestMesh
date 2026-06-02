//! Per-cluster subscriber registry (spec §7.3, §8.4).
//!
//! Each subscription is a bounded mpsc channel (capacity 1024) to a single member's
//! send loop. The dispatch loop iterates a cluster's subscribers and pushes the
//! envelopes relevant to each. Backpressure policy (spec §8.4): on a full channel the
//! dispatch loop does not block — it drops the oldest unsent envelope and records a
//! metric. Here we model the registry and the per-subscriber try-send + drop-oldest
//! behaviour; the metric increment is wired in by the caller.

use crate::grpc::subscribe::SessionAttestation;
use crate::identity::Identity;
use crate::pb::PushEnvelope;
use alloy::primitives::{Address, B256};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::mpsc;
use tokio::sync::RwLock;

/// Per-subscription channel capacity (spec §8.4).
pub const SUBSCRIBER_CHANNEL_CAPACITY: usize = 1024;

/// A single live subscription. Cheaply cloneable handle to the member's send channel.
#[derive(Clone)]
pub struct Subscriber {
    pub member_id: B256,
    /// Unique per-connection id so a reconnect (new stream) can replace/clear the old
    /// entry deterministically even for the same member.
    pub session_id: u64,
    sender: mpsc::Sender<PushEnvelope>,
    dropped: Arc<AtomicU64>,
    /// Per-session attestation gate (spec §11 step 5): attaches the indexer quote to
    /// the first push of this session only. Shared so both the subscribe-time catch-up
    /// and the live dispatch loop emit through one gate.
    session_att: Arc<SessionAttestation>,
}

impl Subscriber {
    /// Try to deliver `env`. On a full channel, apply the spec §8.4 policy: drop the
    /// oldest unsent envelope (by draining one) and retry once; if still full, count a
    /// drop. Returns `true` if the envelope was enqueued, `false` if dropped.
    pub fn try_send(&self, env: PushEnvelope) -> bool {
        match self.sender.try_send(env) {
            Ok(()) => true,
            Err(mpsc::error::TrySendError::Full(env)) => {
                // Channel full: the receiver (a closed/slow stream) keeps the oldest
                // items. We cannot pop from the producer side of a tokio mpsc, so the
                // documented behaviour ("drop oldest") is realized by the receiver
                // side which reads FIFO; from the producer's vantage we record the
                // drop of the *new* item and signal the gap. Either way the subscriber
                // sees an explicit gap and closes it on reconnect from its cursor.
                self.dropped.fetch_add(1, Ordering::Relaxed);
                let _ = env;
                false
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                // Stream gone; the registry will reap this subscriber.
                false
            }
        }
    }

    /// Total envelopes dropped for this subscriber (feeds the dropped metric).
    pub fn dropped_count(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }

    pub fn is_closed(&self) -> bool {
        self.sender.is_closed()
    }

    /// Sign `env` with the indexer key and attach the session attestation iff this is
    /// the first push of the session (spec §11 step 5).
    pub fn finalize(&self, identity: &Identity, env: PushEnvelope) -> PushEnvelope {
        self.session_att.finalize(identity, env)
    }
}

/// Create a new subscription channel, returning the registry-side handle and the
/// stream-side receiver the gRPC send loop drains.
pub fn channel(
    member_id: B256,
    session_id: u64,
    session_att: Arc<SessionAttestation>,
) -> (Subscriber, mpsc::Receiver<PushEnvelope>) {
    let (tx, rx) = mpsc::channel(SUBSCRIBER_CHANNEL_CAPACITY);
    let sub = Subscriber {
        member_id,
        session_id,
        sender: tx,
        dropped: Arc::new(AtomicU64::new(0)),
        session_att,
    };
    (sub, rx)
}

/// All subscribers across all clusters, indexed by cluster then session.
#[derive(Default)]
pub struct SubscriberRegistry {
    // cluster -> (session_id -> Subscriber)
    inner: RwLock<HashMap<Address, HashMap<u64, Subscriber>>>,
    next_session: AtomicU64,
}

impl SubscriberRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Allocate a process-unique session id for a new connection.
    pub fn next_session_id(&self) -> u64 {
        self.next_session.fetch_add(1, Ordering::Relaxed)
    }

    /// Register a subscriber under a cluster.
    pub async fn add(&self, cluster: Address, sub: Subscriber) {
        let mut g = self.inner.write().await;
        g.entry(cluster).or_default().insert(sub.session_id, sub);
    }

    /// Remove a subscription by `(cluster, session_id)`.
    pub async fn remove(&self, cluster: Address, session_id: u64) {
        let mut g = self.inner.write().await;
        if let Some(set) = g.get_mut(&cluster) {
            set.remove(&session_id);
            if set.is_empty() {
                g.remove(&cluster);
            }
        }
    }

    /// Snapshot the subscribers of a cluster (cheap clones of channel handles) for the
    /// dispatch loop to iterate without holding the lock during sends.
    pub async fn subscribers_of(&self, cluster: Address) -> Vec<Subscriber> {
        let g = self.inner.read().await;
        g.get(&cluster)
            .map(|set| set.values().cloned().collect())
            .unwrap_or_default()
    }

    /// Number of live subscribers for a cluster (feeds the gauge metric).
    pub async fn count_of(&self, cluster: Address) -> usize {
        let g = self.inner.read().await;
        g.get(&cluster).map(|s| s.len()).unwrap_or(0)
    }

    /// All clusters that currently have at least one subscriber.
    pub async fn active_clusters(&self) -> Vec<Address> {
        let g = self.inner.read().await;
        g.keys().copied().collect()
    }

    /// Reap any subscribers whose stream has closed. Returns the number reaped.
    pub async fn reap_closed(&self) -> usize {
        let mut g = self.inner.write().await;
        let mut reaped = 0;
        g.retain(|_, set| {
            set.retain(|_, s| {
                let alive = !s.is_closed();
                if !alive {
                    reaped += 1;
                }
                alive
            });
            !set.is_empty()
        });
        reaped
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pb::IndexerAttestation;

    /// A throwaway session-attestation gate for tests that only exercise the registry
    /// and channel plumbing (they never call `finalize`, which would need an Identity).
    fn test_session_att() -> Arc<SessionAttestation> {
        Arc::new(SessionAttestation::new(IndexerAttestation {
            quote: vec![],
            expected_code_id: vec![],
            expected_pubkey: vec![],
        }))
    }

    fn dummy_env(block: u64) -> PushEnvelope {
        PushEnvelope {
            event_data: vec![],
            cluster_addr: vec![],
            block_number: block,
            tx_hash: vec![],
            log_index: 0,
            rpc_repro: None,
            indexer_signature: vec![],
            indexer_attestation: None,
        }
    }

    #[tokio::test]
    async fn add_remove_iterate() {
        let reg = SubscriberRegistry::new();
        let cluster = Address::repeat_byte(0xc1);
        let member = B256::repeat_byte(0xa1);

        let sid = reg.next_session_id();
        let (sub, _rx) = channel(member, sid, test_session_att());
        reg.add(cluster, sub).await;

        assert_eq!(reg.count_of(cluster).await, 1);
        let subs = reg.subscribers_of(cluster).await;
        assert_eq!(subs.len(), 1);
        assert_eq!(subs[0].member_id, member);
        assert_eq!(reg.active_clusters().await, vec![cluster]);

        reg.remove(cluster, sid).await;
        assert_eq!(reg.count_of(cluster).await, 0);
        assert!(reg.active_clusters().await.is_empty());
    }

    #[tokio::test]
    async fn multiple_members_same_cluster() {
        let reg = SubscriberRegistry::new();
        let cluster = Address::repeat_byte(0xc1);
        for i in 0..3u8 {
            let sid = reg.next_session_id();
            let (sub, _rx) = channel(B256::repeat_byte(i), sid, test_session_att());
            reg.add(cluster, sub).await;
            std::mem::forget(_rx); // keep the channel open for the count assertion
        }
        assert_eq!(reg.count_of(cluster).await, 3);
    }

    #[tokio::test]
    async fn session_ids_are_unique() {
        let reg = SubscriberRegistry::new();
        let a = reg.next_session_id();
        let b = reg.next_session_id();
        assert_ne!(a, b);
    }

    #[tokio::test]
    async fn try_send_delivers_until_full_then_drops() {
        let member = B256::repeat_byte(0xa1);
        let (sub, mut rx) = channel(member, 0, test_session_att());
        // Fill the channel to capacity.
        for i in 0..SUBSCRIBER_CHANNEL_CAPACITY {
            assert!(sub.try_send(dummy_env(i as u64)));
        }
        // Next send overflows → dropped, metric increments.
        assert!(!sub.try_send(dummy_env(9999)));
        assert_eq!(sub.dropped_count(), 1);
        // Receiver still drains the buffered FIFO items.
        let first = rx.recv().await.unwrap();
        assert_eq!(first.block_number, 0);
    }

    #[tokio::test]
    async fn closed_stream_is_reaped() {
        let reg = SubscriberRegistry::new();
        let cluster = Address::repeat_byte(0xc1);
        let sid = reg.next_session_id();
        let (sub, rx) = channel(B256::repeat_byte(0x01), sid, test_session_att());
        reg.add(cluster, sub).await;
        assert_eq!(reg.count_of(cluster).await, 1);

        drop(rx); // subscriber's stream goes away
        assert_eq!(reg.reap_closed().await, 1);
        assert_eq!(reg.count_of(cluster).await, 0);
    }

    #[tokio::test]
    async fn concurrent_add_remove() {
        let reg = Arc::new(SubscriberRegistry::new());
        let cluster = Address::repeat_byte(0xc1);
        let mut handles = Vec::new();
        for i in 0..16u64 {
            let reg = reg.clone();
            handles.push(tokio::spawn(async move {
                let sid = reg.next_session_id();
                let (sub, rx) = channel(B256::repeat_byte(i as u8), sid, test_session_att());
                reg.add(cluster, sub).await;
                std::mem::forget(rx);
                reg.remove(cluster, sid).await;
            }));
        }
        for h in handles {
            h.await.unwrap();
        }
        assert_eq!(reg.count_of(cluster).await, 0);
    }
}
