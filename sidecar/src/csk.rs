//! Cluster Shared Key lifecycle (master spec §8, sidecar spec §13).
//!
//! The CSK is originated from dstack KMS state, distributed peer-to-peer in a
//! sealed box, and cached in a KMS-wrapped envelope on the CVM's durable data
//! disk. Plaintext exists only in sidecar memory.

use crate::dstack::DstackRuntime;
use crate::envelopes::{open, seal};
use crate::keys::{CSK_SUBKEY, PURPOSE_CSK};
use alloy::primitives::Address;
use anyhow::{bail, Context, Result};
use chacha20poly1305::aead::{Aead, KeyInit, Payload};
use chacha20poly1305::{XChaCha20Poly1305, XNonce};
use crypto_box::SecretKey as XSecretKey;
use rand::RngCore;
use serde::{Deserialize, Serialize};
use sha3::{Digest, Keccak256};
use std::path::Path;
use zeroize::Zeroizing;

const CACHE_FILE: &str = "csk.v1";
const CACHE_VERSION: u8 = 1;
const CACHE_MAX_BYTES: u64 = 4096;
const CACHE_AAD_DOMAIN: &[u8] = b"attestmesh.csk-cache-envelope.v1";
const PURPOSE_CSK_CACHE: &str = "attestmesh.csk-cache.v1";
const CSK_CACHE_SUBKEY: &str = "wrap-v1";

#[derive(Debug, Serialize, Deserialize)]
struct CacheEnvelope {
    version: u8,
    cluster: [u8; 20],
    member_contract: [u8; 20],
    commitment: [u8; 32],
    nonce: [u8; 24],
    ciphertext: Vec<u8>,
}

fn address_bytes(address: Address) -> [u8; 20] {
    let mut out = [0u8; 20];
    out.copy_from_slice(address.as_slice());
    out
}

fn cache_aad(
    version: u8,
    cluster: &[u8; 20],
    member_contract: &[u8; 20],
    expected_commitment: &[u8; 32],
) -> Vec<u8> {
    let mut aad = Vec::with_capacity(CACHE_AAD_DOMAIN.len() + 1 + 20 + 20 + 32);
    aad.extend_from_slice(CACHE_AAD_DOMAIN);
    aad.push(version);
    aad.extend_from_slice(cluster);
    aad.extend_from_slice(member_contract);
    aad.extend_from_slice(expected_commitment);
    aad
}

/// keccak256(CSK) — the only CSK-derived value that ever touches the chain.
pub fn commitment(csk: &[u8; 32]) -> [u8; 32] {
    let mut out = [0u8; 32];
    out.copy_from_slice(&Keccak256::digest(csk));
    out
}

/// Originator derives the CSK from its own TEE state (master spec §8.1). Only the
/// first registrant ever calls this; everyone else pulls.
pub async fn derive_originator(dstack: &dyn DstackRuntime) -> Result<Zeroizing<[u8; 32]>> {
    dstack.derive_key(PURPOSE_CSK, CSK_SUBKEY).await
}

/// Serve side (master spec §8.2): seal the CSK to a requester's on-chain x25519
/// pubkey. Defense-in-depth so even a compromised wireguard session never exposes it.
pub fn seal_for_peer(csk: &[u8; 32], requester_xpub: &[u8; 32]) -> Result<Vec<u8>> {
    seal(requester_xpub, csk)
}

/// Onboardee pull (master spec §8.3): open a pulled SealedCsk and verify it against
/// the on-chain commitment. A malicious peer cannot hand over a bogus key.
pub fn open_pulled(
    sealed: &[u8],
    x_secret: &XSecretKey,
    x_pub: &[u8; 32],
    expected_commitment: &[u8; 32],
) -> Result<Zeroizing<[u8; 32]>> {
    let pt = open(x_secret, x_pub, sealed)?;
    if pt.len() != 32 {
        bail!("csk wrong length");
    }
    let mut csk = [0u8; 32];
    csk.copy_from_slice(&pt);
    if &commitment(&csk) != expected_commitment {
        bail!("csk commitment mismatch");
    }
    Ok(Zeroizing::new(csk))
}

