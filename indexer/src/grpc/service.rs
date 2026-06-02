//! The Indexer gRPC service (spec §8.2).
//!
//! `Subscribe(stream)` flow:
//!   1. Read the first message; require `Hello`, else `InvalidArgument`.
//!   2. Validate: cluster known? member exists? clamp `from_block` to the member's
//!      registration floor.
//!   3. Compute the effective cursor = max(persisted, hello.from_block, reg-floor).
//!   4. Register the subscription in the cluster's subscriber set.
//!   5. Replay `[cursor, latest_indexed]` relevant events (attestation on the first),
//!      then stream live events as they are dispatched.
//!   6. Process inbound `Ack`s to advance the persistent cursor.

use super::envelope;
use super::subscribe::{attestation_for, SessionAttestation};
use crate::chain::{repro, watcher, HttpProvider};
use crate::identity::Identity;
use crate::metrics::Metrics;
use crate::pb::indexer_server::Indexer;
use crate::pb::{subscribe_message::Inner, PushEnvelope, SubscribeMessage};
use crate::state::cursor::Cursor;
use crate::state::IndexerState;
use alloy::primitives::{Address, B256};
use std::pin::Pin;
use std::sync::Arc;
use tokio_stream::wrappers::ReceiverStream;
use tokio_stream::{Stream, StreamExt};
use tonic::{Request, Response, Status, Streaming};

/// The configured expected code id for the attestation (spec §6.2). For the prototype
/// this is a constant carried on the service; in production it is the OCI image hash.
pub struct IndexerService {
    state: IndexerState,
    identity: Arc<Identity>,
    metrics: Arc<Metrics>,
    /// Optional read provider for subscribe-time catch-up + membership resolution.
    /// `None` in unit contexts that don't drive a live chain.
    provider: Option<Arc<HttpProvider>>,
    expected_code_id: B256,
}

impl IndexerService {
    pub fn new(state: IndexerState, identity: Arc<Identity>, metrics: Arc<Metrics>) -> Self {
        Self {
            state,
            identity,
            metrics,
            provider: None,
            expected_code_id: B256::ZERO,
        }
    }

    pub fn with_provider(mut self, provider: Arc<HttpProvider>) -> Self {
        self.provider = Some(provider);
        self
    }

    pub fn with_expected_code_id(mut self, code_id: B256) -> Self {
        self.expected_code_id = code_id;
        self
    }
}

/// Parse a 20-byte cluster address from the Hello bytes, or `None` if malformed.
fn parse_cluster(bytes: &[u8]) -> Option<Address> {
    (bytes.len() == 20).then(|| Address::from_slice(bytes))
}

/// Parse a 32-byte memberId from the Hello bytes, or `None` if malformed.
fn parse_member_id(bytes: &[u8]) -> Option<B256> {
    (bytes.len() == 32).then(|| B256::from_slice(bytes))
}

#[tonic::async_trait]
impl Indexer for IndexerService {
    type SubscribeStream = Pin<Box<dyn Stream<Item = Result<PushEnvelope, Status>> + Send>>;

