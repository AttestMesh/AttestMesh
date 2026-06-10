//! Health + metrics HTTP endpoint (spec §12).
//!
//! `GET /healthz` → 200 when (chain head lag < 10) AND (gRPC accepting) AND (RPC
//! reachable); else 503 with a JSON body naming the failed check.
//! `GET /metrics` → Prometheus exposition.

use crate::metrics::Metrics;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde_json::json;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

/// Max acceptable head lag before `/healthz` flips to 503 (spec §12).
pub const MAX_HEAD_LAG_BLOCKS: u64 = 10;

/// Liveness signals updated by the runtime loops.
pub struct Health {
    chain_head_lag: AtomicU64,
    grpc_accepting: AtomicBool,
    rpc_reachable: AtomicBool,
    metrics: Arc<Metrics>,
}

impl Health {
    pub fn new(metrics: Arc<Metrics>) -> Self {
        Self {
            chain_head_lag: AtomicU64::new(0),
            grpc_accepting: AtomicBool::new(false),
            rpc_reachable: AtomicBool::new(false),
            metrics,
        }
    }

    pub fn set_head_lag(&self, lag: u64) {
        self.chain_head_lag.store(lag, Ordering::Relaxed);
        self.metrics.chain_head_lag_blocks.set(lag as i64);
    }

    pub fn set_grpc_accepting(&self, up: bool) {
        self.grpc_accepting.store(up, Ordering::Relaxed);
    }

    pub fn set_rpc_reachable(&self, up: bool) {
        self.rpc_reachable.store(up, Ordering::Relaxed);
    }

    /// Evaluate the three checks; `Ok` if all pass, `Err(reason)` otherwise.
    pub fn evaluate(&self) -> Result<(), String> {
        let lag = self.chain_head_lag.load(Ordering::Relaxed);
        if lag >= MAX_HEAD_LAG_BLOCKS {
            return Err(format!("chain head lag {lag} >= {MAX_HEAD_LAG_BLOCKS}"));
        }
        if !self.grpc_accepting.load(Ordering::Relaxed) {
            return Err("gRPC listener not accepting".into());
        }
        if !self.rpc_reachable.load(Ordering::Relaxed) {
            return Err("RPC unreachable".into());
        }
        Ok(())
    }
}

#[derive(Clone)]
struct HttpState {
    health: Arc<Health>,
    metrics: Arc<Metrics>,
}

/// Serve `/healthz` + `/metrics` until the process exits or the future is dropped.
pub async fn serve(
    addr: SocketAddr,
    health: Arc<Health>,
    metrics: Arc<Metrics>,
) -> anyhow::Result<()> {
    let state = HttpState { health, metrics };
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/metrics", get(metrics_handler))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!(%addr, "health/metrics endpoint up");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn healthz(State(s): State<HttpState>) -> Response {
    match s.health.evaluate() {
        Ok(()) => (StatusCode::OK, Json(json!({ "status": "ok" }))).into_response(),
        Err(reason) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "status": "unhealthy", "reason": reason })),
        )
            .into_response(),
    }
}

async fn metrics_handler(State(s): State<HttpState>) -> Response {
    (StatusCode::OK, s.metrics.render()).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unhealthy_until_all_signals_set() {
        let m = Arc::new(Metrics::new());
        let h = Health::new(m);
        // Initially: grpc down, rpc down → unhealthy.
        assert!(h.evaluate().is_err());

        h.set_grpc_accepting(true);
        h.set_rpc_reachable(true);
        h.set_head_lag(0);
        assert!(h.evaluate().is_ok());

        // Head lag past threshold flips back to unhealthy.
        h.set_head_lag(MAX_HEAD_LAG_BLOCKS);
        assert!(h.evaluate().is_err());
    }
}
