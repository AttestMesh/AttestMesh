//! `attestmesh-indexer` entry point (spec §3 lifecycle).

use alloy::primitives::B256;
use anyhow::Result;
use attestmesh_indexer::chain;
use attestmesh_indexer::config::{Config, LogFormat};
use attestmesh_indexer::dstack::{DstackRuntime, UnixSocketDstack};
use attestmesh_indexer::grpc::service::IndexerService;
use attestmesh_indexer::health::{self, Health};
use attestmesh_indexer::identity::Identity;
use attestmesh_indexer::metrics::Metrics;
use attestmesh_indexer::pb::indexer_server::IndexerServer;
use attestmesh_indexer::query::ReadModel;
use attestmesh_indexer::registry;
use attestmesh_indexer::runtime::Runtime;
use attestmesh_indexer::state::cursor::SledCursorStore;
use attestmesh_indexer::state::IndexerState;
use std::sync::Arc;
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

    // Identity (spec §6): derive the Ed25519 signing key + request the binding quote.
    let dstack: Arc<dyn DstackRuntime> =
        Arc::new(UnixSocketDstack::new(config.dstack_socket.clone()));
    let identity = Arc::new(Identity::derive(dstack.as_ref()).await?);
    tracing::info!(
        pubkey = %B256::from_slice(&identity.signing_pubkey()),
        "derived indexer signing identity"
    );

    let provider = Arc::new(chain::connect(&config.rpc_url)?);
    let code_id = expected_code_id();

    // Self-check against IndexerRegistry (spec §6.2) — non-fatal on mismatch.
    match registry::run(&provider, config.indexer_registry_addr, &identity, code_id).await {
        Ok(sc) if sc.ok() => {}
        Ok(_) => {
            tracing::warn!("IndexerRegistry self-check mismatch; continuing (see errors above)")
        }
        Err(e) => {
            tracing::warn!(error = %e, "IndexerRegistry self-check failed to read; continuing")
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
        let chain_id = config.chain_id;
        let gateway_domain = config.gateway_domain.clone();
        let identity_pubkey = B256::from_slice(&identity.signing_pubkey());
        tokio::spawn(async move {
            if let Err(e) =
                health::serve(addr, h, m, rm, chain_id, gateway_domain, identity_pubkey).await
            {
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

    // gRPC listener (spec §8). Built with the provider for subscribe-time catch-up.
    let svc = IndexerService::new(state.clone(), identity.clone(), metrics.clone())
        .with_provider(provider.clone())
        .with_expected_code_id(code_id)
        .with_catchup_batch_size(config.block_batch_size);
    healthstate.set_grpc_accepting(true);

    // Runtime loops (spec §7).
    tokio::spawn(rt.clone().run_block_watcher());
    tokio::spawn(rt.clone().run_cluster_discovery());
    tokio::spawn(rt.clone().run_housekeeping());

    let grpc_addr = config.grpc_addr;
    tracing::info!(%grpc_addr, "gRPC listener up");

    // Serve gRPC with graceful shutdown on SIGTERM/Ctrl-C (spec §3).
    let shutdown = async {
        let _ = tokio::signal::ctrl_c().await;
        tracing::info!("shutdown signal received; draining");
    };
    Server::builder()
        .add_service(IndexerServer::new(svc))
        .serve_with_shutdown(grpc_addr, shutdown)
        .await?;

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
