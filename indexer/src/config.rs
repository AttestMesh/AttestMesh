//! Configuration (spec §5). Env vars only — no secrets; the dstack TEE seed is
//! sufficient for all key material.

use alloy::primitives::Address;
use anyhow::{Context, Result};
use std::net::SocketAddr;
use std::time::Duration;

#[derive(Debug, Clone)]
pub struct Config {
    pub chain_id: u64,
    pub rpc_url: String,
    pub indexer_registry_addr: Address,
    pub cluster_diamond_factory_addr: Address,
    pub grpc_addr: SocketAddr,
    pub health_http_addr: SocketAddr,
    pub dstack_socket: String,
    pub state_dir: String,
    pub block_poll_interval: Duration,
    pub block_batch_size: u64,
    pub log_level: String,
    pub log_format: LogFormat,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LogFormat {
    Json,
    Pretty,
}

impl Config {
    /// Load and validate from the process environment.
    pub fn from_env() -> Result<Self> {
        Ok(Self {
            chain_id: req("CHAIN_ID")?.parse().context("CHAIN_ID must be u64")?,
            rpc_url: req("RPC_URL")?,
            indexer_registry_addr: parse_addr(
                "INDEXER_REGISTRY_ADDR",
                &req("INDEXER_REGISTRY_ADDR")?,
            )?,
            cluster_diamond_factory_addr: parse_addr(
                "CLUSTER_DIAMOND_FACTORY_ADDR",
                &req("CLUSTER_DIAMOND_FACTORY_ADDR")?,
            )?,
            grpc_addr: opt("INDEXER_GRPC_ADDR", "0.0.0.0:50051")
                .parse()
                .context("INDEXER_GRPC_ADDR")?,
            health_http_addr: opt("HEALTH_HTTP_ADDR", "0.0.0.0:9090")
                .parse()
                .context("HEALTH_HTTP_ADDR")?,
            dstack_socket: opt("DSTACK_SOCKET", "/var/run/dstack.sock"),
            state_dir: opt("STATE_DIR", "/var/lib/attestmesh-indexer"),
            block_poll_interval: Duration::from_millis(
                opt("BLOCK_POLL_INTERVAL_MS", "2000")
                    .parse()
                    .context("BLOCK_POLL_INTERVAL_MS")?,
            ),
            block_batch_size: opt("BLOCK_BATCH_SIZE", "200")
                .parse()
                .context("BLOCK_BATCH_SIZE")?,
            log_level: opt("LOG_LEVEL", "info"),
            log_format: match opt("LOG_FORMAT", "json").as_str() {
                "pretty" => LogFormat::Pretty,
                _ => LogFormat::Json,
            },
        })
    }
}

fn req(key: &str) -> Result<String> {
    std::env::var(key).with_context(|| format!("required env var {key} is unset"))
}

fn opt(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

fn parse_addr(key: &str, raw: &str) -> Result<Address> {
    raw.parse()
        .with_context(|| format!("{key} must be a 20-byte hex address"))
}
