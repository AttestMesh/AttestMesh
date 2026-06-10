//! Heartbeat wire format + Ed25519 sign/verify (sidecar spec §11.1).
//!
//! A heartbeat carries the sender's memberId, a millisecond timestamp, and the
//! sender's current connected-set view. It is signed with the sender's Ed25519 key
//! (learned by peers via PeerEndpoint) so it is unspoofable on the wire.

use anyhow::{Context, Result};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use serde::{Deserialize, Serialize};
use sha3::{Digest, Keccak256};

pub const HEARTBEAT_VERSION: u8 = 1;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HeartbeatPayload {
    pub version: u8,
    pub sender_member_id: [u8; 32],
    pub timestamp_ms: u64,
    pub connected_member_ids: Vec<[u8; 32]>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Heartbeat {
    pub payload: HeartbeatPayload,
    /// 64-byte Ed25519 signature (Vec to stay within serde's array support).
    pub signature: Vec<u8>,
}

impl HeartbeatPayload {
    /// Signing input: keccak256(version || sender_member_id || timestamp_ms_be ||
    /// connected_member_ids). Deterministic and independent of CBOR encoding.
    pub fn signing_digest(&self) -> [u8; 32] {
        let mut h = Keccak256::new();
        h.update([self.version]);
        h.update(self.sender_member_id);
        h.update(self.timestamp_ms.to_be_bytes());
        for id in &self.connected_member_ids {
            h.update(id);
        }
        let mut out = [0u8; 32];
        out.copy_from_slice(&h.finalize());
        out
    }
}

pub fn sign(signing_key: &SigningKey, payload: HeartbeatPayload) -> Heartbeat {
    let digest = payload.signing_digest();
    let sig: Signature = signing_key.sign(&digest);
    Heartbeat {
        payload,
        signature: sig.to_bytes().to_vec(),
    }
}

pub fn verify(verifying_key: &VerifyingKey, hb: &Heartbeat) -> bool {
    let sig_bytes: [u8; 64] = match hb.signature.as_slice().try_into() {
        Ok(b) => b,
        Err(_) => return false,
    };
    let digest = hb.payload.signing_digest();
    let sig = Signature::from_bytes(&sig_bytes);
    verifying_key.verify(&digest, &sig).is_ok()
}

pub fn encode(hb: &Heartbeat) -> Result<Vec<u8>> {
    let mut buf = Vec::new();
    ciborium::into_writer(hb, &mut buf)?;
    Ok(buf)
}

pub fn decode(bytes: &[u8]) -> Result<Heartbeat> {
    ciborium::from_reader(bytes).context("decode heartbeat")
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::SigningKey;
    use rand::rngs::OsRng;

    fn payload() -> HeartbeatPayload {
        HeartbeatPayload {
            version: HEARTBEAT_VERSION,
            sender_member_id: [5u8; 32],
            timestamp_ms: 1_700_000_000_000,
            connected_member_ids: vec![[1u8; 32], [2u8; 32]],
        }
    }

    #[test]
    fn sign_verify_round_trip() {
        let sk = SigningKey::generate(&mut OsRng);
        let hb = sign(&sk, payload());
        assert!(verify(&sk.verifying_key(), &hb));
    }

    #[test]
    fn tampered_payload_fails() {
        let sk = SigningKey::generate(&mut OsRng);
        let mut hb = sign(&sk, payload());
        hb.payload.timestamp_ms += 1;
        assert!(!verify(&sk.verifying_key(), &hb));
    }

    #[test]
    fn wrong_key_fails() {
        let sk = SigningKey::generate(&mut OsRng);
        let other = SigningKey::generate(&mut OsRng);
        let hb = sign(&sk, payload());
        assert!(!verify(&other.verifying_key(), &hb));
    }

    #[test]
    fn encode_decode_round_trip() {
        let sk = SigningKey::generate(&mut OsRng);
        let hb = sign(&sk, payload());
        let bytes = encode(&hb).unwrap();
        let back = decode(&bytes).unwrap();
        assert!(verify(&sk.verifying_key(), &back));
    }
}
