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
use std::future::Future;
use std::time::Duration;

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
    event CskCommitmentSet(bytes32 commitment);

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
        function memberById(bytes32 memberId) external view returns (MemberRecord memory);
        function memberCount() external view returns (uint256);
        function memberIdOf(address account) external view returns (bytes32);
        function isClusterMember(address account) external view returns (bool);
        function meshCidr() external view returns (uint32 ip, uint8 prefix);
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
    retry_rpc("eth_blockNumber", || provider.get_block_number()).await
}

/// Read `meshCidr()` for a freshly discovered cluster.
pub async fn mesh_cidr(provider: &HttpProvider, cluster: Address) -> Result<(u32, u8)> {
    let c = IAttest::new(cluster, provider);
    let r = retry_rpc("meshCidr()", || async { c.meshCidr().call().await }).await?;
    Ok((r.ip, r.prefix))
}

/// Read the on-chain registration timestamp for a member.
pub async fn member_registered_at(
    provider: &HttpProvider,
    cluster: Address,
    member_id: B256,
) -> Result<u64> {
    let c = IAttest::new(cluster, provider);
    let r = retry_rpc("memberById()", || async {
        c.memberById(member_id).call().await
    })
    .await?;
    Ok(r._0.registeredAt)
}

/// Fail-closed proof that the sidecar's `GetSelf` facts describe a live member of
/// the explicitly configured dedicated indexer cluster.
pub async fn validate_shared_member(
    provider: &HttpProvider,
    cluster: Address,
    member_contract: Address,
    expected_member_id: B256,
) -> Result<()> {
    let contract = IAttest::new(cluster, provider);
    let onchain_id = retry_rpc("memberIdOf()", || async {
        contract.memberIdOf(member_contract).call().await
    })
    .await?
    ._0;
    let active = retry_rpc("isClusterMember()", || async {
        contract.isClusterMember(member_contract).call().await
    })
    .await?
    ._0;
    validate_shared_member_result(onchain_id, active, expected_member_id)
}

fn validate_shared_member_result(onchain_id: B256, active: bool, expected: B256) -> Result<()> {
    anyhow::ensure!(
        onchain_id == expected,
        "sidecar memberId {expected} does not match indexer cluster memberIdOf {onchain_id}"
    );
    anyhow::ensure!(
        active,
        "sidecar member is not active in the indexer cluster"
    );
    Ok(())
}

async fn retry_rpc<T, E, Fut, F>(label: &'static str, mut f: F) -> Result<T>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = std::result::Result<T, E>>,
    E: std::error::Error + Send + Sync + 'static,
{
    let mut delay = Duration::from_millis(500);
    let mut last_error = None;
    for attempt in 1..=8 {
        match f().await {
            Ok(v) => return Ok(v),
            Err(e) => {
                last_error = Some(e);
                if attempt == 8 {
                    break;
                }
                tracing::warn!(label, attempt, error = ?last_error, "RPC read failed; retrying");
                tokio::time::sleep(delay).await;
                delay = (delay * 2).min(Duration::from_secs(10));
            }
        }
    }
    Err(anyhow::Error::new(last_error.expect("retry loop ran"))).context(label)
}

#[cfg(test)]
mod shared_member_tests {
    use super::*;

    #[test]
    fn requires_matching_active_membership() {
        let id = B256::repeat_byte(7);
        assert!(validate_shared_member_result(id, true, id).is_ok());
        assert!(validate_shared_member_result(B256::repeat_byte(8), true, id).is_err());
        assert!(validate_shared_member_result(id, false, id).is_err());
    }
}
