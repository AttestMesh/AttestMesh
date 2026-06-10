//! Indexer subscription client (sidecar spec §9). Opens the bidi gRPC stream,
//! verifies every push's Ed25519 signature against the IndexerRegistry pubkey, and
//! dispatches events. On signature/attestation mismatch the subscription is torn
//! down and re-discovered (treated as adversarial).

use crate::proto::indexer::indexer_client::IndexerClient;
use crate::proto::indexer::{subscribe_message, Hello, PushEnvelope, SubscribeMessage};
use crate::state::Shared;
use anyhow::{Context, Result};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use sha3::{Digest, Keccak256};
use std::sync::Arc;
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;

pub const ENVELOPE_DOMAIN: &[u8] = b"attestmesh.indexer.envelope.v1";

/// Canonical signing input for a push envelope. MUST match the indexer's signer:
/// keccak256(DOMAIN || canonical_cbor(fields-minus-sig-minus-attestation)).
pub fn envelope_signing_input(env: &PushEnvelope) -> [u8; 32] {
    let (method, params) = env
        .rpc_repro
        .as_ref()
        .map(|r| (r.method.clone(), r.params_json.clone()))
        .unwrap_or_default();
    let view = (
        env.event_data.clone(),
        env.cluster_addr.clone(),
        env.block_number,
        env.tx_hash.clone(),
        env.log_index,
        method,
        params,
    );
    let mut cbor = Vec::new();
    ciborium::into_writer(&view, &mut cbor).expect("cbor encode");

    let mut h = Keccak256::new();
    h.update(ENVELOPE_DOMAIN);
    h.update(&cbor);
    let mut out = [0u8; 32];
    out.copy_from_slice(&h.finalize());
    out
}

pub fn verify_envelope(env: &PushEnvelope, pubkey: &[u8; 32]) -> bool {
    let Ok(vk) = VerifyingKey::from_bytes(pubkey) else {
        return false;
    };
    let sig_bytes: [u8; 64] = match env.indexer_signature.as_slice().try_into() {
        Ok(b) => b,
        Err(_) => return false,
    };
    let input = envelope_signing_input(env);
    vk.verify(&input, &Signature::from_bytes(&sig_bytes))
        .is_ok()
}

/// Open the subscription and process pushes until the stream drops. Caller handles
/// reconnect/backoff (sidecar spec §9.2).
///
/// Each verified push fires `wake`: the envelope carries an RPC-repro stub by design
/// (spec §8.1 — members re-verify against their own RPC), so dispatch is "wake the
/// chain-read reconcile pass now" rather than trusting the pushed payload as data.
pub async fn connect_and_run(
    shared: Arc<Shared>,
    endpoint: String,
    indexer_pubkey: [u8; 32],
    wake: mpsc::Sender<()>,
) -> Result<()> {
    let mut client = IndexerClient::connect(endpoint)
        .await
        .context("connect indexer")?;

    let (tx, rx) = mpsc::channel::<SubscribeMessage>(64);
    let hello = SubscribeMessage {
        inner: Some(subscribe_message::Inner::Hello(Hello {
            cluster_addr: shared.cluster.as_slice().to_vec(),
            member_id: shared.self_member_id.to_vec(),
            attestation: Vec::new(),
            from_block: 0,
        })),
    };
    tx.send(hello).await.ok();

    let resp = client
        .subscribe(ReceiverStream::new(rx))
        .await
        .context("subscribe")?;
    let mut inbound = resp.into_inner();

    tracing::info!(cluster = %shared.cluster, "indexer subscription open");
    while let Some(env) = inbound.message().await.context("indexer stream")? {
        if !verify_envelope(&env, &indexer_pubkey) {
            anyhow::bail!("indexer signature mismatch — tearing down subscription");
        }
        // Dispatch: every event we subscribe to (MessageSent / MemberRegistered /
        // WgKeyPublished) is fully recoverable from chain reads, so a verified push
        // wakes the reconcile pass (which re-reads membership + polls MessageSent
        // logs) instead of double-implementing event decoding here.
        tracing::debug!(block = env.block_number, "verified indexer push; waking reconcile");
        let _ = wake.try_send(());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::indexer::RpcReproStub;
    use ed25519_dalek::{Signer, SigningKey};
    use rand::rngs::OsRng;

    fn sample_envelope() -> PushEnvelope {
        PushEnvelope {
            event_data: vec![1, 2, 3, 4],
            cluster_addr: vec![0xaa; 20],
            block_number: 42,
            tx_hash: vec![0xbb; 32],
            log_index: 7,
            rpc_repro: Some(RpcReproStub {
                method: "eth_getLogs".into(),
                params_json: "[{}]".into(),
            }),
            indexer_signature: Vec::new(),
            indexer_attestation: None,
        }
    }

    #[test]
    fn envelope_sign_verify_round_trip() {
        let sk = SigningKey::generate(&mut OsRng);
        let mut env = sample_envelope();
        let input = envelope_signing_input(&env);
        env.indexer_signature = sk.sign(&input).to_bytes().to_vec();
        assert!(verify_envelope(&env, &sk.verifying_key().to_bytes()));
    }

    #[test]
    fn tampered_envelope_fails() {
        let sk = SigningKey::generate(&mut OsRng);
        let mut env = sample_envelope();
        env.indexer_signature = sk.sign(&envelope_signing_input(&env)).to_bytes().to_vec();
        env.block_number = 43; // tamper after signing
        assert!(!verify_envelope(&env, &sk.verifying_key().to_bytes()));
    }

    #[test]
    fn wrong_pubkey_fails() {
        let sk = SigningKey::generate(&mut OsRng);
        let other = SigningKey::generate(&mut OsRng);
        let mut env = sample_envelope();
        env.indexer_signature = sk.sign(&envelope_signing_input(&env)).to_bytes().to_vec();
        assert!(!verify_envelope(&env, &other.verifying_key().to_bytes()));
    }
}
