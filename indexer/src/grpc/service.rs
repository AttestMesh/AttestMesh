//! The Indexer gRPC service (spec §8.2).
//!
//! `Subscribe(stream)` flow:
//!   1. Read the first message; require `Hello`, else `InvalidArgument`.
//!   2. Validate: cluster known? member exists? clamp `from_block` to the member's
//!      registration floor.
//!   3. Resolve the exact `(block, logIndex)` replay cursor. A v2 subscriber with no
//!      cursor explicitly initializes at the current indexed head.
//!   4. Register the subscription behind a per-session replay/live ordering gate.
//!   5. Replay strictly after the cursor, emit a signed checkpoint, then stream live.
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
use tokio_stream::wrappers::{ReceiverStream, WatchStream};
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
    catchup_batch_size: u64,
}

impl IndexerService {
    pub fn new(state: IndexerState, identity: Arc<Identity>, metrics: Arc<Metrics>) -> Self {
        Self {
            state,
            identity,
            metrics,
            provider: None,
            expected_code_id: B256::ZERO,
            catchup_batch_size: 2_000,
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

    pub fn with_catchup_batch_size(mut self, batch_size: u64) -> Self {
        self.catchup_batch_size = batch_size.max(1);
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

fn replay_start(reg_floor: u64, requested: u64, persisted: Option<Cursor>) -> u64 {
    let requested = if requested == 0 {
        reg_floor
    } else {
        requested.max(reg_floor)
    };
    match persisted {
        Some(cursor) if cursor.log_index == u64::MAX => {
            requested.max(cursor.block_number.saturating_add(1))
        }
        Some(cursor) => requested.max(cursor.block_number),
        None => requested,
    }
}

fn is_after_cursor(block_number: u64, log_index: u64, cursor: Option<Cursor>) -> bool {
    cursor.map_or(true, |c| Cursor::new(block_number, log_index) > c)
}

fn initialize_at_head(protocol_version: u32, requested: u64, persisted: Option<Cursor>) -> bool {
    protocol_version >= envelope::CHECKPOINT_PROTOCOL_VERSION
        && requested == 0
        && persisted.is_none()
}

// `tonic` fixes the streaming error type as `Status`; keep the unavoidable large
// error out of the inline stream combinator so the exception stays narrowly scoped.
#[allow(clippy::result_large_err)]
fn overflow_terminal(overflowed: bool) -> Option<Result<PushEnvelope, Status>> {
    overflowed.then(|| {
        Err(Status::resource_exhausted(
            "subscriber delivery queue overflow; reconnect from cursor",
        ))
    })
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

        // 3. Resolve the exact replay cursor. Protocol-v2 subscribers explicitly
        // initialize an empty cursor at the current indexed head: legacy sidecars
        // never Ack'd, so replaying from MemberRegistered would recreate the boot lag.
        let latest = self.state.last_indexed_block().await;
        let mut persisted = self
            .state
            .cursors()
            .load(cluster, member_id)
            .map_err(|e| Status::internal(format!("cursor load: {e}")))?;
        if initialize_at_head(hello.protocol_version, requested, persisted) {
            let baseline = Cursor::new(latest.max(reg_floor), envelope::CHECKPOINT_LOG_INDEX);
            self.state
                .cursors()
                .advance(cluster, member_id, baseline)
                .map_err(|e| Status::internal(format!("cursor initialize: {e}")))?;
            self.state
                .cursors()
                .flush()
                .map_err(|e| Status::internal(format!("cursor initialize flush: {e}")))?;
            persisted = Some(baseline);
            tracing::info!(cluster = %cluster, member = %member_id,
                block = baseline.block_number,
                "initialized protocol-v2 subscriber cursor at indexed head");
        }
        let effective_from = replay_start(reg_floor, requested, persisted);

        // 4. Register behind a delivery gate. Replay owns the gate until its
        // checkpoint is queued, so live dispatch cannot overtake catch-up.
        let session_id = self.state.subscribers().next_session_id();
        let session_att = Arc::new(SessionAttestation::new(attestation_for(
            &self.identity,
            self.expected_code_id,
        )));
        let (sub, rx, overflow_rx) = crate::state::subscribers::channel(
            member_id,
            session_id,
            hello.protocol_version,
            session_att,
        );
        let replay_guard = sub.replay_guard().await;
        self.state.subscribers().add(cluster, sub.clone()).await;
        self.metrics.set_subscribers(
            cluster,
            self.state.subscribers().count_of(cluster).await as f64,
        );

        // 5. Replay in bounded RPC pages on a separate task. send_replay awaits
        // capacity, so a large legitimate gap cannot be silently truncated.
        let replay_provider = self.provider.clone();
        let replay_identity = self.identity.clone();
        let replay_metrics = self.metrics.clone();
        let replay_sub = sub.clone();
        let replay_batch = self.catchup_batch_size;
        tokio::spawn(async move {
            let _replay_guard = replay_guard;
            if let Some(provider) = replay_provider {
                let mut from = effective_from;
                while from <= latest {
                    let to = from.saturating_add(replay_batch - 1).min(latest);
                    let logs =
                        match watcher::poll_cluster_logs(&provider, &[cluster], from, to).await {
                            Ok(logs) => logs,
                            Err(e) => {
                                replay_metrics.inc_rpc_error("catchup_get_logs");
                                tracing::warn!(error = %e, cluster = %cluster, member = %member_id,
                                "subscribe catch-up failed; closing stream for replay");
                                replay_sub.abort();
                                return;
                            }
                        };
                    for log in logs.into_iter().filter(|log| {
                        log.is_relevant_for(&member_id)
                            && is_after_cursor(log.block_number, log.log_index, persisted)
                    }) {
                        let label = log.kind.label();
                        let stub = repro::build_stub(&log);
                        let env = envelope::build_envelope(&log, &stub);
                        let env = replay_sub.finalize(&replay_identity, env);
                        if !replay_sub.send_replay(env).await {
                            return;
                        }
                        replay_metrics.inc_pushed(cluster, label);
                    }
                    if to == u64::MAX {
                        break;
                    }
                    from = to + 1;
                }
            }
            if replay_sub.supports_checkpoints() {
                let checkpoint = envelope::build_checkpoint(cluster, latest);
                let checkpoint = replay_sub.finalize(&replay_identity, checkpoint);
                if replay_sub.send_replay(checkpoint).await {
                    replay_metrics.inc_pushed(cluster, "Checkpoint");
                }
            }
        });

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
                        } else if ack.log_index == envelope::CHECKPOINT_LOG_INDEX {
                            // A checkpoint proves the whole ordered prefix was handled;
                            // make that empty-block progress durable immediately.
                            if let Err(e) = ack_state.cursors().flush() {
                                tracing::warn!(error = %e, "checkpoint cursor flush failed");
                            }
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

        // A full live channel emits an explicit terminal status. No later checkpoint
        // can pass the gap, so reconnect always resumes from the last durable Ack.
        let messages = ReceiverStream::new(rx).map(Ok);
        let overflow = WatchStream::new(overflow_rx)
            .filter_map(overflow_terminal)
            .take(1);
        let out = messages.merge(overflow);
        Ok(Response::new(Box::pin(out) as Self::SubscribeStream))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replay_resumes_strictly_after_full_cursor() {
        let cursor = Cursor::new(100, 7);
        assert_eq!(replay_start(10, 0, Some(cursor)), 100);
        assert!(!is_after_cursor(100, 7, Some(cursor)));
        assert!(is_after_cursor(100, 8, Some(cursor)));
        assert!(is_after_cursor(101, 0, Some(cursor)));
    }

    #[test]
    fn checkpoint_cursor_starts_at_next_block() {
        let checkpoint = Cursor::new(100, u64::MAX);
        assert_eq!(replay_start(10, 0, Some(checkpoint)), 101);
        assert!(!is_after_cursor(100, u64::MAX, Some(checkpoint)));
        assert!(is_after_cursor(101, 0, Some(checkpoint)));
    }

    #[test]
    fn requested_floor_is_inclusive_and_cannot_precede_registration() {
        assert_eq!(replay_start(50, 0, None), 50);
        assert_eq!(replay_start(50, 40, None), 50);
        assert_eq!(replay_start(50, 70, None), 70);
    }

    #[test]
    fn only_v2_empty_cursor_uses_head_initialization() {
        assert!(initialize_at_head(2, 0, None));
        assert!(!initialize_at_head(1, 0, None));
        assert!(!initialize_at_head(2, 10, None));
        assert!(!initialize_at_head(2, 0, Some(Cursor::new(9, 1))));
    }
}
