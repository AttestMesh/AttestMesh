//! Environment configuration (sidecar spec §5). All config is via env vars; the
//! sidecar fails fast on missing required vars. No secrets in env — key material is
//! derived from the attestation-bound seed at runtime.

use alloy::primitives::Address;
use anyhow::{bail, Context, Result};

/// Which attestation method this node registers with (`ATTESTOR`, multi-attestor
/// spec). Exactly one per node; a cluster may mix methods across nodes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum AttestorMethod {
    #[default]
    Dstack,
    /// Operator-signature method: the node is vouched for by an allowlisted
    /// operator key, NOT hardware attestation (see attestor::operator).
    Operator,
}

#[derive(Debug, Clone)]
pub struct Config {
    /// Attestation method selection (`ATTESTOR=dstack|operator`, default dstack).
    pub attestor: AttestorMethod,
    /// Operator method only: the pre-signed registration voucher minted by
    /// `mesh-voucher` (hex CBOR or JSON). Not secret — useless without the seed.
    pub operator_voucher: Option<String>,
    /// Operator method only: path of the node's local key seed file (generated on
    /// first boot, mode 0600). Explicitly weaker than TEE-derived keys.
    pub attestor_seed_path: String,
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
            attestor: match opt("ATTESTOR", "dstack").trim() {
                "dstack" => AttestorMethod::Dstack,
                "operator" => AttestorMethod::Operator,
                other => bail!("ATTESTOR must be dstack|operator, got {other:?}"),
            },
            operator_voucher: match std::env::var("OPERATOR_VOUCHER") {
                Ok(s) if !s.trim().is_empty() => Some(s.trim().to_string()),
                _ => None,
            },
            attestor_seed_path: opt("ATTESTOR_SEED_PATH", "/var/lib/attestmesh/attestor-seed"),
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // Env vars are process-global; serialize the tests that touch them.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn set_required() {
        std::env::set_var("CHAIN_ID", "8453");
        std::env::set_var("RPC_URL", "http://rpc.example");
        std::env::set_var("BUNDLER_URL", "http://bundler.example");
        std::env::set_var(
            "INDEXER_REGISTRY_ADDR",
            "0xbC003686943fB957100E517D3CEf66c52B5CDdBf",
        );
    }

    fn clear_optional() {
        for k in [
            "ATTESTOR",
            "OPERATOR_VOUCHER",
            "ATTESTOR_SEED_PATH",
            "MEMBER_CONTRACT",
            "GAS_POLICY_ID",
            "GATEWAY_DOMAIN",
            "WG_TCP_PORT",
            "WG_LISTEN_PORT",
            "DSTACK_SOCKET",
            "AGENT_GRPC_SOCKET",
            "HEALTH_HTTP_ADDR",
            "LOG_FORMAT",
            "LOG_LEVEL",
        ] {
            std::env::remove_var(k);
        }
    }

    #[test]
    fn defaults_and_required_parse() {
        let _g = ENV_LOCK.lock().unwrap();
        set_required();
        clear_optional();

        let c = Config::from_env().expect("required set");
        assert_eq!(c.chain_id, 8453);
        assert_eq!(
            c.attestor,
            AttestorMethod::Dstack,
            "default method is dstack"
        );
        assert_eq!(c.operator_voucher, None);
        assert_eq!(c.attestor_seed_path, "/var/lib/attestmesh/attestor-seed");
        assert_eq!(
            c.member_contract, None,
            "Path A: self-discovered at runtime"
        );
        assert_eq!(c.gateway_domain, None, "unset → registration-only mode");
        assert_eq!(c.wg_tcp_port, 51900);
        assert_eq!(
            c.wg_listen_port, 51821,
            "outer wg port must differ from the in-mesh heartbeat port 51820"
        );
        assert_eq!(c.dstack_socket, "/var/run/dstack.sock");
        assert_eq!(c.health_http_addr, "127.0.0.1:9090");
    }

    #[test]
    fn gateway_domain_blank_is_none() {
        let _g = ENV_LOCK.lock().unwrap();
        set_required();
        clear_optional();
        std::env::set_var("GATEWAY_DOMAIN", "   ");
        let c = Config::from_env().unwrap();
        assert_eq!(c.gateway_domain, None, "whitespace-only counts as unset");

        std::env::set_var("GATEWAY_DOMAIN", "dstack-base-prod5.phala.network");
        let c = Config::from_env().unwrap();
        assert_eq!(
            c.gateway_domain.as_deref(),
            Some("dstack-base-prod5.phala.network")
        );
        std::env::remove_var("GATEWAY_DOMAIN");
    }

    #[test]
    fn attestor_selection_parses() {
        let _g = ENV_LOCK.lock().unwrap();
        set_required();
        clear_optional();

        std::env::set_var("ATTESTOR", "operator");
        let c = Config::from_env().unwrap();
        assert_eq!(c.attestor, AttestorMethod::Operator);

        std::env::set_var("ATTESTOR", "tdx-direct");
        assert!(Config::from_env().is_err(), "unknown method must fail fast");
        std::env::remove_var("ATTESTOR");
    }

    #[test]
    fn missing_required_fails() {
        let _g = ENV_LOCK.lock().unwrap();
        set_required();
        std::env::remove_var("RPC_URL");
        assert!(Config::from_env().is_err());
        set_required(); // restore for whoever runs next
    }
}
