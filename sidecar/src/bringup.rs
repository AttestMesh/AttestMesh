//! Mesh bring-up orchestration (master spec §7.1 steps 5–12, sidecar spec §7).
//!
//! Runs after registration succeeds. The chain is the source of truth for
//! membership and peer identity (no off-chain coordination): peers are enumerated
//! directly via RPC reads (`listMembers` / `memberById` / `meshIpOf`), wireguard
//! links bootstrap over the gateway TCP leg (see `transport`), and the encrypted
//! `PeerEndpoint` envelopes — which carry each peer's Ed25519 heartbeat key — are
//! exchanged through `MessageFacet` and recovered by polling `MessageSent` logs
//! over the same RPC (Indexer push is an optimization, not a dependency).
//!
//! Spawned tasks: wg-tcp ingress, peer reconciler (chain → bridges → wg peers →
//! envelope exchange), heartbeat send/recv, CSK originate-or-pull, the peer-control
//! gRPC server (mesh-only), and the app-facing agent gRPC server (UDS).

use crate::chain::{bundler::BundlerClient, dstack_facet, message_facet, network_facet, userop, ChainClient};
use crate::config::Config;
use crate::dstack::DstackRuntime;
use crate::envelopes::{self, PeerEndpoint};
use crate::proto::peer::peer_control_client::PeerControlClient;
use crate::proto::peer::CskRequest;
use crate::state::{AppPeerEvent, Phase, Shared};
use crate::wg::{cidr, peer::WgPeerConfig, MeshControl};
use crate::{csk, transport};
use alloy::primitives::{keccak256, Address, Bytes, B256, U256};
use alloy::providers::Provider;
use alloy::rpc::types::Filter;
use alloy::signers::local::PrivateKeySigner;
use anyhow::{Context, Result};
use std::collections::HashMap;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Mutex;

/// Canonical EIP-4337 v0.7 EntryPoint (identical on every chain).
const ENTRY_POINT: Address =
    alloy::primitives::address!("0000000071727De22E5E9d8BAf0edAc6f37da032");

