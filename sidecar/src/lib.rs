//! AttestMesh node sidecar (`cluster-mesh-agent`) — library crate.
//!
//! Turns "a CVM running in dstack" into "an AttestMesh cluster member": key
//! derivation, on-chain registration, Indexer subscription, wireguard mesh,
//! heartbeats, the first-convergence gate, the CSK lifecycle, and the app façade.
//! See `docs/specs/sidecar.md`.

// tonic's `Status` is a large error type; the gRPC service signatures return it
// directly, so this lint fires on every handler. Boxing would obscure the API.
#![allow(clippy::result_large_err)]

pub mod proto {
    pub mod indexer {
        tonic::include_proto!("attestmesh.indexer.v1");
    }
    pub mod agent {
        tonic::include_proto!("attestmesh.agent.v1");
    }
    pub mod peer {
        tonic::include_proto!("attestmesh.peer.v1");
    }
}

pub mod agent_grpc;
pub mod attestor;
pub mod bringup;
pub mod chain;
pub mod config;
pub mod csk;
pub mod dstack;
pub mod envelopes;
pub mod health;
pub mod heartbeat;
pub mod indexer_client;
pub mod keys;
pub mod peer_grpc;
pub mod state;
pub mod transport;
pub mod wg;

/// Binary entrypoint: load config, init logging, run the bring-up state machine.
pub fn run() -> anyhow::Result<()> {
    let config = config::Config::from_env()?;
    init_tracing(&config);
    let rt = tokio::runtime::Runtime::new()?;
    rt.block_on(state::run(config))
}

fn init_tracing(config: &config::Config) {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_new(&config.log_level).unwrap_or_else(|_| EnvFilter::new("info"));
    if config.log_format == "json" {
        let _ = fmt().json().with_env_filter(filter).try_init();
    } else {
        let _ = fmt().with_env_filter(filter).try_init();
    }
}
