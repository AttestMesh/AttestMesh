//! Cluster Shared Key lifecycle (master spec §8, sidecar spec §13).
//!
//! Origination (derive + commitment), peer-pull serve (seal to requester's xPubKey),
//! onboardee pull (open + verify against the on-chain commitment), and sealed-store
//! persistence. The CSK plaintext never touches the chain — only its keccak256
//! commitment does.

use crate::dstack::DstackRuntime;
use crate::envelopes::{open, seal};
use crate::keys::{CSK_SUBKEY, PURPOSE_CSK};
use anyhow::{bail, Result};
use crypto_box::SecretKey as XSecretKey;
use sha3::{Digest, Keccak256};
use zeroize::Zeroizing;

pub const CSK_SEAL_LABEL: &str = "attestmesh.csk.v1";

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

/// Seal the CSK to the node's dstack sealed store (master spec §8.5).
pub async fn seal_to_store(dstack: &dyn DstackRuntime, csk: &[u8; 32]) -> Result<()> {
    dstack.seal(CSK_SEAL_LABEL, csk).await
}

/// Unseal the CSK on restart (master spec §7.1 step 4 restart path).
pub async fn unseal_from_store(dstack: &dyn DstackRuntime) -> Result<Option<Zeroizing<[u8; 32]>>> {
    match dstack.unseal(CSK_SEAL_LABEL).await? {
        Some(b) if b.len() == 32 => {
            let mut c = [0u8; 32];
            c.copy_from_slice(&b);
            Ok(Some(Zeroizing::new(c)))
        }
        _ => Ok(None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dstack::MockDstack;
    use rand::rngs::OsRng;

    #[tokio::test]
    async fn originator_derive_and_commitment() {
        let d = MockDstack::from_label("originator");
        let csk = derive_originator(&d).await.unwrap();
        let c = commitment(&csk);
        assert_eq!(c.to_vec(), Keccak256::digest(*csk).to_vec());

        // Deterministic across restarts of the same TEE state.
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

        // A malicious peer seals a *different* key.
        let sealed = seal_for_peer(&[99u8; 32], &onboardee_pub).unwrap();
        assert!(open_pulled(&sealed, &onboardee, &onboardee_pub, &real_commitment).is_err());
    }

    #[tokio::test]
    async fn seal_unseal_round_trip() {
        let d = MockDstack::from_label("node");
        let csk = [7u8; 32];
        assert!(unseal_from_store(&d).await.unwrap().is_none());
        seal_to_store(&d, &csk).await.unwrap();
        let back = unseal_from_store(&d).await.unwrap().unwrap();
        assert_eq!(*back, csk);
    }
}
