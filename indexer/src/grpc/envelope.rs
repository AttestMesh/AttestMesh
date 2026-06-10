//! PushEnvelope construction + Ed25519 signing (spec §11, indexer.proto trailing
//! comment). This is the security-critical interop boundary with the sidecar: the
//! signing convention MUST match byte-for-byte on both ends.
//!
//! ```text
//! signing_input = keccak256( b"attestmesh.indexer.envelope.v1" || canonical_cbor )
//! canonical_cbor = deterministic CBOR of the tuple
//!   (event_data, cluster_addr, block_number, tx_hash, log_index,
//!    rpc_repro.method, rpc_repro.params_json)
//! ```
//!
//! i.e. every PushEnvelope field EXCLUDING `indexer_signature` and
//! `indexer_attestation`. The signature is Ed25519 over `signing_input`, verified
//! against `IndexerRegistry.pubKey`.

use crate::chain::repro::ReproStub;
use crate::chain::watcher::IndexedLog;
use crate::pb::{PushEnvelope, RpcReproStub};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use sha3::{Digest, Keccak256};

/// Domain separation tag prefixed before the canonical CBOR (proto trailing comment).
pub const ENVELOPE_DOMAIN: &[u8] = b"attestmesh.indexer.envelope.v1";

/// The signable view of an envelope: exactly the fields included in the signature,
/// in the canonical order. Serialized to deterministic CBOR as a tuple (a CBOR array)
/// via `ciborium`.
type SigningTuple = (
    Vec<u8>, // event_data
    Vec<u8>, // cluster_addr
    u64,     // block_number
    Vec<u8>, // tx_hash
    u64,     // log_index
    String,  // rpc_repro.method
    String,  // rpc_repro.params_json
);

fn signing_tuple(e: &PushEnvelope) -> SigningTuple {
    let (method, params_json) = match &e.rpc_repro {
        Some(r) => (r.method.clone(), r.params_json.clone()),
        None => (String::new(), String::new()),
    };
    (
        e.event_data.clone(),
        e.cluster_addr.clone(),
        e.block_number,
        e.tx_hash.clone(),
        e.log_index,
        method,
        params_json,
    )
}

/// `keccak256(ENVELOPE_DOMAIN || canonical_cbor(signing_tuple(e)))`.
pub fn signing_input(e: &PushEnvelope) -> [u8; 32] {
    let mut cbor = Vec::new();
    ciborium::into_writer(&signing_tuple(e), &mut cbor).expect("cbor encode envelope");

    let mut h = Keccak256::new();
    h.update(ENVELOPE_DOMAIN);
    h.update(&cbor);
    let mut out = [0u8; 32];
    out.copy_from_slice(&h.finalize());
    out
}

/// RLP-encode the verifiable `event_data` = `[address, [topic, ...], data]` (spec
/// §8.1: "RLP-encoded log topics + data"). This is exactly the shape a member can
/// rebuild from an `eth_getLogs` result, so it is independently verifiable.
pub fn encode_event_data(log: &IndexedLog) -> Vec<u8> {
    use alloy_rlp::Encodable;

    // Encode topics as an RLP list of 32-byte strings.
    let mut topics_rlp = Vec::new();
    {
        let payload_len: usize = log.topics.iter().map(|t| t.as_slice().length()).sum();
        alloy_rlp::Header {
            list: true,
            payload_length: payload_len,
        }
        .encode(&mut topics_rlp);
        for t in &log.topics {
            t.as_slice().encode(&mut topics_rlp);
        }
    }

    // Outer list: [address, topics_list, data].
    let addr = log.cluster_addr.as_slice();
    let data = log.data.as_slice();
    let payload_length = addr.length() + topics_rlp.len() + data.length();

    let mut out = Vec::new();
    alloy_rlp::Header {
        list: true,
        payload_length,
    }
    .encode(&mut out);
    addr.encode(&mut out);
    out.extend_from_slice(&topics_rlp);
    data.encode(&mut out);
    out
}

/// Build an unsigned PushEnvelope from an indexed log + its repro stub. `signature`
/// and `attestation` are left empty for the caller to fill via [`sign`].
pub fn build_envelope(log: &IndexedLog, stub: &ReproStub) -> PushEnvelope {
    PushEnvelope {
        event_data: encode_event_data(log),
        cluster_addr: log.cluster_addr.as_slice().to_vec(),
        block_number: log.block_number,
        tx_hash: log.tx_hash.as_slice().to_vec(),
        log_index: log.log_index,
        rpc_repro: Some(RpcReproStub {
            method: stub.method.clone(),
            params_json: stub.params_json.clone(),
        }),
        indexer_signature: Vec::new(),
        indexer_attestation: None,
    }
}

