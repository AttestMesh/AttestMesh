//! Chain interaction (sidecar spec §8). The sidecar never submits raw transactions:
//! reads go through a read-only provider (only the handful of startup reads in §8.4),
//! and every state-mutating call goes through EIP-4337 via the bundler (`bundler.rs`).

pub mod abi;
pub mod bundler;
pub mod dstack_facet;
pub mod message_facet;
pub mod network_facet;
pub mod registry;
pub mod userop;

use crate::keys::KeyMaterial;
use alloy::primitives::{Address, B256, U256};
use alloy::providers::RootProvider;
use alloy::signers::local::PrivateKeySigner;
use alloy::transports::http::Http;
use anyhow::{Context, Result};
use std::future::Future;
use std::time::Duration;

pub type HttpProvider = RootProvider<Http<reqwest::Client>>;

/// A member record as read from AttestFacet (mirrors contracts spec §4.1).
#[derive(Debug, Clone)]
pub struct MemberRecord {
    pub attestor_id: B256,
    pub member_contract: Address,
    pub x_pubkey: B256,
    pub wg_pubkey: B256,
    pub registered_at: u64,
}

impl MemberRecord {
    pub fn exists(&self) -> bool {
        self.member_contract != Address::ZERO
    }
}

/// Read-only chain client + the binding signer (whose address becomes the
/// ClusterMember owner and signs every UserOpHash).
pub struct ChainClient {
    pub chain_id: u64,
    pub member_contract: Address,
    pub signer: PrivateKeySigner,
    provider: HttpProvider,
}

impl ChainClient {
    pub fn new(
        rpc_url: &str,
        chain_id: u64,
        member_contract: Address,
        keys: &KeyMaterial,
    ) -> Result<Self> {
        let url = rpc_url.parse().context("parse RPC_URL")?;
        let provider = RootProvider::new_http(url);
        let signer = PrivateKeySigner::from_slice(&keys.binding_seed[..])
            .context("construct binding signer")?;
        Ok(Self {
            chain_id,
            member_contract,
            signer,
            provider,
        })
    }

    pub fn provider(&self) -> &HttpProvider {
        &self.provider
    }

    pub fn signer_address(&self) -> Address {
        self.signer.address()
    }

    /// §8.4: `member.cluster()` — find the ClusterDiamond from the member address.
    pub async fn cluster_of(&self) -> Result<Address> {
        let m = abi::IClusterMemberView::new(self.member_contract, &self.provider);
        Ok(retry_rpc("read member.cluster()", || async { m.cluster().call().await })
            .await?
            ._0)
    }

    /// §8.4: `AttestFacet.memberOf(memberAddr)` — restart detection.
    pub async fn member_of(&self, cluster: Address) -> Result<MemberRecord> {
        let a = abi::IAttest::new(cluster, &self.provider);
        let r = retry_rpc("read memberOf", || async {
            a.memberOf(self.member_contract).call().await
        })
        .await?
        ._0;
        Ok(MemberRecord {
            attestor_id: r.attestorId,
            member_contract: r.memberContract,
            x_pubkey: r.xPubKey,
            wg_pubkey: r.wgPubKey,
            registered_at: r.registeredAt,
        })
    }

    /// §8.4: `AttestFacet.memberCount()` — CSK-role determination.
    pub async fn member_count(&self, cluster: Address) -> Result<u64> {
        let a = abi::IAttest::new(cluster, &self.provider);
        let c: U256 = retry_rpc("read memberCount", || async {
            a.memberCount().call().await
        })
        .await?
        ._0;
        Ok(c.to::<u64>())
    }

    /// §8.4: `AttestFacet.cskCommitment()` — verify a pulled CSK.
    pub async fn csk_commitment(&self, cluster: Address) -> Result<B256> {
        let a = abi::IAttest::new(cluster, &self.provider);
        Ok(retry_rpc("read cskCommitment", || async {
            a.cskCommitment().call().await
        })
        .await?
        ._0)
    }

    /// `AttestFacet.memberById(memberId)` — peer enumeration during mesh bring-up.
    pub async fn member_by_id(&self, cluster: Address, member_id: B256) -> Result<MemberRecord> {
        let a = abi::IAttest::new(cluster, &self.provider);
        let r = retry_rpc("read memberById", || async {
            a.memberById(member_id).call().await
        })
        .await?
        ._0;
        Ok(MemberRecord {
            attestor_id: r.attestorId,
            member_contract: r.memberContract,
            x_pubkey: r.xPubKey,
            wg_pubkey: r.wgPubKey,
            registered_at: r.registeredAt,
        })
    }

    /// `AttestFacet.meshIpOf(memberId)` — the peer's derived mesh /32.
    pub async fn mesh_ip_of(&self, cluster: Address, member_id: B256) -> Result<u32> {
        let a = abi::IAttest::new(cluster, &self.provider);
        Ok(retry_rpc("read meshIpOf", || async {
            a.meshIpOf(member_id).call().await
        })
        .await?
        ._0)
    }

    /// `AttestFacet.meshCidr()` — the cluster's wireguard CIDR.
    pub async fn mesh_cidr(&self, cluster: Address) -> Result<(u32, u8)> {
        let a = abi::IAttest::new(cluster, &self.provider);
        let r = retry_rpc("read meshCidr", || async { a.meshCidr().call().await }).await?;
        Ok((r.ip, r.prefix))
    }

    pub async fn member_id_of(&self, cluster: Address, who: Address) -> Result<B256> {
        let a = abi::IAttest::new(cluster, &self.provider);
        Ok(retry_rpc("read memberIdOf", || async {
            a.memberIdOf(who).call().await
        })
        .await?
        ._0)
    }

    pub async fn x_pubkey_of(&self, cluster: Address, member_id: B256) -> Result<B256> {
        let a = abi::IAttest::new(cluster, &self.provider);
        Ok(retry_rpc("read xPubKeyOf", || async {
            a.xPubKeyOf(member_id).call().await
        })
        .await?
        ._0)
    }

    /// A member's on-chain Ed25519 heartbeat key, or zero if not yet published (the
    /// member runs an old sidecar, or the cluster predates the ed25519-onchain-key
    /// cut). Zero → fall back to the PeerEndpoint envelope for that peer.
    pub async fn ed25519_key_of(&self, cluster: Address, member_id: B256) -> Result<B256> {
        let a = abi::IAttest::new(cluster, &self.provider);
        Ok(retry_rpc("read ed25519KeyOf", || async {
            a.ed25519KeyOf(member_id).call().await
        })
        .await?
        ._0)
    }

    pub async fn list_members(&self, cluster: Address) -> Result<Vec<B256>> {
        let a = abi::IAttest::new(cluster, &self.provider);
        Ok(retry_rpc("read listMembers", || async {
            a.listMembers().call().await
        })
        .await?
        ._0)
    }
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
