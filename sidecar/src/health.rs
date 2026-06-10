//! Healthcheck surface (sidecar spec §14). HTTP `/healthz` returns 200 once both
//! gates are open (first-converged AND csk-acquired), else 503 with the current
//! phase — this is what docker-compose's `healthcheck:` curls.
//!
//! Also exposes per-peer link transport (`tcp`/`punching`/`udp`) and the punch
//! counters (udp-transport-upgrade spec) on `/healthz` and, in Prometheus text
//! form, on `/metrics`. Transport state is observational only: punch outcomes
//! never gate health.

use crate::state::Shared;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::get;
use axum::{Json, Router};
use std::sync::Arc;

pub async fn serve(shared: Arc<Shared>, addr: String) -> anyhow::Result<()> {
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/metrics", get(metrics))
        .with_state(shared);
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    tracing::info!(%addr, "health server listening");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn healthz(State(shared): State<Arc<Shared>>) -> (StatusCode, Json<serde_json::Value>) {
    let phase = shared.current_phase().await;
    let healthy = shared.gates.healthy();
    let (live_peers, transports) = {
        let peers = shared.peers.lock().await;
        let transports: serde_json::Map<String, serde_json::Value> = peers
            .all()
            .map(|p| {
                (
                    hex::encode(p.member_id),
                    serde_json::Value::String(p.transport.as_str().to_string()),
                )
            })
            .collect();
        (peers.live_count(), transports)
    };
    let (attempts, success, reverts) = shared.punch_metrics.snapshot();
    let body = serde_json::json!({
        "phase": phase.as_str(),
        "first_converged": shared.gates.first_converged(),
        "csk_acquired": shared.gates.csk_acquired(),
        "live_peers": live_peers,
        "transports": transports,
        "punch": {
            "punch_attempts_total": attempts,
            "punch_success_total": success,
            "udp_reverts_total": reverts,
        },
    });
    let code = if healthy {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (code, Json(body))
}

async fn metrics(State(shared): State<Arc<Shared>>) -> String {
    use std::fmt::Write;
    let (attempts, success, reverts) = shared.punch_metrics.snapshot();
    let mut out = String::new();
    let _ = writeln!(out, "# TYPE attestmesh_punch_attempts_total counter");
    let _ = writeln!(out, "attestmesh_punch_attempts_total {attempts}");
    let _ = writeln!(out, "# TYPE attestmesh_punch_success_total counter");
    let _ = writeln!(out, "attestmesh_punch_success_total {success}");
    let _ = writeln!(out, "# TYPE attestmesh_udp_reverts_total counter");
    let _ = writeln!(out, "attestmesh_udp_reverts_total {reverts}");
    let _ = writeln!(out, "# TYPE attestmesh_peer_transport gauge");
    let peers = shared.peers.lock().await;
    for p in peers.all() {
        let _ = writeln!(
            out,
            "attestmesh_peer_transport{{member_id=\"{}\",transport=\"{}\"}} 1",
            hex::encode(p.member_id),
            p.transport.as_str()
        );
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dstack::MockDstack;
    use crate::wg::peer::LinkTransport;
    use alloy::primitives::Address;

    async fn shared() -> Arc<Shared> {
        let dstack = MockDstack::from_label("health-test");
        let keys = Arc::new(crate::keys::derive_all(&dstack).await.unwrap());
        Shared::new(
            keys,
            Address::repeat_byte(0x11),
            Address::repeat_byte(0x22),
            0x0a0d0000,
            16,
            51821,
        )
    }

    /// Per-peer transport + punch counters surface on /healthz and /metrics
    /// without ever influencing the health verdict itself.
    #[tokio::test]
    async fn healthz_and_metrics_expose_transport_state() {
        use std::sync::atomic::Ordering::Relaxed;
        let shared = shared().await;
        let id = [7u8; 32];
        {
            let mut peers = shared.peers.lock().await;
            peers.ensure_chain(id, 0x0a0d0002, [2u8; 32]);
            peers.set_transport(
                &id,
                LinkTransport::Udp {
                    endpoint: "203.0.113.7:51821".parse().unwrap(),
                },
            );
        }
        shared.punch_metrics.attempts_total.fetch_add(3, Relaxed);
        shared.punch_metrics.success_total.fetch_add(1, Relaxed);
        shared.punch_metrics.udp_reverts_total.fetch_add(2, Relaxed);

        let (code, Json(body)) = healthz(State(shared.clone())).await;
        // gates are closed: a UDP-latched link must not make the node healthy
        assert_eq!(code, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body["transports"][hex::encode(id)], "udp");
        assert_eq!(body["punch"]["punch_attempts_total"], 3);
        assert_eq!(body["punch"]["punch_success_total"], 1);
        assert_eq!(body["punch"]["udp_reverts_total"], 2);

        let text = metrics(State(shared)).await;
        assert!(text.contains("attestmesh_punch_attempts_total 3"));
        assert!(text.contains("attestmesh_punch_success_total 1"));
        assert!(text.contains("attestmesh_udp_reverts_total 2"));
        assert!(text.contains(&format!(
            "attestmesh_peer_transport{{member_id=\"{}\",transport=\"udp\"}} 1",
            hex::encode(id)
        )));
    }
}
