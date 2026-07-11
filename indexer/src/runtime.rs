//! Runtime wiring (spec §3 lifecycle, §7 loops).
//!
//! Boot → derive identity → registry self-check → load cursors (implicit; sled opens
//! lazily) → catch up against chain head → start gRPC + health → enter the three
//! concurrent loops:
//!   * block-watcher (§7.1): poll cluster logs, dispatch to subscribers.
//!   * cluster-discovery (§7.2): poll `ClusterDeployed`, grow the watched set.
//!   * (the per-cluster dispatch of §7.3 is folded into the watcher's fan-out here —
//!     one chain-ordered pass per poll keeps ordering without N task spawns.)

use crate::chain::{self, watcher, HttpProvider};
use crate::config::Config;
use crate::health::Health;
use crate::identity::Identity;
use crate::metrics::Metrics;
use crate::query::{MeshCidr, ReadModel};
use crate::state::cursor::Cursor;
use crate::state::IndexerState;
use alloy::primitives::Address;
use std::sync::Arc;
use std::time::Duration;

/// getLogs chunk for cluster discovery (providers cap ranges; Alchemy ~10k blocks).
const DISCOVERY_CHUNK: u64 = 10_000;

/// The inclusive `(from, to)` getLogs ranges covering `[start, head]` in `chunk`-sized
/// pages. Pure so the catch-up bounds are unit-tested: the live-found failure mode was
/// an unbounded genesis scan (~47M blocks on Base) that kept the health/gRPC listeners
/// from ever starting.
fn scan_ranges(start: u64, head: u64, chunk: u64) -> impl Iterator<Item = (u64, u64)> {
    let chunk = chunk.max(1);
    let mut from = start;
    std::iter::from_fn(move || {
        if from > head {
            return None;
        }
        let to = (from + chunk - 1).min(head);
        let range = (from, to);
        from = to + 1;
        Some(range)
    })
}

/// Where the event catch-up starts: just past the persisted cursor, but never below
/// the configured floor (the factory deploy block — nothing exists before it).
fn events_scan_start(last_indexed: u64, floor: u64) -> u64 {
    last_indexed.saturating_add(1).max(floor)
}

/// Factory discovery uses the same cursor rule as event indexing. In particular, a
/// failed boot discovery must retry from the configured factory-deploy floor rather
/// than accidentally scanning from genesis while `last_factory_block` is still zero.
fn discovery_scan_start(last_scanned: u64, floor: u64) -> u64 {
    last_scanned.saturating_add(1).max(floor)
}

async fn record_discovered_cluster(
    state: &IndexerState,
    read_model: &ReadModel,
    deployment: crate::query::ClusterDeployment,
    cidr: MeshCidr,
) -> bool {
    let inserted = state.add_cluster(deployment.cluster).await;
    read_model.add_cluster(deployment, cidr).await;
    inserted
}

/// Shared handles passed to the loops.
#[derive(Clone)]
pub struct Runtime {
    pub config: Arc<Config>,
    pub provider: Arc<HttpProvider>,
    pub state: IndexerState,
    pub identity: Arc<Identity>,
    pub metrics: Arc<Metrics>,
    pub health: Arc<Health>,
    pub read_model: Arc<ReadModel>,
}

