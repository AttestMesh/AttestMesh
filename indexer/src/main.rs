//! `attestmesh-indexer` entry point (spec §3 lifecycle).

use alloy::primitives::B256;
use anyhow::{Context, Result};
use attestmesh_indexer::chain;
use attestmesh_indexer::config::{Config, IdentityMode, LogFormat};
use attestmesh_indexer::dstack::{
    validated_app_id, validated_compose_hash, DstackRuntime, UnixSocketDstack,
};
use attestmesh_indexer::grpc::service::IndexerService;
use attestmesh_indexer::health::{self, AdmissionMetadata, Health, StatusMetadata};
use attestmesh_indexer::identity::Identity;
use attestmesh_indexer::metrics::Metrics;
use attestmesh_indexer::pb::indexer_server::IndexerServer;
use attestmesh_indexer::query::ReadModel;
use attestmesh_indexer::registry;
use attestmesh_indexer::runtime::Runtime;
use attestmesh_indexer::state::cursor::SledCursorStore;
use attestmesh_indexer::state::IndexerState;
use std::sync::Arc;
use std::time::Duration;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::Server;

/// Compile-time expected code id (spec §6.2: derived from the OCI image hash). The
/// build/release pipeline overrides this via the `INDEXER_CODE_ID` env at image-bake
/// time; absent that it is zero, which simply makes the self-check log a mismatch.
fn expected_code_id() -> B256 {
    match std::env::var("INDEXER_CODE_ID")
        .ok()
        .and_then(|s| s.parse::<B256>().ok())
    {
        Some(id) => id,
        None => B256::ZERO,
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let config = Config::from_env()?;
    init_tracing(&config);
    tracing::info!(chain_id = config.chain_id, "attestmesh-indexer starting");

    let provider = Arc::new(chain::connect(&config.rpc_url)?);

    // Identity (spec §6): instance mode preserves the current per-CVM derivation.
    // Shared mode obtains the CSK and self facts from the co-located sidecar, then
    // binds those facts to dstack /Info and the configured dedicated cluster.
    let dstack: Arc<dyn DstackRuntime> =
        Arc::new(UnixSocketDstack::new(config.dstack_socket.clone()));
    let (identity, code_id, admission) = match config.identity_mode {
        IdentityMode::Instance => {
            let identity = Arc::new(Identity::derive(dstack.as_ref()).await?);
            let code_id = expected_code_id();
            let admission = AdmissionMetadata {
                code_id,
                identity_mode: config.identity_mode.as_str(),
                indexer_cluster: None,
                serving_member_id: None,
                serving_member_contract: None,
                serving_mesh_ip: None,
            };
            (identity, code_id, admission)
        }
        IdentityMode::ClusterShared => {
            let cluster = config
                .indexer_cluster_addr
                .expect("shared-mode cluster validated by Config::from_env");
            let agent_addr = config
                .agent_grpc_addr
                .as_deref()
                .expect("shared-mode Agent address validated by Config::from_env");
            let dstack_info = dstack.info().await.context("dstack /Info")?;
            let code_id = B256::from(validated_compose_hash(&dstack_info)?);
            let app_id = alloy::primitives::Address::from(validated_app_id(&dstack_info)?);
            let facts =
                attestmesh_indexer::agent::fetch_facts(agent_addr, Duration::from_secs(5)).await?;
            anyhow::ensure!(
                facts.member_contract == app_id,
                "sidecar GetSelf member contract {} does not match dstack /Info app_id {}",
                facts.member_contract,
                app_id
            );
            chain::validate_shared_member(
                &provider,
                cluster,
                facts.member_contract,
                facts.member_id,
            )
            .await
            .context("validate sidecar member in INDEXER_CLUSTER_ADDR")?;

            let identity = Arc::new(Identity::derive_shared(&facts.csk, dstack.as_ref()).await?);
            let admission = AdmissionMetadata {
                code_id,
                identity_mode: config.identity_mode.as_str(),
                indexer_cluster: Some(cluster),
                serving_member_id: Some(facts.member_id),
                serving_member_contract: Some(facts.member_contract),
                serving_mesh_ip: Some(facts.mesh_ip),
            };
            (identity, code_id, admission)
        }
    };
    tracing::info!(
        pubkey = %B256::from_slice(&identity.signing_pubkey()),
        code_id = %code_id,
        identity_mode = config.identity_mode.as_str(),
        indexer_cluster = ?config.indexer_cluster_addr,
        serving_member_id = ?admission.serving_member_id,
        "derived indexer signing identity"
    );

    // Preserve the historical non-fatal instance-mode self-check. Shared mode uses
    // the strict admission loop below, after its HTTP/read model has had time to warm.
    if config.identity_mode == IdentityMode::Instance {
        match registry::run(&provider, config.indexer_registry_addr, &identity, code_id).await {
            Ok(sc) if sc.ok() => {}
            Ok(_) => {
                tracing::warn!("IndexerRegistry self-check mismatch; continuing instance mode")
            }
            Err(error) => tracing::warn!(
                error = %error,
                "IndexerRegistry self-check failed to read; continuing instance mode"
            ),
        }
    }

    // Persistent cursor store (spec §9). Plaintext on a persistent volume in v1.
    let cursors = Arc::new(SledCursorStore::open(&config.state_dir)?);
    let state = IndexerState::new(cursors);

    let metrics = Arc::new(Metrics::new());
    let healthstate = Arc::new(Health::new(metrics.clone()));
    let read_model = Arc::new(ReadModel::new());

    let config = Arc::new(config);

    // Start the HTTP surface before catch-up. Catch-up can take a while on a
    // public RPC, and operators still need /status for the signing pubkey and
    // diagnostics while the read model is warming.
    {
        let addr = config.health_http_addr;
        let h = healthstate.clone();
        let m = metrics.clone();
        let rm = read_model.clone();
        let metadata = StatusMetadata {
            chain_id: config.chain_id,
            gateway_domain: config.gateway_domain.clone(),
            identity_pubkey: B256::from_slice(&identity.signing_pubkey()),
            admission: admission.clone(),
        };
        tokio::spawn(async move {
            if let Err(e) = health::serve(addr, h, m, rm, metadata).await {
                tracing::error!(error = %e, "health endpoint exited");
            }
        });
    }

    let rt = Runtime {
        config: config.clone(),
        provider: provider.clone(),
        state: state.clone(),
        identity: identity.clone(),
        metrics: metrics.clone(),
        health: healthstate.clone(),
        read_model: read_model.clone(),
    };

    // Catch up to head before accepting subscriptions (spec §3).
    if let Err(e) = rt.boot_catchup().await {
        tracing::error!(error = %e, "boot catch-up failed; entering loops to retry");
    }

    // Runtime loops (spec §7).
    tokio::spawn(rt.clone().run_block_watcher());
    tokio::spawn(rt.clone().run_cluster_discovery());
    tokio::spawn(rt.clone().run_housekeeping());

    // A shared candidate is deliberately allowed to warm everything above while
    // /healthz reports gRPC closed. Only the exact v1 registry key+code pair opens
    // the externally trusted subscription surface.
    let admission_policy = registry::AdmissionPolicy::default();
    if config.identity_mode == IdentityMode::ClusterShared {
        registry::wait_for_shared_authorization(
            &provider,
            config.indexer_registry_addr,
            &identity,
            code_id,
            admission_policy,
        )
        .await?;
    }

    // gRPC listener (spec §8). The service itself remains the current protocol-v2
    // implementation; shared admission is entirely outside its request path.
    let svc = IndexerService::new(state.clone(), identity.clone(), metrics.clone())
        .with_provider(provider.clone())
        .with_expected_code_id(code_id)
        .with_catchup_batch_size(config.block_batch_size);
    let grpc_addr = config.grpc_addr;
    let listener = tokio::net::TcpListener::bind(grpc_addr)
        .await
        .with_context(|| format!("bind indexer gRPC {grpc_addr}"))?;
    healthstate.set_grpc_accepting(true);
    tracing::info!(%grpc_addr, "gRPC listener up");

    match config.identity_mode {
        IdentityMode::Instance => {
            let shutdown = async {
                let _ = tokio::signal::ctrl_c().await;
                tracing::info!("shutdown signal received; draining");
            };
            Server::builder()
                .add_service(IndexerServer::new(svc))
                .serve_with_incoming_shutdown(TcpListenerStream::new(listener), shutdown)
                .await?;
        }
        IdentityMode::ClusterShared => {
            // `select!` intentionally drops the server future on authorization loss,
            // closing existing streams as well as the listener. Graceful draining an
            // unbounded streaming RPC would leave a revoked replica serving forever.
            let monitor = registry::monitor_shared_authorization(
                &provider,
                config.indexer_registry_addr,
                &identity,
                code_id,
                admission_policy,
            );
            let server = Server::builder()
                .add_service(IndexerServer::new(svc))
                .serve_with_incoming(TcpListenerStream::new(listener));
            tokio::pin!(monitor);
            tokio::pin!(server);
            let serving_result: Result<()> = tokio::select! {
                result = &mut server => {
                    result.context("shared-mode gRPC server")?;
                    Err(anyhow::anyhow!("shared-mode gRPC server exited unexpectedly"))
                }
                _ = tokio::signal::ctrl_c() => {
                    tracing::info!("shutdown signal received");
                    Ok(())
                }
                result = &mut monitor => match result {
                    Ok(check) => Err(anyhow::anyhow!(
                        "shared-mode registry authorization revoked (code_match={}, pubkey_match={})",
                        check.code_id_matches,
                        check.pubkey_matches
                    )),
                    Err(error) => Err(error),
                },
            };
            healthstate.set_grpc_accepting(false);
            serving_result?;
        }
    }

    // Persist cursors on the way out (spec §3 graceful shutdown).
    let _ = state.cursors().flush();
    tracing::info!("attestmesh-indexer stopped");
    Ok(())
}

fn init_tracing(config: &Config) {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_new(&config.log_level).unwrap_or_else(|_| EnvFilter::new("info"));
    match config.log_format {
        LogFormat::Json => {
            fmt().json().with_env_filter(filter).init();
        }
        LogFormat::Pretty => {
            fmt().pretty().with_env_filter(filter).init();
        }
    }
}
