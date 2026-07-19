//! Health + metrics HTTP endpoint (spec §12).
//!
//! `GET /healthz` → 200 when (chain head lag < 10) AND (gRPC accepting) AND (RPC
//! reachable); else 503 with a JSON body naming the failed check.
//! `GET /metrics` → Prometheus exposition.

use crate::metrics::Metrics;
use crate::query::{build_health, build_topology, ReadModel};
use alloy::primitives::{Address, B256};
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde_json::json;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

/// Max acceptable head lag before `/healthz` flips to 503 (spec §12).
pub const MAX_HEAD_LAG_BLOCKS: u64 = 10;

/// Immutable boot facts used by the LB/deployer to reject a heterogeneous or
/// accidentally non-dedicated shared-mode candidate.
#[derive(Debug, Clone)]
pub struct AdmissionMetadata {
    pub code_id: B256,
    pub identity_mode: &'static str,
    pub indexer_cluster: Option<Address>,
    pub serving_member_id: Option<B256>,
    pub serving_member_contract: Option<Address>,
    pub serving_mesh_ip: Option<u32>,
}

#[derive(Debug, Clone)]
pub struct StatusMetadata {
    pub chain_id: u64,
    pub gateway_domain: Option<String>,
    pub identity_pubkey: B256,
    pub admission: AdmissionMetadata,
}

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
    read_model: Arc<ReadModel>,
    chain_id: u64,
    gateway_domain: Option<String>,
    identity_pubkey: B256,
    admission: AdmissionMetadata,
}

/// Serve `/healthz` + `/metrics` until the process exits or the future is dropped.
pub async fn serve(
    addr: SocketAddr,
    health: Arc<Health>,
    metrics: Arc<Metrics>,
    read_model: Arc<ReadModel>,
    metadata: StatusMetadata,
) -> anyhow::Result<()> {
    let state = HttpState {
        health,
        metrics,
        read_model,
        chain_id: metadata.chain_id,
        gateway_domain: metadata.gateway_domain,
        identity_pubkey: metadata.identity_pubkey,
        admission: metadata.admission,
    };
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/status", get(status))
        .route("/metrics", get(metrics_handler))
        .route("/mesh/clusters", get(mesh_clusters))
        .route("/mesh/members", get(mesh_members))
        .route("/mesh/topology", get(mesh_topology))
        .route("/mesh/health", get(mesh_health))
        .route("/mesh/timeline", get(mesh_timeline))
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

async fn status(State(s): State<HttpState>) -> Response {
    let lag = s.health.chain_head_lag.load(Ordering::Relaxed);
    let grpc_accepting = s.health.grpc_accepting.load(Ordering::Relaxed);
    let rpc_reachable = s.health.rpc_reachable.load(Ordering::Relaxed);
    let health_reason = s.health.evaluate().err();
    let snapshots = s
        .read_model
        .snapshots(s.chain_id, s.gateway_domain.as_deref())
        .await;
    let member_count: usize = snapshots.iter().map(|s| s.member_count).sum();
    let admission = admission_json(&s.admission);
    let status_admission = status_admission_json(&s.admission);
    let code_id = status_admission["codeId"].clone();
    let identity_mode = status_admission["identityMode"].clone();
    let indexer_cluster = status_admission["indexerCluster"].clone();
    let serving_member_id = status_admission["servingMemberId"].clone();
    (
        StatusCode::OK,
        Json(json!({
            "chainId": s.chain_id,
            "pubKey": format!("{:#x}", s.identity_pubkey),
            "codeId": code_id,
            "identityMode": identity_mode,
            "indexerCluster": indexer_cluster,
            "servingMemberId": serving_member_id,
            "admission": admission,
            "health": {
                "ok": health_reason.is_none(),
                "reason": health_reason,
                "chainHeadLagBlocks": lag,
                "grpcAccepting": grpc_accepting,
                "rpcReachable": rpc_reachable,
            },
            "readModel": {
                "clusterCount": snapshots.len(),
                "memberCount": member_count,
                "atBlock": snapshots.iter().map(|s| s.at_block).max(),
            }
        })),
    )
        .into_response()
}

fn admission_json(metadata: &AdmissionMetadata) -> serde_json::Value {
    json!({
        "codeId": format!("{:#x}", metadata.code_id),
        "identityMode": metadata.identity_mode,
        "indexerCluster": metadata.indexer_cluster.map(|value| format!("{value:#x}")),
        "servingMemberId": metadata.serving_member_id.map(|value| format!("{value:#x}")),
        "servingMemberContract": metadata
            .serving_member_contract
            .map(|value| format!("{value:#x}")),
        "servingMeshIp": metadata.serving_mesh_ip,
    })
}

fn status_admission_json(metadata: &AdmissionMetadata) -> serde_json::Value {
    json!({
        "codeId": format!("{:#x}", metadata.code_id),
        "identityMode": metadata.identity_mode,
        "indexerCluster": metadata.indexer_cluster.map(|value| format!("{value:#x}")),
        "servingMemberId": metadata.serving_member_id.map(|value| format!("{value:#x}")),
    })
}

async fn metrics_handler(State(s): State<HttpState>) -> Response {
    (StatusCode::OK, s.metrics.render()).into_response()
}

async fn mesh_clusters(State(s): State<HttpState>) -> Response {
    let snapshots = s
        .read_model
        .snapshots(s.chain_id, s.gateway_domain.as_deref())
        .await;
    let at_block = snapshots.iter().map(|s| s.at_block).max();
    let member_count: usize = snapshots.iter().map(|s| s.member_count).sum();
    (
        StatusCode::OK,
        Json(json!({
            "chainId": snapshots.first().map(|s| s.chain_id),
            "atBlock": at_block,
            "clusterCount": snapshots.len(),
            "memberCount": member_count,
            "clusters": snapshots,
        })),
    )
        .into_response()
}

async fn mesh_members(
    State(s): State<HttpState>,
    Query(params): Query<HashMap<String, String>>,
) -> Response {
    let snapshots = s
        .read_model
        .snapshots(s.chain_id, s.gateway_domain.as_deref())
        .await;
    if let Some(cluster) = params.get("cluster") {
        let cluster_lc = cluster.to_ascii_lowercase();
        if let Some(snapshot) = snapshots
            .into_iter()
            .find(|s| s.cluster.to_ascii_lowercase() == cluster_lc)
        {
            return (StatusCode::OK, Json(json!(snapshot))).into_response();
        }
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "unknown_cluster", "message": format!("unknown cluster {cluster}")})),
        )
            .into_response();
    }
    mesh_clusters(State(s)).await
}

