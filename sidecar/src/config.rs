//! Environment configuration (sidecar spec §5). All config is via env vars; the
//! sidecar fails fast on missing required vars. No secrets in env — key material is
//! derived from the attestation-bound seed at runtime.

use alloy::primitives::Address;
use anyhow::{Context, Result};

#[derive(Debug, Clone)]
pub struct Config {
    /// The ClusterMember contract address. `None` when unset in env: Path A (dstack base
    /// KMS) mints the app_id at `phala deploy` time, so the member contract — which equals
    /// that app_id — is unknown until the CVM reads its own `/Info` at runtime.
    pub member_contract: Option<Address>,
    pub chain_id: u64,
    pub rpc_url: String,
    pub bundler_url: String,
    /// Alchemy Gas Manager policy id for sponsored UserOps (sidecar spec §8.2).
    pub gas_policy_id: String,
    pub indexer_registry_addr: Address,
    /// dstack gateway base domain (e.g. `dstack-base-prod5.phala.network`). Peer
    /// ingress hostnames are `<app_id>-<port>s.<domain>`. Unset → mesh bring-up
    /// is skipped (registration-only mode).
    pub gateway_domain: Option<String>,
    /// TCP port of the wg-over-TCP ingress (exposed through the gateway).
    pub wg_tcp_port: u16,
    /// Wireguard outer listen port. Distinct from the in-mesh heartbeat port
    /// (51820): kernel wg owns its UDP socket, so they must not collide.
    pub wg_listen_port: u16,
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
            member_contract: match std::env::var("MEMBER_CONTRACT") {
                Ok(s) if !s.trim().is_empty() => Some(s.parse().context("MEMBER_CONTRACT")?),
                _ => None,
            },
            chain_id: req("CHAIN_ID")?.parse().context("CHAIN_ID")?,
            rpc_url: req("RPC_URL")?,
            bundler_url: req("BUNDLER_URL")?,
            gas_policy_id: opt("GAS_POLICY_ID", ""),
            indexer_registry_addr: req("INDEXER_REGISTRY_ADDR")?
                .parse()
                .context("INDEXER_REGISTRY_ADDR")?,
            gateway_domain: match std::env::var("GATEWAY_DOMAIN") {
                Ok(s) if !s.trim().is_empty() => Some(s.trim().to_string()),
                _ => None,
            },
            wg_tcp_port: opt("WG_TCP_PORT", "51900").parse().context("WG_TCP_PORT")?,
            wg_listen_port: opt("WG_LISTEN_PORT", "51821")
                .parse()
                .context("WG_LISTEN_PORT")?,
            dstack_socket: opt("DSTACK_SOCKET", "/var/run/dstack.sock"),
            agent_grpc_socket: opt("AGENT_GRPC_SOCKET", "/var/run/attestmesh/agent.sock"),
            health_http_addr: opt("HEALTH_HTTP_ADDR", "127.0.0.1:9090"),
            log_format: opt("LOG_FORMAT", "json"),
            log_level: opt("LOG_LEVEL", "info"),
        })
    }
}
