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
    /// Signing identity source. Instance mode preserves the current per-CVM key;
    /// cluster-shared mode derives one generation key from the indexer cluster CSK.
    pub identity_mode: IdentityMode,
    /// Dedicated indexer ClusterDiamond. Required only in cluster-shared mode.
    pub indexer_cluster_addr: Option<Address>,
    /// Co-located sidecar Agent gRPC unix socket. Required only in shared mode.
    pub agent_grpc_addr: Option<String>,
    pub log_level: String,
    pub log_format: LogFormat,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IdentityMode {
    Instance,
    ClusterShared,
}

impl IdentityMode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Instance => "instance",
            Self::ClusterShared => "cluster-shared",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LogFormat {
    Json,
    Pretty,
}

impl Config {
    /// Load and validate from the process environment.
    pub fn from_env() -> Result<Self> {
        let identity_mode = match opt("INDEXER_IDENTITY", "instance").as_str() {
            "instance" => IdentityMode::Instance,
            "cluster-shared" => IdentityMode::ClusterShared,
            other => anyhow::bail!(
                "INDEXER_IDENTITY must be 'instance' or 'cluster-shared', got '{other}'"
            ),
        };
        let (indexer_cluster_addr, agent_grpc_addr) = match identity_mode {
            IdentityMode::Instance => (None, None),
            IdentityMode::ClusterShared => {
                let cluster = optional_addr("INDEXER_CLUSTER_ADDR")?
                    .context("INDEXER_IDENTITY=cluster-shared requires INDEXER_CLUSTER_ADDR")?;
                anyhow::ensure!(
                    cluster != Address::ZERO,
                    "INDEXER_CLUSTER_ADDR must be nonzero in cluster-shared mode"
                );
                let agent = std::env::var("AGENT_GRPC_ADDR")
                    .ok()
                    .filter(|value| !value.trim().is_empty())
                    .context("INDEXER_IDENTITY=cluster-shared requires AGENT_GRPC_ADDR")?;
                anyhow::ensure!(
                    is_unix_agent_addr(&agent),
                    "AGENT_GRPC_ADDR must be an absolute unix-socket path or unix:/path"
                );
                (Some(cluster), Some(agent))
            }
        };

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
            identity_mode,
            indexer_cluster_addr,
            agent_grpc_addr,
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

fn optional_addr(key: &str) -> Result<Option<Address>> {
    std::env::var(key)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .map(|value| parse_addr(key, &value))
        .transpose()
}

fn is_unix_agent_addr(addr: &str) -> bool {
    addr.starts_with('/')
        || addr
            .strip_prefix("unix:")
            .is_some_and(|path| path.starts_with('/'))
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
        std::env::remove_var("INDEXER_IDENTITY");
        std::env::remove_var("INDEXER_CLUSTER_ADDR");
        std::env::remove_var("AGENT_GRPC_ADDR");
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
        assert_eq!(c.identity_mode, IdentityMode::Instance);
        assert_eq!(c.indexer_cluster_addr, None);
        assert_eq!(c.agent_grpc_addr, None);

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

    #[test]
    fn cluster_shared_requires_cluster_and_agent_uds() {
        let _g = ENV_LOCK.lock().unwrap();
        set_required();
        std::env::set_var("INDEXER_IDENTITY", "cluster-shared");

        assert!(Config::from_env()
            .unwrap_err()
            .to_string()
            .contains("INDEXER_CLUSTER_ADDR"));

        std::env::set_var(
            "INDEXER_CLUSTER_ADDR",
            "0x1111111111111111111111111111111111111111",
        );
        assert!(Config::from_env()
            .unwrap_err()
            .to_string()
            .contains("AGENT_GRPC_ADDR"));

        std::env::set_var("AGENT_GRPC_ADDR", "http://127.0.0.1:50052");
        assert!(Config::from_env()
            .unwrap_err()
            .to_string()
            .contains("unix-socket"));

        std::env::set_var("AGENT_GRPC_ADDR", "/var/run/attestmesh/agent.sock");
        let c = Config::from_env().unwrap();
        assert_eq!(c.identity_mode, IdentityMode::ClusterShared);
        assert_eq!(
            c.indexer_cluster_addr,
            Some(
                "0x1111111111111111111111111111111111111111"
                    .parse()
                    .unwrap()
            )
        );
        assert_eq!(
            c.agent_grpc_addr.as_deref(),
            Some("/var/run/attestmesh/agent.sock")
        );

        set_required();
    }

    #[test]
    fn invalid_identity_mode_fails() {
        let _g = ENV_LOCK.lock().unwrap();
        set_required();
        std::env::set_var("INDEXER_IDENTITY", "shared-ish");
        assert!(Config::from_env().is_err());
        set_required();
    }

    #[test]
    fn instance_mode_ignores_shared_only_settings() {
        let _g = ENV_LOCK.lock().unwrap();
        set_required();
        std::env::set_var("INDEXER_CLUSTER_ADDR", "not-an-address");
        std::env::set_var("AGENT_GRPC_ADDR", "http://127.0.0.1:50052");

        let config = Config::from_env().unwrap();
        assert_eq!(config.identity_mode, IdentityMode::Instance);
        assert_eq!(config.indexer_cluster_addr, None);
        assert_eq!(config.agent_grpc_addr, None);
        set_required();
    }
}