impl Runtime {
    /// Discover existing clusters and catch the cluster-event cursor up to head before
    /// the steady-state loops start (spec §7.1 catch-up, §7.2).
    pub async fn boot_catchup(&self) -> anyhow::Result<()> {
        let head = chain::block_number(&self.provider).await?;
        self.health.set_rpc_reachable(true);

        // Discover all clusters from the factory's history. INDEXER_START_BLOCK (the
        // factory deploy block) bounds the scan — nothing exists before it, and a
        // genesis scan on a mainnet never finishes. Chunked: providers cap getLogs
        // ranges (Alchemy ~10k blocks).
        let floor = self.config.start_block;
        tracing::info!(floor, head, "boot catch-up: discovering clusters");
        for (from, to) in scan_ranges(floor, head, DISCOVERY_CHUNK) {
            let new = watcher::poll_new_clusters(
                &self.provider,
                self.config.cluster_diamond_factory_addr,
                from,
                to,
            )
            .await?;
            for d in new {
                self.add_discovered_cluster(d).await?;
            }
        }
        self.state.set_last_factory_block(head).await;
        self.read_model.set_scanned_to_block(head).await;
        self.metrics
            .clusters_watched
            .set(self.state.cluster_count().await as i64);

        // Page cluster events forward BLOCK_BATCH_SIZE at a time until caught up.
        let clusters = self.state.known_clusters().await;
        let start = events_scan_start(self.state.last_indexed_block().await, floor);
        let batch = self.config.block_batch_size.max(1);
        tracing::info!(
            clusters = clusters.len(),
            start,
            head,
            "boot catch-up: paging cluster events"
        );
        let mut batches = 0u64;
        if !clusters.is_empty() {
            for (from, to) in scan_ranges(start, head, batch) {
                let logs = watcher::poll_cluster_logs(&self.provider, &clusters, from, to).await?;
                self.ingest_for_cache(&logs).await;
                batches += 1;
                if batches % 100 == 0 {
                    tracing::info!(from, head, "boot catch-up progress");
                }
            }
        }
        self.state.set_last_indexed_block(head).await;
        self.read_model.set_scanned_to_block(head).await;
        self.health.set_head_lag(0);
        tracing::info!(head, "boot catch-up complete");
        Ok(())
    }

    /// During catch-up we only populate the member cache (so later subscribers pass the
    /// existence check and get the right cursor floor). We do NOT push these to
    /// subscribers — none are connected yet, and each will replay from its cursor on
    /// subscribe (spec §8.2 step 5).
    async fn ingest_for_cache(&self, logs: &[watcher::IndexedLog]) {
        for log in logs {
            let mut registered_at = None;
            if let watcher::EventKind::MemberRegistered {
                member_id,
                member_contract,
                ..
            } = log.kind
            {
                self.state
                    .record_member(
                        log.cluster_addr,
                        member_id,
                        member_contract,
                        log.block_number,
                    )
                    .await;
                match chain::member_registered_at(&self.provider, log.cluster_addr, member_id).await
                {
                    Ok(ts) => registered_at = Some(ts),
                    Err(e) => {
                        tracing::debug!(
                            error = %e,
                            cluster = %log.cluster_addr,
                            member = %member_id,
                            "could not read member registeredAt; using block number"
                        );
                    }
                }
            }
            self.read_model.ingest(log, registered_at).await;
        }
    }

    async fn add_discovered_cluster(
        &self,
        deployment: crate::query::ClusterDeployment,
    ) -> anyhow::Result<bool> {
        let cluster = deployment.cluster;
        // Resolve metadata before mutating the known-cluster set. If this read fails,
        // the next discovery pass must be able to retry the complete operation.
        let (ip, prefix) = chain::mesh_cidr(&self.provider, cluster).await?;
        Ok(record_discovered_cluster(
            &self.state,
            &self.read_model,
            deployment,
            MeshCidr { ip, prefix },
        )
        .await)
    }

    /// The block-watcher loop (spec §7.1) + inline dispatch (§7.3).
    pub async fn run_block_watcher(self) {
        let poll = self.config.block_poll_interval;
        let mut backoff = poll;
        loop {
            match self.watch_tick().await {
                Ok(()) => {
                    backoff = poll;
                    tokio::time::sleep(poll).await;
                }
                Err(e) => {
                    self.metrics.inc_rpc_error("block_watcher");
                    self.health.set_rpc_reachable(false);
                    tracing::warn!(error = %e, "block-watcher tick failed; backing off");
                    tokio::time::sleep(backoff).await;
                    // Exponential backoff capped at 30s (spec §13 RPC-down handling).
                    backoff = (backoff * 2).min(Duration::from_secs(30));
                }
            }
        }
    }

