//! Indexer subscription client (sidecar spec §9). Opens the bidi gRPC stream,
//! verifies every push's Ed25519 signature against the IndexerRegistry pubkey, and
//! dispatches events. On signature/attestation mismatch the subscription is torn
//! down and re-discovered (treated as adversarial).

use crate::proto::indexer::indexer_client::IndexerClient;
use crate::proto::indexer::{
    subscribe_message, Ack, DeliveryCursor, Hello, PushEnvelope, SubscribeMessage,
};
use crate::state::Shared;
use alloy::sol_types::SolEvent;
use alloy_rlp::Decodable;
use anyhow::{Context, Result};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use sha3::{Digest, Keccak256};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::sync::{mpsc, oneshot};
use tokio_stream::wrappers::ReceiverStream;

pub const ENVELOPE_DOMAIN: &[u8] = b"attestmesh.indexer.envelope.v1";
pub const PROTOCOL_VERSION: u32 = 3;
pub const CHECKPOINT_LOG_INDEX: u64 = u64::MAX;
const CURSOR_FILE: &str = "indexer-cursor.v1";
const CURSOR_BYTES: u64 = 69;

/// Load the last durably handled cursor. A missing file is a genuinely fresh
/// subscription; malformed state is ignored conservatively and logged.
pub async fn load_cursor(
    state_dir: Option<&Path>,
    cluster: alloy::primitives::Address,
    member_id: &[u8; 32],
) -> Option<(u64, u64)> {
    let bytes = match crate::storage::read_optional(state_dir, CURSOR_FILE, CURSOR_BYTES).await {
        Ok(Some(bytes)) => bytes,
        Ok(None) => return None,
        Err(error) => {
            tracing::warn!(error = ?error, "Indexer cursor cache read failed; replay baseline reset");
            return None;
        }
    };
    if bytes.len() != CURSOR_BYTES as usize || bytes[0] != 1 {
        tracing::warn!("Indexer cursor cache has an unknown or truncated format; ignoring");
        return None;
    }
    if bytes[1..21] != cluster.as_slice()[..] || bytes[21..53] != member_id[..] {
        tracing::warn!("Indexer cursor cache belongs to another cluster/member; ignoring");
        return None;
    }
    let mut block = [0u8; 8];
    let mut log_index = [0u8; 8];
    block.copy_from_slice(&bytes[53..61]);
    log_index.copy_from_slice(&bytes[61..69]);
    Some((u64::from_be_bytes(block), u64::from_be_bytes(log_index)))
}

async fn store_cursor(
    state_dir: Option<&Path>,
    cluster: alloy::primitives::Address,
    member_id: &[u8; 32],
    block: u64,
    log_index: u64,
) -> Result<()> {
    let Some(state_dir) = state_dir else {
        return Ok(());
    };
    let mut bytes = Vec::with_capacity(CURSOR_BYTES as usize);
    bytes.push(1);
    bytes.extend_from_slice(cluster.as_slice());
    bytes.extend_from_slice(member_id);
    bytes.extend_from_slice(&block.to_be_bytes());
    bytes.extend_from_slice(&log_index.to_be_bytes());
    crate::storage::atomic_write(state_dir, CURSOR_FILE, &bytes).await?;
    Ok(())
}

#[derive(Debug)]
pub enum IndexedEvent {
    MessageSent {
        sender: [u8; 32],
        envelope_id: [u8; 32],
        ciphertext: Vec<u8>,
        block_number: u64,
    },
    Reconcile,
}

pub struct DispatchRequest {
    pub event: IndexedEvent,
    pub completion: oneshot::Sender<std::result::Result<(), String>>,
}

pub fn is_checkpoint(env: &PushEnvelope) -> bool {
    env.event_data.is_empty()
        && env.tx_hash.is_empty()
        && env.log_index == CHECKPOINT_LOG_INDEX
        && env.rpc_repro.is_none()
}

