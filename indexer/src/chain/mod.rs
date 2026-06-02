//! Chain interaction (spec §7). The indexer is read-only: it polls `eth_getLogs` /
//! `eth_blockNumber` and makes a handful of `AttestFacet` view calls. It never submits
//! transactions.

pub mod repro;
pub mod verify_member;
pub mod watcher;

use alloy::primitives::{Address, B256};
use alloy::providers::{Provider, RootProvider};
use alloy::sol;
use alloy::transports::http::Http;
use anyhow::{Context, Result};

/// HTTP read provider type (matches the sidecar's validated alloy 0.8 pattern).
pub type HttpProvider = RootProvider<Http<reqwest::Client>>;

sol! {
    // ── Events we decode off ClusterDiamonds (spec §7.1 allowlist) ────────────
    // topics[0] for each is the keccak of its canonical signature; alloy exposes it
    // as `<Event>::SIGNATURE_HASH`.

    #[derive(Debug)]
    event MemberRegistered(
        bytes32 indexed memberId,
        address indexed memberContract,
        bytes32 indexed attestorId,
        bytes32 xPubKey,
        bytes32 wgPubKey
    );

    #[derive(Debug)]
    event WgKeyPublished(bytes32 indexed memberId, bytes32 wgPubKey);

    #[derive(Debug)]
    event MessageSent(
        bytes32 indexed senderMemberId,
        bytes32 indexed recipientMemberId,
        bytes32 indexed envelopeId,
        bytes ciphertext
    );

    // ── Cluster discovery (spec §7.2) ────────────────────────────────────────
    #[derive(Debug)]
    event ClusterDeployed(address indexed cluster, address indexed clusterOwner, bytes32 salt);

    // ── AttestFacet reads for membership checks (spec §8.2) ───────────────────
    #[sol(rpc)]
    interface IAttest {
        struct MemberRecord {
            bytes32 attestorId;
            address memberContract;
            bytes32 xPubKey;
            bytes32 wgPubKey;
            uint64 registeredAt;
        }
        function memberOf(address account) external view returns (MemberRecord memory);
        function memberCount() external view returns (uint256);
    }

    // ── IndexerRegistry self-check (spec §6.2) ────────────────────────────────
    #[sol(rpc)]
    interface IndexerRegistryView {
        function current()
            external
            view
            returns (string endpoint, bytes32 codeId, bytes32 pubKey, uint64 updatedAt);
    }
}

/// Construct the read-only HTTP provider from an RPC URL string.
pub fn connect(rpc_url: &str) -> Result<HttpProvider> {
    let url = rpc_url.parse().context("parse RPC_URL")?;
    Ok(RootProvider::new_http(url))
}

/// Self-check result against IndexerRegistry (spec §6.2).
#[derive(Debug, Clone)]
pub struct RegistryRecord {
    pub endpoint: String,
    pub code_id: B256,
    pub pubkey: B256,
    pub updated_at: u64,
}

/// Read `IndexerRegistry.current()` (spec §6.2).
pub async fn read_registry(provider: &HttpProvider, registry: Address) -> Result<RegistryRecord> {
    let r = IndexerRegistryView::new(registry, provider);
    let c = r
        .current()
        .call()
        .await
        .context("read IndexerRegistry.current()")?;
    Ok(RegistryRecord {
        endpoint: c.endpoint,
        code_id: c.codeId,
        pubkey: c.pubKey,
        updated_at: c.updatedAt,
    })
}

/// Current chain head (`eth_blockNumber`).
pub async fn block_number(provider: &HttpProvider) -> Result<u64> {
    provider.get_block_number().await.context("eth_blockNumber")
}
