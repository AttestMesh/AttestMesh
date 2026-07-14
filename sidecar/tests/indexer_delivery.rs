use alloy::primitives::{Address, Bytes, Log, LogData, B256, U256};
use alloy::sol_types::SolEvent;
use alloy_rlp::Encodable;
use cluster_mesh_agent::chain::abi::IMessageEvents::MessageSent;
use cluster_mesh_agent::dstack::MockDstack;
use cluster_mesh_agent::indexer_client::{self, DispatchRequest, IndexedEvent};
use cluster_mesh_agent::keys;
use cluster_mesh_agent::proto::indexer::indexer_server::{Indexer, IndexerServer};
use cluster_mesh_agent::proto::indexer::{
    subscribe_message, PushEnvelope, RpcReproStub, SubscribeMessage,
};
use cluster_mesh_agent::state::Shared;
use ed25519_dalek::{Signer, SigningKey};
use rand::rngs::OsRng;
use std::pin::Pin;
use std::sync::Arc;
use tokio::sync::mpsc;
use tokio_stream::wrappers::{ReceiverStream, TcpListenerStream};
use tokio_stream::{Stream, StreamExt};
use tonic::{Request, Response, Status, Streaming};

fn encode_log(log: &Log) -> Vec<u8> {
    let mut topics_rlp = Vec::new();
    let topics_len: usize = log
        .data
        .topics()
        .iter()
        .map(|topic| topic.as_slice().length())
        .sum();
    alloy_rlp::Header {
        list: true,
        payload_length: topics_len,
    }
    .encode(&mut topics_rlp);
    for topic in log.data.topics() {
        topic.as_slice().encode(&mut topics_rlp);
    }

    let data = log.data.data.as_ref();
    let payload_length = log.address.as_slice().length() + topics_rlp.len() + data.length();
    let mut encoded = Vec::new();
    alloy_rlp::Header {
        list: true,
        payload_length,
    }
    .encode(&mut encoded);
    log.address.as_slice().encode(&mut encoded);
    encoded.extend_from_slice(&topics_rlp);
    data.encode(&mut encoded);
    encoded
}

fn sign(mut envelope: PushEnvelope, key: &SigningKey) -> PushEnvelope {
    envelope.indexer_signature = key
        .sign(&indexer_client::envelope_signing_input(&envelope))
        .to_bytes()
        .to_vec();
    envelope
}

struct FakeIndexer {
    signing_key: SigningKey,
    cluster: Address,
    acks: mpsc::UnboundedSender<(u64, u64)>,
    hellos: mpsc::UnboundedSender<(u32, u64, Option<(u64, u64)>)>,
}

#[tonic::async_trait]
impl Indexer for FakeIndexer {
    type SubscribeStream =
        Pin<Box<dyn Stream<Item = Result<PushEnvelope, Status>> + Send + 'static>>;

    async fn subscribe(
        &self,
        request: Request<Streaming<SubscribeMessage>>,
    ) -> Result<Response<Self::SubscribeStream>, Status> {
        let mut inbound = request.into_inner();
        let hello = match inbound.next().await {
            Some(Ok(SubscribeMessage {
                inner: Some(subscribe_message::Inner::Hello(hello)),
            })) => hello,
            _ => return Err(Status::invalid_argument("missing Hello")),
        };
        if hello.protocol_version != indexer_client::PROTOCOL_VERSION {
            return Err(Status::failed_precondition("wrong protocol version"));
        }
        let exact = hello
            .resume_cursor
            .map(|cursor| (cursor.block_number, cursor.log_index));
        let _ = self
            .hellos
            .send((hello.protocol_version, hello.from_block, exact));

        let sender = B256::repeat_byte(0x33);
        let recipient = B256::from_slice(&hello.member_id);
        let envelope_id = B256::repeat_byte(0x44);
        let ciphertext = b"sealed-indexed-message";
        let mut data = Vec::new();
        data.extend_from_slice(B256::from(U256::from(32)).as_slice());
        data.extend_from_slice(B256::from(U256::from(ciphertext.len())).as_slice());
        let mut padded = ciphertext.to_vec();
        padded.resize(32, 0);
        data.extend_from_slice(&padded);
        let log = Log {
            address: self.cluster,
            data: LogData::new_unchecked(
                vec![MessageSent::SIGNATURE_HASH, sender, recipient, envelope_id],
                Bytes::from(data),
            ),
        };
        let event = sign(
            PushEnvelope {
                event_data: encode_log(&log),
                cluster_addr: self.cluster.as_slice().to_vec(),
                block_number: 77,
                tx_hash: vec![0xaa; 32],
                log_index: 3,
                rpc_repro: Some(RpcReproStub {
                    method: "eth_getLogs".into(),
                    params_json: "[{}]".into(),
                }),
                indexer_signature: Vec::new(),
                indexer_attestation: None,
            },
            &self.signing_key,
        );
        let checkpoint = sign(
            PushEnvelope {
                event_data: Vec::new(),
                cluster_addr: self.cluster.as_slice().to_vec(),
                block_number: 78,
                tx_hash: Vec::new(),
                log_index: u64::MAX,
                rpc_repro: None,
                indexer_signature: Vec::new(),
                indexer_attestation: None,
            },
            &self.signing_key,
        );

        let (out_tx, out_rx) = mpsc::channel(2);
        out_tx.send(event).await.unwrap();
        let acks = self.acks.clone();
        tokio::spawn(async move {
            let mut received = 0;
            while let Some(Ok(message)) = inbound.next().await {
                if let Some(subscribe_message::Inner::Ack(ack)) = message.inner {
                    let _ = acks.send((ack.block_number, ack.log_index));
                    received += 1;
                    if received == 1 {
                        // Keep the checkpoint behind the event Ack so the test can
                        // inspect the durable event cursor at that exact boundary.
                        if out_tx.send(checkpoint.clone()).await.is_err() {
                            break;
                        }
                    }
                    if received == 2 {
                        break;
                    }
                }
            }
            drop(out_tx);
        });
        Ok(Response::new(Box::pin(ReceiverStream::new(out_rx).map(Ok))))
    }
}