    async fn watch_tick(&self) -> anyhow::Result<()> {
        let head = chain::block_number(&self.provider).await?;
        self.health.set_rpc_reachable(true);

        let last = self.state.last_indexed_block().await;
        self.health.set_head_lag(head.saturating_sub(last));

        if head <= last {
            return Ok(());
        }

        let clusters = self.state.known_clusters().await;
        if !clusters.is_empty() {
            let batch = self.config.block_batch_size.max(1);
            let mut from = events_scan_start(last, self.config.start_block);
            while from <= head {
                let to = (from + batch - 1).min(head);
                let logs = watcher::poll_cluster_logs(&self.provider, &clusters, from, to).await?;
                self.ingest_for_cache(&logs).await;
                // Publish the indexed watermark before delivery. A subscription that
                // races this batch may replay duplicates, but exact cursor filtering
                // makes duplicates harmless and prevents a missed prefix.
                self.state.set_last_indexed_block(to).await;
                self.dispatch(&logs, &clusters, to).await;
                from = to + 1;
            }
        }
        self.state.set_last_indexed_block(head).await;
        self.health.set_head_lag(0);
        Ok(())
    }

    /// Fan a chain-ordered batch of logs out to every relevant subscriber (spec §7.3).
    async fn dispatch(
        &self,
        logs: &[watcher::IndexedLog],
        clusters: &[Address],
        indexed_through: u64,
    ) {
        for log in logs {
            let subs = self
                .state
                .subscribers()
                .subscribers_of(log.cluster_addr)
                .await;
            if subs.is_empty() {
                continue;
            }
            let stub = crate::chain::repro::build_stub(log);
            let base = crate::grpc::envelope::build_envelope(log, &stub);
            for sub in subs {
                if !log.is_relevant_for(&sub.member_id) {
                    continue;
                }
                // Each subscriber gets its own finalized copy (attestation gating + sig
                // are per-session, but the signature bytes are identical across
                // sessions for the same envelope — the attestation differs).
                let env = sub.finalize(&self.identity, base.clone());
                if sub.send_live(env).await {
                    self.metrics.inc_pushed(log.cluster_addr, log.kind.label());
                } else {
                    self.metrics.inc_dropped(log.cluster_addr, sub.member_id);
                    tracing::warn!(
                        cluster = %log.cluster_addr, member = %sub.member_id,
                        "subscriber channel unavailable; closing stream for replay"
                    );
                }
            }
        }

        // A signed checkpoint advances cursors across blocks with no relevant logs.
        // It is queued after every event in this indexed page for each v2 subscriber.
        for cluster in clusters {
            let subs = self.state.subscribers().subscribers_of(*cluster).await;
            for sub in subs.into_iter().filter(|sub| sub.supports_checkpoints()) {
                let checkpoint = crate::grpc::envelope::build_checkpoint(*cluster, indexed_through);
                let checkpoint = sub.finalize(&self.identity, checkpoint);
                if sub.send_live(checkpoint).await {
                    self.metrics.inc_pushed(*cluster, "Checkpoint");
                } else {
                    self.metrics.inc_dropped(*cluster, sub.member_id);
                }
            }
        }
    }

    /// The cluster-discovery loop (spec §7.2). Runs every ~60s.
    pub async fn run_cluster_discovery(self) {
        loop {
            tokio::time::sleep(Duration::from_secs(60)).await;
            if let Err(e) = self.discovery_tick().await {
                self.metrics.inc_rpc_error("cluster_discovery");
                tracing::warn!(error = %e, "cluster-discovery tick failed");
            }
        }
    }

    async fn discovery_tick(&self) -> anyhow::Result<()> {
        let head = chain::block_number(&self.provider).await?;
        let from = discovery_scan_start(
            self.state.last_factory_block().await,
            self.config.start_block,
        );
        if from <= head {
            for (start, end) in scan_ranges(from, head, DISCOVERY_CHUNK) {
                let new = watcher::poll_new_clusters(
                    &self.provider,
                    self.config.cluster_diamond_factory_addr,
                    start,
                    end,
                )
                .await?;
                for d in new {
                    let cluster = d.cluster;
                    // Always hydrate the in-memory read model. A prior process may
                    // have persisted the cluster set and then exited before the
                    // ephemeral read model was populated.
                    if self.add_discovered_cluster(d).await? {
                        tracing::info!(cluster = %cluster, "discovered new cluster");
                    }
                }
            }
            self.state.set_last_factory_block(head).await;
            self.read_model.set_scanned_to_block(head).await;
            self.metrics
                .clusters_watched
                .set(self.state.cluster_count().await as i64);
        }
        Ok(())
    }

