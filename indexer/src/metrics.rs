//! Prometheus metrics (spec §12).

use alloy::primitives::{Address, B256};
use prometheus::{
    register_gauge_vec_with_registry, register_int_counter_vec_with_registry,
    register_int_gauge_with_registry, Encoder, GaugeVec, IntCounterVec, IntGauge, Registry,
    TextEncoder,
};
use std::time::Instant;

/// All indexer metrics, plus the registry they live in.
pub struct Metrics {
    registry: Registry,
    start: Instant,

    pub chain_head_lag_blocks: IntGauge,
    pub clusters_watched: IntGauge,
    pub subscribers: GaugeVec,         // {cluster}
    pub events_pushed: IntCounterVec,  // {cluster,event}
    pub events_dropped: IntCounterVec, // {cluster,member}
    pub acks_received: IntCounterVec,  // {cluster,member}
    pub rpc_errors: IntCounterVec,     // {kind}
}

impl Metrics {
    pub fn new() -> Self {
        let registry = Registry::new();
        let chain_head_lag_blocks = register_int_gauge_with_registry!(
            "indexer_chain_head_lag_blocks",
            "Blocks between chain head and last indexed block",
            registry
        )
        .unwrap();
        let clusters_watched = register_int_gauge_with_registry!(
            "indexer_clusters_watched_total",
            "Number of cluster diamonds being watched",
            registry
        )
        .unwrap();
        let subscribers = register_gauge_vec_with_registry!(
            "indexer_subscribers_total",
            "Live subscribers per cluster",
            &["cluster"],
            registry
        )
        .unwrap();
        let events_pushed = register_int_counter_vec_with_registry!(
            "indexer_events_pushed_total",
            "Push envelopes delivered per cluster/event",
            &["cluster", "event"],
            registry
        )
        .unwrap();
        let events_dropped = register_int_counter_vec_with_registry!(
            "indexer_events_dropped_total",
            "Push envelopes dropped due to backpressure per cluster/member",
            &["cluster", "member"],
            registry
        )
        .unwrap();
        let acks_received = register_int_counter_vec_with_registry!(
            "indexer_acks_received_total",
            "Acks received per cluster/member",
            &["cluster", "member"],
            registry
        )
        .unwrap();
        let rpc_errors = register_int_counter_vec_with_registry!(
            "indexer_rpc_errors_total",
            "RPC errors by kind",
            &["kind"],
            registry
        )
        .unwrap();

        Self {
            registry,
            start: Instant::now(),
            chain_head_lag_blocks,
            clusters_watched,
            subscribers,
            events_pushed,
            events_dropped,
            acks_received,
            rpc_errors,
        }
    }

    pub fn uptime_seconds(&self) -> u64 {
        self.start.elapsed().as_secs()
    }

    /// Render the Prometheus exposition format, appending the uptime gauge (computed
    /// live each scrape rather than stored).
    pub fn render(&self) -> String {
        let mut buf = Vec::new();
        let encoder = TextEncoder::new();
        let families = self.registry.gather();
        let _ = encoder.encode(&families, &mut buf);
        let mut out = String::from_utf8(buf).unwrap_or_default();
        out.push_str(&format!(
            "# HELP indexer_uptime_seconds Process uptime in seconds\n# TYPE indexer_uptime_seconds gauge\nindexer_uptime_seconds {}\n",
            self.uptime_seconds()
        ));
        out
    }

    // ── Convenience setters using domain types ─────────────────────────────────

    pub fn inc_pushed(&self, cluster: Address, event: &str) {
        self.events_pushed
            .with_label_values(&[&fmt_addr(cluster), event])
            .inc();
    }

    pub fn inc_dropped(&self, cluster: Address, member: B256) {
        self.events_dropped
            .with_label_values(&[&fmt_addr(cluster), &fmt_b256(member)])
            .inc();
    }

    pub fn inc_ack(&self, cluster: Address, member: B256) {
        self.acks_received
            .with_label_values(&[&fmt_addr(cluster), &fmt_b256(member)])
            .inc();
    }

    pub fn inc_rpc_error(&self, kind: &str) {
        self.rpc_errors.with_label_values(&[kind]).inc();
    }

    pub fn set_subscribers(&self, cluster: Address, n: f64) {
        self.subscribers
            .with_label_values(&[&fmt_addr(cluster)])
            .set(n);
    }
}

impl Default for Metrics {
    fn default() -> Self {
        Self::new()
    }
}

fn fmt_addr(a: Address) -> String {
    format!("0x{}", hex::encode(a.as_slice()))
}

fn fmt_b256(b: B256) -> String {
    format!("0x{}", hex::encode(b.as_slice()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_includes_uptime_and_counters() {
        let m = Metrics::new();
        m.inc_pushed(Address::repeat_byte(0xc1), "MemberRegistered");
        m.set_subscribers(Address::repeat_byte(0xc1), 2.0);
        let out = m.render();
        assert!(out.contains("indexer_uptime_seconds"));
        assert!(out.contains("indexer_events_pushed_total"));
        assert!(out.contains("MemberRegistered"));
    }
}
