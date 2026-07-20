//! Node HTTP surface (sidecar spec §14). HTTP `/healthz` returns 200 once both
//! gates are open (first-converged AND csk-acquired), else 503 with the current
//! phase — this is what docker-compose's `healthcheck:` curls.
//!
//! Also exposes per-peer link transport (`tcp`/`punching`/`udp`) and the punch
//! counters (udp-transport-upgrade spec) on `/healthz` and, in Prometheus text
//! form, on `/metrics`. Transport state is observational only: punch outcomes
//! never gate health. Indexer connectivity and cursor progress are likewise
//! diagnostic only and never alter the two-gate health verdict.
//!
//! Finally, `GET /attestation` serves this member's public TEE attestation bundle
//! (dstack quote + identity + TCB) locally — the same node-local pattern TeeSQL
//! uses — so a UI or remote verifier can pull and verify each node directly rather
//! than trusting a central API. Only public material is returned (derived *public*
//! keys already published on chain, the guest-agent `/Info`, and the raw TEE quote);
//! no secret key material, CSK, or env values ever appear.

use crate::dstack::DstackRuntime;
use crate::state::Shared;
use axum::extract::{FromRef, State};
use axum::http::StatusCode;
use axum::routing::get;
use axum::{Json, Router};
use std::sync::Arc;

/// Axum state for the sidecar HTTP surface. Bundles the cross-cutting `Shared`
/// runtime state with the dstack runtime handle so `/attestation` can re-query the
/// guest agent for a fresh TEE quote. `FromRef` lets the pre-existing `/healthz`
/// and `/metrics` handlers keep extracting `State<Arc<Shared>>` unchanged.
#[derive(Clone)]
pub struct HttpState {
    pub shared: Arc<Shared>,
    pub dstack: Arc<dyn DstackRuntime>,
}

impl FromRef<HttpState> for Arc<Shared> {
    fn from_ref(state: &HttpState) -> Self {
        state.shared.clone()
    }
}

impl FromRef<HttpState> for Arc<dyn DstackRuntime> {
    fn from_ref(state: &HttpState) -> Self {
        state.dstack.clone()
    }
}

