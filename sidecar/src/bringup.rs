//! Mesh bring-up orchestration (master spec §7.1 steps 5–12, sidecar spec §7).
//!
//! Runs after registration succeeds. The chain is the source of truth for
//! membership and peer identity (no off-chain coordination): peers are enumerated
//! directly via RPC reads (`listMembers` / `memberById` / `meshIpOf`), wireguard
//! links bootstrap over the gateway TCP leg (see `transport`), and the encrypted
//! `PeerEndpoint` envelopes — which carry each peer's Ed25519 heartbeat key — are
//! exchanged through `MessageFacet` and delivered by the signed Indexer stream.
//!
//! Spawned tasks: wg-tcp ingress, peer reconciler (chain → bridges → wg peers →
//! envelope exchange), heartbeat send/recv, CSK originate-or-pull, the peer-control
//! gRPC server (mesh-only), and the app-facing agent gRPC server (UDS).

use crate::chain::{
    bundler::BundlerClient, dstack_facet, message_facet, network_facet, userop, ChainClient,
};
use crate::config::Config;
use crate::dstack::DstackRuntime;
use crate::envelopes::{self, PeerEndpoint};
use crate::proto::peer::peer_control_client::PeerControlClient;
use crate::proto::peer::CskRequest;
use crate::state::{AppPeerEvent, Phase, Shared};
use crate::wg::{
    cidr,
    peer::{PeerTable, WgPeerConfig},
    MeshControl,
};
use crate::{csk, transport};
use alloy::primitives::{keccak256, Address, Bytes, B256, U256};
use alloy::signers::local::PrivateKeySigner;
use anyhow::{Context, Result};
use rand::Rng;
use std::collections::{HashMap, VecDeque};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;
use tokio::task::JoinSet;
use tonic::transport::Endpoint;

/// Canonical EIP-4337 v0.7 EntryPoint (identical on every chain).
const ENTRY_POINT: Address =
    alloy::primitives::address!("0000000071727De22E5E9d8BAf0edAc6f37da032");

pub const PEER_GRPC_PORT: u16 = 50051;
const RECONCILE_INTERVAL: Duration = Duration::from_secs(15);
const CSK_CONNECT_TIMEOUT: Duration = Duration::from_secs(1);
const CSK_RPC_TIMEOUT: Duration = Duration::from_secs(2);
const CSK_RETRY_MIN: Duration = Duration::from_millis(250);
const CSK_RETRY_MAX: Duration = Duration::from_secs(2);
const CSK_PULL_PARALLELISM: usize = 8;
/// Base wait before re-sending our PeerEndpoint to a peer whose Ed25519 key is
/// still unknown (a fresh send is delivered through its Indexer subscription).
/// Doubles per attempt up to [`ENVELOPE_RESEND_MAX`]: every resend is a
/// sponsored UserOp, and a peer that never answers (an on-chain orphan whose VM is
/// gone — this cluster has no removeMember) would otherwise cost 144 ops/day from
/// every live node, forever (live-found 2026-07: ~1.3k ops/day fleet-wide).
const ENVELOPE_RESEND: Duration = Duration::from_secs(600);
/// Resend backoff ceiling: one envelope per day to a peer that never answers.
const ENVELOPE_RESEND_MAX: Duration = Duration::from_secs(86_400);

/// Our PeerEndpoint send history toward one peer (drives the resend backoff).
#[derive(Clone, Copy)]
struct SendState {
    at_ms: u64,
    attempts: u32,
}

/// Delay before resend attempt `attempts + 1`: `ENVELOPE_RESEND * 2^attempts`,
/// capped at [`ENVELOPE_RESEND_MAX`].
fn resend_delay_ms(attempts: u32) -> u64 {
    let base = ENVELOPE_RESEND.as_millis() as u64;
    let max = ENVELOPE_RESEND_MAX.as_millis() as u64;
    base.saturating_mul(1u64 << attempts.min(16)).min(max)
}