/// Sign an envelope in place: computes `signing_input` over the signature-excluded
/// view and sets `indexer_signature` (spec §11 steps 2–4).
pub fn sign(key: &SigningKey, envelope: &mut PushEnvelope) {
    // Defensive: signing must always be computed over the canonical view regardless of
    // any pre-set signature/attestation bytes.
    envelope.indexer_signature = Vec::new();
    let input = signing_input(envelope);
    let sig: Signature = key.sign(&input);
    envelope.indexer_signature = sig.to_bytes().to_vec();
}

/// Verify an envelope's signature against `pubkey` (the subscriber-side mirror, spec
/// §11 final paragraph). Recomputes `signing_input` over the signature-excluded view.
pub fn verify(pubkey: &VerifyingKey, envelope: &PushEnvelope) -> bool {
    let sig_bytes: [u8; 64] = match envelope.indexer_signature.as_slice().try_into() {
        Ok(b) => b,
        Err(_) => return false,
    };
    let input = signing_input(envelope);
    let sig = Signature::from_bytes(&sig_bytes);
    pubkey.verify(&input, &sig).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::chain::watcher::EventKind;
    use alloy::primitives::{Address, B256};
    use ed25519_dalek::SigningKey;
    use rand::rngs::OsRng;

    fn fixture_log() -> IndexedLog {
        IndexedLog {
            cluster_addr: Address::repeat_byte(0xc1),
            block_number: 4242,
            tx_hash: B256::repeat_byte(0xbb),
            log_index: 3,
            topics: vec![B256::repeat_byte(0xaa), B256::repeat_byte(0x01)],
            data: vec![9, 8, 7, 6],
            kind: EventKind::WgKeyPublished {
                member_id: B256::repeat_byte(0x01),
            },
        }
    }

    fn fixture_envelope() -> PushEnvelope {
        let log = fixture_log();
        let stub = crate::chain::repro::build_stub(&log);
        build_envelope(&log, &stub)
    }

    #[test]
    fn sign_and_verify_round_trip() {
        let key = SigningKey::generate(&mut OsRng);
        let mut env = fixture_envelope();
        sign(&key, &mut env);
        assert!(verify(&key.verifying_key(), &env));
    }

    #[test]
    fn tampered_event_data_fails() {
        let key = SigningKey::generate(&mut OsRng);
        let mut env = fixture_envelope();
        sign(&key, &mut env);
        env.event_data.push(0xff); // tamper after signing
        assert!(!verify(&key.verifying_key(), &env));
    }

    #[test]
    fn tampered_block_number_fails() {
        let key = SigningKey::generate(&mut OsRng);
        let mut env = fixture_envelope();
        sign(&key, &mut env);
        env.block_number += 1;
        assert!(!verify(&key.verifying_key(), &env));
    }

    #[test]
    fn tampered_repro_params_fails() {
        let key = SigningKey::generate(&mut OsRng);
        let mut env = fixture_envelope();
        sign(&key, &mut env);
        if let Some(r) = env.rpc_repro.as_mut() {
            r.params_json.push(' ');
        }
        assert!(!verify(&key.verifying_key(), &env));
    }

    #[test]
    fn wrong_key_fails() {
        let key = SigningKey::generate(&mut OsRng);
        let other = SigningKey::generate(&mut OsRng);
        let mut env = fixture_envelope();
        sign(&key, &mut env);
        assert!(!verify(&other.verifying_key(), &env));
    }

    #[test]
    fn attestation_field_is_excluded_from_signature() {
        // Setting/clearing indexer_attestation must NOT change validity, since it is
        // excluded from the signing view (proto trailing comment).
        let key = SigningKey::generate(&mut OsRng);
        let mut env = fixture_envelope();
        sign(&key, &mut env);
        assert!(verify(&key.verifying_key(), &env));

        env.indexer_attestation = Some(crate::pb::IndexerAttestation {
            quote: vec![1, 2, 3],
            expected_code_id: B256::repeat_byte(0x07).to_vec(),
            expected_pubkey: key.verifying_key().to_bytes().to_vec(),
        });
        assert!(
            verify(&key.verifying_key(), &env),
            "attestation is outside the signed view"
        );
    }

    #[test]
    fn event_data_rlp_round_trips_to_address_topics_data() {
        use alloy_rlp::Decodable;
        let log = fixture_log();
        let encoded = encode_event_data(&log);
        // Decode as the canonical alloy primitive Log to confirm reproducibility.
        let mut slice = encoded.as_slice();
        let decoded = alloy::primitives::Log::decode(&mut slice).expect("rlp decode log");
        assert_eq!(decoded.address, log.cluster_addr);
        assert_eq!(decoded.data.topics(), log.topics.as_slice());
        assert_eq!(decoded.data.data.as_ref(), log.data.as_slice());
    }
}