pub async fn serve(state: HttpState, addr: String) -> anyhow::Result<()> {
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/metrics", get(metrics))
        .route("/attestation", get(attestation))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    tracing::info!(%addr, "health server listening");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn healthz(State(shared): State<Arc<Shared>>) -> (StatusCode, Json<serde_json::Value>) {
    let phase = shared.current_phase().await;
    let indexer = shared.get_indexer_status().await;
    let first_converged = shared.gates.first_converged();
    let csk_acquired = shared.gates.csk_acquired();
    let healthy = first_converged && csk_acquired;
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
        "first_converged": first_converged,
        "csk_acquired": csk_acquired,
        "live_peers": live_peers,
        "transports": transports,
        "punch": {
            "punch_attempts_total": attempts,
            "punch_success_total": success,
            "udp_reverts_total": reverts,
        },
        "indexer_connected": indexer.connected,
        "indexer_caught_up": indexer.caught_up,
        "indexer_cursor_block": indexer.cursor_block,
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
    let peer_transports: Vec<([u8; 32], &'static str)> = {
        let peers = shared.peers.lock().await;
        peers
            .all()
            .map(|peer| (peer.member_id, peer.transport.as_str()))
            .collect()
    };
    for (member_id, transport) in peer_transports {
        let _ = writeln!(
            out,
            "attestmesh_peer_transport{{member_id=\"{}\",transport=\"{}\"}} 1",
            hex::encode(member_id),
            transport
        );
    }
    out
}

/// Attestor label for this member's attestation method. Mirrors the on-chain
/// attestor id (`state::DSTACK_ATTESTOR_ID`) and the mesh-state-api display label:
/// the generic HTTP layer stays method-agnostic and names the method, while the
/// method-specific bytes come only through the `DstackRuntime` provider.
const ATTESTOR_LABEL: &str = "dstack";

/// Recipe (also emitted in the response) for the 64-byte quote `report_data`, so a
/// remote verifier can independently recompute it from the public fields we return
/// and confirm the TEE quote is bound to *this* member's identity:
/// `report_data[..32] = keccak256(cluster ‖ memberContract ‖ xPub ‖ wgPub ‖ ed25519Pub)`,
/// `report_data[32..] = 0`.
const REPORT_DATA_BINDING: &str =
    "keccak256(cluster||memberContract||xPubKey||wgPubKey||ed25519PubKey) in report_data[0..32]";

fn attestation_report_data(shared: &Shared) -> [u8; 64] {
    use alloy::primitives::keccak256;
    let mut preimage = Vec::with_capacity(20 + 20 + 32 + 32 + 32);
    preimage.extend_from_slice(shared.cluster.as_slice());
    preimage.extend_from_slice(shared.member_contract.as_slice());
    preimage.extend_from_slice(&shared.keys.x_pub);
    preimage.extend_from_slice(&shared.keys.wg_pub);
    preimage.extend_from_slice(&shared.keys.ed25519_pub);
    let digest = keccak256(&preimage);
    let mut report_data = [0u8; 64];
    report_data[..32].copy_from_slice(digest.as_slice());
    report_data
}

/// `codeId = bytes32(bytes20(app_id))`: the 20-byte dstack app_id left-aligned,
/// upper 12 bytes zero — the same value `chain::dstack_facet::build_kms_material`
/// puts on chain and that the cluster boot gate allowlists.
fn code_id_from_app_id(app_id: &[u8]) -> [u8; 32] {
    let mut code_id = [0u8; 32];
    let n = app_id.len().min(20);
    code_id[..n].copy_from_slice(&app_id[..n]);
    code_id
}

/// `GET /attestation` — this member's public TEE attestation bundle, served
/// node-locally (TeeSQL pattern). Returns `200 OK` with the full bundle when a
/// fresh quote is obtained, or `503 Service Unavailable` with `available:false`
/// and an `errors` map when the dstack guest agent is unreachable. Never returns
/// secret material: only *public* keys (already published on chain), the public
/// `/Info`, and the raw TEE quote whose `report_data` binds them together.
async fn attestation(
    State(shared): State<Arc<Shared>>,
    State(dstack): State<Arc<dyn DstackRuntime>>,
) -> (StatusCode, Json<serde_json::Value>) {
    let phase = shared.current_phase().await;
    let report_data = attestation_report_data(&shared);

    // Local, always-available public identity. These are the same values published
    // on chain during registration (xPubKey/wgPubKey) and via publishEd25519Key.
    let identity = serde_json::json!({
        "x_pub": format!("0x{}", hex::encode(shared.keys.x_pub)),
        "ed25519_pub": format!("0x{}", hex::encode(shared.keys.ed25519_pub)),
        "wg_pub": format!("0x{}", hex::encode(shared.keys.wg_pub)),
    });

    let mut body = serde_json::json!({
        "attestor": ATTESTOR_LABEL,
        "member_id": format!("0x{}", hex::encode(shared.self_member_id)),
        "member_contract": shared.member_contract.to_string(),
        "cluster": shared.cluster.to_string(),
        "mesh_ip": crate::wg::cidr::fmt_ipv4(shared.self_mesh_ip),
        "phase": phase.as_str(),
        "identity": identity,
        "report_data": format!("0x{}", hex::encode(report_data)),
        "report_data_binding": REPORT_DATA_BINDING,
    });

    let mut errors = serde_json::Map::new();

    // dstack `/Info`: app_id, compose_hash, instance/device id, TCB status.
    match dstack.info().await {
        Ok(info) => {
            body["dstack"] = serde_json::json!({
                "app_id": format!("0x{}", hex::encode(&info.app_id)),
                "compose_hash": format!("0x{}", hex::encode(&info.compose_hash)),
                "instance_id": format!("0x{}", hex::encode(&info.instance_id)),
                "device_id": format!("0x{}", hex::encode(&info.device_id)),
                "tcb_status": info.tcb_status,
            });
            body["code_id"] =
                serde_json::json!(format!("0x{}", hex::encode(code_id_from_app_id(&info.app_id))));
        }
        Err(e) => {
            errors.insert("info".to_string(), serde_json::json!(e.to_string()));
        }
    }

    // dstack `/GetQuote`: the raw TEE-signed attestation blob over `report_data`.
    match dstack.get_quote(report_data).await {
        Ok(quote) => {
            body["quote"] = serde_json::json!({
                "provider": ATTESTOR_LABEL,
                "format": "raw",
                "len": quote.len(),
                "bytes": format!("0x{}", hex::encode(&quote)),
            });
        }
        Err(e) => {
            errors.insert("quote".to_string(), serde_json::json!(e.to_string()));
        }
    }

    let available = errors.is_empty();
    body["available"] = serde_json::json!(available);
    if !available {
        body["errors"] = serde_json::Value::Object(errors);
    }

    let code = if available {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (code, Json(body))
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

    /// Transport, punch, and Indexer diagnostics surface without influencing the
    /// convergence-plus-CSK health verdict.
    #[tokio::test]
    async fn healthz_and_metrics_expose_diagnostics_without_gating() {
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
        shared.set_indexer_connected(true).await;
        shared.set_indexer_progress(1234, 56, true).await;

        let (code, Json(body)) = healthz(State(shared.clone())).await;
        // Gates are closed: neither UDP state nor a caught-up Indexer makes the
        // node healthy.
        assert_eq!(code, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body["transports"][hex::encode(id)], "udp");
        assert_eq!(body["punch"]["punch_attempts_total"], 3);
        assert_eq!(body["punch"]["punch_success_total"], 1);
        assert_eq!(body["punch"]["udp_reverts_total"], 2);
        assert_eq!(body["indexer_connected"], true);
        assert_eq!(body["indexer_caught_up"], true);
        assert_eq!(body["indexer_cursor_block"], 1234);

        // Conversely, an Indexer outage is diagnostic and must not restart an
        // application after the convergence and CSK gates have opened.
        shared.gates.latch_first_converged();
        shared.gates.set_csk_acquired();
        shared.set_indexer_connected(false).await;
        let (code, Json(body)) = healthz(State(shared.clone())).await;
        assert_eq!(code, StatusCode::OK);
        assert_eq!(body["indexer_connected"], false);

        let text = metrics(State(shared)).await;
        assert!(text.contains("attestmesh_punch_attempts_total 3"));
        assert!(text.contains("attestmesh_punch_success_total 1"));
        assert!(text.contains("attestmesh_udp_reverts_total 2"));
        assert!(text.contains(&format!(
            "attestmesh_peer_transport{{member_id=\"{}\",transport=\"udp\"}} 1",
            hex::encode(id)
        )));
    }

    /// A dstack runtime whose guest-agent calls always fail — models the socket
    /// being unavailable so `/attestation` must report `available:false` + 503
    /// rather than fabricating a quote.
    struct FailingDstack;

    #[async_trait::async_trait]
    impl DstackRuntime for FailingDstack {
        async fn derive_key(
            &self,
            _: &str,
            _: &str,
        ) -> anyhow::Result<zeroize::Zeroizing<[u8; 32]>> {
            anyhow::bail!("dstack socket unavailable")
        }
        async fn get_quote(&self, _: [u8; 64]) -> anyhow::Result<Vec<u8>> {
            anyhow::bail!("dstack socket unavailable")
        }
        async fn get_key(&self, _: &str, _: &str) -> anyhow::Result<crate::dstack::DstackKey> {
            anyhow::bail!("dstack socket unavailable")
        }
        async fn info(&self) -> anyhow::Result<crate::dstack::DstackInfo> {
            anyhow::bail!("dstack socket unavailable")
        }
    }

    /// Happy path: `/attestation` returns 200 with the identity, dstack `/Info`
    /// fields, a bound quote, and a verifier-recomputable `report_data` — and never
    /// leaks secret key material.
    #[tokio::test]
    async fn attestation_bundle_is_public_bound_and_available() {
        let shared = shared().await;
        let dstack: Arc<dyn DstackRuntime> = Arc::new(MockDstack::from_label("health-test"));

        let (code, Json(body)) = attestation(State(shared.clone()), State(dstack.clone())).await;
        assert_eq!(code, StatusCode::OK);
        assert_eq!(body["available"], true);
        assert_eq!(body["attestor"], "dstack");

        // Identity is the public keys, hex-0x encoded.
        assert_eq!(
            body["identity"]["x_pub"],
            format!("0x{}", hex::encode(shared.keys.x_pub))
        );
        assert_eq!(
            body["identity"]["wg_pub"],
            format!("0x{}", hex::encode(shared.keys.wg_pub))
        );
        assert_eq!(
            body["identity"]["ed25519_pub"],
            format!("0x{}", hex::encode(shared.keys.ed25519_pub))
        );

        // dstack /Info surfaced; code_id is bytes20(app_id) left-aligned.
        let info = dstack.info().await.unwrap();
        assert_eq!(
            body["dstack"]["app_id"],
            format!("0x{}", hex::encode(&info.app_id))
        );
        assert_eq!(body["dstack"]["tcb_status"], info.tcb_status);
        assert_eq!(
            body["code_id"],
            format!("0x{}", hex::encode(code_id_from_app_id(&info.app_id)))
        );

        // report_data is recomputable by a verifier from the public fields.
        let expected_rd = attestation_report_data(&shared);
        assert_eq!(
            body["report_data"],
            format!("0x{}", hex::encode(expected_rd))
        );

        // The quote is present and actually commits to that report_data (the mock
        // quote embeds report_data so the binding is checkable).
        let quote = dstack.get_quote(expected_rd).await.unwrap();
        assert_eq!(body["quote"]["len"], quote.len());
        assert_eq!(
            body["quote"]["bytes"],
            format!("0x{}", hex::encode(&quote))
        );

        // No-secrets invariant: the serialized bundle must not contain any secret
        // key material.
        let serialized = serde_json::to_string(&body).unwrap();
        for secret in [
            hex::encode(shared.keys.x_secret.to_bytes()),
            hex::encode(shared.keys.wg_secret.to_bytes()),
            hex::encode(shared.keys.ed_signing.to_bytes()),
            hex::encode(*shared.keys.binding_seed),
        ] {
            assert!(
                !serialized.contains(&secret),
                "attestation bundle leaked secret key material"
            );
        }
    }

    /// Failure path: the guest agent is down, so `/attestation` returns 503 with
    /// `available:false` and an `errors` map — and never a fabricated quote.
    #[tokio::test]
    async fn attestation_reports_unavailable_when_dstack_down() {
        let shared = shared().await;
        let dstack: Arc<dyn DstackRuntime> = Arc::new(FailingDstack);

        let (code, Json(body)) = attestation(State(shared.clone()), State(dstack)).await;
        assert_eq!(code, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body["available"], false);
        assert!(body["errors"]["quote"].is_string());
        assert!(body["errors"]["info"].is_string());
        // No quote fabricated on the failure path.
        assert!(body.get("quote").is_none());
        // Public identity is still present (it needs no guest agent).
        assert_eq!(
            body["identity"]["x_pub"],
            format!("0x{}", hex::encode(shared.keys.x_pub))
        );
    }
}