struct Ctx {
    config: Config,
    dstack: Arc<dyn DstackRuntime>,
    chain: Arc<ChainClient>,
    shared: Arc<Shared>,
    wg: Arc<dyn MeshControl>,
    bundler: Arc<BundlerClient>,
    owner_signer: PrivateKeySigner,
    /// Serializes UserOp submission (EntryPoint nonces are fetched per-submit).
    submit_lock: Mutex<()>,
    /// Our own gateway ingress hostname (SNI), advertised in PeerEndpoint.
    self_sni: String,
    /// UDP punch upgrader (None when WG_UDP_PUNCH=false). Mesh bring-up and
    /// health never depend on it.
    puncher: Option<Arc<transport::punch::Puncher>>,
    /// Peer Ed25519 keys learned from PeerEndpoint envelopes, mirrored to the
    /// durable state volume so a restart doesn't forget them.
    learned_keys: Mutex<crate::peer_cache::LearnedKeys>,
    /// PeerEndpoint send/reply history shared by reconciliation and Indexer message
    /// dispatch so the sponsored resend backoff has one authoritative state.
    last_sent: Mutex<HashMap<[u8; 32], SendState>>,
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn sni_for(member_contract: Address, tcp_port: u16, gw_domain: &str) -> String {
    // TLS-passthrough route: `<app_id>-<port>s.<gateway-domain>`.
    format!(
        "{}-{}s.{}",
        hex::encode(member_contract.as_slice()),
        tcp_port,
        gw_domain
    )
}

impl Ctx {
    /// Wrap an inner cluster call as `ClusterMember.execute` and submit it as a
    /// sponsored UserOp signed by the registration-derived owner key.
    async fn submit_op(&self, inner: Bytes) -> Result<B256> {
        let outer = userop::wrap_execute(self.shared.cluster, inner);
        let op = userop::UserOperation::new(self.shared.member_contract, U256::ZERO, outer);
        let _g = self.submit_lock.lock().await;
        self.bundler.submit(&self.owner_signer, op).await
    }
}

async fn resend_due(ctx: &Ctx, member_id: &[u8; 32], peer_key_known: bool, now: u64) -> bool {
    let sent = ctx.last_sent.lock().await;
    match sent.get(member_id) {
        None => true,
        Some(state) => {
            !peer_key_known && now.saturating_sub(state.at_ms) > resend_delay_ms(state.attempts)
        }
    }
}

async fn reply_due(ctx: &Ctx, member_id: &[u8; 32], now: u64) -> bool {
    let sent = ctx.last_sent.lock().await;
    sent.get(member_id).map_or(true, |state| {
        now.saturating_sub(state.at_ms) > ENVELOPE_RESEND.as_millis() as u64
    })
}

/// Record either a successful or failed sponsored send and return the prior attempt
/// count for logging. Both paths share the same backoff budget.
async fn record_send_attempt(ctx: &Ctx, member_id: [u8; 32], now: u64) -> u32 {
    let mut sent = ctx.last_sent.lock().await;
    let attempts = sent.get(&member_id).map_or(0, |state| state.attempts);
    sent.insert(
        member_id,
        SendState {
            at_ms: now,
            attempts: attempts.saturating_add(1),
        },
    );
    attempts
}

/// Spawn all mesh bring-up tasks. Returns once spawned (health::serve blocks after).
pub async fn launch(
    config: Config,
    dstack: Arc<dyn DstackRuntime>,
    chain: Arc<ChainClient>,
    shared: Arc<Shared>,
    wg: Arc<dyn MeshControl>,
) -> Result<()> {
    let Some(gw_domain) = config.gateway_domain.clone() else {
        tracing::warn!("GATEWAY_DOMAIN unset — mesh bring-up skipped (registration-only mode)");
        return Ok(());
    };

    shared.set_phase(Phase::Subscribing).await;

    // The owner key: the same /GetKey-derived signer registration installed as the
    // ClusterMember owner; every post-registration UserOp must be signed by it.
    let owner_signer = dstack_facet::derive_owner_signer(dstack.as_ref())
        .await
        .context("derive owner signer for post-registration UserOps")?;

    let bundler = Arc::new(BundlerClient::new(
        config.bundler_url.clone(),
        config.rpc_url.clone(),
        ENTRY_POINT,
        config.chain_id,
        config.gas_policy_id.clone(),
    ));

    // Restart path: keys learned in previous runs short-circuit the envelope
    // resend loop (each resend is a sponsored UserOp).
    let learned_keys = crate::peer_cache::load(config.sidecar_state_dir.as_deref()).await;
    if !learned_keys.is_empty() {
        tracing::info!(
            peers = learned_keys.len(),
            "loaded persisted peer Ed25519 keys"
        );
    }

    let self_sni = sni_for(shared.member_contract, config.wg_tcp_port, &gw_domain);

    // UDP punch upgrader (udp-transport-upgrade spec). Gateway TCP stays the
    // bootstrap path and permanent fallback; this only upgrades live links.
    let puncher = if config.wg_udp_punch {
        Some(transport::punch::Puncher::new(
            shared.clone(),
            wg.clone(),
            transport::punch::PunchConfig {
                enabled: true,
                timeout: Duration::from_secs(config.punch_timeout_secs),
                backoff_initial_secs: config.punch_retry_backoff_secs,
                wg_listen_port: shared.wg_listen_port,
            },
            Arc::new(transport::punch::DnsEgress::new(gw_domain.clone())),
        ))
    } else {
        tracing::info!("WG_UDP_PUNCH=false — links stay on gateway TCP");
        None
    };

    let ctx = Arc::new(Ctx {
        config: config.clone(),
        dstack,
        chain: chain.clone(),
        shared: shared.clone(),
        wg,
        bundler: bundler.clone(),
        owner_signer,
        submit_lock: Mutex::new(()),
        self_sni,
        puncher: puncher.clone(),
        learned_keys: Mutex::new(learned_keys),
        last_sent: Mutex::new(HashMap::new()),
    });

    // 1. wg-over-TCP ingress (peers reach us through the gateway).
    {
        let (tcp_port, wg_port) = (config.wg_tcp_port, shared.wg_listen_port);
        tokio::spawn(async move {
            if let Err(e) = transport::serve_ingress(tcp_port, wg_port).await {
                tracing::error!(error = %e, "wg-tcp ingress exited");
            }
        });
    }

    // 1b. punch scheduler + UDP-path watchdog (no-ops while no link qualifies).
    if let Some(p) = &puncher {
        tokio::spawn(p.clone().run_initiator());
        tokio::spawn(p.clone().run_watchdog());
    }

    // 2. heartbeats (verification activates per-peer once its Ed25519 key arrives).
    crate::heartbeat::spawn(shared.clone());

    // 2b. Publish our Ed25519 heartbeat key on chain (ed25519-onchain-key spec) so
    // peers learn it via a read, not a sponsored envelope. One idempotent op per node
    // lifetime; retries on paymaster/RPC hiccups, then exits.
    {
        let ctx = ctx.clone();
        tokio::spawn(async move { publish_own_ed25519_loop(ctx).await });
    }

    // Indexer is the sole event source. State-change events wake the direct-view
    // reconciler; MessageSent events are handled and Ack'd through event_rx.
    if let Some((block, log_index)) = crate::indexer_client::load_cursor(
        config.sidecar_state_dir.as_deref(),
        shared.cluster,
        &shared.self_member_id,
    )
    .await
    {
        shared.set_indexer_progress(block, log_index, false).await;
        tracing::info!(block, log_index, "loaded durable Indexer replay checkpoint");
    }
    let (wake_tx, wake_rx) = tokio::sync::mpsc::channel::<()>(8);
    let (event_tx, event_rx) =
        tokio::sync::mpsc::channel::<crate::indexer_client::DispatchRequest>(128);

    {
        let ctx = ctx.clone();
        let wake = wake_tx.clone();
        tokio::spawn(async move { indexer_event_loop(ctx, event_rx, wake).await });
    }

    // 3. peer reconciler: chain → bridges → wg peers → envelope exchange.
    {
        let ctx = ctx.clone();
        let gw = gw_domain.clone();
        tokio::spawn(async move { reconcile_loop(ctx, gw, wake_rx).await });
    }

    // Serial-console diagnostics for mesh bring-up. This exposes only public peer
    // identifiers/keys and kernel counters; no secrets or application payloads.
    {
        let ctx = ctx.clone();
        tokio::spawn(async move { wg_diagnostic_loop(ctx).await });
    }

    // 3b. Indexer subscription (sidecar spec §9): discover via IndexerRegistry,
    // verify every push, dispatch, Ack, and reconnect with backoff.
    {
        let shared = shared.clone();
        let chain = chain.clone();
        let registry = config.indexer_registry_addr;
        let state_dir = config.sidecar_state_dir.clone();
        tokio::spawn(
            async move { indexer_loop(shared, chain, registry, event_tx, state_dir).await },
        );
    }

    // 4. CSK originate-or-pull.
    {
        let ctx = ctx.clone();
        tokio::spawn(async move { csk_loop(ctx).await });
    }

    // 5. peer-control gRPC on the mesh IP (only members can reach it, over wg).
    {
        let shared = shared.clone();
        let chain = chain.clone();
        let puncher = puncher.clone();
        tokio::spawn(async move { serve_peer_grpc(shared, chain, puncher).await });
    }

    // 6. app-facing agent gRPC on the unix socket.
    {
        let shared = shared.clone();
        let chain = chain.clone();
        let path = config.agent_grpc_socket.clone();
        tokio::spawn(async move { serve_agent_grpc(shared, chain, bundler, path).await });
    }

    Ok(())
}

async fn wg_diagnostic_loop(ctx: Arc<Ctx>) {
    loop {
        tokio::time::sleep(Duration::from_secs(15)).await;
        let peers: Vec<_> = {
            let table = ctx.shared.peers.lock().await;
            table
                .all()
                .map(|p| (p.member_id, p.mesh_ip, p.wg_pub, p.configured, p.live, p.transport))
                .collect()
        };
        if peers.is_empty() {
            let phase = ctx.shared.current_phase().await;
            tracing::info!(phase = phase.as_str(),
                "wg diagnostic: no peers discovered");
            continue;
        }
        for (member_id, mesh_ip, wg_pub, configured, live, transport) in peers {
            match ctx.wg.peer_status(&wg_pub).await {
                Ok(Some(status)) => {
                    let phase = ctx.shared.current_phase().await;
                    tracing::info!(
                        peer = %hex::encode(member_id),
                        mesh_ip = %cidr::fmt_ipv4(mesh_ip),
                        configured,
                        live,
                        transport = transport.as_str(),
                        endpoint = ?status.endpoint,
                        handshake_unix = status.last_handshake_unix,
                        phase = phase.as_str(),
                        "wg diagnostic"
                    )
                }
                Ok(None) => tracing::warn!(peer = %hex::encode(member_id), configured,
                    "wg diagnostic: peer absent from kernel interface"),
                Err(error) => tracing::warn!(peer = %hex::encode(member_id), error = ?error,
                    "wg diagnostic: status read failed"),
            }
        }
    }
}

/// One pass + steady-state loop: enumerate members from current chain views,
/// configure peers, and send transitional PeerEndpoint envelopes when enabled.
async fn reconcile_loop(
    ctx: Arc<Ctx>,
    gw_domain: String,
    mut wake: tokio::sync::mpsc::Receiver<()>,
) {
    loop {
        if let Err(e) = reconcile_once(&ctx, &gw_domain).await {
            tracing::warn!(error = ?e, "peer reconcile pass failed; retrying");
        }
        // Indexer state events cut the latency; the interval reconciles current views.
        tokio::select! {
            _ = tokio::time::sleep(RECONCILE_INTERVAL) => {}
            _ = wake.recv() => {}
        }
    }
}

/// Discover the Indexer from the on-chain registry and hold the subscription open,
/// re-discovering + reconnecting with backoff (spec §9.2). There is deliberately no
/// direct-log fallback: an unavailable Indexer pauses event delivery only.
async fn indexer_loop(
    shared: Arc<Shared>,
    chain: Arc<ChainClient>,
    registry: Address,
    dispatch: tokio::sync::mpsc::Sender<crate::indexer_client::DispatchRequest>,
    state_dir: Option<std::path::PathBuf>,
) {
    shared.set_indexer_connected(false).await;
    let mut reconnect_backoff = Duration::from_secs(1);
    loop {
        let info = match crate::chain::registry::read_indexer(chain.provider(), registry).await {
            Ok(i) => i,
            Err(e) => {
                tracing::warn!(error = ?e, "IndexerRegistry read failed; retrying");
                tokio::time::sleep(Duration::from_secs(60)).await;
                continue;
            }
        };
        if info.endpoint.is_empty() {
            tracing::info!("no indexer registered; event delivery paused");
            tokio::time::sleep(Duration::from_secs(300)).await;
            continue;
        }
        tracing::info!(endpoint = %info.endpoint, "subscribing to indexer");
        let result = crate::indexer_client::connect_and_run(
            shared.clone(),
            info.endpoint.clone(),
            info.pubkey.0,
            dispatch.clone(),
            state_dir.clone(),
        )
        .await;
        // connect_and_run marks the diagnostic connected only after Subscribe opens.
        // A hot LB cutover therefore gets the minimum retry, while repeated failures
        // before a stream opens back off to avoid hammering the endpoint.
        let had_open_stream = shared.get_indexer_status().await.connected;
        match result {
            Ok(()) => tracing::info!("indexer stream ended; re-discovering"),
            Err(e) => tracing::warn!(error = ?e, "indexer subscription failed; re-discovering"),
        }
        shared.set_indexer_connected(false).await;
        let retry_in = if had_open_stream {
            reconnect_backoff = Duration::from_secs(1);
            Duration::from_secs(1)
        } else {
            let current = reconnect_backoff;
            reconnect_backoff = (reconnect_backoff * 2).min(Duration::from_secs(30));
            current
        };
        tracing::info!(
            retry_ms = retry_in.as_millis(),
            "retrying Indexer discovery"
        );
        tokio::time::sleep(retry_in).await;
    }
}

async fn reconcile_once(ctx: &Ctx, gw_domain: &str) -> Result<()> {
    let cluster = ctx.shared.cluster;
    let members = ctx.chain.list_members(cluster).await?;
    if let Some(originator) = members.first() {
        ctx.shared.set_originator_member_id(originator.0).await;
    }
    let others: Vec<[u8; 32]> = members
        .iter()
        .map(|m| m.0)
        .filter(|m| *m != ctx.shared.self_member_id)
        .collect();

    if others.is_empty() {
        ctx.shared.set_phase(Phase::WaitingPeers).await;
        return Ok(());
    }

    for member_id in &others {
        let configured = ctx
            .shared
            .peers
            .lock()
            .await
            .get(member_id)
            .map(|p| p.configured)
            .unwrap_or(false);

        if !configured {
            ctx.shared.set_phase(Phase::WgConfiguring).await;
            let rec = ctx
                .chain
                .member_by_id(cluster, B256::from(*member_id))
                .await?;
            if !rec.exists() {
                continue;
            }
            let mesh_ip = ctx
                .chain
                .mesh_ip_of(cluster, B256::from(*member_id))
                .await?;
            let sni = sni_for(rec.member_contract, ctx.config.wg_tcp_port, gw_domain);
            let endpoint =
                transport::spawn_peer_bridge(sni.clone(), 443, ctx.shared.wg_listen_port)
                    .await
                    .context("spawn peer bridge")?;
            ctx.wg
                .add_peer(&WgPeerConfig {
                    public_key: rec.wg_pubkey.0,
                    endpoint: endpoint.to_string(),
                    allowed_ip: mesh_ip,
                    persistent_keepalive: 25,
                })
                .await
                .context("wg add_peer")?;
            let newly_configured = {
                let mut peers = ctx.shared.peers.lock().await;
                peers.ensure_chain(*member_id, mesh_ip, rec.wg_pubkey.0);
                peers.set_endpoint(member_id, sni.clone());
                // The bridge address is the punch-upgrade revert target; the
                // bridge task itself stays alive even while the link rides UDP.
                peers.set_bridge_addr(member_id, endpoint);
                peers.mark_configured(member_id)
            };
            if newly_configured {
                ctx.shared.peer_change.notify_one();
            }
            let _ = ctx.shared.peer_event_tx.send(AppPeerEvent::Joined {
                member_id: *member_id,
                mesh_ip,
            });
            tracing::info!(
                member_id = %hex::encode(member_id),
                mesh_ip = %cidr::fmt_ipv4(mesh_ip),
                bridge = %endpoint,
                gateway = %sni,
                "peer configured (wg over gateway TCP)"
            );
        }

        // Resolve the peer's Ed25519 heartbeat key. Preferred source is ON CHAIN
        // (ed25519-onchain-key spec) — a plain read, no sponsored message. A member
        // running the new sidecar has published it, so the whole envelope exchange
        // below is skipped. Fall back to the sealed learned-keys cache (envelope-era)
        // only for peers whose key is not yet on chain (old sidecar / pre-cut cluster).
        if ctx
            .shared
            .peers
            .lock()
            .await
            .ed25519_of(member_id)
            .is_none()
        {
            let on_chain = ctx
                .chain
                .ed25519_key_of(cluster, B256::from(*member_id))
                .await
                .unwrap_or(B256::ZERO);
            if on_chain != B256::ZERO {
                ctx.shared
                    .peers
                    .lock()
                    .await
                    .set_ed25519(member_id, on_chain.0);
            } else if let Some(ed) = ctx.learned_keys.lock().await.get(member_id).copied() {
                ctx.shared.peers.lock().await.set_ed25519(member_id, ed);
            }
        }

        // Envelope FALLBACK (mixed fleet only): if the peer's key is still unknown —
        // i.e. it hasn't published on chain — send ours via the sponsored envelope so
        // an un-upgraded peer can still learn it. Backed off exponentially. Once every
        // node is on the new sidecar this branch never fires (peer_ed_known is true
        // from the chain read above).
        let peer_ed_known = ctx
            .shared
            .peers
            .lock()
            .await
            .ed25519_of(member_id)
            .is_some();
        let now = now_ms();
        let due = ctx.config.peer_envelope_fallback
            && resend_due(ctx, member_id, peer_ed_known, now).await;
        if due {
            match send_peer_endpoint(ctx, *member_id).await {
                Ok(tx) => {
                    let attempts = record_send_attempt(ctx, *member_id, now).await;
                    tracing::info!(peer = %hex::encode(member_id), tx = %tx, attempts,
                        "PeerEndpoint envelope sent");
                }
                Err(e) => {
                    // Failures back off exactly like resends: a systematic bundler/
                    // paymaster rejection must not retry at reconcile cadence — each
                    // attempt consumes a sponsorship (live-found: 19 peers x 15s
                    // burned a 100-op policy counter in ~90s with zero landed ops).
                    let attempts = record_send_attempt(ctx, *member_id, now).await;
                    tracing::warn!(peer = %hex::encode(member_id), error = ?e, attempts,
                        "PeerEndpoint envelope send failed; backing off");
                }
            }
        }
    }

    if ctx.shared.peers.lock().await.live_count() > 0 || ctx.shared.gates.first_converged() {
        if ctx.shared.gates.healthy() {
            ctx.shared.set_phase(Phase::Healthy).await;
        } else {
            ctx.shared.set_phase(Phase::Heartbeating).await;
        }
    }
    Ok(())
}

/// Seal our PeerEndpoint (gateway host + Ed25519 key) to the peer's on-chain
/// x25519 key and submit `MessageFacet.send`. The on-chain envelopeId is salted —
/// (recipient, envelopeId) is dedup'd by the facet — while receivers demux on the
/// `kind` field inside the ciphertext.
async fn send_peer_endpoint(ctx: &Ctx, peer_id: [u8; 32]) -> Result<B256> {
    let xpub = ctx
        .chain
        .x_pubkey_of(ctx.shared.cluster, B256::from(peer_id))
        .await?;
    if xpub == B256::ZERO {
        anyhow::bail!("peer has no x25519 key on chain");
    }
    let mut pe = PeerEndpoint::new(
        ctx.shared.self_member_id,
        ctx.self_sni.clone(),
        443,
        ctx.shared.keys.wg_pub,
        ctx.shared.keys.ed25519_pub,
    );
    // Should-Have (udp-transport-upgrade spec): advertise our UDP candidate to
    // cut one negotiation round trip. Optional CBOR fields — old peers ignore.
    if let Some(p) = &ctx.puncher {
        if let Some((ip, port)) = p.advertised_udp_candidate().await {
            pe.udp_ip = Some(ip);
            pe.udp_port = Some(port);
        }
    }
    let ct = envelopes::seal(&xpub.0, &pe.encode()?)?;
    let mut salt = Vec::with_capacity(72);
    salt.extend_from_slice(&envelopes::peer_endpoint_envelope_id());
    salt.extend_from_slice(&ctx.shared.self_member_id);
    salt.extend_from_slice(&now_ms().to_le_bytes());
    let envelope_id = keccak256(&salt);
    let inner =
        message_facet::build_send_calldata(B256::from(peer_id), envelope_id, Bytes::from(ct));
    ctx.submit_op(inner).await
}

/// Serialize signed Indexer events through the existing bring-up handlers. Completion
/// is returned to the transport task, which sends the durable Ack only afterward.
async fn indexer_event_loop(
    ctx: Arc<Ctx>,
    mut events: tokio::sync::mpsc::Receiver<crate::indexer_client::DispatchRequest>,
    wake: tokio::sync::mpsc::Sender<()>,
) {
    while let Some(request) = events.recv().await {
        let result = match request.event {
            crate::indexer_client::IndexedEvent::MessageSent {
                sender,
                envelope_id,
                ciphertext,
                block_number,
            } => handle_indexed_message(&ctx, sender, envelope_id, &ciphertext, block_number).await,
            crate::indexer_client::IndexedEvent::Reconcile => {
                let _ = wake.try_send(());
                Ok(())
            }
        };
        let completion = result.map_err(|error| format!("{error:#}"));
        let _ = request.completion.send(completion);
    }
}

/// Absorb one chain-authenticated MessageSent event. Invalid ciphertext or an invalid
/// internal sender binding is a handled drop and is Ack'd so one poison message cannot
/// wedge the member cursor indefinitely.
async fn handle_indexed_message(
    ctx: &Ctx,
    sender: [u8; 32],
    envelope_id: [u8; 32],
    ciphertext: &[u8],
    block_number: u64,
) -> Result<()> {
    let Ok(plaintext) = envelopes::open(
        &ctx.shared.keys.x_secret,
        &ctx.shared.keys.x_pub,
        ciphertext,
    ) else {
        tracing::debug!(sender = %hex::encode(sender), envelope = %hex::encode(envelope_id),
            block = block_number, "dropping undecryptable indexed message");
        return Ok(());
    };

    match envelopes::classify_internal(&plaintext) {
        Some(peer_endpoint) => {
            if peer_endpoint.member_id != sender {
                tracing::warn!(sender = %hex::encode(sender),
                    claimed = %hex::encode(peer_endpoint.member_id),
                    "dropping PeerEndpoint with sender-binding mismatch");
                return Ok(());
            }
            {
                let mut peers = ctx.shared.peers.lock().await;
                if peers.set_ed25519(&sender, peer_endpoint.ed25519_pub) {
                    tracing::info!(peer = %hex::encode(sender), host = %peer_endpoint.host,
                        "PeerEndpoint envelope absorbed (Ed25519 key learned)");
                }
                if let Some(udp) = peer_endpoint.udp_addr() {
                    peers.set_advertised_udp(&sender, Some(udp));
                }
            }
            {
                let mut learned = ctx.learned_keys.lock().await;
                if learned.get(&sender) != Some(&peer_endpoint.ed25519_pub) {
                    learned.insert(sender, peer_endpoint.ed25519_pub);
                    if let Err(error) =
                        crate::peer_cache::store(ctx.config.sidecar_state_dir.as_deref(), &learned)
                            .await
                    {
                        tracing::warn!(error = ?error, "peer-key cache write failed (non-fatal)");
                    }
                }
            }

            let now = now_ms();
            if ctx.config.peer_envelope_fallback && reply_due(ctx, &sender, now).await {
                match send_peer_endpoint(ctx, sender).await {
                    Ok(tx) => {
                        let attempts = record_send_attempt(ctx, sender, now).await;
                        tracing::info!(peer = %hex::encode(sender), tx = %tx, attempts,
                            "PeerEndpoint reply sent (peer announced itself)");
                    }
                    Err(error) => {
                        let attempts = record_send_attempt(ctx, sender, now).await;
                        tracing::warn!(peer = %hex::encode(sender), error = ?error, attempts,
                            "PeerEndpoint reply failed; backing off");
                    }
                }
            }
        }
        None => {
            let bytes = plaintext.len();
            if ctx
                .shared
                .incoming_tx
                .send(crate::state::AppIncoming {
                    sender_member_id: sender,
                    payload: plaintext,
                    block_number,
                })
                .is_ok()
            {
                tracing::debug!(sender = %hex::encode(sender), block = block_number, bytes,
                    "app message forwarded to SubscribeMessages");
            }
        }
    }
    Ok(())
}

/// Publish our Ed25519 heartbeat key on chain once, idempotently (ed25519-onchain-key
/// spec). Skips if already equal; retries with backoff on paymaster/RPC failure, then
/// exits. One sponsored op per node lifetime — replaces the per-peer envelope storm.
async fn publish_own_ed25519_loop(ctx: Arc<Ctx>) {
    let self_id = B256::from(ctx.shared.self_member_id);
    let want = B256::from(ctx.shared.keys.ed25519_pub);
    let mut backoff = Duration::from_secs(10);
    loop {
        match ctx.chain.ed25519_key_of(ctx.shared.cluster, self_id).await {
            Ok(cur) if cur == want => {
                tracing::info!("Ed25519 heartbeat key already published on chain");
                return;
            }
            Ok(_) => match ctx
                .submit_op(network_facet::build_publish_ed25519_calldata(want))
                .await
            {
                Ok(tx) => {
                    tracing::info!(tx = %tx, "published Ed25519 heartbeat key on chain");
                    return;
                }
                Err(e) => tracing::warn!(error = ?e, "publishEd25519Key failed; retrying"),
            },
            // Revert here means the cluster predates the ed25519 cut — retry slowly in
            // case the cut lands later; harmless (one read per backoff interval).
            Err(e) => tracing::warn!(error = ?e, "read ed25519KeyOf(self) failed; retrying"),
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(300));
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct CskCandidate {
    member_id: [u8; 32],
    mesh_ip: u32,
    live: bool,
    originator: bool,
}

fn ordered_csk_candidates(
    peers: &PeerTable,
    originator_member_id: Option<[u8; 32]>,
) -> Vec<CskCandidate> {
    let mut candidates: Vec<CskCandidate> = peers
        .all()
        .filter(|peer| peer.configured)
        .map(|peer| CskCandidate {
            member_id: peer.member_id,
            mesh_ip: peer.mesh_ip,
            live: peer.live,
            originator: originator_member_id == Some(peer.member_id),
        })
        .collect();
    candidates.sort_by_key(|candidate| {
        let priority = if candidate.originator {
            0
        } else if candidate.live {
            1
        } else {
            2
        };
        (priority, candidate.member_id)
    });
    candidates
}

#[derive(Clone, Debug)]
enum CskProbeResult {
    Sealed(Vec<u8>),
    Unavailable,
    Timeout,
    Failed(String),
}

#[async_trait::async_trait]
trait CskPeerRequester: Send + Sync + 'static {
    async fn request(
        &self,
        candidate: &CskCandidate,
        requester_member_id: [u8; 32],
    ) -> CskProbeResult;
}

struct TonicCskPeerRequester {
    port: u16,
    connect_timeout: Duration,
    rpc_timeout: Duration,
}

impl Default for TonicCskPeerRequester {
    fn default() -> Self {
        Self {
            port: PEER_GRPC_PORT,
            connect_timeout: CSK_CONNECT_TIMEOUT,
            rpc_timeout: CSK_RPC_TIMEOUT,
        }
    }
}

#[async_trait::async_trait]
impl CskPeerRequester for TonicCskPeerRequester {
    async fn request(
        &self,
        candidate: &CskCandidate,
        requester_member_id: [u8; 32],
    ) -> CskProbeResult {
        let url = format!("http://{}:{}", cidr::fmt_ipv4(candidate.mesh_ip), self.port);
        let endpoint = match Endpoint::from_shared(url) {
            Ok(endpoint) => endpoint
                .connect_timeout(self.connect_timeout)
                .timeout(self.rpc_timeout),
            Err(e) => return CskProbeResult::Failed(format!("invalid endpoint: {e}")),
        };
        let channel = match tokio::time::timeout(self.connect_timeout, endpoint.connect()).await {
            Ok(Ok(channel)) => channel,
            Ok(Err(e)) => return CskProbeResult::Failed(format!("connect: {e}")),
            Err(_) => return CskProbeResult::Timeout,
        };
        let mut client = PeerControlClient::new(channel);
        let mut request = tonic::Request::new(CskRequest {
            requester_member_id: requester_member_id.to_vec(),
        });
        request.set_timeout(self.rpc_timeout);
        match tokio::time::timeout(self.rpc_timeout, client.request_cluster_shared_key(request))
            .await
        {
            Err(_) => CskProbeResult::Timeout,
            Ok(Ok(response)) => CskProbeResult::Sealed(response.into_inner().sealed_csk),
            Ok(Err(status)) if status.code() == tonic::Code::Unavailable => {
                CskProbeResult::Unavailable
            }
            Ok(Err(status)) if status.code() == tonic::Code::DeadlineExceeded => {
                CskProbeResult::Timeout
            }
            Ok(Err(status)) => CskProbeResult::Failed(format!("grpc {}", status.code())),
        }
    }
}

#[derive(Default, Debug)]
struct CskPullStats {
    candidates: usize,
    attempts: usize,
    max_in_flight: usize,
    timeouts: usize,
    unavailable: usize,
    failed: usize,
    verification_failed: usize,
    elapsed_ms: u128,
}

struct CskPullSuccess {
    csk: zeroize::Zeroizing<[u8; 32]>,
    candidate: CskCandidate,
    stats: CskPullStats,
}

async fn pull_csk_from_candidates(
    requester: Arc<dyn CskPeerRequester>,
    candidates: Vec<CskCandidate>,
    requester_member_id: [u8; 32],
    requester_xsecret: &crypto_box::SecretKey,
    requester_xpub: &[u8; 32],
    expected_commitment: &[u8; 32],
) -> (Option<CskPullSuccess>, CskPullStats) {
    let started = Instant::now();
    let mut stats = CskPullStats {
        candidates: candidates.len(),
        ..Default::default()
    };
    let mut pending = VecDeque::from(candidates);
    let mut running = JoinSet::new();

    let start_next = |running: &mut JoinSet<_>,
                      pending: &mut VecDeque<CskCandidate>,
                      requester: &Arc<dyn CskPeerRequester>| {
        if let Some(candidate) = pending.pop_front() {
            let requester = requester.clone();
            running.spawn(async move {
                let result = requester.request(&candidate, requester_member_id).await;
                (candidate, result)
            });
            true
        } else {
            false
        }
    };

    while running.len() < CSK_PULL_PARALLELISM && start_next(&mut running, &mut pending, &requester)
    {
    }
    stats.max_in_flight = stats.max_in_flight.max(running.len());

    while let Some(joined) = running.join_next().await {
        match joined {
            Ok((candidate, result)) => {
                stats.attempts += 1;
                match result {
                    CskProbeResult::Sealed(sealed) => match csk::open_pulled(
                        &sealed,
                        requester_xsecret,
                        requester_xpub,
                        expected_commitment,
                    ) {
                        Ok(csk) => {
                            running.abort_all();
                            stats.elapsed_ms = started.elapsed().as_millis();
                            return (
                                Some(CskPullSuccess {
                                    csk,
                                    candidate,
                                    stats,
                                }),
                                CskPullStats::default(),
                            );
                        }
                        Err(e) => {
                            stats.verification_failed += 1;
                            tracing::warn!(
                                peer = %hex::encode(candidate.member_id),
                                mesh_ip = %cidr::fmt_ipv4(candidate.mesh_ip),
                                error = ?e,
                                "pulled CSK failed verification"
                            );
                        }
                    },
                    CskProbeResult::Unavailable => stats.unavailable += 1,
                    CskProbeResult::Timeout => stats.timeouts += 1,
                    CskProbeResult::Failed(error) => {
                        stats.failed += 1;
                        tracing::debug!(
                            peer = %hex::encode(candidate.member_id),
                            mesh_ip = %cidr::fmt_ipv4(candidate.mesh_ip),
                            error,
                            "CSK peer probe failed"
                        );
                    }
                }
            }
            Err(e) => {
                stats.failed += 1;
                tracing::warn!(error = ?e, "CSK peer probe task failed");
            }
        }
        while running.len() < CSK_PULL_PARALLELISM
            && start_next(&mut running, &mut pending, &requester)
        {}
        stats.max_in_flight = stats.max_in_flight.max(running.len());
    }
    stats.elapsed_ms = started.elapsed().as_millis();
    (None, stats)
}

fn jittered_retry(delay: Duration) -> Duration {
    let percent = rand::thread_rng().gen_range(80u128..=120);
    Duration::from_millis(((delay.as_millis() * percent) / 100).max(1) as u64)
        .clamp(CSK_RETRY_MIN, CSK_RETRY_MAX)
}

async fn hold_csk(ctx: &Ctx, csk: &[u8; 32]) {
    *ctx.shared.csk.lock().await = Some(*csk);
    ctx.shared.gates.set_csk_acquired();
}

async fn persist_and_hold_csk(
    ctx: &Ctx,
    csk: &[u8; 32],
    expected_commitment: &[u8; 32],
    source: &'static str,
) {
    match csk::store_cache(
        ctx.dstack.as_ref(),
        ctx.config.sidecar_state_dir.as_deref(),
        ctx.shared.cluster,
        ctx.shared.member_contract,
        expected_commitment,
        csk,
    )
    .await
    {
        Ok(true) => tracing::info!(source, "CSK encrypted cache updated"),
        Ok(false) => tracing::debug!(source, "CSK durable cache is not configured"),
        Err(e) => tracing::warn!(source, error = ?e, "CSK cache write failed (non-fatal)"),
    }
    hold_csk(ctx, csk).await;
}

async fn resolve_originator(ctx: &Ctx) -> Result<Option<[u8; 32]>> {
    if let Some(originator) = ctx.shared.get_originator_member_id().await {
        return Ok(Some(originator));
    }
    let members = ctx.chain.list_members(ctx.shared.cluster).await?;
    if let Some(originator) = members.first() {
        ctx.shared.set_originator_member_id(originator.0).await;
        Ok(Some(originator.0))
    } else {
        Ok(None)
    }
}

/// CSK lifecycle (master spec §8): authenticated cache, deterministic originator
/// re-derivation, or bounded parallel pull from configured peers.
async fn csk_loop(ctx: Arc<Ctx>) {
    let requester: Arc<dyn CskPeerRequester> = Arc::new(TonicCskPeerRequester::default());
    let mut retry = CSK_RETRY_MIN;
    let mut cache_checked_for: Option<[u8; 32]> = None;
    loop {
        if ctx.shared.gates.csk_acquired() {
            return;
        }
        let notified = ctx.shared.peer_change.notified();
        match csk_once(&ctx, requester.clone(), &mut cache_checked_for).await {
            Ok(true) => return,
            Ok(false) => {}
            Err(e) => tracing::debug!(error = ?e, "CSK pass incomplete; retrying"),
        }
        tokio::select! {
            _ = notified => retry = CSK_RETRY_MIN,
            _ = tokio::time::sleep(jittered_retry(retry)) => {
                retry = (retry * 2).min(CSK_RETRY_MAX);
            }
        }
    }
}

async fn csk_once(
    ctx: &Ctx,
    requester: Arc<dyn CskPeerRequester>,
    cache_checked_for: &mut Option<[u8; 32]>,
) -> Result<bool> {
    let cluster = ctx.shared.cluster;
    let chain_commitment = ctx.chain.csk_commitment(cluster).await?;

    if chain_commitment == B256::ZERO {
        let originator = resolve_originator(ctx).await?;
        if originator != Some(ctx.shared.self_member_id) {
            return Ok(false);
        }
        let csk = csk::derive_originator(ctx.dstack.as_ref()).await?;
        let expected_commitment = csk::commitment(&csk);
        let inner =
            message_facet::build_set_csk_commitment_calldata(B256::from(expected_commitment));
        let tx = ctx.submit_op(inner).await.context("setCskCommitment")?;
        persist_and_hold_csk(ctx, &csk, &expected_commitment, "originated").await;
        tracing::info!(tx = %tx, "CSK originated + commitment set on-chain");
        return Ok(true);
    }

    if *cache_checked_for != Some(chain_commitment.0) {
        *cache_checked_for = Some(chain_commitment.0);
        let started = Instant::now();
        match csk::load_cache(
            ctx.dstack.as_ref(),
            ctx.config.sidecar_state_dir.as_deref(),
            cluster,
            ctx.shared.member_contract,
            &chain_commitment.0,
        )
        .await
        {
            Ok(Some(csk)) => {
                hold_csk(ctx, &csk).await;
                tracing::info!(
                    elapsed_ms = started.elapsed().as_millis(),
                    "CSK loaded from authenticated encrypted cache"
                );
                return Ok(true);
            }
            Ok(None) => tracing::debug!("CSK encrypted cache miss"),
            Err(e) => tracing::warn!(error = ?e, "CSK cache rejected; falling back to peer pull"),
        }
    }

    let originator = resolve_originator(ctx).await?;
    if originator == Some(ctx.shared.self_member_id) {
        let csk = csk::derive_originator(ctx.dstack.as_ref()).await?;
        anyhow::ensure!(
            csk::commitment(&csk) == chain_commitment.0,
            "originator-derived CSK does not match on-chain commitment"
        );
        persist_and_hold_csk(ctx, &csk, &chain_commitment.0, "originator-rederived").await;
        tracing::info!(
            "CSK re-derived + verified against on-chain commitment (originator restart)"
        );
        return Ok(true);
    }

    ctx.shared.set_phase(Phase::PullingCsk).await;
    let candidates = {
        let peers = ctx.shared.peers.lock().await;
        ordered_csk_candidates(&peers, originator)
    };
    let (success, stats) = pull_csk_from_candidates(
        requester,
        candidates,
        ctx.shared.self_member_id,
        &ctx.shared.keys.x_secret,
        &ctx.shared.keys.x_pub,
        &chain_commitment.0,
    )
    .await;
    if let Some(success) = success {
        persist_and_hold_csk(ctx, &success.csk, &chain_commitment.0, "peer-pull").await;
        tracing::info!(
            from = %format!("http://{}:{}", cidr::fmt_ipv4(success.candidate.mesh_ip), PEER_GRPC_PORT),
            peer = %hex::encode(success.candidate.member_id),
            candidates = success.stats.candidates,
            attempts = success.stats.attempts,
            max_in_flight = success.stats.max_in_flight,
            timeouts = success.stats.timeouts,
            unavailable = success.stats.unavailable,
            failed = success.stats.failed,
            verification_failed = success.stats.verification_failed,
            elapsed_ms = success.stats.elapsed_ms,
            "CSK pulled + verified against on-chain commitment"
        );
        return Ok(true);
    }
    tracing::debug!(
        candidates = stats.candidates,
        attempts = stats.attempts,
        max_in_flight = stats.max_in_flight,
        timeouts = stats.timeouts,
        unavailable = stats.unavailable,
        failed = stats.failed,
        verification_failed = stats.verification_failed,
        elapsed_ms = stats.elapsed_ms,
        "CSK peer pull round incomplete"
    );
    Ok(false)
}

async fn serve_peer_grpc(
    shared: Arc<Shared>,
    chain: Arc<ChainClient>,
    puncher: Option<Arc<transport::punch::Puncher>>,
) {
    let addr = SocketAddr::V4(SocketAddrV4::new(
        Ipv4Addr::from(shared.self_mesh_ip),
        PEER_GRPC_PORT,
    ));
    loop {
        let svc = crate::peer_grpc::PeerControlService::new(
            shared.clone(),
            chain.clone(),
            puncher.clone(),
        );
        match tonic::transport::Server::builder()
            .add_service(svc.into_server())
            .serve(addr)
            .await
        {
            Ok(()) => return,
            Err(e) => {
                tracing::warn!(error = %e, %addr, "peer gRPC serve failed; retrying");
                tokio::time::sleep(Duration::from_secs(10)).await;
            }
        }
    }
}

async fn serve_agent_grpc(
    shared: Arc<Shared>,
    chain: Arc<ChainClient>,
    bundler: Arc<BundlerClient>,
    socket_path: String,
) {
    // TODO(security, M4): If the co-located app uses another uid/container, add
    // SO_PEERCRED-based uid/gid allowlisting; same-uid file permissions cannot
    // distinguish the trusted app from other processes running under that uid.
    let uds = match prepare_agent_socket(Path::new(&socket_path)) {
        Ok(l) => l,
        Err(e) => {
            tracing::error!(error = %e, path = %socket_path, "agent gRPC UDS setup failed");
            return;
        }
    };
    tracing::info!(path = %socket_path, "agent gRPC listening");
    let svc = crate::agent_grpc::AgentService::new(shared, chain, bundler);
    if let Err(e) = tonic::transport::Server::builder()
        .add_service(svc.into_server())
        .serve_with_incoming(tokio_stream::wrappers::UnixListenerStream::new(uds))
        .await
    {
        tracing::error!(error = %e, "agent gRPC exited");
    }
}

fn prepare_agent_socket(socket_path: &Path) -> Result<tokio::net::UnixListener> {
    if let Some(dir) = socket_path.parent() {
        std::fs::create_dir_all(dir)
            .with_context(|| format!("create agent gRPC socket dir {}", dir.display()))?;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))
            .with_context(|| format!("chmod agent gRPC socket dir {}", dir.display()))?;
    }
    match std::fs::remove_file(socket_path) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => {
            return Err(e).with_context(|| {
                format!("remove stale agent gRPC socket {}", socket_path.display())
            })
        }
    }
    let uds = tokio::net::UnixListener::bind(socket_path)
        .with_context(|| format!("bind agent gRPC socket {}", socket_path.display()))?;
    // IMPORTANT: 0600 assumes the trusted app shares the sidecar's uid. A
    // different-uid app would lose its intended CSK access and requires an
    // explicitly configured shared group with a 0660 socket (and directory
    // traversal), or SO_PEERCRED-based allowlisting instead.
    if let Err(e) = std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o600)) {
        // This UDS gates intentional CSK export and node-authored send RPCs. Drop
        // and unlink it rather than serving with ambient-umask permissions.
        drop(uds);
        let _ = std::fs::remove_file(socket_path);
        return Err(e)
            .with_context(|| format!("chmod agent gRPC socket {}", socket_path.display()));
    }
    Ok(uds)
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloy::sol_types::SolValue;
    use axum::extract::State;
    use axum::routing::post;
    use axum::{Json, Router};
    use rand::rngs::OsRng;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[tokio::test]
    async fn prepare_agent_socket_sets_private_permissions() {
        let temp = tempfile::tempdir().unwrap();
        let socket_dir = temp.path().join("agent");
        let socket_path = socket_dir.join("agent.sock");

        let listener = prepare_agent_socket(&socket_path).unwrap();

        let socket_mode = std::fs::metadata(&socket_path)
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        let dir_mode = std::fs::metadata(&socket_dir).unwrap().permissions().mode() & 0o777;
        assert_eq!(socket_mode, 0o600);
        assert_eq!(dir_mode, 0o700);
        drop(listener);
    }

    #[derive(Clone)]
    struct RejectLogsRpc {
        list_members_result: String,
        log_calls: Arc<AtomicUsize>,
        view_calls: Arc<AtomicUsize>,
    }

    async fn reject_logs_rpc(
        State(state): State<RejectLogsRpc>,
        Json(request): Json<serde_json::Value>,
    ) -> Json<serde_json::Value> {
        let id = request
            .get("id")
            .cloned()
            .unwrap_or(serde_json::Value::Null);
        match request.get("method").and_then(|method| method.as_str()) {
            Some("eth_getLogs") => {
                state.log_calls.fetch_add(1, Ordering::SeqCst);
                Json(serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": id,
                    "error": {"code": -32000, "message": "archive logs disabled"}
                }))
            }
            Some("eth_call") => {
                state.view_calls.fetch_add(1, Ordering::SeqCst);
                Json(serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": id,
                    "result": state.list_members_result
                }))
            }
            _ => Json(serde_json::json!({
                "jsonrpc": "2.0",
                "id": id,
                "error": {"code": -32601, "message": "unsupported test method"}
            })),
        }
    }

    #[tokio::test]
    async fn current_view_reconcile_never_uses_log_rpc() {
        let dstack: Arc<dyn DstackRuntime> =
            Arc::new(crate::dstack::MockDstack::from_label("reject-logs-sidecar"));
        let keys = Arc::new(crate::keys::derive_all(dstack.as_ref()).await.unwrap());
        let member = Address::repeat_byte(0x11);
        let cluster = Address::repeat_byte(0xc1);
        let shared = Shared::new(keys.clone(), member, cluster, 0x0a0d0000, 16, 51821);
        let list_members = vec![B256::from(shared.self_member_id)].abi_encode();
        let log_calls = Arc::new(AtomicUsize::new(0));
        let view_calls = Arc::new(AtomicUsize::new(0));
        let rpc_state = RejectLogsRpc {
            list_members_result: format!("0x{}", hex::encode(list_members)),
            log_calls: log_calls.clone(),
            view_calls: view_calls.clone(),
        };
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let rpc_server = tokio::spawn(async move {
            axum::serve(
                listener,
                Router::new()
                    .route("/", post(reject_logs_rpc))
                    .with_state(rpc_state),
            )
            .await
            .unwrap();
        });
        let rpc_url = format!("http://{addr}");
        let chain = Arc::new(ChainClient::new(&rpc_url, 8453, member, &keys).unwrap());
        let ctx = Ctx {
            config: Config {
                member_contract: Some(member),
                chain_id: 8453,
                rpc_url: rpc_url.clone(),
                bundler_url: "http://bundler.invalid".into(),
                gas_policy_id: String::new(),
                indexer_registry_addr: Address::repeat_byte(0x22),
                gateway_domain: Some("gateway.invalid".into()),
                peer_envelope_fallback: false,
                wg_tcp_port: 51900,
                wg_listen_port: 51821,
                wg_udp_punch: false,
                punch_timeout_secs: 10,
                punch_retry_backoff_secs: 30,
                dstack_socket: String::new(),
                sidecar_state_dir: None,
                agent_grpc_socket: String::new(),
                health_http_addr: String::new(),
                log_format: "json".into(),
                log_level: "info".into(),
            },
            dstack: dstack.clone(),
            chain,
            shared: shared.clone(),
            wg: Arc::new(crate::wg::MockWg::default()),
            bundler: Arc::new(BundlerClient::new(
                "http://bundler.invalid",
                rpc_url,
                ENTRY_POINT,
                8453,
                "",
            )),
            owner_signer: dstack_facet::derive_owner_signer(dstack.as_ref())
                .await
                .unwrap(),
            submit_lock: Mutex::new(()),
            self_sni: "self.gateway.invalid".into(),
            puncher: None,
            learned_keys: Mutex::new(HashMap::new()),
            last_sent: Mutex::new(HashMap::new()),
        };

        reconcile_once(&ctx, "gateway.invalid").await.unwrap();
        assert_eq!(view_calls.load(Ordering::SeqCst), 1);
        assert_eq!(log_calls.load(Ordering::SeqCst), 0);
        assert_eq!(shared.current_phase().await, Phase::WaitingPeers);
        rpc_server.abort();
    }

    /// The peer ingress hostname derives purely from chain state + config — the
    /// gateway's TLS-passthrough route needs the `s` suffix (a plain route is
    /// HTTP-parsed and drops raw TCP; verified live).
    #[test]
    fn sni_for_builds_the_passthrough_hostname() {
        let member: Address = "0xa87128971070f41c26871ce361d3eded7ecf909b"
            .parse()
            .unwrap();
        assert_eq!(
            sni_for(member, 51900, "dstack-base-prod5.phala.network"),
            "a87128971070f41c26871ce361d3eded7ecf909b-51900s.dstack-base-prod5.phala.network"
        );
    }

    /// MessageFacet dedups (recipient, envelopeId) on chain, so resends must salt
    /// the id; receivers demux on the `kind` INSIDE the ciphertext instead.
    #[test]
    fn envelope_ids_are_salted_per_send() {
        let base = envelopes::peer_endpoint_envelope_id();
        let self_id = [7u8; 32];
        let id_at = |ms: u64| {
            let mut salt = Vec::with_capacity(72);
            salt.extend_from_slice(&base);
            salt.extend_from_slice(&self_id);
            salt.extend_from_slice(&ms.to_le_bytes());
            keccak256(&salt)
        };
        assert_ne!(
            id_at(1),
            id_at(2),
            "different send instants → different ids"
        );
        assert_ne!(id_at(1).0, base, "salted id differs from the bare kind id");
    }

    /// Every resend is a sponsored UserOp; the backoff must double per attempt and
    /// cap at one envelope/day so an on-chain orphan (no removeMember on live
    /// clusters) can't cost 144 ops/day per live node forever.
    #[test]
    fn resend_backoff_doubles_and_caps_at_a_day() {
        let base = ENVELOPE_RESEND.as_millis() as u64;
        assert_eq!(resend_delay_ms(0), base);
        assert_eq!(resend_delay_ms(1), base * 2);
        assert_eq!(resend_delay_ms(3), base * 8);
        let day = ENVELOPE_RESEND_MAX.as_millis() as u64;
        assert_eq!(resend_delay_ms(8), day, "600s * 256 > 24h → capped");
        assert_eq!(
            resend_delay_ms(u32::MAX),
            day,
            "no overflow at extreme attempts"
        );
    }

    fn peer_id(n: u8) -> [u8; 32] {
        let mut id = [0u8; 32];
        id[31] = n;
        id
    }

    #[test]
    fn csk_candidates_prioritize_originator_then_live_then_member_id() {
        let mut peers = PeerTable::new();
        for n in [3u8, 1, 2, 4] {
            peers.ensure_chain(peer_id(n), u32::from(n), [n; 32]);
            peers.mark_configured(&peer_id(n));
        }
        peers.set_live(&peer_id(2), true);
        peers.set_live(&peer_id(4), true);

        let ordered = ordered_csk_candidates(&peers, Some(peer_id(3)));
        assert_eq!(
            ordered.iter().map(|c| c.member_id).collect::<Vec<_>>(),
            vec![peer_id(3), peer_id(2), peer_id(4), peer_id(1)]
        );
    }

    #[derive(Clone)]
    struct FakePlan {
        delay: Duration,
        result: CskProbeResult,
    }

    struct FakeRequester {
        plans: HashMap<u32, FakePlan>,
        total_attempts: AtomicUsize,
        in_flight: AtomicUsize,
        max_in_flight: AtomicUsize,
    }

    impl FakeRequester {
        fn new(plans: HashMap<u32, FakePlan>) -> Self {
            Self {
                plans,
                total_attempts: AtomicUsize::new(0),
                in_flight: AtomicUsize::new(0),
                max_in_flight: AtomicUsize::new(0),
            }
        }
    }

    struct InFlightGuard<'a>(&'a AtomicUsize);

    impl Drop for InFlightGuard<'_> {
        fn drop(&mut self) {
            self.0.fetch_sub(1, Ordering::SeqCst);
        }
    }

    #[async_trait::async_trait]
    impl CskPeerRequester for FakeRequester {
        async fn request(
            &self,
            candidate: &CskCandidate,
            _requester_member_id: [u8; 32],
        ) -> CskProbeResult {
            self.total_attempts.fetch_add(1, Ordering::SeqCst);
            let current = self.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
            self.max_in_flight.fetch_max(current, Ordering::SeqCst);
            let _guard = InFlightGuard(&self.in_flight);
            let Some(plan) = self.plans.get(&candidate.mesh_ip).cloned() else {
                return CskProbeResult::Failed("missing fake plan".into());
            };
            tokio::time::sleep(plan.delay).await;
            plan.result
        }
    }

    fn candidate(n: u8) -> CskCandidate {
        CskCandidate {
            member_id: peer_id(n),
            mesh_ip: u32::from(n),
            live: true,
            originator: n == 1,
        }
    }

    #[tokio::test]
    async fn first_valid_csk_cancels_slow_dead_peers() {
        let xsecret = crypto_box::SecretKey::generate(&mut OsRng);
        let xpub = *xsecret.public_key().as_bytes();
        let key = [42u8; 32];
        let expected = csk::commitment(&key);
        let sealed = csk::seal_for_peer(&key, &xpub).unwrap();
        let fake = Arc::new(FakeRequester::new(HashMap::from([
            (
                1,
                FakePlan {
                    delay: Duration::from_millis(500),
                    result: CskProbeResult::Timeout,
                },
            ),
            (
                2,
                FakePlan {
                    delay: Duration::from_millis(20),
                    result: CskProbeResult::Sealed(sealed),
                },
            ),
        ])));
        let started = Instant::now();
        let (success, _) = pull_csk_from_candidates(
            fake.clone(),
            vec![candidate(1), candidate(2)],
            peer_id(99),
            &xsecret,
            &xpub,
            &expected,
        )
        .await;
        assert_eq!(*success.unwrap().csk, key);
        assert!(started.elapsed() < Duration::from_millis(200));
        tokio::task::yield_now().await;
        assert_eq!(fake.in_flight.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn csk_pull_caps_parallelism_and_accounts_for_unavailable_peers() {
        let xsecret = crypto_box::SecretKey::generate(&mut OsRng);
        let xpub = *xsecret.public_key().as_bytes();
        let mut plans = HashMap::new();
        let mut candidates = Vec::new();
        for n in 1u8..=20 {
            plans.insert(
                u32::from(n),
                FakePlan {
                    delay: Duration::from_millis(10),
                    result: CskProbeResult::Unavailable,
                },
            );
            candidates.push(candidate(n));
        }
        let fake = Arc::new(FakeRequester::new(plans));
        let (success, stats) = pull_csk_from_candidates(
            fake.clone(),
            candidates,
            peer_id(99),
            &xsecret,
            &xpub,
            &csk::commitment(&[1u8; 32]),
        )
        .await;
        assert!(success.is_none());
        assert_eq!(stats.attempts, 20);
        assert_eq!(stats.unavailable, 20);
        assert_eq!(stats.max_in_flight, CSK_PULL_PARALLELISM);
        assert_eq!(
            fake.max_in_flight.load(Ordering::SeqCst),
            CSK_PULL_PARALLELISM
        );
    }

    #[tokio::test]
    async fn all_unavailable_candidates_can_be_retried_in_later_rounds() {
        let xsecret = crypto_box::SecretKey::generate(&mut OsRng);
        let xpub = *xsecret.public_key().as_bytes();
        let fake = Arc::new(FakeRequester::new(HashMap::from([
            (
                1,
                FakePlan {
                    delay: Duration::from_millis(1),
                    result: CskProbeResult::Unavailable,
                },
            ),
            (
                2,
                FakePlan {
                    delay: Duration::from_millis(1),
                    result: CskProbeResult::Unavailable,
                },
            ),
        ])));

        for _ in 0..2 {
            let (success, stats) = pull_csk_from_candidates(
                fake.clone(),
                vec![candidate(1), candidate(2)],
                peer_id(99),
                &xsecret,
                &xpub,
                &csk::commitment(&[1u8; 32]),
            )
            .await;
            assert!(success.is_none());
            assert_eq!(stats.unavailable, 2);
        }
        assert_eq!(fake.total_attempts.load(Ordering::SeqCst), 4);
    }

    #[tokio::test]
    async fn bogus_response_is_rejected_while_another_peer_can_succeed() {
        let xsecret = crypto_box::SecretKey::generate(&mut OsRng);
        let xpub = *xsecret.public_key().as_bytes();
        let real_key = [17u8; 32];
        let expected = csk::commitment(&real_key);
        let fake = Arc::new(FakeRequester::new(HashMap::from([
            (
                1,
                FakePlan {
                    delay: Duration::from_millis(1),
                    result: CskProbeResult::Sealed(csk::seal_for_peer(&[99u8; 32], &xpub).unwrap()),
                },
            ),
            (
                2,
                FakePlan {
                    delay: Duration::from_millis(20),
                    result: CskProbeResult::Sealed(csk::seal_for_peer(&real_key, &xpub).unwrap()),
                },
            ),
        ])));
        let (success, _) = pull_csk_from_candidates(
            fake,
            vec![candidate(1), candidate(2)],
            peer_id(99),
            &xsecret,
            &xpub,
            &expected,
        )
        .await;
        let success = success.unwrap();
        assert_eq!(*success.csk, real_key);
        assert_eq!(success.stats.verification_failed, 1);
    }

    #[tokio::test]
    async fn tonic_requester_bounds_a_hung_peer() {
        let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = tokio::spawn(async move {
            let (_stream, _) = listener.accept().await.unwrap();
            tokio::time::sleep(Duration::from_secs(5)).await;
        });
        let requester = TonicCskPeerRequester {
            port,
            connect_timeout: Duration::from_millis(100),
            rpc_timeout: Duration::from_millis(100),
        };
        let candidate = CskCandidate {
            member_id: peer_id(1),
            mesh_ip: u32::from(Ipv4Addr::LOCALHOST),
            live: true,
            originator: true,
        };
        let started = Instant::now();
        let result = requester.request(&candidate, peer_id(99)).await;
        assert!(
            matches!(result, CskProbeResult::Timeout | CskProbeResult::Failed(_)),
            "unexpected probe result: {result:?}"
        );
        assert!(started.elapsed() < Duration::from_millis(500));
        server.abort();
    }

    #[test]
    fn csk_retry_jitter_stays_within_twenty_percent() {
        for _ in 0..100 {
            let delay = jittered_retry(Duration::from_millis(1000));
            assert!((Duration::from_millis(800)..=Duration::from_millis(1200)).contains(&delay));
            assert!(jittered_retry(CSK_RETRY_MIN) >= CSK_RETRY_MIN);
            assert!(jittered_retry(CSK_RETRY_MAX) <= CSK_RETRY_MAX);
        }
    }
}