/// Load and authenticate the durable CSK envelope. Metadata is checked before
/// KMS/decryption work and the plaintext is independently checked against the
/// current on-chain commitment after decryption.
pub async fn load_cache(
    dstack: &dyn DstackRuntime,
    state_dir: Option<&Path>,
    cluster: Address,
    member_contract: Address,
    expected_commitment: &[u8; 32],
) -> Result<Option<Zeroizing<[u8; 32]>>> {
    let Some(bytes) = crate::storage::read_optional(state_dir, CACHE_FILE, CACHE_MAX_BYTES).await?
    else {
        return Ok(None);
    };
    let mut reader = std::io::Cursor::new(bytes.as_slice());
    let envelope: CacheEnvelope =
        ciborium::from_reader(&mut reader).context("decode CSK cache envelope")?;
    anyhow::ensure!(
        reader.position() == bytes.len() as u64,
        "trailing data in CSK cache envelope"
    );
    anyhow::ensure!(
        envelope.version == CACHE_VERSION,
        "unsupported CSK cache version {}",
        envelope.version
    );
    let cluster = address_bytes(cluster);
    let member_contract = address_bytes(member_contract);
    anyhow::ensure!(envelope.cluster == cluster, "CSK cache cluster mismatch");
    anyhow::ensure!(
        envelope.member_contract == member_contract,
        "CSK cache member mismatch"
    );
    anyhow::ensure!(
        &envelope.commitment == expected_commitment,
        "CSK cache commitment is stale"
    );

    let kek = dstack
        .derive_key(PURPOSE_CSK_CACHE, CSK_CACHE_SUBKEY)
        .await
        .context("derive CSK cache wrapping key")?;
    let cipher = XChaCha20Poly1305::new_from_slice(kek.as_slice())
        .map_err(|_| anyhow::anyhow!("invalid CSK cache wrapping key"))?;
    let aad = cache_aad(
        envelope.version,
        &envelope.cluster,
        &envelope.member_contract,
        &envelope.commitment,
    );
    let plaintext = cipher
        .decrypt(
            XNonce::from_slice(&envelope.nonce),
            Payload {
                msg: &envelope.ciphertext,
                aad: &aad,
            },
        )
        .map_err(|_| anyhow::anyhow!("CSK cache authentication failed"))?;
    let plaintext = Zeroizing::new(plaintext);
    anyhow::ensure!(
        plaintext.len() == 32,
        "CSK cache plaintext has wrong length"
    );
    let mut csk = [0u8; 32];
    csk.copy_from_slice(&plaintext);
    anyhow::ensure!(
        &commitment(&csk) == expected_commitment,
        "CSK cache plaintext commitment mismatch"
    );
    Ok(Some(Zeroizing::new(csk)))
}

