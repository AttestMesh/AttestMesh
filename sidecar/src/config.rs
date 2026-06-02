//! Environment configuration (sidecar spec §5). All config is via env vars; the
//! sidecar fails fast on missing required vars. No secrets in env — key material is
//! derived from the attestation-bound seed at runtime.

use alloy::primitives::Address;
use anyhow::{Context, Result};

#[derive(Debug, Clone)]
pub struct Config {
    pub member_contract: Address,
    pub chain_id: u64,
    pub rpc_url: String,
    pub bundler_url: String,
    pub indexer_registry_addr: Address,
    pub dstack_socket: String,
    pub agent_grpc_socket: String,
    pub health_http_addr: String,
    pub log_format: String,
    pub log_level: String,
}

fn req(key: &str) -> Result<String> {
    std::env::var(key).with_context(|| format!("missing required env var {key}"))
}

fn opt(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

impl Config {
    pub fn from_env() -> Result<Self> {
        Ok(Self {
            member_contract: req("MEMBER_CONTRACT")?.parse().context("MEMBER_CONTRACT")?,
            chain_id: req("CHAIN_ID")?.parse().context("CHAIN_ID")?,
            rpc_url: req("RPC_URL")?,
            bundler_url: req("BUNDLER_URL")?,
            indexer_registry_addr: req("INDEXER_REGISTRY_ADDR")?
                .parse()
                .context("INDEXER_REGISTRY_ADDR")?,
            dstack_socket: opt("DSTACK_SOCKET", "/var/run/dstack.sock"),
            agent_grpc_socket: opt("AGENT_GRPC_SOCKET", "/var/run/attestmesh/agent.sock"),
            health_http_addr: opt("HEALTH_HTTP_ADDR", "127.0.0.1:9090"),
            log_format: opt("LOG_FORMAT", "json"),
            log_level: opt("LOG_LEVEL", "info"),
        })
    }
}