pub const PEER_GRPC_PORT: u16 = 50051;
const RECONCILE_INTERVAL: Duration = Duration::from_secs(15);
const CSK_RETRY: Duration = Duration::from_secs(10);
/// Base wait before re-sending our PeerEndpoint to a peer whose Ed25519 key is
/// still unknown (the peer is symmetric-polling, so a fresh send lands in its log
/// window). Doubles per attempt up to [`ENVELOPE_RESEND_MAX`]: every resend is a
/// sponsored UserOp, and a peer that never answers (an on-chain orphan whose VM is
/// gone — this cluster has no removeMember) would otherwise cost 144 ops/day from
/// every live node, forever (live-found 2026-07: ~1.3k ops/day fleet-wide).
const ENVELOPE_RESEND: Duration = Duration::from_secs(600);
/// Resend backoff ceiling: one envelope per day to a peer that never answers.
const ENVELOPE_RESEND_MAX: Duration = Duration::from_secs(86_400);
/// How far back the first MessageSent log poll reaches (Base ≈ 2s blocks ≈ 2.2h).
const LOG_LOOKBACK_BLOCKS: u64 = 4000;

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
    /// dstack sealed store so a restart doesn't forget them (and restart the
    /// sponsored-UserOp resend loop toward peers that will never reply).
    learned_keys: Mutex<crate::peer_cache::LearnedKeys>,
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
    let learned_keys = crate::peer_cache::load(dstack.as_ref()).await;
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

    // Indexer pushes wake the reconcile pass early; polling remains the fallback.
    let (wake_tx, wake_rx) = tokio::sync::mpsc::channel::<()>(8);

    // 3. peer reconciler: chain → bridges → wg peers → envelope exchange.
    {
        let ctx = ctx.clone();
        let gw = gw_domain.clone();
        tokio::spawn(async move { reconcile_loop(ctx, gw, wake_rx).await });
    }

    // 3b. Indexer subscription (sidecar spec §9): discover via IndexerRegistry,
    // verify every push, reconnect with backoff. Absent registration → poll-only.
    {
        let shared = shared.clone();
        let chain = chain.clone();
        let registry = config.indexer_registry_addr;
        tokio::spawn(async move { indexer_loop(shared, chain, registry, wake_tx).await });
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

/// One pass + steady-state loop: enumerate members from chain, configure any new
/// peer (bridge + wg), send our PeerEndpoint envelope, and poll MessageSent logs
/// for inbound envelopes (peers' Ed25519 keys).
async fn reconcile_loop(
    ctx: Arc<Ctx>,
    gw_domain: String,
    mut wake: tokio::sync::mpsc::Receiver<()>,
) {
    let mut last_sent: HashMap<[u8; 32], SendState> = HashMap::new();
    let mut next_from_block: Option<u64> = None;

    loop {
        if let Err(e) = reconcile_once(&ctx, &gw_domain, &mut last_sent, &mut next_from_block).await
        {
            tracing::warn!(error = ?e, "peer reconcile pass failed; retrying");
        }
        // Indexer pushes cut the latency; the interval is the poll fallback.
        tokio::select! {
            _ = tokio::time::sleep(RECONCILE_INTERVAL) => {}
            _ = wake.recv() => {}
        }
    }
}

/// Discover the Indexer from the on-chain registry and hold the subscription open,
/// re-discovering + reconnecting with backoff (spec §9.2). An empty registry means
/// no indexer is operated yet — the reconcile poll remains the only event source.
async fn indexer_loop(
    shared: Arc<Shared>,
    chain: Arc<ChainClient>,
    registry: Address,
    wake: tokio::sync::mpsc::Sender<()>,
) {
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
            tracing::info!("no indexer registered; staying in poll-only mode");
            tokio::time::sleep(Duration::from_secs(300)).await;
            continue;
        }
        tracing::info!(endpoint = %info.endpoint, "subscribing to indexer");
        match crate::indexer_client::connect_and_run(
            shared.clone(),
            info.endpoint.clone(),
            info.pubkey.0,
            wake.clone(),
        )
        .await
        {
            Ok(()) => tracing::info!("indexer stream ended; re-discovering"),
            Err(e) => tracing::warn!(error = ?e, "indexer subscription failed; re-discovering"),
        }
        tokio::time::sleep(Duration::from_secs(15)).await;
    }
}

