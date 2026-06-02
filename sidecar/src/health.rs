//! Healthcheck surface (sidecar spec §14). HTTP `/healthz` returns 200 once both
//! gates are open (first-converged AND csk-acquired), else 503 with the current
//! phase — this is what docker-compose's `healthcheck:` curls.

use crate::state::Shared;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::get;
use axum::{Json, Router};
use std::sync::Arc;

pub async fn serve(shared: Arc<Shared>, addr: String) -> anyhow::Result<()> {
    let app = Router::new()
        .route("/healthz", get(healthz))
        .with_state(shared);
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    tracing::info!(%addr, "health server listening");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn healthz(State(shared): State<Arc<Shared>>) -> (StatusCode, Json<serde_json::Value>) {
    let phase = shared.current_phase().await;
    let healthy = shared.gates.healthy();
    let body = serde_json::json!({
        "phase": phase.as_str(),
        "first_converged": shared.gates.first_converged(),
        "csk_acquired": shared.gates.csk_acquired(),
        "live_peers": shared.peers.lock().await.live_count(),
    });
    let code = if healthy {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (code, Json(body))
}
