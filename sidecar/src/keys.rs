//! Attestation-bound key derivation (sidecar spec §6).
//!
//! All key material comes from the dstack runtime's `derive_key`. The Curve25519
//! identity root yields both an x25519 (sealed-box) key and an Ed25519 (heartbeat
//! signing) key — they share one stored 32-byte secret. The wireguard key and the
//! secp256k1 binding key are derived under their own purpose strings.

use crate::dstack::DstackRuntime;
use anyhow::Result;
use crypto_box::{PublicKey as XPublicKey, SecretKey as XSecretKey};
use ed25519_dalek::{SigningKey, VerifyingKey};
use zeroize::Zeroizing;

pub const PURPOSE_IDENTITY: &str = "attestmesh.identity.v1";
pub const PURPOSE_WIREGUARD: &str = "attestmesh.wireguard.v1";
pub const PURPOSE_BINDING: &str = "attestmesh.binding.v1";
pub const PURPOSE_CSK: &str = "attestmesh.cluster-shared.v1";
pub const CSK_SUBKEY: &str = "csk-v1";

/// All node identity key material. Secrets zeroize on drop.
pub struct KeyMaterial {
    pub x_secret: XSecretKey,
    pub ed_signing: SigningKey,
    pub wg_secret: XSecretKey,
    /// secp256k1 binding private key (raw scalar). The chain layer builds the
    /// alloy signer from this; it signs the registration binding + every UserOpHash.
    pub binding_seed: Zeroizing<[u8; 32]>,

    pub x_pub: [u8; 32],
    pub ed25519_pub: [u8; 32],
    pub wg_pub: [u8; 32],
}

impl KeyMaterial {
    pub fn x_public(&self) -> XPublicKey {
        self.x_secret.public_key()
    }
}

/// Derive the full identity key set from the attestation-bound seed.
pub async fn derive_all(dstack: &dyn DstackRuntime) -> Result<KeyMaterial> {
    let identity_seed = dstack.derive_key(PURPOSE_IDENTITY, "").await?;
    let wg_seed = dstack.derive_key(PURPOSE_WIREGUARD, "").await?;
    let binding_seed = dstack.derive_key(PURPOSE_BINDING, "").await?;

    // Ed25519 signing key from the identity seed.
    let ed_signing = SigningKey::from_bytes(&identity_seed);
    let ed_verifying: VerifyingKey = ed_signing.verifying_key();

    // x25519 sealed-box key from the SAME identity seed (one stored secret).
    let x_secret = XSecretKey::from(*identity_seed);
    let x_pub = x_secret.public_key();

    // wireguard key from its own purpose.
    let wg_secret = XSecretKey::from(*wg_seed);
    let wg_pub = wg_secret.public_key();

    Ok(KeyMaterial {
        x_pub: *x_pub.as_bytes(),
        ed25519_pub: ed_verifying.to_bytes(),
        wg_pub: *wg_pub.as_bytes(),
        x_secret,
        ed_signing,
        wg_secret,
        binding_seed: Zeroizing::new(*binding_seed),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dstack::MockDstack;

    #[tokio::test]
    async fn derive_all_is_deterministic_per_tee_state() {
        let d1 = MockDstack::from_label("node-A");
        let k1 = derive_all(&d1).await.unwrap();
        // Same TEE state → same keys.
        let d1b = MockDstack::from_label("node-A");
        let k1b = derive_all(&d1b).await.unwrap();
        assert_eq!(k1.x_pub, k1b.x_pub);
        assert_eq!(k1.ed25519_pub, k1b.ed25519_pub);
        assert_eq!(k1.wg_pub, k1b.wg_pub);
        assert_eq!(*k1.binding_seed, *k1b.binding_seed);

        // Different TEE state → different keys.
        let d2 = MockDstack::from_label("node-B");
        let k2 = derive_all(&d2).await.unwrap();
        assert_ne!(k1.x_pub, k2.x_pub);
        assert_ne!(k1.wg_pub, k2.wg_pub);
    }

    #[tokio::test]
    async fn x_and_wg_keys_differ() {
        let d = MockDstack::from_label("node");
        let k = derive_all(&d).await.unwrap();
        assert_ne!(
            k.x_pub, k.wg_pub,
            "x25519 and wireguard keys must be distinct"
        );
    }
}
