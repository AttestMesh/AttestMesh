//! App-facing gRPC façade over a unix domain socket (sidecar spec §12). The app
//! never holds AttestMesh keys, sees ciphertexts, or talks to the chain/Indexer
//! directly — only this surface. Message stream is decryption-filtered.

use crate::chain::bundler::BundlerClient;
use crate::chain::{message_facet, userop, ChainClient};
use crate::proto::agent::agent_server::{Agent, AgentServer};
use crate::proto::agent::{
    peer_event, ClusterSharedKey, Empty, IncomingMessage, MeshStatus, PeerEvent, PeerInfo,
    PeerJoined, PeerList, PeerLiveness, SelfInfo, SendRequest, SendResponse,
};
use crate::state::{AppPeerEvent, Shared};
use alloy::primitives::{keccak256, Bytes, B256};
use std::pin::Pin;
use std::sync::Arc;
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::{Stream, StreamExt};
use tonic::{Request, Response, Status};

pub struct AgentService {
    shared: Arc<Shared>,
    chain: Arc<ChainClient>,
    bundler: Arc<BundlerClient>,
}

impl AgentService {
    pub fn new(shared: Arc<Shared>, chain: Arc<ChainClient>, bundler: Arc<BundlerClient>) -> Self {
        Self {
            shared,
            chain,
            bundler,
        }
    }

    pub fn into_server(self) -> AgentServer<Self> {
        AgentServer::new(self)
    }
}

type IncomingStream = Pin<Box<dyn Stream<Item = Result<IncomingMessage, Status>> + Send>>;
type PeerEventStream = Pin<Box<dyn Stream<Item = Result<PeerEvent, Status>> + Send>>;

#[tonic::async_trait]
impl Agent for AgentService {
    async fn get_mesh_status(&self, _: Request<Empty>) -> Result<Response<MeshStatus>, Status> {
        let phase = self.shared.current_phase().await;
        let live = self.shared.peers.lock().await.live_count() as u32;
        Ok(Response::new(MeshStatus {
            phase: phase.as_str().to_string(),
            first_converged: self.shared.gates.first_converged(),
            csk_acquired: self.shared.gates.csk_acquired(),
            live_peer_count: live,
        }))
    }

    async fn get_self(&self, _: Request<Empty>) -> Result<Response<SelfInfo>, Status> {
        Ok(Response::new(SelfInfo {
            member_id: self.shared.self_member_id.to_vec(),
            member_contract: self.shared.member_contract.as_slice().to_vec(),
            mesh_ip: self.shared.self_mesh_ip,
        }))
    }

    async fn list_peers(&self, _: Request<Empty>) -> Result<Response<PeerList>, Status> {
        let peers = self
            .shared
            .peers
            .lock()
            .await
            .all()
            .map(|p| PeerInfo {
                member_id: p.member_id.to_vec(),
                mesh_ip: p.mesh_ip,
                live: p.live,
            })
            .collect();
        Ok(Response::new(PeerList { peers }))
    }

    async fn get_cluster_shared_key(
        &self,
        _: Request<Empty>,
    ) -> Result<Response<ClusterSharedKey>, Status> {
        match self.shared.csk.lock().await.as_ref() {
            Some(k) => Ok(Response::new(ClusterSharedKey { key: k.to_vec() })),
            None => Err(Status::unavailable("CSK not yet acquired")),
        }
    }

    async fn send_message(
        &self,
        req: Request<SendRequest>,
    ) -> Result<Response<SendResponse>, Status> {
        let r = req.into_inner();
        let recipient: [u8; 32] = r
            .recipient_member_id
            .try_into()
            .map_err(|_| Status::invalid_argument("bad recipient"))?;

        // Seal the app payload to the recipient's on-chain x25519 pubkey.
        let xpub = self
            .chain
            .x_pubkey_of(self.shared.cluster, B256::from(recipient))
            .await
            .map_err(|e| Status::internal(e.to_string()))?;
        if xpub == B256::ZERO {
            return Err(Status::failed_precondition(
                "recipient is not a cluster member",
            ));
        }
        let ciphertext = crate::envelopes::seal(&xpub.0, &r.payload)
            .map_err(|e| Status::internal(e.to_string()))?;

        let envelope_id = if r.envelope_id.len() == 32 {
            B256::from_slice(&r.envelope_id)
        } else {
            keccak256(&r.payload)
        };

        // Wrap as MessageFacet.send(...) inside ClusterMember.execute(...) and submit.
        let inner = message_facet::build_send_calldata(
            B256::from(recipient),
            envelope_id,
            Bytes::from(ciphertext),
        );
        let call_data = userop::wrap_execute(self.shared.cluster, inner);
        let op =
            userop::UserOperation::new(self.shared.member_contract, Default::default(), call_data);
        let tx_hash = self
            .bundler
            .submit(&self.chain.signer, op)
            .await
            .map_err(|e| Status::internal(format!("send failed: {e}")))?;

        Ok(Response::new(SendResponse {
            envelope_id: envelope_id.to_vec(),
            tx_hash: tx_hash.to_vec(),
        }))
    }

    type SubscribeMessagesStream = IncomingStream;

    async fn subscribe_messages(
        &self,
        _: Request<Empty>,
    ) -> Result<Response<Self::SubscribeMessagesStream>, Status> {
        let rx = self.shared.incoming_tx.subscribe();
        let stream = BroadcastStream::new(rx).filter_map(|item| {
            item.ok().map(|m| {
                Ok(IncomingMessage {
                    sender_member_id: m.sender_member_id.to_vec(),
                    payload: m.payload,
                    block_number: m.block_number,
                })
            })
        });
        Ok(Response::new(Box::pin(stream)))
    }

    type SubscribePeerEventsStream = PeerEventStream;

    async fn subscribe_peer_events(
        &self,
        _: Request<Empty>,
    ) -> Result<Response<Self::SubscribePeerEventsStream>, Status> {
        let rx = self.shared.peer_event_tx.subscribe();
        let stream = BroadcastStream::new(rx).filter_map(|item| {
            item.ok().map(|ev| {
                let kind = match ev {
                    AppPeerEvent::Joined { member_id, mesh_ip } => {
                        peer_event::Kind::Joined(PeerJoined {
                            member_id: member_id.to_vec(),
                            mesh_ip,
                        })
                    }
                    AppPeerEvent::Liveness { member_id, up } => {
                        peer_event::Kind::Liveness(PeerLiveness {
                            member_id: member_id.to_vec(),
                            up,
                        })
                    }
                };
                Ok(PeerEvent { kind: Some(kind) })
            })
        });
        Ok(Response::new(Box::pin(stream)))
    }
}
