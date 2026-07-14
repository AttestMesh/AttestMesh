//! Attestation-bound identity (spec §6).
//!
//! Instance mode keeps the existing Ed25519 key derived from this CVM's dstack
//! seed. Cluster-shared mode derives the key from the dedicated indexer cluster's
//! CSK using HKDF-SHA512, so homogeneous replicas in one generation share a
//! registry identity. Each replica still produces its own quote for that public
//! key, but the quote is diagnostic only in shared mode: the shared key signature
//! proves generation membership, not which physical replica served an envelope.

use crate::dstack::DstackRuntime;
use alloy::primitives::{keccak256, FixedBytes};
use alloy::sol_types::SolValue;
use anyhow::Result;
use ed25519_dalek::{SigningKey, VerifyingKey};
use hkdf::Hkdf;
use sha2::Sha512;
use zeroize::Zeroizing;

/// dstack purpose string for the envelope-signing key (spec §6 table).
pub const PURPOSE_SIGNING: &str = "attestmesh.indexer.signing.v1";

/// Domain-separated HKDF info for a generation-shared signing seed.
pub const SHARED_SIGNING_INFO: &str = "attestmesh.indexer.signing.v2";

/// dstack purpose string for the (milestone-B) TLS key. Derived for parity but
/// unused in v1, which terminates TLS at a load balancer (spec §6, §15 item 3).
pub const PURPOSE_TLS: &str = "attestmesh.indexer.tls.v1";

/// report_data domain tag (spec §6.1): `keccak256(abi.encode("attestmesh.indexer.v1",
/// signing_pubkey))`.
pub const REPORT_DATA_TAG: &str = "attestmesh.indexer.v1";

/// The indexer's attestation-bound identity.
pub struct Identity {
    signing_key: SigningKey,
    verifying_key: VerifyingKey,
    /// TEE quote committing to `verifying_key`, generated once at boot and reused on
    /// every reconnect (spec §6.1). In shared mode it cannot authenticate the
    /// serving replica because the envelope signature is intentionally shared.
    quote: Vec<u8>,
}

impl Identity {
    /// Derive the signing key from the TEE seed and request the binding quote.
    pub async fn derive(dstack: &dyn DstackRuntime) -> Result<Self> {
        let seed = dstack.derive_key(PURPOSE_SIGNING, "").await?;
        Self::from_seed(&seed, dstack).await
    }

    /// Derive the generation-shared signing identity from a cluster CSK.
    pub async fn derive_shared(csk: &[u8; 32], dstack: &dyn DstackRuntime) -> Result<Self> {
        let seed = shared_signing_seed(csk);
        Self::from_seed(&seed, dstack).await
    }

    async fn from_seed(seed: &[u8; 32], dstack: &dyn DstackRuntime) -> Result<Self> {
        let signing_key = SigningKey::from_bytes(seed);
        let verifying_key = signing_key.verifying_key();

        let report_data = report_data_for(&verifying_key.to_bytes());
        let quote = dstack.get_quote(report_data).await?;

        Ok(Self {
            signing_key,
            verifying_key,
            quote,
        })
    }

    pub fn signing_key(&self) -> &SigningKey {
        &self.signing_key
    }

    pub fn verifying_key(&self) -> &VerifyingKey {
        &self.verifying_key
    }

    /// The 32-byte Ed25519 public key — this is what IndexerRegistry's `pubKey`
    /// field points to (spec §6).
    pub fn signing_pubkey(&self) -> [u8; 32] {
        self.verifying_key.to_bytes()
    }

    pub fn quote(&self) -> &[u8] {
        &self.quote
    }
}

/// `HKDF-SHA512(ikm=CSK, salt=zeros, info="attestmesh.indexer.signing.v2")`.
fn shared_signing_seed(csk: &[u8; 32]) -> Zeroizing<[u8; 32]> {
    let hkdf = Hkdf::<Sha512>::new(None, csk);
    let mut seed = Zeroizing::new([0u8; 32]);
    hkdf.expand(SHARED_SIGNING_INFO.as_bytes(), seed.as_mut())
        .expect("32 bytes is a valid HKDF-SHA512 output length");
    seed
}