async fn mesh_topology(State(s): State<HttpState>) -> Response {
    let snapshots = s
        .read_model
        .snapshots(s.chain_id, s.gateway_domain.as_deref())
        .await;
    let topologies = snapshots.iter().map(build_topology).collect::<Vec<_>>();
    let at_block = topologies.iter().map(|t| t.at_block).max();
    (
        StatusCode::OK,
        Json(json!({
            "chainId": snapshots.first().map(|s| s.chain_id),
            "atBlock": at_block,
            "clusterCount": topologies.len(),
            "clusters": topologies,
        })),
    )
        .into_response()
}

async fn mesh_health(State(s): State<HttpState>) -> Response {
    let snapshots = s
        .read_model
        .snapshots(s.chain_id, s.gateway_domain.as_deref())
        .await;
    let health = snapshots.iter().map(build_health).collect::<Vec<_>>();
    let at_block = health.iter().map(|h| h.at_block).max();
    let member_count: usize = health.iter().map(|h| h.member_count).sum();
    (
        StatusCode::OK,
        Json(json!({
            "chainId": snapshots.first().map(|s| s.chain_id),
            "atBlock": at_block,
            "clusterCount": health.len(),
            "memberCount": member_count,
            "cskCommitted": !health.is_empty() && health.iter().all(|h| h.csk_committed),
            "clusters": health,
        })),
    )
        .into_response()
}

async fn mesh_timeline(State(s): State<HttpState>) -> Response {
    let timelines = s.read_model.timelines().await;
    let mut events = timelines
        .iter()
        .flat_map(|t| {
            t.events.iter().cloned().map(|mut e| {
                e.args.insert(
                    "cluster".into(),
                    serde_json::Value::String(t.cluster.clone()),
                );
                e
            })
        })
        .collect::<Vec<_>>();
    events.sort_by_key(|e| (e.block, e.log_index));
    let from_block = timelines.iter().map(|t| t.from_block).min();
    let to_block = timelines.iter().map(|t| t.to_block).max();
    (
        StatusCode::OK,
        Json(json!({
            "clusterCount": timelines.len(),
            "fromBlock": from_block,
            "toBlock": to_block,
            "eventCount": events.len(),
            "events": events,
            "clusters": timelines,
        })),
    )
        .into_response()
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

    #[test]
    fn admission_metadata_is_machine_readable() {
        let metadata = AdmissionMetadata {
            code_id: B256::repeat_byte(0x11),
            identity_mode: "cluster-shared",
            indexer_cluster: Some(Address::repeat_byte(0x22)),
            serving_member_id: Some(B256::repeat_byte(0x33)),
            serving_member_contract: Some(Address::repeat_byte(0x44)),
            serving_mesh_ip: Some(0x0a0d0001),
        };
        let fields = admission_json(&metadata);
        assert_eq!(fields["identityMode"], "cluster-shared");
        assert_eq!(fields["codeId"], format!("{:#x}", B256::repeat_byte(0x11)));
        assert_eq!(
            fields["indexerCluster"],
            format!("{:#x}", Address::repeat_byte(0x22))
        );
        assert_eq!(
            fields["servingMemberId"],
            format!("{:#x}", B256::repeat_byte(0x33))
        );

        // These four fields are the stable top-level /status admission contract
        // consumed by the LB preflight.
        let status = status_admission_json(&metadata);
        assert_eq!(status["identityMode"], "cluster-shared");
        assert_eq!(status["codeId"], fields["codeId"]);
        assert_eq!(status["indexerCluster"], fields["indexerCluster"]);
        assert_eq!(status["servingMemberId"], fields["servingMemberId"]);
        assert_eq!(status.as_object().unwrap().len(), 4);
    }
}