    async fn subscribe(
        &self,
        request: Request<Streaming<SubscribeMessage>>,
    ) -> Result<Response<Self::SubscribeStream>, Status> {
        let mut inbound = request.into_inner();

        // 1. First message MUST be Hello.
        let hello = match inbound.next().await {
            Some(Ok(SubscribeMessage {
                inner: Some(Inner::Hello(h)),
            })) => h,
            Some(Ok(_)) => return Err(Status::invalid_argument("first message must be Hello")),
            Some(Err(e)) => return Err(e),
            None => return Err(Status::invalid_argument("stream closed before Hello")),
        };

        let cluster = parse_cluster(&hello.cluster_addr)
            .ok_or_else(|| Status::invalid_argument("cluster_addr must be 20 bytes"))?;
        let member_id = parse_member_id(&hello.member_id)
            .ok_or_else(|| Status::invalid_argument("member_id must be 32 bytes"))?;

        // 2. Validate cluster membership.
        if !self.state.is_known_cluster(cluster).await {
            return Err(Status::not_found("cluster not followed by this indexer"));
        }
        // The member must exist in the cluster. v1 takes the claim at face value but
        // still confirms existence (spec §8.2 step 2, §8.3): prefer the cached record
        // learned from MemberRegistered; fall back to an on-chain read if we have a
        // provider and the cache misses (e.g. a member that registered before this
        // process started its catch-up of that cluster).
        let reg_floor = match self.state.member_info(cluster, member_id).await {
            Some(info) => info.registered_at_block,
            None => {
                return Err(Status::not_found("member not found in cluster storage"));
            }
        };

        // 2b. Clamp from_block to the registration floor (no rewinding past join).
        let requested = hello.from_block;
        if requested != 0 && requested < reg_floor {
            tracing::warn!(
                cluster = %cluster, member = %member_id, requested, reg_floor,
                "clamping from_block to member registration floor"
            );
        }

        // 3. Effective cursor = max(persisted, requested, reg_floor).
        let persisted = self
            .state
            .cursors()
            .load(cluster, member_id)
            .map_err(|e| Status::internal(format!("cursor load: {e}")))?;
        let mut effective_from = reg_floor.max(requested);
        if let Some(c) = persisted {
            effective_from = effective_from.max(c.block_number);
        }

        // 4. Register the subscription. The per-session attestation gate is shared with
        //    the live dispatch loop via the Subscriber handle.
        let session_id = self.state.subscribers().next_session_id();
        let session_att = Arc::new(SessionAttestation::new(attestation_for(
            &self.identity,
            self.expected_code_id,
        )));
        let (sub, rx) =
            crate::state::subscribers::channel(member_id, session_id, session_att.clone());
        self.state.subscribers().add(cluster, sub.clone()).await;
        self.metrics.set_subscribers(
            cluster,
            self.state.subscribers().count_of(cluster).await as f64,
        );

        // 5. Catch-up replay of [effective_from, latest_indexed] for this member.
        //    Done before live events flow through the same channel so ordering holds.
        if let Some(provider) = self.provider.clone() {
            let latest = self.state.last_indexed_block().await;
            if latest >= effective_from {
                match watcher::poll_cluster_logs(&provider, &[cluster], effective_from, latest)
                    .await
                {
                    Ok(logs) => {
                        for log in logs.into_iter().filter(|l| l.is_relevant_for(&member_id)) {
                            let stub = repro::build_stub(&log);
                            let env = envelope::build_envelope(&log, &stub);
                            let env = session_att.finalize(&self.identity, env);
                            if sub.try_send(env) {
                                self.metrics.inc_pushed(cluster, log.kind.label());
                            } else {
                                self.metrics.inc_dropped(cluster, member_id);
                            }
                        }
                    }
                    Err(e) => {
                        self.metrics.inc_rpc_error("catchup_get_logs");
                        tracing::warn!(error = %e, "subscribe catch-up failed; continuing live");
                    }
                }
            }
        }

        // 6. Spawn the inbound Ack handler. It advances the persistent cursor and is
        //    naturally torn down when the inbound stream ends (stream-drop), at which
        //    point we deregister the subscriber.
        let ack_state = self.state.clone();
        let ack_metrics = self.metrics.clone();
        tokio::spawn(async move {
            while let Some(msg) = inbound.next().await {
                match msg {
                    Ok(SubscribeMessage {
                        inner: Some(Inner::Ack(ack)),
                    }) => {
                        let cur = Cursor::new(ack.block_number, ack.log_index);
                        if let Err(e) = ack_state.cursors().advance(cluster, member_id, cur) {
                            tracing::warn!(error = %e, "cursor advance failed");
                        }
                        ack_metrics.inc_ack(cluster, member_id);
                    }
                    Ok(_) => { /* a second Hello mid-stream is ignored in v1 */ }
                    Err(_) => break, // stream error → reconnect path
                }
            }
            // Inbound ended: deregister and refresh the gauge.
            ack_state.subscribers().remove(cluster, session_id).await;
            ack_metrics.set_subscribers(
                cluster,
                ack_state.subscribers().count_of(cluster).await as f64,
            );
            // Persist cursors at session end (best effort).
            let _ = ack_state.cursors().flush();
        });

        // The outbound stream is the subscriber's live channel; catch-up envelopes were
        // already enqueued above and the per-cluster dispatch loop feeds live ones.
        let out = ReceiverStream::new(rx).map(Ok);
        Ok(Response::new(Box::pin(out) as Self::SubscribeStream))
    }
}