async fn reconcile_once(
    ctx: &Ctx,
    gw_domain: &str,
    last_sent: &mut HashMap<[u8; 32], SendState>,
    next_from_block: &mut Option<u64>,
) -> Result<()> {
    let cluster = ctx.shared.cluster;
    let members = ctx.chain.list_members(cluster).await?;
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
            {
                let mut peers = ctx.shared.peers.lock().await;
                peers.ensure_chain(*member_id, mesh_ip, rec.wg_pubkey.0);
                peers.set_endpoint(member_id, sni.clone());
                // The bridge address is the punch-upgrade revert target; the
                // bridge task itself stays alive even while the link rides UDP.
                peers.set_bridge_addr(member_id, endpoint);
                peers.mark_configured(member_id);
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
        if ctx.shared.peers.lock().await.ed25519_of(member_id).is_none() {
            let on_chain = ctx
                .chain
                .ed25519_key_of(cluster, B256::from(*member_id))
                .await
                .unwrap_or(B256::ZERO);
            if on_chain != B256::ZERO {
                ctx.shared.peers.lock().await.set_ed25519(member_id, on_chain.0);
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
        let due = match last_sent.get(member_id) {
            None => true,
            Some(s) => !peer_ed_known && now.saturating_sub(s.at_ms) > resend_delay_ms(s.attempts),
        };
        if due {
            match send_peer_endpoint(ctx, *member_id).await {
                Ok(tx) => {
                    let attempts = last_sent.get(member_id).map_or(0, |s| s.attempts);
                    tracing::info!(peer = %hex::encode(member_id), tx = %tx, attempts,
                        "PeerEndpoint envelope sent");
                    last_sent.insert(
                        *member_id,
                        SendState {
                            at_ms: now,
                            attempts: attempts.saturating_add(1),
                        },
                    );
                }
                Err(e) => {
                    // Failures back off exactly like resends: a systematic bundler/
                    // paymaster rejection must not retry at reconcile cadence — each
                    // attempt consumes a sponsorship (live-found: 19 peers x 15s
                    // burned a 100-op policy counter in ~90s with zero landed ops).
                    let attempts = last_sent.get(member_id).map_or(0, |s| s.attempts);
                    last_sent.insert(
                        *member_id,
                        SendState { at_ms: now, attempts: attempts.saturating_add(1) },
                    );
                    tracing::warn!(peer = %hex::encode(member_id), error = ?e, attempts,
                        "PeerEndpoint envelope send failed; backing off");
                }
            }
        }
    }

    poll_envelopes(ctx, next_from_block, last_sent).await?;

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

/// Poll `MessageSent` logs addressed to us and absorb PeerEndpoint envelopes
/// (chain-authenticated sender: the facet emits the sender's memberId).
async fn poll_envelopes(
    ctx: &Ctx,
    next_from_block: &mut Option<u64>,
    last_sent: &mut HashMap<[u8; 32], SendState>,
) -> Result<()> {
    use crate::chain::abi::IMessageEvents::MessageSent;
    use alloy::sol_types::SolEvent;

    let head = ctx.chain.provider().get_block_number().await?;
    let from = match *next_from_block {
        Some(b) if b <= head => b,
        Some(_) => return Ok(()),
        None => head.saturating_sub(LOG_LOOKBACK_BLOCKS),
    };

    let filter = Filter::new()
        .address(ctx.shared.cluster)
        .event_signature(MessageSent::SIGNATURE_HASH)
        .topic2(B256::from(ctx.shared.self_member_id))
        .from_block(from)
        .to_block(head);
    let logs = ctx.chain.provider().get_logs(&filter).await?;
    *next_from_block = Some(head + 1);

    for log in logs {
        let block_number = log.block_number.unwrap_or(0);
        let Ok(ev) = log.log_decode::<MessageSent>() else {
            continue;
        };
        let m = ev.inner.data;
        let sender = m.senderMemberId.0;
        let Ok(pt) = envelopes::open(
            &ctx.shared.keys.x_secret,
            &ctx.shared.keys.x_pub,
            &m.ciphertext,
        ) else {
            continue; // not for us / not openable — fine, other envelope kinds exist
        };

        // Demux on the inner `kind`: a well-formed PeerEndpoint carrying the reserved
        // kind is sidecar-internal; every other decrypted payload is an opaque
        // application message forwarded to the app via SubscribeMessages (master spec
        // §7.1 step 7, sidecar spec §12.3). The sidecar never parses app protocols.
        match envelopes::classify_internal(&pt) {
            Some(pe) => {
                if pe.member_id != sender {
                    continue; // sender-binding mismatch on an internal envelope — drop
                }
                {
                    let mut peers = ctx.shared.peers.lock().await;
                    if peers.set_ed25519(&sender, pe.ed25519_pub) {
                        tracing::info!(peer = %hex::encode(sender), host = %pe.host,
                            "PeerEndpoint envelope absorbed (Ed25519 key learned)");
                    }
                    if let Some(udp) = pe.udp_addr() {
                        peers.set_advertised_udp(&sender, Some(udp));
                    }
                }
                // Mirror to the sealed store regardless of the table update: the
                // table entry may not exist yet (envelope raced our configure pass)
                // — the reconcile loop applies cached keys once it does.
                {
                    let mut learned = ctx.learned_keys.lock().await;
                    if learned.get(&sender) != Some(&pe.ed25519_pub) {
                        learned.insert(sender, pe.ed25519_pub);
                        if let Err(e) =
                            crate::peer_cache::store(ctx.dstack.as_ref(), &learned).await
                        {
                            tracing::warn!(error = ?e, "peer-key seal failed (non-fatal)");
                        }
                    }
                }
                // The sender is announcing because it does not know OUR key (fresh
                // join, or a restart wiped its memory — it can't ask, it can only
                // announce). Reply with our own envelope so it converges instead of
                // resending forever. Replying only when our last send to it is at
                // least one resend period old makes two live nodes settle after one
                // round trip instead of ping-ponging.
                let now = now_ms();
                let reply_due = last_sent.get(&sender).map_or(true, |s| {
                    now.saturating_sub(s.at_ms) > ENVELOPE_RESEND.as_millis() as u64
                });
                if reply_due {
                    match send_peer_endpoint(ctx, sender).await {
                        Ok(tx) => {
                            let attempts = last_sent.get(&sender).map_or(0, |s| s.attempts);
                            tracing::info!(peer = %hex::encode(sender), tx = %tx,
                                "PeerEndpoint reply sent (peer announced itself)");
                            last_sent.insert(
                                sender,
                                SendState {
                                    at_ms: now,
                                    attempts: attempts.saturating_add(1),
                                },
                            );
                        }
                        Err(e) => {
                            let attempts = last_sent.get(&sender).map_or(0, |s| s.attempts);
                            last_sent.insert(
                                sender,
                                SendState { at_ms: now, attempts: attempts.saturating_add(1) },
                            );
                            tracing::warn!(peer = %hex::encode(sender), error = ?e,
                                "PeerEndpoint reply failed; backing off");
                        }
                    }
                }
            }
            None => {
                // Application message: forward verbatim to SubscribeMessages
                // subscribers. send() errs only when there are no subscribers yet —
                // not an error (same broadcast semantics as peer_event_tx).
                let bytes = pt.len();
                if ctx
                    .shared
                    .incoming_tx
                    .send(crate::state::AppIncoming {
                        sender_member_id: sender,
                        payload: pt,
                        block_number,
                    })
                    .is_ok()
                {
                    tracing::debug!(sender = %hex::encode(sender), block = block_number,
                        bytes, "app message forwarded to SubscribeMessages");
                }
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

/// CSK lifecycle (master spec §8): restart-unseal, originate (memberIds[0]) or
/// pull from a live peer over the mesh, then hold for peer-pull serving.
async fn csk_loop(ctx: Arc<Ctx>) {
    // Restart fast path: the CSK survives in the dstack sealed store.
    match csk::unseal_from_store(ctx.dstack.as_ref()).await {
        Ok(Some(c)) => {
            *ctx.shared.csk.lock().await = Some(*c);
            ctx.shared.gates.set_csk_acquired();
            tracing::info!("CSK unsealed from store (restart path)");
            return;
        }
        Ok(None) => {}
        // Diagnostic, not fatal: the originator re-derives, onboardees re-pull.
        Err(e) => tracing::warn!(error = ?e, "sealed-store unseal failed (guest agent /Unseal)"),
    }

    loop {
        if ctx.shared.gates.csk_acquired() {
            return;
        }
        if let Err(e) = csk_once(&ctx).await {
            tracing::debug!(error = ?e, "csk pass incomplete; retrying");
        }
        tokio::time::sleep(CSK_RETRY).await;
    }
}

async fn csk_once(ctx: &Ctx) -> Result<()> {
    let cluster = ctx.shared.cluster;
    let commitment = ctx.chain.csk_commitment(cluster).await?;

    if commitment == B256::ZERO {
        // Only memberIds[0] may set the commitment (AttestFacet.setCskCommitment).
        let members = ctx.chain.list_members(cluster).await?;
        if members.first().map(|m| m.0) != Some(ctx.shared.self_member_id) {
            return Ok(()); // originator hasn't committed yet; keep waiting
        }
        let c = csk::derive_originator(ctx.dstack.as_ref()).await?;
        let inner =
            message_facet::build_set_csk_commitment_calldata(B256::from(csk::commitment(&c)));
        let tx = ctx.submit_op(inner).await.context("setCskCommitment")?;
        // Best-effort: the commitment is on-chain already, and the originator can
        // always re-derive (deterministic KMS derivation), so a store failure
        // must not fail the pass here.
        if let Err(e) = csk::seal_to_store(ctx.dstack.as_ref(), &c).await {
            tracing::warn!(error = ?e, "CSK seal_to_store failed (non-fatal)");
        }
        *ctx.shared.csk.lock().await = Some(*c);
        ctx.shared.gates.set_csk_acquired();
        tracing::info!(tx = %tx, "CSK originated + commitment set on-chain");
        return Ok(());
    }

    // Originator restart path: the CSK is deterministically KMS-derived, so when
    // the sealed store is lost the originator re-derives and verifies against the
    // on-chain commitment. Without this, a restarted originator would join every
    // other empty-handed node in the pull path and the cluster would deadlock
    // (peer_grpc only serves a held CSK).
    if let Ok(c) = csk::derive_originator(ctx.dstack.as_ref()).await {
        if csk::commitment(&c) == commitment.0 {
            if let Err(e) = csk::seal_to_store(ctx.dstack.as_ref(), &c).await {
                tracing::warn!(error = ?e, "CSK seal_to_store failed (non-fatal)");
            }
            *ctx.shared.csk.lock().await = Some(*c);
            ctx.shared.gates.set_csk_acquired();
            tracing::info!(
                "CSK re-derived + verified against on-chain commitment (originator restart)"
            );
            return Ok(());
        }
    }

    // Onboardee: pull from any configured peer over the mesh.
    ctx.shared.set_phase(Phase::PullingCsk).await;
    let targets: Vec<u32> = {
        let peers = ctx.shared.peers.lock().await;
        peers
            .all()
            .filter(|p| p.configured)
            .map(|p| p.mesh_ip)
            .collect()
    };
    for ip in targets {
        let url = format!("http://{}:{}", cidr::fmt_ipv4(ip), PEER_GRPC_PORT);
        let Ok(mut client) = PeerControlClient::connect(url.clone()).await else {
            continue;
        };
        let req = CskRequest {
            requester_member_id: ctx.shared.self_member_id.to_vec(),
        };
        let Ok(resp) = client.request_cluster_shared_key(req).await else {
            continue;
        };
        match csk::open_pulled(
            &resp.into_inner().sealed_csk,
            &ctx.shared.keys.x_secret,
            &ctx.shared.keys.x_pub,
            &commitment.0,
        ) {
            Ok(c) => {
                csk::seal_to_store(ctx.dstack.as_ref(), &c).await?;
                *ctx.shared.csk.lock().await = Some(*c);
                ctx.shared.gates.set_csk_acquired();
                tracing::info!(from = %url, "CSK pulled + verified against on-chain commitment");
                return Ok(());
            }
            Err(e) => tracing::warn!(from = %url, error = ?e, "pulled CSK failed verification"),
        }
    }
    anyhow::bail!("no peer served the CSK yet")
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
    if let Some(dir) = std::path::Path::new(&socket_path).parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::remove_file(&socket_path);
    let uds = match tokio::net::UnixListener::bind(&socket_path) {
        Ok(l) => l,
        Err(e) => {
            tracing::error!(error = %e, path = %socket_path, "agent gRPC UDS bind failed");
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

#[cfg(test)]
mod tests {
    use super::*;

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
}
