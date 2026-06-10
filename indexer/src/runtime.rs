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
use crate::state::cursor::Cursor;
use crate::state::IndexerState;
use alloy::primitives::Address;
use std::sync::Arc;
use std::time::Duration;

/// Shared handles passed to the loops.
#[derive(Clone)]
pub struct Runtime {
    pub config: Arc<Config>,
    pub provider: Arc<HttpProvider>,
    pub state: IndexerState,
    pub identity: Arc<Identity>,
    pub metrics: Arc<Metrics>,
    pub health: Arc<Health>,
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
        const DISCOVERY_CHUNK: u64 = 10_000;
        let mut from = floor;
        tracing::info!(floor, head, "boot catch-up: discovering clusters");
        while from <= head {
            let to = (from + DISCOVERY_CHUNK - 1).min(head);
            let new = watcher::poll_new_clusters(
                &self.provider,
                self.config.cluster_diamond_factory_addr,
                from,
                to,
            )
            .await?;
            for c in new {
                self.state.add_cluster(c).await;
            }
            from = to + 1;
        }
        self.state.set_last_factory_block(head).await;
        self.metrics
            .clusters_watched
            .set(self.state.cluster_count().await as i64);

        // Page cluster events forward BLOCK_BATCH_SIZE at a time until caught up.
        let clusters = self.state.known_clusters().await;
        let mut from = self
            .state
            .last_indexed_block()
            .await
            .saturating_add(1)
            .max(floor);
        let batch = self.config.block_batch_size.max(1);
        tracing::info!(clusters = clusters.len(), from, head, "boot catch-up: paging cluster events");
        let mut batches = 0u64;
        while from <= head && !clusters.is_empty() {
            let to = (from + batch - 1).min(head);
            let logs = watcher::poll_cluster_logs(&self.provider, &clusters, from, to).await?;
            self.ingest_for_cache(&logs).await;
            from = to + 1;
            batches += 1;
            if batches % 100 == 0 {
                tracing::info!(from, head, "boot catch-up progress");
            }
        }
        self.state.set_last_indexed_block(head).await;
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
            if let watcher::EventKind::MemberRegistered { member_id } = log.kind {
                // memberContract is the 2nd indexed topic of MemberRegistered.
                if let Some(contract_topic) = log.topics.get(2) {
                    let contract = Address::from_slice(&contract_topic.as_slice()[12..]);
                    self.state
                        .record_member(log.cluster_addr, member_id, contract, log.block_number)
                        .await;
                }
            }
        }
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
            let mut from = last + 1;
            while from <= head {
                let to = (from + batch - 1).min(head);
                let logs = watcher::poll_cluster_logs(&self.provider, &clusters, from, to).await?;
                self.ingest_for_cache(&logs).await;
                self.dispatch(&logs).await;
                from = to + 1;
            }
        }
        self.state.set_last_indexed_block(head).await;
        self.health.set_head_lag(0);
        Ok(())
    }

    /// Fan a chain-ordered batch of logs out to every relevant subscriber (spec §7.3).
    async fn dispatch(&self, logs: &[watcher::IndexedLog]) {
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
                if sub.try_send(env) {
                    self.metrics.inc_pushed(log.cluster_addr, log.kind.label());
                } else {
                    self.metrics.inc_dropped(log.cluster_addr, sub.member_id);
                    tracing::warn!(
                        cluster = %log.cluster_addr, member = %sub.member_id,
                        "subscriber channel full; dropped envelope (will replay on reconnect)"
                    );
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
        let from = self.state.last_factory_block().await.saturating_add(1);
        if from <= head {
            let new = watcher::poll_new_clusters(
                &self.provider,
                self.config.cluster_diamond_factory_addr,
                from,
                head,
            )
            .await?;
            for c in new {
                if self.state.add_cluster(c).await {
                    tracing::info!(cluster = %c, "discovered new cluster");
                }
            }
            self.state.set_last_factory_block(head).await;
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
