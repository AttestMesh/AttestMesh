//! Client for the co-located sidecar's existing app-facing Agent gRPC.
//!
//! In cluster-shared mode the sidecar registers this CVM in the dedicated indexer
//! cluster and acquires the CSK. The indexer waits on that existing API over the
//! node-local unix socket; no new sidecar wire surface is introduced.

use crate::agent_pb::agent_client::AgentClient;
use crate::agent_pb::Empty;
use alloy::primitives::{Address, B256};
use anyhow::{Context, Result};
use std::time::Duration;
use tonic::transport::{Channel, Endpoint, Uri};
use zeroize::Zeroizing;

/// Sidecar facts needed to establish and audit this replica's shared identity.
pub struct AgentFacts {
    pub csk: Zeroizing<[u8; 32]>,
    pub member_id: B256,
    pub member_contract: Address,
    pub mesh_ip: u32,
}

async fn connect(addr: &str) -> Result<Channel> {
    let path = unix_path(addr).with_context(|| {
        format!("AGENT_GRPC_ADDR must be an absolute unix-socket path or unix:/path: {addr}")
    })?;
    let path = path.to_owned();
    Endpoint::try_from("http://[::]:50052")?
        .connect_with_connector(tower::service_fn(move |_: Uri| {
            let path = path.clone();
            async move {
                let stream = tokio::net::UnixStream::connect(path).await?;
                Ok::<_, std::io::Error>(hyper_util::rt::TokioIo::new(stream))
            }
        }))
        .await
        .with_context(|| format!("connect sidecar Agent gRPC at {addr}"))
}

fn unix_path(addr: &str) -> Option<&str> {
    if addr.starts_with('/') {
        Some(addr)
    } else {
        addr.strip_prefix("unix:")
            .filter(|path| path.starts_with('/'))
    }
}

/// Wait until the sidecar has acquired the CSK, then return it with `GetSelf`.
/// Connection failures and `Unavailable` are expected during co-located startup.
pub async fn fetch_facts(addr: &str, retry: Duration) -> Result<AgentFacts> {
    loop {
        match try_fetch(addr).await {
            Ok(facts) => return Ok(facts),
            Err(error) => {
                tracing::info!(
                    error = ?error,
                    retry_ms = retry.as_millis(),
                    "shared identity is waiting for sidecar Agent facts"
                );
                tokio::time::sleep(retry).await;
            }
        }
    }
}

async fn try_fetch(addr: &str) -> Result<AgentFacts> {
    let mut client = AgentClient::new(connect(addr).await?);

    let key = client
        .get_cluster_shared_key(Empty {})
        .await
        .context("Agent.GetClusterSharedKey")?
        .into_inner()
        .key;
    let csk: [u8; 32] = key
        .as_slice()
        .try_into()
        .map_err(|_| anyhow::anyhow!("sidecar CSK is {} bytes, expected 32", key.len()))?;
    anyhow::ensure!(csk != [0u8; 32], "sidecar returned an all-zero CSK");

    let self_info = client
        .get_self(Empty {})
        .await
        .context("Agent.GetSelf")?
        .into_inner();
    let member_id = fixed::<32>("memberId", &self_info.member_id)?;
    let member_contract = fixed::<20>("memberContract", &self_info.member_contract)?;
    anyhow::ensure!(member_id != [0u8; 32], "sidecar returned a zero memberId");
    anyhow::ensure!(
        member_contract != [0u8; 20],
        "sidecar returned a zero memberContract"
    );

    Ok(AgentFacts {
        csk: Zeroizing::new(csk),
        member_id: B256::from(member_id),
        member_contract: Address::from(member_contract),
        mesh_ip: self_info.mesh_ip,
    })
}