/// Encrypt and atomically persist the CSK. Returns false when durable state is not
/// configured; this preserves backwards compatibility for minimal composes.
pub async fn store_cache(
    dstack: &dyn DstackRuntime,
    state_dir: Option<&Path>,
    cluster: Address,
    member_contract: Address,
    expected_commitment: &[u8; 32],
    csk: &[u8; 32],
) -> Result<bool> {
    let Some(state_dir) = state_dir else {
        return Ok(false);
    };
    anyhow::ensure!(
        &commitment(csk) == expected_commitment,
        "refusing to cache CSK with mismatched commitment"
    );
    let cluster = address_bytes(cluster);
    let member_contract = address_bytes(member_contract);
    let kek = dstack
        .derive_key(PURPOSE_CSK_CACHE, CSK_CACHE_SUBKEY)
        .await
        .context("derive CSK cache wrapping key")?;
    let cipher = XChaCha20Poly1305::new_from_slice(kek.as_slice())
        .map_err(|_| anyhow::anyhow!("invalid CSK cache wrapping key"))?;
    let mut nonce = [0u8; 24];
    rand::rngs::OsRng.fill_bytes(&mut nonce);
    let aad = cache_aad(
        CACHE_VERSION,
        &cluster,
        &member_contract,
        expected_commitment,
    );
    let ciphertext = cipher
        .encrypt(
            XNonce::from_slice(&nonce),
            Payload {
                msg: csk,
                aad: &aad,
            },
        )
        .map_err(|_| anyhow::anyhow!("encrypt CSK cache envelope"))?;
    let envelope = CacheEnvelope {
        version: CACHE_VERSION,
        cluster,
        member_contract,
        commitment: *expected_commitment,
        nonce,
        ciphertext,
    };
    let mut encoded = Vec::new();
    ciborium::into_writer(&envelope, &mut encoded).context("encode CSK cache envelope")?;
    crate::storage::atomic_write(state_dir, CACHE_FILE, &encoded).await?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dstack::MockDstack;
    use rand::rngs::OsRng;

    fn addresses() -> (Address, Address) {
        (Address::repeat_byte(0x11), Address::repeat_byte(0x22))
    }

    #[tokio::test]
    async fn originator_derive_and_commitment() {
        let d = MockDstack::from_label("originator");
        let csk = derive_originator(&d).await.unwrap();
        let c = commitment(&csk);
        assert_eq!(c.to_vec(), Keccak256::digest(*csk).to_vec());

        let d2 = MockDstack::from_label("originator");
        let csk2 = derive_originator(&d2).await.unwrap();
        assert_eq!(*csk, *csk2);
    }

    #[test]
    fn serve_then_pull_round_trip() {
        let onboardee = XSecretKey::generate(&mut OsRng);
        let onboardee_pub = *onboardee.public_key().as_bytes();
        let csk = [42u8; 32];
        let comm = commitment(&csk);

        let sealed = seal_for_peer(&csk, &onboardee_pub).unwrap();
        let pulled = open_pulled(&sealed, &onboardee, &onboardee_pub, &comm).unwrap();
        assert_eq!(*pulled, csk);
    }

    #[test]
    fn bogus_csk_rejected_by_commitment() {
        let onboardee = XSecretKey::generate(&mut OsRng);
        let onboardee_pub = *onboardee.public_key().as_bytes();
        let real_commitment = commitment(&[1u8; 32]);
        let sealed = seal_for_peer(&[99u8; 32], &onboardee_pub).unwrap();
        assert!(open_pulled(&sealed, &onboardee, &onboardee_pub, &real_commitment).is_err());
    }

    #[tokio::test]
    async fn encrypted_cache_round_trip_and_atomic_replacement() {
        let temp = tempfile::tempdir().unwrap();
        let d = MockDstack::from_label("node");
        let (cluster, member) = addresses();
        let first = [7u8; 32];
        let second = [8u8; 32];
        store_cache(
            &d,
            Some(temp.path()),
            cluster,
            member,
            &commitment(&first),
            &first,
        )
        .await
        .unwrap();
        assert_eq!(
            *load_cache(&d, Some(temp.path()), cluster, member, &commitment(&first))
                .await
                .unwrap()
                .unwrap(),
            first
        );
        store_cache(
            &d,
            Some(temp.path()),
            cluster,
            member,
            &commitment(&second),
            &second,
        )
        .await
        .unwrap();
        assert_eq!(
            *load_cache(&d, Some(temp.path()), cluster, member, &commitment(&second))
                .await
                .unwrap()
                .unwrap(),
            second
        );
        assert_eq!(std::fs::read_dir(temp.path()).unwrap().count(), 1);
    }

    #[tokio::test]
    async fn cache_is_bound_to_kms_cluster_member_and_commitment() {
        let temp = tempfile::tempdir().unwrap();
        let d = MockDstack::from_label("node");
        let wrong_d = MockDstack::from_label("other-node");
        let (cluster, member) = addresses();
        let key = [9u8; 32];
        let expected = commitment(&key);
        store_cache(&d, Some(temp.path()), cluster, member, &expected, &key)
            .await
            .unwrap();

        assert!(
            load_cache(&wrong_d, Some(temp.path()), cluster, member, &expected)
                .await
                .is_err()
        );
        assert!(load_cache(
            &d,
            Some(temp.path()),
            Address::repeat_byte(0x33),
            member,
            &expected
        )
        .await
        .is_err());
        assert!(load_cache(
            &d,
            Some(temp.path()),
            cluster,
            Address::repeat_byte(0x44),
            &expected
        )
        .await
        .is_err());
        assert!(load_cache(
            &d,
            Some(temp.path()),
            cluster,
            member,
            &commitment(&[10u8; 32])
        )
        .await
        .is_err());
    }

    #[tokio::test]
    async fn corrupt_and_unknown_version_cache_fall_back() {
        let temp = tempfile::tempdir().unwrap();
        let d = MockDstack::from_label("node");
        let (cluster, member) = addresses();
        let key = [11u8; 32];
        let expected = commitment(&key);
        std::fs::write(temp.path().join(CACHE_FILE), b"not-cbor").unwrap();
        assert!(
            load_cache(&d, Some(temp.path()), cluster, member, &expected)
                .await
                .is_err()
        );

        store_cache(&d, Some(temp.path()), cluster, member, &expected, &key)
            .await
            .unwrap();
        let valid = std::fs::read(temp.path().join(CACHE_FILE)).unwrap();
        let mut envelope: CacheEnvelope = ciborium::from_reader(valid.as_slice()).unwrap();
        envelope.ciphertext[0] ^= 1;
        let mut tampered = Vec::new();
        ciborium::into_writer(&envelope, &mut tampered).unwrap();
        std::fs::write(temp.path().join(CACHE_FILE), tampered).unwrap();
        assert!(
            load_cache(&d, Some(temp.path()), cluster, member, &expected)
                .await
                .is_err()
        );

        let mut trailing = valid;
        trailing.push(0);
        std::fs::write(temp.path().join(CACHE_FILE), trailing).unwrap();
        assert!(
            load_cache(&d, Some(temp.path()), cluster, member, &expected)
                .await
                .is_err()
        );

        let envelope = CacheEnvelope {
            version: CACHE_VERSION + 1,
            cluster: address_bytes(cluster),
            member_contract: address_bytes(member),
            commitment: expected,
            nonce: [0u8; 24],
            ciphertext: vec![0u8; 48],
        };
        let mut encoded = Vec::new();
        ciborium::into_writer(&envelope, &mut encoded).unwrap();
        std::fs::write(temp.path().join(CACHE_FILE), encoded).unwrap();
        assert!(
            load_cache(&d, Some(temp.path()), cluster, member, &expected)
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn durable_cache_is_optional() {
        let d = MockDstack::from_label("node");
        let (cluster, member) = addresses();
        let key = [12u8; 32];
        let expected = commitment(&key);
        assert!(!store_cache(&d, None, cluster, member, &expected, &key)
            .await
            .unwrap());
        assert!(load_cache(&d, None, cluster, member, &expected)
            .await
            .unwrap()
            .is_none());
    }
}
