//! `DstackProvider` — the dstack attestation method behind the provider seam.
//!
//! Wraps the existing `dstack.rs` runtime client + `chain/dstack_facet.rs` proof
//! flow verbatim (multi-attestor spec: zero behavior change for dstack nodes).
//! All dstack-specific detail — the KMS sig chain, /Info app_id self-discovery
//! (Path A), the sealed store, KMS-derived CSK origination — stays in this module.

use super::{AttestationProvider, RegisterCall};
use crate::chain::dstack_facet;
use crate::dstack::DstackRuntime;
use crate::keys::KeyMaterial;
use alloy::primitives::{keccak256, Address, B256};
use alloy::signers::local::PrivateKeySigner;
use anyhow::{Context, Result};
use async_trait::async_trait;
use std::sync::Arc;
use zeroize::Zeroizing;

pub struct DstackProvider {
    runtime: Arc<dyn DstackRuntime>,
}

impl DstackProvider {
    pub fn new(runtime: Arc<dyn DstackRuntime>) -> Self {
        Self { runtime }
    }
}

#[async_trait]
impl AttestationProvider for DstackProvider {
    fn attestor_id(&self) -> [u8; 32] {
        keccak256(crate::state::DSTACK_ATTESTOR_ID).0
    }

    async fn derive_keys(&self) -> Result<KeyMaterial> {
        crate::keys::derive_all(self.runtime.as_ref()).await
    }

    async fn build_register_call(
        &self,
        cluster: Address,
        member: Address,
        x_pub: [u8; 32],
        wg_pub: [u8; 32],
    ) -> Result<RegisterCall> {
        let (proof, signer) = dstack_facet::build_proof_from_runtime(
            self.runtime.as_ref(),
            cluster,
            member,
            B256::from(x_pub),
            B256::from(wg_pub),
        )
        .await
        .context("build_proof_from_runtime (/Info + /GetKey -> DstackProof)")?;
        tracing::info!(owner = %signer.address(), code_id = %proof.codeId,
            purpose = %proof.purpose, "dstack proof built; derived key is the member owner");
        let calldata = dstack_facet::build_register_calldata(
            proof,
            member,
            B256::from(x_pub),
            B256::from(wg_pub),
        );
        Ok(RegisterCall { calldata, signer })
    }

    async fn owner_signer(&self) -> Result<PrivateKeySigner> {
        dstack_facet::derive_owner_signer(self.runtime.as_ref()).await
    }

    async fn self_member_contract(&self) -> Result<Address> {
        // Path A: the member contract IS this CVM's provisioned app_id.
        let info = self
            .runtime
            .info()
            .await
            .context("dstack /Info for app_id self-discovery (MEMBER_CONTRACT unset)")?;
        if info.app_id.len() != 20 {
            anyhow::bail!(
                "dstack app_id is {} bytes, expected a 20-byte address",
                info.app_id.len()
            );
        }
        Ok(Address::from_slice(&info.app_id))
    }

    fn supports_csk_origination(&self) -> bool {
        true // the CSK derives from the dstack KMS (csk.rs)
    }

    async fn derive_csk_originator(&self) -> Result<Zeroizing<[u8; 32]>> {
        crate::csk::derive_originator(self.runtime.as_ref()).await
    }

    async fn csk_seal(&self, csk: &[u8; 32]) -> Result<()> {
        crate::csk::seal_to_store(self.runtime.as_ref(), csk).await
    }

    async fn csk_unseal(&self) -> Result<Option<Zeroizing<[u8; 32]>>> {
        crate::csk::unseal_from_store(self.runtime.as_ref()).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dstack::MockDstack;

    /// The provider is a verbatim wrapper: keys and CSK behavior must be
    /// bit-identical to calling the underlying flows directly.
    #[tokio::test]
    async fn provider_wraps_existing_flow_verbatim() {
        let runtime = Arc::new(MockDstack::from_label("node-A"));
        let provider = DstackProvider::new(runtime.clone());

        let via_provider = provider.derive_keys().await.unwrap();
        let direct = crate::keys::derive_all(runtime.as_ref()).await.unwrap();
        assert_eq!(via_provider.x_pub, direct.x_pub);
        assert_eq!(via_provider.ed25519_pub, direct.ed25519_pub);
        assert_eq!(via_provider.wg_pub, direct.wg_pub);
        assert_eq!(*via_provider.binding_seed, *direct.binding_seed);

        let csk = provider.derive_csk_originator().await.unwrap();
        let csk_direct = crate::csk::derive_originator(runtime.as_ref())
            .await
            .unwrap();
        assert_eq!(*csk, *csk_direct);

        assert!(provider.supports_csk_origination());
        assert_eq!(
            provider.attestor_id(),
            keccak256(b"attestmesh.attestor.dstack").0
        );
    }

    #[tokio::test]
    async fn csk_seal_unseal_round_trips_through_provider() {
        let provider = DstackProvider::new(Arc::new(MockDstack::from_label("node-B")));
        assert!(provider.csk_unseal().await.unwrap().is_none());
        provider.csk_seal(&[7u8; 32]).await.unwrap();
        assert_eq!(*provider.csk_unseal().await.unwrap().unwrap(), [7u8; 32]);
    }
}