fn fixed<const N: usize>(label: &str, value: &[u8]) -> Result<[u8; N]> {
    value
        .try_into()
        .map_err(|_| anyhow::anyhow!("sidecar {label} is {} bytes, expected {N}", value.len()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent_pb::agent_server::{Agent, AgentServer};
    use crate::agent_pb::{
        ClusterSharedKey, IncomingMessage, MeshStatus, PeerEvent, PeerList, SelfInfo, SendRequest,
        SendResponse,
    };
    use std::pin::Pin;
    use std::sync::atomic::{AtomicU32, Ordering};
    use tokio_stream::Stream;
    use tonic::{Request, Response, Status};

    struct FakeAgent {
        unavailable_calls: u32,
        calls: AtomicU32,
    }

    type ResponseStream<T> = Pin<Box<dyn Stream<Item = Result<T, Status>> + Send>>;

    #[tonic::async_trait]
    impl Agent for FakeAgent {
        async fn get_cluster_shared_key(
            &self,
            _: Request<Empty>,
        ) -> Result<Response<ClusterSharedKey>, Status> {
            if self.calls.fetch_add(1, Ordering::SeqCst) < self.unavailable_calls {
                return Err(Status::unavailable("CSK not acquired"));
            }
            Ok(Response::new(ClusterSharedKey {
                key: vec![0x42; 32],
            }))
        }

        async fn get_self(&self, _: Request<Empty>) -> Result<Response<SelfInfo>, Status> {
            Ok(Response::new(SelfInfo {
                member_id: vec![0x07; 32],
                member_contract: vec![0xaa; 20],
                mesh_ip: 0x0a0d0001,
            }))
        }

        async fn get_mesh_status(&self, _: Request<Empty>) -> Result<Response<MeshStatus>, Status> {
            Err(Status::unimplemented("not used"))
        }

        async fn list_peers(&self, _: Request<Empty>) -> Result<Response<PeerList>, Status> {
            Err(Status::unimplemented("not used"))
        }

        async fn send_message(
            &self,
            _: Request<SendRequest>,
        ) -> Result<Response<SendResponse>, Status> {
            Err(Status::unimplemented("not used"))
        }

        type SubscribeMessagesStream = ResponseStream<IncomingMessage>;

        async fn subscribe_messages(
            &self,
            _: Request<Empty>,
        ) -> Result<Response<Self::SubscribeMessagesStream>, Status> {
            Err(Status::unimplemented("not used"))
        }

        type SubscribePeerEventsStream = ResponseStream<PeerEvent>;

        async fn subscribe_peer_events(
            &self,
            _: Request<Empty>,
        ) -> Result<Response<Self::SubscribePeerEventsStream>, Status> {
            Err(Status::unimplemented("not used"))
        }
    }

    async fn serve_agent(unavailable_calls: u32) -> (tempfile::TempDir, String) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agent.sock");
        let listener = tokio::net::UnixListener::bind(&path).unwrap();
        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(AgentServer::new(FakeAgent {
                    unavailable_calls,
                    calls: AtomicU32::new(0),
                }))
                .serve_with_incoming(tokio_stream::wrappers::UnixListenerStream::new(listener))
                .await
                .ok();
        });
        (dir, path.to_string_lossy().into_owned())
    }

    #[test]
    fn accepts_only_unix_socket_addresses() {
        assert_eq!(unix_path("/run/agent.sock"), Some("/run/agent.sock"));
        assert_eq!(unix_path("unix:/run/agent.sock"), Some("/run/agent.sock"));
        assert_eq!(unix_path("http://127.0.0.1:1"), None);
        assert_eq!(unix_path("relative.sock"), None);
    }

    #[test]
    fn fixed_width_facts_are_strict() {
        assert_eq!(fixed::<32>("id", &[7u8; 32]).unwrap(), [7u8; 32]);
        assert!(fixed::<32>("id", &[7u8; 31]).is_err());
        assert!(fixed::<20>("member", &[7u8; 21]).is_err());
    }

    #[tokio::test]
    async fn fetches_facts_over_uds_after_csk_gate_opens() {
        let (_dir, addr) = serve_agent(2).await;
        let facts = tokio::time::timeout(
            Duration::from_secs(2),
            fetch_facts(&addr, Duration::from_millis(5)),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(*facts.csk, [0x42; 32]);
        assert_eq!(facts.member_id, B256::repeat_byte(0x07));
        assert_eq!(facts.member_contract, Address::repeat_byte(0xaa));
        assert_eq!(facts.mesh_ip, 0x0a0d0001);
    }
}
