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
    pub gateway_domain: Option<String>,
    pub dstack_socket: String,
    pub state_dir: String,
    pub block_poll_interval: Duration,
    pub block_batch_size: u64,
    /// Lower bound for the boot catch-up scan — the factory's deploy block (no
    /// clusters can exist before it). 0 (the default) means genesis, which on a
    /// mainnet is effectively unbootable (~hundreds of thousands of getLogs calls);
    /// the deploy routine computes this via a getCode binary search.
    pub start_block: u64,
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
            gateway_domain: {
                let raw = opt("GATEWAY_DOMAIN", "");
                if raw.trim().is_empty() {
                    None
                } else {
                    Some(raw)
                }
            },
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
            start_block: opt("INDEXER_START_BLOCK", "0")
                .parse()
                .context("INDEXER_START_BLOCK")?,
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // Env vars are process-global; serialize the tests that touch them.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn set_required() {
        std::env::set_var("CHAIN_ID", "8453");
        std::env::set_var("RPC_URL", "http://rpc.example");
        std::env::set_var(
            "INDEXER_REGISTRY_ADDR",
            "0xbC003686943fB957100E517D3CEf66c52B5CDdBf",
        );
        std::env::set_var(
            "CLUSTER_DIAMOND_FACTORY_ADDR",
            "0xf6E85fD138E3208d3AAE63ce4E2A33f20e82b9fb",
        );
    }

    #[test]
    fn defaults_parse_and_start_block_floor() {
        let _g = ENV_LOCK.lock().unwrap();
        set_required();
        std::env::remove_var("INDEXER_START_BLOCK");
        std::env::remove_var("BLOCK_BATCH_SIZE");

        let c = Config::from_env().expect("required set");
        assert_eq!(c.chain_id, 8453);
        assert_eq!(
            c.start_block, 0,
            "default floor is genesis (deploy routine overrides)"
        );
        assert_eq!(c.block_batch_size, 200);
        assert_eq!(c.grpc_addr.port(), 50051);
        assert_eq!(c.state_dir, "/var/lib/attestmesh-indexer");

        std::env::set_var("INDEXER_START_BLOCK", "46868742");
        let c = Config::from_env().unwrap();
        assert_eq!(
            c.start_block, 46_868_742,
            "the live Base factory deploy block"
        );
        std::env::remove_var("INDEXER_START_BLOCK");
    }

    #[test]
    fn bad_address_fails() {
        let _g = ENV_LOCK.lock().unwrap();
        set_required();
        std::env::set_var("INDEXER_REGISTRY_ADDR", "not-an-address");
        assert!(Config::from_env().is_err());
        set_required(); // restore
    }
}
