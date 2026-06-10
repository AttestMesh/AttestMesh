//! Peer-control gRPC server (sidecar spec §12.5, §13). Bound to the node's mesh IP
//! on the attestmesh0 interface — only other cluster members reachable over the
//! encrypted mesh can call it. Serves the CSK peer-pull and the UDP hole-punch
//! negotiation (docs/specs/udp-transport-upgrade.md).

use crate::chain::ChainClient;
use crate::proto::peer::peer_control_server::{PeerControl, PeerControlServer};
use crate::proto::peer::{CskRequest, Empty, PunchAccept, PunchOffer, PunchReport, SealedCsk};
use crate::state::Shared;
use crate::transport::punch::Puncher;
use alloy::primitives::B256;
use std::sync::Arc;
use tonic::{Request, Response, Status};

pub struct PeerControlService {
    shared: Arc<Shared>,
    chain: Arc<ChainClient>,
    /// `None` when the punch upgrade is disabled (`WG_UDP_PUNCH=false`): the
    /// punch RPCs then answer UNIMPLEMENTED — the same surface an old sidecar
    /// presents — so peers settle on TCP cleanly.
    puncher: Option<Arc<Puncher>>,
}

impl PeerControlService {
    pub fn new(
        shared: Arc<Shared>,
        chain: Arc<ChainClient>,
        puncher: Option<Arc<Puncher>>,
    ) -> Self {
        Self {
            shared,
            chain,
            puncher,
        }
    }

    pub fn into_server(self) -> PeerControlServer<Self> {
        PeerControlServer::new(self)
    }
}

#[tonic::async_trait]
impl PeerControl for PeerControlService {
    async fn request_cluster_shared_key(
        &self,
        req: Request<CskRequest>,
    ) -> Result<Response<SealedCsk>, Status> {
        // Only serve if we hold the CSK (else the requester tries another peer).
        let csk = *self.shared.csk.lock().await;
        let Some(csk) = csk else {
            return Err(Status::unavailable("CSK not held yet"));
        };

        let mid: [u8; 32] = req
            .into_inner()
            .requester_member_id
            .try_into()
            .map_err(|_| Status::invalid_argument("bad member id"))?;

        // Verify the requester is a current member (its xPubKey exists on chain).
        let xpub = self
            .chain
            .x_pubkey_of(self.shared.cluster, B256::from(mid))
            .await
            .map_err(|e| Status::internal(e.to_string()))?;
        if xpub == B256::ZERO {
            return Err(Status::permission_denied(
                "requester is not a cluster member",
            ));
        }

        // Seal the CSK to the requester's on-chain x25519 pubkey (defense-in-depth).
        let sealed = crate::csk::seal_for_peer(&csk, &xpub.0)
            .map_err(|e| Status::internal(e.to_string()))?;
        Ok(Response::new(SealedCsk { sealed_csk: sealed }))
    }

    async fn negotiate_punch(
        &self,
        req: Request<PunchOffer>,
    ) -> Result<Response<PunchAccept>, Status> {
        let Some(p) = &self.puncher else {
            return Err(Status::unimplemented("punch upgrade disabled"));
        };
        p.clone()
            .handle_offer(req.into_inner())
            .await
            .map(Response::new)
    }

    async fn report_punch(&self, req: Request<PunchReport>) -> Result<Response<Empty>, Status> {
        let Some(p) = &self.puncher else {
            return Err(Status::unimplemented("punch upgrade disabled"));
        };
        p.handle_report(req.into_inner()).await?;
        Ok(Response::new(Empty {}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dstack::MockDstack;
    use crate::state::Shared;
    use alloy::primitives::Address;
    use std::sync::Arc;

    async fn service() -> PeerControlService {
        let dstack = MockDstack::from_label("peer-grpc-test");
        let keys = Arc::new(crate::keys::derive_all(&dstack).await.unwrap());
        let shared = Shared::new(
            keys.clone(),
            Address::repeat_byte(0x11),
            Address::repeat_byte(0x22),
            0x0a0d0000,
            16,
            51821,
        );
        // Never dialed in these tests — both paths below return before any RPC.
        let chain = Arc::new(
            ChainClient::new(
                "http://127.0.0.1:1",
                8453,
                Address::repeat_byte(0x11),
                &keys,
            )
            .unwrap(),
        );
        PeerControlService::new(shared, chain, None)
    }

    #[tokio::test]
    async fn refuses_when_csk_not_held() {
        // The exact shape of the live pulling-csk deadlock: an empty-handed node
        // must answer `unavailable` (so the requester tries another peer), never
        // hang or fabricate.
        let svc = service().await;
        let req = Request::new(CskRequest {
            requester_member_id: vec![9u8; 32],
        });
        let err = svc.request_cluster_shared_key(req).await.unwrap_err();
        assert_eq!(err.code(), tonic::Code::Unavailable);
    }

    #[tokio::test]
    async fn rejects_malformed_member_id() {
        let svc = service().await;
        *svc.shared.csk.lock().await = Some([5u8; 32]);
        let req = Request::new(CskRequest {
            requester_member_id: vec![9u8; 7], // not 32 bytes
        });
        let err = svc.request_cluster_shared_key(req).await.unwrap_err();
        assert_eq!(err.code(), tonic::Code::InvalidArgument);
    }

    /// With the punch upgrade disabled the node presents the exact same surface
    /// an old sidecar does — UNIMPLEMENTED — so peers settle on TCP cleanly.
    #[tokio::test]
    async fn punch_rpcs_unimplemented_when_disabled() {
        let svc = service().await;

        let offer = Request::new(PunchOffer {
            requester_member_id: vec![1u8; 32],
            candidates: vec![],
            start_at_ms: 0,
            nonce: vec![0u8; 16],
        });
        let err = svc.negotiate_punch(offer).await.unwrap_err();
        assert_eq!(err.code(), tonic::Code::Unimplemented);

        let report = Request::new(PunchReport {
            nonce: vec![0u8; 16],
            success: true,
            observed_source: None,
        });
        let err = svc.report_punch(report).await.unwrap_err();
        assert_eq!(err.code(), tonic::Code::Unimplemented);
    }
}