/// `report_data = keccak256(abi.encode("attestmesh.indexer.v1", signing_pubkey))`,
/// right-zero-padded into the 64-byte report_data slot (spec §6.1). The 32-byte hash
/// occupies the high bytes; the remaining 32 bytes are zero.
pub fn report_data_for(signing_pubkey: &[u8; 32]) -> [u8; 64] {
    // Solidity `abi.encode(string, bytes32)`: head = (offset_to_string, pubkey),
    // tail = (len, padded-bytes). alloy's SolValue tuple encoding matches this exactly.
    let encoded = (
        REPORT_DATA_TAG.to_string(),
        FixedBytes::<32>::from(*signing_pubkey),
    )
        .abi_encode();
    let hash = keccak256(encoded);

    let mut out = [0u8; 64];
    out[..32].copy_from_slice(hash.as_slice());
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dstack::MockDstack;

    #[tokio::test]
    async fn signing_key_is_deterministic_per_tee_state() {
        let k1 = Identity::derive(&MockDstack::from_label("indexer-A"))
            .await
            .unwrap();
        let k1b = Identity::derive(&MockDstack::from_label("indexer-A"))
            .await
            .unwrap();
        assert_eq!(k1.signing_pubkey(), k1b.signing_pubkey());

        let k2 = Identity::derive(&MockDstack::from_label("indexer-B"))
            .await
            .unwrap();
        assert_ne!(k1.signing_pubkey(), k2.signing_pubkey());
    }

    #[tokio::test]
    async fn quote_commits_to_pubkey() {
        let id = Identity::derive(&MockDstack::from_label("indexer-A"))
            .await
            .unwrap();
        let expected = report_data_for(&id.signing_pubkey());
        // The mock quote wraps report_data after a tag (see MockDstack::get_quote).
        assert!(id.quote().windows(64).any(|w| w == expected));
    }

    #[tokio::test]
    async fn shared_key_depends_on_csk_not_replica_seed() {
        let csk = [0x42u8; 32];
        let a = Identity::derive_shared(&csk, &MockDstack::from_label("replica-A"))
            .await
            .unwrap();
        let b = Identity::derive_shared(&csk, &MockDstack::from_label("replica-B"))
            .await
            .unwrap();
        assert_eq!(a.signing_pubkey(), b.signing_pubkey());

        let rotated = Identity::derive_shared(&[0x43u8; 32], &MockDstack::from_label("replica-A"))
            .await
            .unwrap();
        assert_ne!(a.signing_pubkey(), rotated.signing_pubkey());

        let instance = Identity::derive(&MockDstack::from_label("replica-A"))
            .await
            .unwrap();
        assert_ne!(a.signing_pubkey(), instance.signing_pubkey());
    }

    #[tokio::test]
    async fn every_replica_quote_commits_to_shared_pubkey() {
        let csk = [0x42u8; 32];
        let a = Identity::derive_shared(&csk, &MockDstack::from_label("replica-A"))
            .await
            .unwrap();
        let b = Identity::derive_shared(&csk, &MockDstack::from_label("replica-B"))
            .await
            .unwrap();
        let expected = report_data_for(&a.signing_pubkey());
        assert!(a.quote().windows(64).any(|window| window == expected));
        assert!(b.quote().windows(64).any(|window| window == expected));
    }

    #[test]
    fn shared_hkdf_vector_is_stable() {
        let seed = shared_signing_seed(&[0x42u8; 32]);
        assert_eq!(
            hex::encode(seed.as_slice()),
            "02912771d9c974f4e9b1869f2c1dac643a49a7d4d3a0226086370bbbc35d5ead"
        );
    }

    #[test]
    fn report_data_is_keccak_of_abi_encoding() {
        // Stable, hand-checkable vector: differs from the naive concat encoding.
        let pk = [0x11u8; 32];
        let rd = report_data_for(&pk);
        // Tail 32 bytes are zero padding.
        assert_eq!(&rd[32..], &[0u8; 32]);
        // Distinct pubkeys produce distinct report_data.
        assert_ne!(
            report_data_for(&[0x11u8; 32]),
            report_data_for(&[0x12u8; 32])
        );
    }
}