    /// Periodically reap subscribers whose streams have closed + refresh gauges.
    pub async fn run_housekeeping(self) {
        loop {
            tokio::time::sleep(Duration::from_secs(15)).await;
            let reaped = self.state.subscribers().reap_closed().await;
            if reaped > 0 {
                tracing::debug!(reaped, "reaped closed subscriber streams");
            }
            for cluster in self.state.subscribers().active_clusters().await {
                let n = self.state.subscribers().count_of(cluster).await;
                self.metrics.set_subscribers(cluster, n as f64);
            }
        }
    }
}

/// Helper exposed for the (out-of-scope) integration harness: advance a member cursor.
pub fn ack_cursor(
    state: &IndexerState,
    cluster: Address,
    member: alloy::primitives::B256,
    c: Cursor,
) {
    let _ = state.cursors().advance(cluster, member, c);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_ranges_cover_exactly_once_with_partial_tail() {
        let ranges: Vec<_> = scan_ranges(100, 125, 10).collect();
        assert_eq!(ranges, vec![(100, 109), (110, 119), (120, 125)]);
    }

    #[test]
    fn scan_ranges_single_block_and_empty() {
        assert_eq!(scan_ranges(5, 5, 10).collect::<Vec<_>>(), vec![(5, 5)]);
        assert_eq!(
            scan_ranges(6, 5, 10).count(),
            0,
            "start past head scans nothing"
        );
    }

    #[test]
    fn scan_ranges_tolerate_zero_chunk() {
        // A misconfigured chunk must not loop forever on the same block.
        assert_eq!(
            scan_ranges(1, 3, 0).collect::<Vec<_>>(),
            vec![(1, 1), (2, 2), (3, 3)]
        );
    }

    /// Regression (live bug 8 in docs/deployment.md): with the factory-deploy floor
    /// applied, the catch-up workload is proportional to the factory's age — not the
    /// chain's. The live numbers: floor 46_868_742 on a ~47.1M head needed ~134
    /// batches at 2000; from genesis it would have been ~23.6k ranges of 2000.
    #[test]
    fn floor_bounds_the_catchup_workload() {
        let head = 47_136_575;
        let floor = 46_868_742;
        let floored = scan_ranges(events_scan_start(0, floor), head, 2_000).count();
        let genesis = scan_ranges(events_scan_start(0, 0), head, 2_000).count();
        assert_eq!(floored, 134);
        assert!(genesis > 23_000);
    }

    #[test]
    fn events_scan_start_resumes_past_cursor_but_not_below_floor() {
        assert_eq!(
            events_scan_start(0, 500),
            500,
            "fresh store starts at the floor"
        );
        assert_eq!(
            events_scan_start(700, 500),
            701,
            "persisted cursor wins past the floor"
        );
        assert_eq!(events_scan_start(u64::MAX, 0), u64::MAX, "no overflow");
    }

    #[test]
    fn discovery_retry_never_scans_before_factory_floor() {
        assert_eq!(discovery_scan_start(0, 46_868_742), 46_868_742);
        assert_eq!(discovery_scan_start(46_900_000, 46_868_742), 46_900_001);
    }

    #[tokio::test]
    async fn known_cluster_is_rehydrated_into_a_fresh_read_model() {
        let dir = tempfile::TempDir::new().unwrap();
        let cursors = Arc::new(
            crate::state::cursor::SledCursorStore::open(dir.path().to_str().unwrap()).unwrap(),
        );
        let state = IndexerState::new(cursors);
        let read_model = ReadModel::new();
        let cluster = Address::repeat_byte(0x42);
        state.add_cluster(cluster).await;

        let inserted = record_discovered_cluster(
            &state,
            &read_model,
            crate::query::ClusterDeployment {
                cluster,
                cluster_owner: Address::repeat_byte(0x11),
                salt: alloy::primitives::B256::repeat_byte(0x22),
                deployed_at_block: 123,
                deployment_tx_hash: alloy::primitives::B256::repeat_byte(0x33),
                deployment_log_index: 4,
            },
            MeshCidr {
                ip: u32::from_be_bytes([10, 18, 0, 0]),
                prefix: 16,
            },
        )
        .await;

        assert!(!inserted, "the durable cluster set was already populated");
        assert_eq!(read_model.snapshots(8453, None).await.len(), 1);
    }
}
