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
        Ok(m.cluster()
            .call()
            .await
            .context("read member.cluster()")?
            ._0)
    }

    /// §8.4: `AttestFacet.memberOf(memberAddr)` — restart detection.
    pub async fn member_of(&self, cluster: Address) -> Result<MemberRecord> {
        let a = abi::IAttest::new(cluster, &self.provider);
        let r = a
            .memberOf(self.member_contract)
            .call()
            .await
            .context("read memberOf")?
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
        let c: U256 = a.memberCount().call().await.context("read memberCount")?._0;
        Ok(c.to::<u64>())
    }

    /// §8.4: `AttestFacet.cskCommitment()` — verify a pulled CSK.
    pub async fn csk_commitment(&self, cluster: Address) -> Result<B256> {
        let a = abi::IAttest::new(cluster, &self.provider);
        Ok(a.cskCommitment()
            .call()
            .await
            .context("read cskCommitment")?
            ._0)
    }

    /// `AttestFacet.memberById(memberId)` — peer enumeration during mesh bring-up.
    pub async fn member_by_id(&self, cluster: Address, member_id: B256) -> Result<MemberRecord> {
        let a = abi::IAttest::new(cluster, &self.provider);
        let r = a
            .memberById(member_id)
            .call()
            .await
            .context("read memberById")?
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
        Ok(a.meshIpOf(member_id)
            .call()
            .await
            .context("read meshIpOf")?
            ._0)
    }

    /// `AttestFacet.meshCidr()` — the cluster's wireguard CIDR.
    pub async fn mesh_cidr(&self, cluster: Address) -> Result<(u32, u8)> {
        let a = abi::IAttest::new(cluster, &self.provider);
        let r = a.meshCidr().call().await.context("read meshCidr")?;
        Ok((r.ip, r.prefix))
    }

    pub async fn member_id_of(&self, cluster: Address, who: Address) -> Result<B256> {
        let a = abi::IAttest::new(cluster, &self.provider);
        Ok(a.memberIdOf(who)
            .call()
            .await
            .context("read memberIdOf")?
            ._0)
    }

    pub async fn x_pubkey_of(&self, cluster: Address, member_id: B256) -> Result<B256> {
        let a = abi::IAttest::new(cluster, &self.provider);
        Ok(a.xPubKeyOf(member_id)
            .call()
            .await
            .context("read xPubKeyOf")?
            ._0)
    }

    pub async fn list_members(&self, cluster: Address) -> Result<Vec<B256>> {
        let a = abi::IAttest::new(cluster, &self.provider);
        Ok(a.listMembers().call().await.context("read listMembers")?._0)
    }
}
