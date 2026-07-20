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

use crate::dstack::{DstackInfo, DstackRuntime};
use crate::state::Shared;
use axum::extract::{FromRef, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::routing::get;
use axum::{Json, Router};
use std::collections::HashMap;
use std::sync::Arc;

/// Upper bound on the verifier-supplied `/attestation?nonce=` challenge (raw bytes
/// after hex-decoding). 32 bytes is the natural challenge size; we accept up to 64
/// so callers can pass a slightly larger opaque token, and reject anything longer
/// to keep the request cheap and bounded.
const MAX_NONCE_BYTES: usize = 64;

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

fn app(state: HttpState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/metrics", get(metrics))
        .route(
            "/attestation",
            get(attestation).options(attestation_preflight),
        )
        .with_state(state)
}

pub async fn serve(state: HttpState, addr: String) -> anyhow::Result<()> {
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    tracing::info!(%addr, "health server listening");
    axum::serve(listener, app(state)).await?;
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
/// and confirm the TEE quote is bound to *this* member's identity **and** to their
/// own freshness challenge:
/// `report_data[..32] = keccak256(cluster ‖ memberContract ‖ xPub ‖ wgPub ‖ ed25519Pub)`,
/// `report_data[32..] = keccak256(nonce)` when a `?nonce=` challenge is supplied,
/// else all-zero.
const REPORT_DATA_BINDING: &str =
    "report_data[0..32]=keccak256(cluster||memberContract||xPubKey||wgPubKey||ed25519PubKey); report_data[32..64]=keccak256(nonce) if a ?nonce= challenge is supplied, else 0";

/// Identity half of `report_data` (bytes `[0..32]`): binds the quote to this
/// member's on-chain identity. Constant across requests — this is what a verifier
/// recomputes to confirm the quote is *this* node's, not the freshness input.
fn attestation_identity_binding(shared: &Shared) -> [u8; 32] {
    use alloy::primitives::keccak256;
    let mut preimage = Vec::with_capacity(20 + 20 + 32 + 32 + 32);
    preimage.extend_from_slice(shared.cluster.as_slice());
    preimage.extend_from_slice(shared.member_contract.as_slice());
    preimage.extend_from_slice(&shared.keys.x_pub);
    preimage.extend_from_slice(&shared.keys.wg_pub);
    preimage.extend_from_slice(&shared.keys.ed25519_pub);
    *keccak256(&preimage)
}

/// Assemble the 64-byte quote `report_data`. The identity binding fills `[0..32]`;
/// the optional verifier freshness challenge fills `[32..64]` as `keccak256(nonce)`
/// (hashing lets any 1..=MAX_NONCE_BYTES challenge map cleanly to 32 bytes). With no
/// challenge the upper half is zero, matching the pre-nonce identity-only binding.
fn attestation_report_data(shared: &Shared, nonce: Option<&[u8]>) -> [u8; 64] {
    use alloy::primitives::keccak256;
    let mut report_data = [0u8; 64];
    report_data[..32].copy_from_slice(&attestation_identity_binding(shared));
    if let Some(nonce) = nonce {
        report_data[32..].copy_from_slice(keccak256(nonce).as_slice());
    }
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

/// Validate a `DstackInfo` before it is allowed to count as successful evidence.
///
/// Issue [P2]: a lenient `/Info` client (or a malformed guest-agent response) can
/// yield a `DstackInfo` whose fields are empty/zero — which would otherwise be
/// serialized as a "successful" bundle with blank identifiers and an all-zero
/// `code_id`. We reject that here so the handler falls through to `errors.info` +
/// 503 instead of publishing hollow attestation evidence. `app_id` must be exactly
/// the 20-byte contract address (`code_id = bytes20(app_id)` depends on it); the
/// other identifiers must have their canonical lengths.
fn require_info_field_len(name: &str, actual: usize, expected: usize) -> Result<(), String> {
    if actual != expected {
        return Err(format!(
            "/Info {name} is {actual} bytes, expected {expected} bytes"
        ));
    }
    Ok(())
}

fn validate_dstack_info(info: &DstackInfo) -> Result<(), String> {
    require_info_field_len("app_id", info.app_id.len(), 20)?;
    require_info_field_len("compose_hash", info.compose_hash.len(), 32)?;
    require_info_field_len("instance_id", info.instance_id.len(), 20)?;
    require_info_field_len("device_id", info.device_id.len(), 32)?;
    Ok(())
}

/// Byte layout of an Intel TDX ECDSA quote v4. The quote header is 48 bytes and
/// is followed by the 584-byte TDREPORT body described by Intel's quote ABI.
/// Measurements are decoded only for the TDX TEE type; the raw quote remains the
/// canonical evidence a verifier must cryptographically validate.
const TDX_TEE_TYPE: u32 = 0x0000_0081;
const TDX_QUOTE_V4_HEADER_LEN: usize = 48;
const TDX_REPORT_BODY_LEN: usize = 584;

fn quote_hex(bytes: &[u8]) -> String {
    format!("0x{}", hex::encode(bytes))
}

/// Decode the signed report body from a TDX quote and prove that its embedded
/// REPORTDATA is the value requested from `/GetQuote`. This is structural parsing,
/// not DCAP verification; callers still verify the returned raw quote and Intel
/// certificate chain independently.
fn decode_tdx_quote(
    quote: &[u8],
    expected_report_data: &[u8; 64],
) -> Result<serde_json::Value, String> {
    let min_len = TDX_QUOTE_V4_HEADER_LEN + TDX_REPORT_BODY_LEN;
    if quote.len() < min_len {
        return Err(format!(
            "TDX quote is {} bytes, expected at least {min_len} bytes",
            quote.len()
        ));
    }

    let version = u16::from_le_bytes([quote[0], quote[1]]);
    if version != 4 {
        return Err(format!("unsupported TDX quote version {version}"));
    }
    let attestation_key_type = u16::from_le_bytes([quote[2], quote[3]]);
    let tee_type = u32::from_le_bytes(quote[4..8].try_into().unwrap());
    if tee_type != TDX_TEE_TYPE {
        return Err(format!("quote TEE type 0x{tee_type:08x} is not Intel TDX"));
    }

    let body = &quote[TDX_QUOTE_V4_HEADER_LEN..min_len];
    let embedded_report_data = &body[520..584];
    if embedded_report_data != expected_report_data {
        return Err("TDX quote REPORTDATA does not match requested report_data".to_string());
    }

    let td_attributes = u64::from_le_bytes(body[120..128].try_into().unwrap());
    Ok(serde_json::json!({
        "header": {
            "version": version,
            "attestation_key_type": attestation_key_type,
            "tee_type": "tdx",
            "tee_type_raw": format!("0x{tee_type:08x}"),
            "qe_svn": u16::from_le_bytes([quote[8], quote[9]]),
            "pce_svn": u16::from_le_bytes([quote[10], quote[11]]),
            "qe_vendor_id": quote_hex(&quote[12..28]),
            "user_data": quote_hex(&quote[28..48]),
        },
        "measurements": {
            "tee_tcb_svn": quote_hex(&body[0..16]),
            "mr_seam": quote_hex(&body[16..64]),
            "mr_signer_seam": quote_hex(&body[64..112]),
            "seam_attributes": quote_hex(&body[112..120]),
            "td_attributes": quote_hex(&body[120..128]),
            "debug": td_attributes & 1 != 0,
            "xfam": quote_hex(&body[128..136]),
            "mrtd": quote_hex(&body[136..184]),
            "mr_config_id": quote_hex(&body[184..232]),
            "mr_owner": quote_hex(&body[232..280]),
            "mr_owner_config": quote_hex(&body[280..328]),
            "rtmr0": quote_hex(&body[328..376]),
            "rtmr1": quote_hex(&body[376..424]),
            "rtmr2": quote_hex(&body[424..472]),
            "rtmr3": quote_hex(&body[472..520]),
        },
    }))
}

/// Parse the optional `?nonce=` freshness challenge. Accepts hex (with or without a
/// `0x` prefix). Returns:
/// - `Ok(None)` when absent (identity-only binding, `fresh:false`),
/// - `Ok(Some(bytes))` for a well-formed 1..=`MAX_NONCE_BYTES` challenge,
/// - `Err(msg)` for a malformed/empty/oversized nonce — the caller turns this into a
///   `400`, never a silent `200`, so a verifier is never misled into thinking they
///   got a fresh, challenge-bound quote when they did not.
fn parse_nonce(params: &HashMap<String, String>) -> Result<Option<Vec<u8>>, String> {
    let Some(raw) = params.get("nonce") else {
        return Ok(None);
    };
    let raw = raw.trim();
    if raw.is_empty() {
        return Err("nonce query parameter is empty".to_string());
    }
    let bytes =
        hex::decode(raw.trim_start_matches("0x")).map_err(|e| format!("nonce must be hex: {e}"))?;
    if bytes.is_empty() {
        return Err("nonce decoded to zero bytes".to_string());
    }
    if bytes.len() > MAX_NONCE_BYTES {
        return Err(format!(
            "nonce is {} bytes, max {MAX_NONCE_BYTES}",
            bytes.len()
        ));
    }
    Ok(Some(bytes))
}

/// CORS headers for the public `/attestation` endpoint. The bundle is public by
/// design and carries only public material, so an open read policy is correct and
/// lets browser UIs on any origin fetch and verify a node directly. Scoped to this
/// endpoint's responses only (see `attestation` / `attestation_preflight`); the
/// method-agnostic `/healthz` and `/metrics` surfaces are unchanged.
fn apply_attestation_cors(headers: &mut HeaderMap) {
    use axum::http::header::{
        ACCESS_CONTROL_ALLOW_HEADERS, ACCESS_CONTROL_ALLOW_METHODS, ACCESS_CONTROL_ALLOW_ORIGIN,
        ACCESS_CONTROL_MAX_AGE,
    };
    headers.insert(ACCESS_CONTROL_ALLOW_ORIGIN, "*".parse().unwrap());
    headers.insert(
        ACCESS_CONTROL_ALLOW_METHODS,
        "GET, OPTIONS".parse().unwrap(),
    );
    headers.insert(ACCESS_CONTROL_ALLOW_HEADERS, "*".parse().unwrap());
    headers.insert(ACCESS_CONTROL_MAX_AGE, "86400".parse().unwrap());
}

/// `OPTIONS /attestation` preflight — answer browser CORS preflight with `204` and
/// the same open policy the `GET` handler returns.
async fn attestation_preflight() -> (StatusCode, HeaderMap) {
    let mut headers = HeaderMap::new();
    apply_attestation_cors(&mut headers);
    (StatusCode::NO_CONTENT, headers)
}

/// `GET /attestation` — this member's public TEE attestation bundle, served
/// node-locally (TeeSQL pattern). Returns `200 OK` with the full bundle when a
/// fresh quote is obtained, `503 Service Unavailable` with `available:false` and an
/// `errors` map when the dstack guest agent is unreachable *or* returns malformed
/// `/Info`, or `400 Bad Request` when a supplied `?nonce=` challenge is malformed.
///
/// A verifier-supplied `?nonce=` challenge (hex) is folded into `report_data[32..64]`
/// as `keccak256(nonce)` and echoed back, so a remote party can prove the quote is
/// **fresh** rather than a replay of a captured response. Never returns secret
/// material: only *public* keys (already published on chain), the public `/Info`,
/// and the raw TEE quote whose `report_data` binds them together.
async fn attestation(
    State(shared): State<Arc<Shared>>,
    State(dstack): State<Arc<dyn DstackRuntime>>,
    Query(params): Query<HashMap<String, String>>,
) -> (StatusCode, HeaderMap, Json<serde_json::Value>) {
    // Parse the freshness challenge first: a malformed nonce is a client error, and
    // returning 200 for it would defeat the anti-replay guarantee (the verifier would
    // believe they got a challenge-bound quote).
    let nonce = match parse_nonce(&params) {
        Ok(n) => n,
        Err(msg) => {
            let mut headers = HeaderMap::new();
            apply_attestation_cors(&mut headers);
            let body = serde_json::json!({
                "available": false,
                "error": msg,
            });
            return (StatusCode::BAD_REQUEST, headers, Json(body));
        }
    };

    let phase = shared.current_phase().await;
    let report_data = attestation_report_data(&shared, nonce.as_deref());

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
        // Freshness: echo the verifier's challenge (if any) so they can confirm it
        // was bound into report_data[32..64]. `fresh` is true only when a nonce was
        // supplied and accepted — an identity-only bundle is not replay-evident.
        "fresh": nonce.is_some(),
        "nonce": nonce.as_ref().map(|n| format!("0x{}", hex::encode(n))),
    });

    let mut errors = serde_json::Map::new();

    // dstack `/Info`: app_id, compose_hash, and instance/device id. Validate
    // the fields before trusting them — a malformed/empty `/Info` must NOT be
    // published as successful evidence (issue [P2]).
    match dstack.info().await {
        Ok(info) => match validate_dstack_info(&info) {
            Ok(()) => {
                body["dstack"] = serde_json::json!({
                    "app_id": format!("0x{}", hex::encode(&info.app_id)),
                    "compose_hash": format!("0x{}", hex::encode(&info.compose_hash)),
                    "instance_id": format!("0x{}", hex::encode(&info.instance_id)),
                    "device_id": format!("0x{}", hex::encode(&info.device_id)),
                });
                body["code_id"] = serde_json::json!(format!(
                    "0x{}",
                    hex::encode(code_id_from_app_id(&info.app_id))
                ));
            }
            Err(msg) => {
                errors.insert("info".to_string(), serde_json::json!(msg));
            }
        },
        Err(e) => {
            errors.insert("info".to_string(), serde_json::json!(e.to_string()));
        }
    }

    // dstack `/GetQuote`: the raw TEE-signed attestation blob over `report_data`.
    match dstack.get_quote(report_data).await {
        Ok(quote) if !quote.is_empty() => {
            // Real dstack evidence is a TDX quote. Tests use a deliberately tagged
            // mock quote, whose only contract is that it embeds report_data.
            let decoded = if quote.starts_with(b"MOCKQUOTE-v1") {
                quote
                    .ends_with(&report_data)
                    .then(|| serde_json::json!({"format": "mock"}))
                    .ok_or_else(|| "mock quote REPORTDATA mismatch".to_string())
            } else {
                decode_tdx_quote(&quote, &report_data)
            };
            match decoded {
                Ok(decoded) => {
                    body["quote"] = serde_json::json!({
                        "provider": ATTESTOR_LABEL,
                        "format": "raw",
                        "len": quote.len(),
                        "bytes": format!("0x{}", hex::encode(&quote)),
                        "header": decoded.get("header").cloned().unwrap_or_default(),
                    });
                    if let Some(measurements) = decoded.get("measurements") {
                        body["measurements"] = measurements.clone();
                    }
                }
                Err(msg) => {
                    errors.insert("quote".to_string(), serde_json::json!(msg));
                }
            }
        }
        Ok(_) => {
            errors.insert("quote".to_string(), serde_json::json!("empty quote"));
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
    let mut headers = HeaderMap::new();
    apply_attestation_cors(&mut headers);
    (code, headers, Json(body))
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

        let (code, headers, Json(body)) = attestation(
            State(shared.clone()),
            State(dstack.clone()),
            Query(HashMap::new()),
        )
        .await;
        assert_eq!(code, StatusCode::OK);
        assert_eq!(body["available"], true);
        assert_eq!(body["attestor"], "dstack");
        // CORS is applied so browser UIs can read the public bundle cross-origin.
        assert_eq!(
            headers
                .get(axum::http::header::ACCESS_CONTROL_ALLOW_ORIGIN)
                .unwrap(),
            "*"
        );
        // No freshness challenge was supplied: identity-only binding, fresh:false.
        assert_eq!(body["fresh"], false);
        assert!(body["nonce"].is_null());

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
        assert_eq!(
            body["code_id"],
            format!("0x{}", hex::encode(code_id_from_app_id(&info.app_id)))
        );

        // report_data is recomputable by a verifier from the public fields.
        let expected_rd = attestation_report_data(&shared, None);
        assert_eq!(
            body["report_data"],
            format!("0x{}", hex::encode(expected_rd))
        );

        // The quote is present and actually commits to that report_data (the mock
        // quote embeds report_data so the binding is checkable).
        let quote = dstack.get_quote(expected_rd).await.unwrap();
        assert_eq!(body["quote"]["len"], quote.len());
        assert_eq!(body["quote"]["bytes"], format!("0x{}", hex::encode(&quote)));

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

        let (code, _headers, Json(body)) =
            attestation(State(shared.clone()), State(dstack), Query(HashMap::new())).await;
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

    /// A dstack runtime that answers `/GetQuote` but returns a malformed `/Info`
    /// (empty app_id). Models a guest-agent error body that a lenient client would
    /// silently accept — the handler must reject it (issue [P2]) rather than publish
    /// a hollow bundle with a zero code_id.
    struct MalformedInfoDstack(MockDstack);

    #[async_trait::async_trait]
    impl DstackRuntime for MalformedInfoDstack {
        async fn derive_key(
            &self,
            p: &str,
            s: &str,
        ) -> anyhow::Result<zeroize::Zeroizing<[u8; 32]>> {
            self.0.derive_key(p, s).await
        }
        async fn get_quote(&self, rd: [u8; 64]) -> anyhow::Result<Vec<u8>> {
            self.0.get_quote(rd).await
        }
        async fn get_key(&self, p: &str, s: &str) -> anyhow::Result<crate::dstack::DstackKey> {
            self.0.get_key(p, s).await
        }
        async fn info(&self) -> anyhow::Result<crate::dstack::DstackInfo> {
            Ok(crate::dstack::DstackInfo {
                app_id: Vec::new(),
                compose_hash: Vec::new(),
                instance_id: Vec::new(),
                device_id: Vec::new(),
            })
        }
    }

    /// Issue [P2]: a malformed `/Info` (empty fields) must NOT be reported as
    /// `available:true`. Even though `/GetQuote` succeeds, the handler must surface
    /// `errors.info`, omit the `dstack`/`code_id` fields, and return 503.
    #[tokio::test]
    async fn attestation_rejects_malformed_info() {
        let shared = shared().await;
        let dstack: Arc<dyn DstackRuntime> =
            Arc::new(MalformedInfoDstack(MockDstack::from_label("health-test")));

        let (code, _headers, Json(body)) =
            attestation(State(shared.clone()), State(dstack), Query(HashMap::new())).await;
        assert_eq!(code, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body["available"], false);
        assert!(body["errors"]["info"].is_string());
        // The malformed /Info must not be published as evidence.
        assert!(body.get("dstack").is_none());
        assert!(body.get("code_id").is_none());
    }

    /// A provider returning `Ok` with no quote bytes must not produce a successful
    /// attestation bundle, even if its `/Info` response is valid.
    struct EmptyQuoteDstack(MockDstack);

    #[async_trait::async_trait]
    impl DstackRuntime for EmptyQuoteDstack {
        async fn derive_key(
            &self,
            p: &str,
            s: &str,
        ) -> anyhow::Result<zeroize::Zeroizing<[u8; 32]>> {
            self.0.derive_key(p, s).await
        }
        async fn get_quote(&self, _: [u8; 64]) -> anyhow::Result<Vec<u8>> {
            Ok(Vec::new())
        }
        async fn get_key(&self, p: &str, s: &str) -> anyhow::Result<crate::dstack::DstackKey> {
            self.0.get_key(p, s).await
        }
        async fn info(&self) -> anyhow::Result<crate::dstack::DstackInfo> {
            self.0.info().await
        }
    }

    #[tokio::test]
    async fn attestation_rejects_empty_quote() {
        let shared = shared().await;
        let dstack: Arc<dyn DstackRuntime> =
            Arc::new(EmptyQuoteDstack(MockDstack::from_label("health-test")));

        let (code, _headers, Json(body)) =
            attestation(State(shared), State(dstack), Query(HashMap::new())).await;
        assert_eq!(code, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body["available"], false);
        assert_eq!(body["errors"]["quote"], "empty quote");
        assert!(body.get("quote").is_none());
    }

    #[test]
    fn decodes_tdx_v4_measurements_and_checks_report_data() {
        let report_data = [0xa5; 64];
        let mut quote = vec![0u8; TDX_QUOTE_V4_HEADER_LEN + TDX_REPORT_BODY_LEN];
        quote[0..2].copy_from_slice(&4u16.to_le_bytes());
        quote[2..4].copy_from_slice(&2u16.to_le_bytes());
        quote[4..8].copy_from_slice(&TDX_TEE_TYPE.to_le_bytes());
        let body = &mut quote[TDX_QUOTE_V4_HEADER_LEN..];
        body[120..128].copy_from_slice(&1u64.to_le_bytes());
        body[136..184].fill(0x11);
        body[328..376].fill(0x20);
        body[376..424].fill(0x21);
        body[424..472].fill(0x22);
        body[472..520].fill(0x23);
        body[520..584].copy_from_slice(&report_data);

        let decoded = decode_tdx_quote(&quote, &report_data).unwrap();
        assert_eq!(decoded["header"]["version"], 4);
        assert_eq!(decoded["header"]["tee_type"], "tdx");
        assert_eq!(decoded["measurements"]["debug"], true);
        assert_eq!(
            decoded["measurements"]["mrtd"],
            format!("0x{}", "11".repeat(48))
        );
        assert_eq!(
            decoded["measurements"]["rtmr3"],
            format!("0x{}", "23".repeat(48))
        );

        let err = decode_tdx_quote(&quote, &[0x5a; 64]).unwrap_err();
        assert!(err.contains("REPORTDATA does not match"));
    }

    /// Issue [P1]: a verifier-supplied `?nonce=` challenge is folded into
    /// `report_data[32..64]` as keccak256(nonce), echoed back, and marks the bundle
    /// `fresh:true` — so the same captured response cannot be replayed for a
    /// different challenge.
    #[tokio::test]
    async fn attestation_binds_nonce_for_freshness() {
        use alloy::primitives::keccak256;
        let shared = shared().await;
        let dstack: Arc<dyn DstackRuntime> = Arc::new(MockDstack::from_label("health-test"));

        let nonce_hex = "0xdeadbeef";
        let mut params = HashMap::new();
        params.insert("nonce".to_string(), nonce_hex.to_string());

        let (code, _headers, Json(body)) =
            attestation(State(shared.clone()), State(dstack.clone()), Query(params)).await;
        assert_eq!(code, StatusCode::OK);
        assert_eq!(body["fresh"], true);
        assert_eq!(body["nonce"], nonce_hex);

        // report_data[32..64] == keccak256(nonce); [0..32] is the identity binding.
        let nonce_bytes = hex::decode("deadbeef").unwrap();
        let expected_rd = attestation_report_data(&shared, Some(&nonce_bytes));
        assert_eq!(
            body["report_data"],
            format!("0x{}", hex::encode(expected_rd))
        );
        assert_eq!(
            &expected_rd[32..],
            keccak256(&nonce_bytes).as_slice(),
            "upper half must bind the challenge"
        );
        assert_eq!(
            &expected_rd[..32],
            &attestation_report_data(&shared, None)[..32],
            "identity half is unchanged by the nonce"
        );
        // Anti-replay: a different challenge yields a different report_data, so a
        // captured response cannot satisfy a fresh challenge.
        let other = attestation_report_data(&shared, Some(b"different"));
        assert_ne!(expected_rd, other);

        // The quote actually commits to the nonce-bound report_data.
        let quote = dstack.get_quote(expected_rd).await.unwrap();
        assert_eq!(body["quote"]["len"], quote.len());
    }

    /// Issue [P1]: a malformed `?nonce=` (non-hex) is a client error — 400, never a
    /// silent 200 that would mislead a verifier into thinking they got a fresh quote.
    #[tokio::test]
    async fn attestation_rejects_malformed_nonce() {
        let shared = shared().await;
        let dstack: Arc<dyn DstackRuntime> = Arc::new(MockDstack::from_label("health-test"));

        let mut params = HashMap::new();
        params.insert("nonce".to_string(), "nothex!!".to_string());

        let (code, _headers, Json(body)) =
            attestation(State(shared), State(dstack), Query(params)).await;
        assert_eq!(code, StatusCode::BAD_REQUEST);
        assert_eq!(body["available"], false);
        assert!(body["error"].is_string());
    }

    /// Issue [P2]: CORS must be real routing behavior, not just a direct-handler
    /// unit test artifact. Exercise the Axum router over HTTP to prove
    /// `OPTIONS /attestation` answers preflight and `GET /attestation` carries CORS,
    /// while `/healthz` stays unchanged (CORS scoped to `/attestation`).
    #[tokio::test]
    async fn attestation_route_supports_scoped_cors() {
        let shared = shared().await;
        let dstack: Arc<dyn DstackRuntime> = Arc::new(MockDstack::from_label("health-test"));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            axum::serve(listener, app(HttpState { shared, dstack }))
                .await
                .unwrap();
        });
        let client = reqwest::Client::builder().no_proxy().build().unwrap();

        let preflight = client
            .request(
                reqwest::Method::OPTIONS,
                format!("http://{addr}/attestation"),
            )
            .header("origin", "https://ui.example")
            .header("access-control-request-method", "GET")
            .send()
            .await
            .unwrap();
        assert_eq!(preflight.status().as_u16(), StatusCode::NO_CONTENT.as_u16());
        assert_eq!(
            preflight
                .headers()
                .get("access-control-allow-origin")
                .unwrap()
                .to_str()
                .unwrap(),
            "*"
        );
        assert_eq!(
            preflight
                .headers()
                .get("access-control-allow-methods")
                .unwrap()
                .to_str()
                .unwrap(),
            "GET, OPTIONS"
        );

        let get = client
            .get(format!("http://{addr}/attestation?nonce=deadbeef"))
            .header("origin", "https://ui.example")
            .send()
            .await
            .unwrap();
        assert_eq!(get.status().as_u16(), StatusCode::OK.as_u16());
        assert_eq!(
            get.headers()
                .get("access-control-allow-origin")
                .unwrap()
                .to_str()
                .unwrap(),
            "*"
        );
        let body: serde_json::Value = get.json().await.unwrap();
        assert_eq!(body["fresh"], true);
        assert_eq!(body["nonce"], "0xdeadbeef");

        let healthz = client
            .get(format!("http://{addr}/healthz"))
            .header("origin", "https://ui.example")
            .send()
            .await
            .unwrap();
        assert!(healthz
            .headers()
            .get("access-control-allow-origin")
            .is_none());

        server.abort();
    }
}
