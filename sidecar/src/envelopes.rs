//! Sealed-box envelopes (master spec §3.2, §7.1) + the PeerEndpoint payload.
//!
//! Encryption follows libsodium's sealed box: an ephemeral x25519 keypair, a
//! blake2b-24 nonce over `eph_pub || recipient_pub`, then XSalsa20-Poly1305 via
//! crypto_box. Output is `eph_pub (32) || ciphertext`. Anonymous to the recipient;
//! only the recipient's x25519 secret can open it.

use anyhow::{bail, Result};
use blake2::digest::{Update, VariableOutput};
use blake2::Blake2bVar;
use crypto_box::aead::Aead;
use crypto_box::{Nonce, PublicKey as XPublicKey, SalsaBox, SecretKey as XSecretKey};
use rand::rngs::OsRng;
use serde::{Deserialize, Serialize};
use sha3::{Digest, Keccak256};

/// Reserved on-chain envelopeId for wireguard peer-endpoint exchange (contracts §5.2).
pub fn peer_endpoint_envelope_id() -> [u8; 32] {
    let mut out = [0u8; 32];
    out.copy_from_slice(&Keccak256::digest(b"attestmesh.peer-endpoint.v1"));
    out
}

fn sealed_box_nonce(eph_pub: &[u8; 32], recipient_pub: &[u8; 32]) -> Nonce {
    let mut h = Blake2bVar::new(24).expect("blake2b-24");
    h.update(eph_pub);
    h.update(recipient_pub);
    let mut nonce = [0u8; 24];
    h.finalize_variable(&mut nonce).expect("nonce out");
    *Nonce::from_slice(&nonce)
}

/// Seal `plaintext` to `recipient_xpub`. Returns `eph_pub || ciphertext`.
pub fn seal(recipient_xpub: &[u8; 32], plaintext: &[u8]) -> Result<Vec<u8>> {
    let recipient = XPublicKey::from(*recipient_xpub);
    let eph_secret = XSecretKey::generate(&mut OsRng);
    let eph_pub = eph_secret.public_key();
    let nonce = sealed_box_nonce(eph_pub.as_bytes(), recipient_xpub);

    let salsa = SalsaBox::new(&recipient, &eph_secret);
    let ct = salsa
        .encrypt(&nonce, plaintext)
        .map_err(|_| anyhow::anyhow!("seal failed"))?;

    let mut out = Vec::with_capacity(32 + ct.len());
    out.extend_from_slice(eph_pub.as_bytes());
    out.extend_from_slice(&ct);
    Ok(out)
}

/// Open a sealed box with the recipient's secret. `recipient_xpub` must be the
/// public half of `recipient_secret` (used in the nonce derivation).
pub fn open(
    recipient_secret: &XSecretKey,
    recipient_xpub: &[u8; 32],
    sealed: &[u8],
) -> Result<Vec<u8>> {
    if sealed.len() < 32 {
        bail!("sealed box too short");
    }
    let mut eph = [0u8; 32];
    eph.copy_from_slice(&sealed[..32]);
    let eph_pub = XPublicKey::from(eph);
    let nonce = sealed_box_nonce(&eph, recipient_xpub);

    let salsa = SalsaBox::new(&eph_pub, recipient_secret);
    salsa
        .decrypt(&nonce, &sealed[32..])
        .map_err(|_| anyhow::anyhow!("open failed"))
}

/// The sidecar-internal peer-endpoint payload exchanged over MessageFacet
/// (master spec §7.1 step 7). Carries the peer's wireguard endpoint + Ed25519 key.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PeerEndpoint {
    /// keccak256("attestmesh.peer-endpoint.v1") — demux discriminator.
    pub kind: [u8; 32],
    pub member_id: [u8; 32],
    pub host: String,
    pub port: u16,
    pub wg_pub: [u8; 32],
    pub ed25519_pub: [u8; 32],
}

impl PeerEndpoint {
    pub fn new(
        member_id: [u8; 32],
        host: String,
        port: u16,
        wg_pub: [u8; 32],
        ed25519_pub: [u8; 32],
    ) -> Self {
        Self {
            kind: peer_endpoint_envelope_id(),
            member_id,
            host,
            port,
            wg_pub,
            ed25519_pub,
        }
    }

    pub fn is_peer_endpoint(&self) -> bool {
        self.kind == peer_endpoint_envelope_id()
    }

    pub fn encode(&self) -> Result<Vec<u8>> {
        let mut buf = Vec::new();
        ciborium::into_writer(self, &mut buf)?;
        Ok(buf)
    }

    pub fn decode(bytes: &[u8]) -> Result<Self> {
        Ok(ciborium::from_reader(bytes)?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sealed_box_round_trip() {
        let recipient = XSecretKey::generate(&mut OsRng);
        let recipient_pub = *recipient.public_key().as_bytes();

        let msg = b"hello attestmesh sealed box";
        let sealed = seal(&recipient_pub, msg).unwrap();
        assert_ne!(&sealed[32..], &msg[..]); // actually encrypted
        let opened = open(&recipient, &recipient_pub, &sealed).unwrap();
        assert_eq!(opened, msg);
    }

    #[test]
    fn wrong_recipient_cannot_open() {
        let recipient = XSecretKey::generate(&mut OsRng);
        let recipient_pub = *recipient.public_key().as_bytes();
        let attacker = XSecretKey::generate(&mut OsRng);
        let attacker_pub = *attacker.public_key().as_bytes();

        let sealed = seal(&recipient_pub, b"secret").unwrap();
        assert!(open(&attacker, &attacker_pub, &sealed).is_err());
    }

    #[test]
    fn peer_endpoint_round_trip() {
        let pe = PeerEndpoint::new([7u8; 32], "10.13.1.2".into(), 51820, [9u8; 32], [3u8; 32]);
        assert!(pe.is_peer_endpoint());
        let bytes = pe.encode().unwrap();
        let back = PeerEndpoint::decode(&bytes).unwrap();
        assert_eq!(pe, back);
    }

    #[test]
    fn peer_endpoint_through_sealed_box() {
        let recipient = XSecretKey::generate(&mut OsRng);
        let recipient_pub = *recipient.public_key().as_bytes();
        let pe = PeerEndpoint::new([1u8; 32], "host".into(), 1234, [2u8; 32], [4u8; 32]);

        let sealed = seal(&recipient_pub, &pe.encode().unwrap()).unwrap();
        let opened = open(&recipient, &recipient_pub, &sealed).unwrap();
        assert_eq!(PeerEndpoint::decode(&opened).unwrap(), pe);
    }
}