/// Decode and validate the signed event payload. The Indexer is selected by the
/// on-chain registry, but malformed/cross-cluster data still tears down the stream.
pub fn decode_event(
    env: &PushEnvelope,
    expected_cluster: alloy::primitives::Address,
    self_member_id: &[u8; 32],
) -> Result<IndexedEvent> {
    anyhow::ensure!(
        env.cluster_addr.as_slice() == expected_cluster.as_slice(),
        "indexer envelope cluster mismatch"
    );
    anyhow::ensure!(
        env.tx_hash.len() == 32,
        "indexer event tx_hash must be 32 bytes"
    );
    anyhow::ensure!(
        env.log_index != CHECKPOINT_LOG_INDEX,
        "event uses checkpoint cursor"
    );
    let repro = env
        .rpc_repro
        .as_ref()
        .context("indexer event missing rpc_repro")?;
    anyhow::ensure!(
        repro.method == "eth_getLogs",
        "unsupported rpc_repro method"
    );

    let mut encoded = env.event_data.as_slice();
    let log = alloy::primitives::Log::decode(&mut encoded).context("decode indexer event RLP")?;
    anyhow::ensure!(encoded.is_empty(), "trailing bytes in indexer event RLP");
    anyhow::ensure!(log.address == expected_cluster, "RLP log address mismatch");
    let topic0 = log
        .data
        .topics()
        .first()
        .context("indexer event has no topic0")?;

    use crate::chain::abi::IMessageEvents::MessageSent;
    if *topic0 != MessageSent::SIGNATURE_HASH {
        return Ok(IndexedEvent::Reconcile);
    }

    let message = MessageSent::decode_log_data(&log.data, true)
        .map_err(|e| anyhow::anyhow!("decode MessageSent: {e}"))?;
    anyhow::ensure!(
        message.recipientMemberId.0 == *self_member_id,
        "MessageSent recipient mismatch"
    );
    Ok(IndexedEvent::MessageSent {
        sender: message.senderMemberId.0,
        envelope_id: message.envelopeId.0,
        ciphertext: message.ciphertext.to_vec(),
        block_number: env.block_number,
    })
}

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
/// Events are processed serially and Ack'd only after the bring-up dispatcher reports
/// completion. The rpc_repro remains signed diagnostic material; protocol v2 trusts
/// the registry-pinned Indexer and does not execute a member-side `eth_getLogs` call.
pub async fn connect_and_run(
    shared: Arc<Shared>,
    endpoint: String,
    indexer_pubkey: [u8; 32],
    dispatch: mpsc::Sender<DispatchRequest>,
    state_dir: Option<PathBuf>,
) -> Result<()> {
    let state_dir = state_dir
        .context("SIDECAR_STATE_DIR is required for protocol-v3 durable delivery cursors")?;
    // Explicit TLS config for https endpoints (the gateway-terminated route).
    // assume_http2: the dstack gateway serves gRPC/h2 but may answer ALPN with
    // http/1.1 — gRPC requires h2, so trust the verified reality over ALPN.
    let mut ep =
        tonic::transport::Channel::from_shared(endpoint.clone()).context("indexer endpoint URI")?;
    if endpoint.starts_with("https://") {
        let tls = tonic::transport::ClientTlsConfig::new()
            .with_native_roots()
            .assume_http2(true);
        ep = ep.tls_config(tls).context("indexer TLS config")?;
    }
    let channel = ep.connect().await.context("connect indexer")?;
    let mut client = IndexerClient::new(channel);

    let (tx, rx) = mpsc::channel::<SubscribeMessage>(64);
    // The exact sidecar cursor is authoritative across independent Indexer replicas.
    // Keep `from_block` populated as a boundary-block fallback for a pre-v3 server.
    let resume = shared.indexer_resume_cursor().await;
    let from_block = resume.map_or(0, |cursor| cursor.0);
    let hello = SubscribeMessage {
        inner: Some(subscribe_message::Inner::Hello(Hello {
            cluster_addr: shared.cluster.as_slice().to_vec(),
            member_id: shared.self_member_id.to_vec(),
            attestation: Vec::new(),
            from_block,
            protocol_version: PROTOCOL_VERSION,
            resume_cursor: resume.map(|(block_number, log_index)| DeliveryCursor {
                block_number,
                log_index,
            }),
        })),
    };
    tx.send(hello).await.context("send indexer Hello")?;

    let resp = client
        .subscribe(ReceiverStream::new(rx))
        .await
        .context("subscribe")?;
    let mut inbound = resp.into_inner();

    tracing::info!(cluster = %shared.cluster, "indexer subscription open");
    shared.set_indexer_connected(true).await;
    let mut last_handled = resume;
    while let Some(env) = inbound.message().await.context("indexer stream")? {
        if !verify_envelope(&env, &indexer_pubkey) {
            anyhow::bail!("indexer signature mismatch — tearing down subscription");
        }
        anyhow::ensure!(
            env.cluster_addr.as_slice() == shared.cluster.as_slice(),
            "indexer envelope cluster mismatch"
        );

        let position = (env.block_number, env.log_index);
        let checkpoint = is_checkpoint(&env);
        let decoded = if checkpoint {
            None
        } else {
            // Validate every signed event before duplicate suppression. A repeated
            // cursor is allowed by at-least-once delivery, but it is not a license
            // for the Indexer to send a malformed envelope at that position.
            Some(decode_event(&env, shared.cluster, &shared.self_member_id)?)
        };
        if decoded.is_some() && last_handled.is_some_and(|last| position <= last) {
            tracing::debug!(
                block = position.0,
                log_index = position.1,
                "duplicate indexer event; acknowledging without redispatch"
            );
        } else if checkpoint {
            anyhow::ensure!(
                env.cluster_addr.len() == 20,
                "checkpoint cluster must be 20 bytes"
            );
        } else {
            let (completion, completed) = oneshot::channel();
            dispatch
                .send(DispatchRequest {
                    event: decoded.expect("non-checkpoint envelope was decoded"),
                    completion,
                })
                .await
                .context("indexer event dispatcher stopped")?;
            completed
                .await
                .context("indexer event dispatcher dropped completion")?
                .map_err(anyhow::Error::msg)?;
        }

        let advances = last_handled.map_or(true, |last| position > last);
        if advances {
            // Make the handled position crash-durable before telling any replica it
            // may advance its local cursor. If this write fails, tear down without
            // Ack so the position is replayed on reconnect.
            store_cursor(
                Some(state_dir.as_path()),
                shared.cluster,
                &shared.self_member_id,
                position.0,
                position.1,
            )
            .await
            .context("persist Indexer cursor before Ack")?;
            last_handled = Some(position);
        }
        shared
            .set_indexer_progress(env.block_number, env.log_index, checkpoint)
            .await;
        let ack = SubscribeMessage {
            inner: Some(subscribe_message::Inner::Ack(Ack {
                block_number: env.block_number,
                log_index: env.log_index,
            })),
        };
        tx.send(ack).await.context("send indexer Ack")?;
        tracing::debug!(
            block = env.block_number,
            log_index = env.log_index,
            checkpoint,
            "handled + acknowledged indexer envelope"
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::chain::abi::IMessageEvents::MessageSent;
    use crate::proto::indexer::RpcReproStub;
    use alloy::primitives::{Address, B256, U256};
    use alloy::sol_types::SolEvent;
    use alloy_rlp::Encodable;
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

    fn encoded_message_log(cluster: Address, recipient: [u8; 32]) -> Vec<u8> {
        let sender = B256::repeat_byte(0x11);
        let envelope_id = B256::repeat_byte(0x22);
        let payload = b"sealed-message";
        let mut data = Vec::new();
        data.extend_from_slice(B256::from(U256::from(32)).as_slice());
        data.extend_from_slice(B256::from(U256::from(payload.len())).as_slice());
        let mut padded = payload.to_vec();
        padded.resize(32, 0);
        data.extend_from_slice(&padded);
        let topics = vec![
            MessageSent::SIGNATURE_HASH,
            sender,
            B256::from(recipient),
            envelope_id,
        ];

        let mut topics_rlp = Vec::new();
        let topics_len: usize = topics.iter().map(|topic| topic.as_slice().length()).sum();
        alloy_rlp::Header {
            list: true,
            payload_length: topics_len,
        }
        .encode(&mut topics_rlp);
        for topic in &topics {
            topic.as_slice().encode(&mut topics_rlp);
        }
        let addr = cluster.as_slice();
        let payload_length = addr.length() + topics_rlp.len() + data.as_slice().length();
        let mut out = Vec::new();
        alloy_rlp::Header {
            list: true,
            payload_length,
        }
        .encode(&mut out);
        addr.encode(&mut out);
        out.extend_from_slice(&topics_rlp);
        data.as_slice().encode(&mut out);
        out
    }

    fn message_envelope(cluster: Address, recipient: [u8; 32]) -> PushEnvelope {
        PushEnvelope {
            event_data: encoded_message_log(cluster, recipient),
            cluster_addr: cluster.as_slice().to_vec(),
            block_number: 99,
            tx_hash: vec![0xbb; 32],
            log_index: 4,
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

    #[test]
    fn decodes_addressed_message_and_rejects_wrong_recipient_or_cluster() {
        let cluster = Address::repeat_byte(0xc1);
        let recipient = [0x44; 32];
        let env = message_envelope(cluster, recipient);
        match decode_event(&env, cluster, &recipient).unwrap() {
            IndexedEvent::MessageSent {
                sender,
                envelope_id,
                ciphertext,
                block_number,
            } => {
                assert_eq!(sender, [0x11; 32]);
                assert_eq!(envelope_id, [0x22; 32]);
                assert_eq!(ciphertext, b"sealed-message");
                assert_eq!(block_number, 99);
            }
            IndexedEvent::Reconcile => panic!("expected MessageSent"),
        }
        assert!(decode_event(&env, cluster, &[0x45; 32]).is_err());
        assert!(decode_event(&env, Address::repeat_byte(0xc2), &recipient).is_err());

        let mut malformed = env.clone();
        malformed.event_data = vec![0xff];
        assert!(decode_event(&malformed, cluster, &recipient).is_err());

        let mut trailing = env;
        trailing.event_data.push(0x00);
        assert!(decode_event(&trailing, cluster, &recipient).is_err());
    }

    #[test]
    fn checkpoint_shape_is_distinct_from_events() {
        let checkpoint = PushEnvelope {
            event_data: Vec::new(),
            cluster_addr: vec![0xcc; 20],
            block_number: 123,
            tx_hash: Vec::new(),
            log_index: CHECKPOINT_LOG_INDEX,
            rpc_repro: None,
            indexer_signature: vec![0; 64],
            indexer_attestation: None,
        };
        assert!(is_checkpoint(&checkpoint));
        let mut malformed = checkpoint;
        malformed.tx_hash = vec![0; 32];
        assert!(!is_checkpoint(&malformed));
    }

    #[tokio::test]
    async fn checkpoint_cursor_round_trips_and_corruption_is_ignored() {
        let temp = tempfile::tempdir().unwrap();
        let cluster = Address::repeat_byte(0xc1);
        let member = [0xa1; 32];
        assert_eq!(load_cursor(Some(temp.path()), cluster, &member).await, None);
        store_cursor(
            Some(temp.path()),
            cluster,
            &member,
            123,
            CHECKPOINT_LOG_INDEX,
        )
        .await
        .unwrap();
        assert_eq!(
            load_cursor(Some(temp.path()), cluster, &member).await,
            Some((123, CHECKPOINT_LOG_INDEX))
        );
        assert_eq!(
            load_cursor(Some(temp.path()), Address::repeat_byte(0xc2), &member).await,
            None
        );
        tokio::fs::write(temp.path().join(CURSOR_FILE), b"bad")
            .await
            .unwrap();
        assert_eq!(load_cursor(Some(temp.path()), cluster, &member).await, None);
    }
}