#[tokio::test]
async fn signed_indexer_message_delivery_acks_only_after_dispatch_and_checkpoint() {
    let cluster = Address::repeat_byte(0xc1);
    let member = Address::repeat_byte(0x11);
    let dstack = MockDstack::from_label("indexer-integration-member");
    let key_material = Arc::new(keys::derive_all(&dstack).await.unwrap());
    let shared = Shared::new(key_material, member, cluster, 0x0a0d0000, 16, 51821);

    let signing_key = SigningKey::generate(&mut OsRng);
    let pubkey = signing_key.verifying_key().to_bytes();
    let (ack_tx, mut ack_rx) = mpsc::unbounded_channel();
    let (hello_tx, mut hello_rx) = mpsc::unbounded_channel();
    let service = FakeIndexer {
        signing_key,
        cluster,
        acks: ack_tx,
        hellos: hello_tx,
    };

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(IndexerServer::from_arc(Arc::new(service)))
            .serve_with_incoming(TcpListenerStream::new(listener))
            .await
            .unwrap();
    });

    let (dispatch_tx, mut dispatch_rx) = mpsc::channel::<DispatchRequest>(1);
    let cursor_dir = tempfile::tempdir().unwrap();
    let client_shared = shared.clone();
    let first_dispatch = dispatch_tx.clone();
    let first_cursor_dir = cursor_dir.path().to_path_buf();
    let client = tokio::spawn(async move {
        indexer_client::connect_and_run(
            client_shared,
            format!("http://{addr}"),
            pubkey,
            first_dispatch,
            Some(first_cursor_dir),
        )
        .await
    });

    assert_eq!(
        hello_rx.recv().await.unwrap(),
        (indexer_client::PROTOCOL_VERSION, 0, None)
    );

    let request = dispatch_rx.recv().await.unwrap();
    match request.event {
        IndexedEvent::MessageSent {
            sender,
            envelope_id,
            ciphertext,
            block_number,
        } => {
            assert_eq!(sender, [0x33; 32]);
            assert_eq!(envelope_id, [0x44; 32]);
            assert_eq!(ciphertext, b"sealed-indexed-message");
            assert_eq!(block_number, 77);
        }
        IndexedEvent::Reconcile => panic!("expected indexed MessageSent dispatch"),
    }
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(50), ack_rx.recv())
            .await
            .is_err(),
        "event was Ack'd before handler completion"
    );
    request.completion.send(Ok(())).unwrap();

    assert_eq!(ack_rx.recv().await.unwrap(), (77, 3));
    assert_eq!(
        indexer_client::load_cursor(
            Some(cursor_dir.path()),
            shared.cluster,
            &shared.self_member_id,
        )
        .await,
        Some((77, 3)),
        "the exact event cursor must be durable before its Ack reaches the server"
    );
    assert_eq!(ack_rx.recv().await.unwrap(), (78, u64::MAX));
    client.await.unwrap().unwrap();
    let status = shared.get_indexer_status().await;
    assert!(status.connected);
    assert!(status.caught_up);
    assert_eq!(status.cursor_block, 78);
    assert_eq!(
        indexer_client::load_cursor(
            Some(cursor_dir.path()),
            shared.cluster,
            &shared.self_member_id,
        )
        .await,
        Some((78, u64::MAX))
    );

    // Connectivity is report-only: losing the Indexer must not close either of the
    // existing convergence/CSK health gates or trigger a different event source.
    shared.gates.latch_first_converged();
    shared.gates.set_csk_acquired();
    shared.set_indexer_connected(false).await;
    assert!(shared.gates.healthy());
    assert!(!shared.get_indexer_status().await.connected);

    // A reconnect through a newly selected LB backend carries the last handled
    // exact cursor, including across a sidecar process restart. `from_block` stays
    // populated for old servers, while a v3 server can skip the handled log index.
    let restarted = Shared::new(shared.keys.clone(), member, cluster, 0x0a0d0000, 16, 51821);
    let (saved_block, saved_log_index) = indexer_client::load_cursor(
        Some(cursor_dir.path()),
        restarted.cluster,
        &restarted.self_member_id,
    )
    .await
    .unwrap();
    restarted
        .set_indexer_progress(saved_block, saved_log_index, false)
        .await;
    let reconnect = tokio::spawn(async move {
        indexer_client::connect_and_run(
            restarted,
            format!("http://{addr}"),
            pubkey,
            dispatch_tx,
            Some(cursor_dir.path().to_path_buf()),
        )
        .await
    });
    assert_eq!(
        hello_rx.recv().await.unwrap(),
        (indexer_client::PROTOCOL_VERSION, 78, Some((78, u64::MAX)))
    );
    // The fake server deliberately repeats an older event. The client validates it
    // but uses its exact in-memory/durable cursor to avoid redispatch.
    assert_eq!(ack_rx.recv().await.unwrap(), (77, 3));
    assert_eq!(ack_rx.recv().await.unwrap(), (78, u64::MAX));
    let unexpected =
        tokio::time::timeout(std::time::Duration::from_millis(50), dispatch_rx.recv()).await;
    assert!(
        !matches!(unexpected, Ok(Some(_))),
        "events at or below the exact resume cursor must not be redispatched"
    );
    reconnect.await.unwrap().unwrap();
}
