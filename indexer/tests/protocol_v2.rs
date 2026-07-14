use alloy::primitives::{Address, B256};
use attestmesh_indexer::dstack::MockDstack;
use attestmesh_indexer::grpc::{envelope, service::IndexerService};
use attestmesh_indexer::identity::Identity;
use attestmesh_indexer::metrics::Metrics;
use attestmesh_indexer::pb::indexer_client::IndexerClient;
use attestmesh_indexer::pb::indexer_server::IndexerServer;
use attestmesh_indexer::pb::{subscribe_message, Ack, DeliveryCursor, Hello, SubscribeMessage};
use attestmesh_indexer::state::cursor::{Cursor, CursorStore, SledCursorStore};
use attestmesh_indexer::state::IndexerState;
use std::sync::Arc;
use std::time::Duration;
use tempfile::TempDir;
use tokio::sync::{mpsc, oneshot};
use tokio_stream::wrappers::{ReceiverStream, TcpListenerStream};

#[tokio::test]
async fn empty_v2_cursor_starts_at_head_and_empty_checkpoint_advances_it() {
    let state_dir = TempDir::new().unwrap();
    let cursors = Arc::new(SledCursorStore::open(state_dir.path().to_str().unwrap()).unwrap());
    let state = IndexerState::new(cursors.clone());
    let cluster = Address::repeat_byte(0xc1);
    let member = B256::repeat_byte(0xa1);
    state.add_cluster(cluster).await;
    state
        .record_member(cluster, member, Address::repeat_byte(0x11), 40)
        .await;
    state.set_last_indexed_block(100).await;

    let identity = Arc::new(
        Identity::derive(&MockDstack::from_label("protocol-v2-indexer"))
            .await
            .unwrap(),
    );
    let service = IndexerService::new(state.clone(), identity.clone(), Arc::new(Metrics::new()));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (shutdown_tx, shutdown_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(IndexerServer::new(service))
            .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                let _ = shutdown_rx.await;
            })
            .await
            .unwrap();
    });

    let mut client = IndexerClient::connect(format!("http://{addr}"))
        .await
        .unwrap();
    let (outbound_tx, outbound_rx) = mpsc::channel(8);
    outbound_tx
        .send(SubscribeMessage {
            inner: Some(subscribe_message::Inner::Hello(Hello {
                cluster_addr: cluster.as_slice().to_vec(),
                member_id: member.as_slice().to_vec(),
                attestation: Vec::new(),
                from_block: 0,
                protocol_version: envelope::CHECKPOINT_PROTOCOL_VERSION,
                resume_cursor: None,
            })),
        })
        .await
        .unwrap();
    let mut inbound = client
        .subscribe(ReceiverStream::new(outbound_rx))
        .await
        .unwrap()
        .into_inner();

    // Initialization is part of accepting the subscription, not dependent on its
    // first Ack, so a rollout never falls back to MemberRegistered history.
    assert_eq!(
        cursors.load(cluster, member).unwrap(),
        Some(Cursor::new(100, u64::MAX))
    );
    let catchup = inbound.message().await.unwrap().unwrap();
    assert!(envelope::is_checkpoint(&catchup));
    assert_eq!(catchup.block_number, 100);
    assert!(envelope::verify(identity.verifying_key(), &catchup));
    outbound_tx
        .send(SubscribeMessage {
            inner: Some(subscribe_message::Inner::Ack(Ack {
                block_number: catchup.block_number,
                log_index: catchup.log_index,
            })),
        })
        .await
        .unwrap();

    // Model an indexed batch with no relevant logs. The signed checkpoint must be
    // delivered and its Ack must move the full durable cursor across the empty span.
    let subscriber = state
        .subscribers()
        .subscribers_of(cluster)
        .await
        .into_iter()
        .next()
        .unwrap();
    let empty_batch = subscriber.finalize(&identity, envelope::build_checkpoint(cluster, 125));
    assert!(subscriber.send_live(empty_batch).await);
    let checkpoint = inbound.message().await.unwrap().unwrap();
    assert!(envelope::is_checkpoint(&checkpoint));
    assert_eq!(checkpoint.block_number, 125);
    assert!(envelope::verify(identity.verifying_key(), &checkpoint));
    outbound_tx
        .send(SubscribeMessage {
            inner: Some(subscribe_message::Inner::Ack(Ack {
                block_number: checkpoint.block_number,
                log_index: checkpoint.log_index,
            })),
        })
        .await
        .unwrap();

    tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            if cursors.load(cluster, member).unwrap() == Some(Cursor::new(125, u64::MAX)) {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();

    drop(outbound_tx);
    drop(inbound);
    let _ = shutdown_tx.send(());
    server.await.unwrap();
}

#[tokio::test]
async fn an_unacked_exact_cursor_session_does_not_advance_replica_state() {
    let state_dir = TempDir::new().unwrap();
    let cursors = Arc::new(SledCursorStore::open(state_dir.path().to_str().unwrap()).unwrap());
    let state = IndexerState::new(cursors.clone());
    let cluster = Address::repeat_byte(0xc2);
    let member = B256::repeat_byte(0xa2);
    state.add_cluster(cluster).await;
    state
        .record_member(cluster, member, Address::repeat_byte(0x12), 40)
        .await;
    state.set_last_indexed_block(100).await;

    let identity = Arc::new(
        Identity::derive(&MockDstack::from_label("protocol-v3-indexer"))
            .await
            .unwrap(),
    );
    let service = IndexerService::new(state, identity, Arc::new(Metrics::new()));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (shutdown_tx, shutdown_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(IndexerServer::new(service))
            .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                let _ = shutdown_rx.await;
            })
            .await
            .unwrap();
    });

    for _ in 0..2 {
        let mut client = IndexerClient::connect(format!("http://{addr}"))
            .await
            .unwrap();
        let (outbound_tx, outbound_rx) = mpsc::channel(8);
        outbound_tx
            .send(SubscribeMessage {
                inner: Some(subscribe_message::Inner::Hello(Hello {
                    cluster_addr: cluster.as_slice().to_vec(),
                    member_id: member.as_slice().to_vec(),
                    attestation: Vec::new(),
                    // Kept for a pre-v3 server; the exact cursor below is authoritative.
                    from_block: 50,
                    protocol_version: envelope::EXACT_CURSOR_PROTOCOL_VERSION,
                    resume_cursor: Some(DeliveryCursor {
                        block_number: 50,
                        log_index: 3,
                    }),
                })),
            })
            .await
            .unwrap();
        let mut inbound = client
            .subscribe(ReceiverStream::new(outbound_rx))
            .await
            .unwrap()
            .into_inner();
        let checkpoint = inbound.message().await.unwrap().unwrap();
        assert!(envelope::is_checkpoint(&checkpoint));
        assert_eq!(checkpoint.block_number, 100);

        // Drop without Ack. A second connection with the same explicit cursor must
        // receive the checkpoint again, and the replica-local store must stay empty.
        drop(inbound);
        drop(outbound_tx);
        assert_eq!(cursors.load(cluster, member).unwrap(), None);
    }

    let _ = shutdown_tx.send(());
    server.await.unwrap();
}
