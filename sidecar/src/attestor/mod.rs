//! The attestation-provider seam (multi-attestor spec, sidecar component §6).
//!
//! Everything attestation-method-specific lives behind [`AttestationProvider`]:
//! key derivation, the register-call builder, the bootstrap/owner signer, member
//! self-discovery, and the CSK origination/seal surface. `state::run` and
//! `bringup` depend only on this trait, so adding a method (Intel TDX direct,
//! AMD SEV-SNP, NVIDIA CC, ...) is a new provider module — core stays blind.
//!
//! Selection is `ATTESTOR=dstack|operator` (config). dstack wraps the existing
//! runtime flow verbatim; operator consumes a pre-signed `mesh-voucher`
//! registration voucher and a local seed file (NOT hardware attestation — the
//! provider logs a prominent warning).

pub mod dstack;
pub mod operator;

use crate::config::{AttestorMethod, Config};
use crate::keys::KeyMaterial;
use alloy::primitives::{Address, Bytes};
use alloy::signers::local::PrivateKeySigner;
use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use zeroize::Zeroizing;

/// The ABI-encoded inner register call for this method's selector, plus the signer
/// the bundler/member will bootstrap-validate the UserOp against (it also becomes
/// the ClusterMember's EIP-4337 owner).
#[derive(Debug)]
pub struct RegisterCall {
    pub calldata: Bytes,
    pub signer: PrivateKeySigner,
}

#[async_trait]
pub trait AttestationProvider: Send + Sync {
    /// The on-chain attestor id this provider registers under
    /// (e.g. keccak256("attestmesh.attestor.dstack")).
    fn attestor_id(&self) -> [u8; 32];

    /// Derive the node's full identity key set from this method's seed source.
    async fn derive_keys(&self) -> Result<KeyMaterial>;

    /// ABI-encoded inner call for this method's register selector, plus the signer
    /// the bundler/member will bootstrap-validate against.
    async fn build_register_call(
        &self,
        cluster: Address,
        member: Address,
        x_pub: [u8; 32],
        wg_pub: [u8; 32],
    ) -> Result<RegisterCall>;

    /// The signer registration installed as the ClusterMember owner; every
    /// post-registration UserOp must be signed by it. Must be re-derivable across
    /// restarts (bit-identical).
    async fn owner_signer(&self) -> Result<PrivateKeySigner>;

    /// Self-discover the member contract when `MEMBER_CONTRACT` is unset (dstack
    /// Path A reads its own app_id; methods without self-discovery error out).
    async fn self_member_contract(&self) -> Result<Address>;

    /// Whether this node may act as the CSK originator (master spec §8.1). v1
    /// gates origination to dstack: the CSK is KMS-derived there, and an
    /// operator-admitted node must only ever be an onboardee (the P2P pull is
    /// method-agnostic).
    fn supports_csk_origination(&self) -> bool;

    /// Originator-only CSK derivation. Errors on providers that can't originate.
    async fn derive_csk_originator(&self) -> Result<Zeroizing<[u8; 32]>>;

    /// Persist the CSK across restarts (dstack: sealed store; operator: no store —
    /// the node re-pulls after a restart).
    async fn csk_seal(&self, csk: &[u8; 32]) -> Result<()>;

    /// Recover a previously persisted CSK, or `None` when nothing is stored.
    async fn csk_unseal(&self) -> Result<Option<Zeroizing<[u8; 32]>>>;
}

/// Build the provider selected by `ATTESTOR` (multi-attestor spec).
pub fn make_provider(config: &Config) -> Result<Arc<dyn AttestationProvider>> {
    match config.attestor {
        AttestorMethod::Dstack => {
            let runtime: Arc<dyn crate::dstack::DstackRuntime> = Arc::new(
                crate::dstack::UnixSocketDstack::new(config.dstack_socket.clone()),
            );
            Ok(Arc::new(dstack::DstackProvider::new(runtime)))
        }
        AttestorMethod::Operator => Ok(Arc::new(operator::OperatorProvider::from_config(config)?)),
    }
}
