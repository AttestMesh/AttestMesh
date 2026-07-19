//! UDP hole-punch link upgrader (docs/specs/udp-transport-upgrade.md).
//!
//! Upgrades each established wireguard link in place from the gateway-TCP leg to
//! a direct punched UDP path, with gateway TCP as the permanent fallback. The
//! punch fires from the kernel wireguard socket itself — we retarget the wg peer
//! endpoint and let wg's own handshake initiations + persistent keepalives be
//! the probes, so the NAT mapping that opens is the one wireguard keeps using.
//! Coordination rides the already-authenticated in-mesh `PeerControl` channel;
//! no STUN, no rendezvous, no on-chain traffic.
//!
//! Mesh health never depends on punch outcomes: `first_converged` and heartbeat
//! liveness ride inside wireguard and are transport-oblivious, and every failure
//! path here ends in "endpoint reverted to the TCP bridge, retry with backoff".

use crate::proto::peer::peer_control_client::PeerControlClient;
use crate::proto::peer::{Candidate, CandidateKind, PunchAccept, PunchOffer, PunchReport};
use crate::state::Shared;
use crate::wg::cidr;
use crate::wg::peer::{LinkTransport, MemberId, PeerTable, PunchStatus};
use crate::wg::MeshControl;
use anyhow::{Context, Result};
use async_trait::async_trait;
use std::net::{IpAddr, SocketAddr};
use std::sync::atomic::Ordering::Relaxed;
use std::sync::Arc;
use std::time::Duration;
use tonic::{Code, Status};

/// Backoff ceiling (spec: doubling, cap 3600 s). Also the re-probe cadence for
/// peers that answered UNIMPLEMENTED (an older sidecar may upgrade later).
pub const BACKOFF_CAP_SECS: u64 = 3600;
/// How far ahead of "now" the initiator proposes T0 — must comfortably cover
/// the NegotiatePunch round trip over the mesh.
const PUNCH_LEAD_MS: u64 = 1_500;
/// Floor the responder applies to an offered T0 that is already (nearly) past.
const MIN_LEAD_MS: u64 = 500;
/// wg device poll cadence while waiting for the handshake inside the window.
const POLL_INTERVAL: Duration = Duration::from_millis(250);
/// A UDP path whose newest handshake is older than wireguard's rekey-attempt
/// window is dead (keepalives keep handshakes fresher than this when alive).
const WG_REKEY_TIMEOUT_SECS: u64 = 180;
const INITIATOR_TICK: Duration = Duration::from_secs(5);
const WATCHDOG_TICK: Duration = Duration::from_secs(10);

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[derive(Debug, Clone)]
pub struct PunchConfig {
    pub enabled: bool,
    /// Window after T0 in which a fresh handshake on the candidate path counts.
    pub timeout: Duration,
    /// Initial retry backoff; doubles per failed attempt up to [`BACKOFF_CAP_SECS`].
    pub backoff_initial_secs: u64,
    /// Our wg outer listen port — the port-preservation guess for our mapping.
    pub wg_listen_port: u16,
}

/// Deterministic initiator rule (avoids offer glare): the peer with the
/// lexically lower memberId sends `PunchOffer`.
pub fn is_initiator(self_id: &MemberId, peer_id: &MemberId) -> bool {
    self_id < peer_id
}

/// Exponential backoff in ms for the given attempt count (1-based), capped.
pub fn backoff_ms(cfg: &PunchConfig, attempts: u32) -> u64 {
    let exp = attempts.saturating_sub(1).min(20);
    cfg.backoff_initial_secs
        .saturating_mul(1u64 << exp)
        .min(BACKOFF_CAP_SECS)
        .saturating_mul(1000)
}

/// A punch stuck in `Punching` longer than this is presumed crashed and reset.
fn stale_punching_ms(cfg: &PunchConfig) -> u64 {
    PUNCH_LEAD_MS + 3 * cfg.timeout.as_millis() as u64 + 5_000
}

pub fn to_socket_addr(c: &Candidate) -> Option<SocketAddr> {
    let ip: IpAddr = c.ip.parse().ok()?;
    let port = u16::try_from(c.port).ok()?;
    if port == 0 {
        return None;
    }
    Some(SocketAddr::new(ip, port))
}

pub fn candidate(addr: SocketAddr, kind: CandidateKind) -> Candidate {
    Candidate {
        ip: addr.ip().to_string(),
        port: addr.port() as u32,
        kind: kind as i32,
    }
}

/// Order punch targets best-first: a stored peer-reflexive mapping (the precise
/// external address a prior attempt observed), then the peer's offered
/// candidates by kind, then its envelope-advertised address as a last resort.
/// Loopback/unspecified candidates are dropped — a confused peer must not be
/// able to alias our local bridge addresses.
pub fn rank_targets(
    offered: &[Candidate],
    stored_reflexive: Option<SocketAddr>,
    advertised: Option<SocketAddr>,
) -> Vec<SocketAddr> {
    let mut ranked: Vec<(u8, SocketAddr)> = Vec::new();
    if let Some(a) = stored_reflexive {
        ranked.push((0, a));
    }
    for c in offered {
        let Some(a) = to_socket_addr(c) else { continue };
        let rank = match c.kind() {
            CandidateKind::PeerReflexive => 1,
            CandidateKind::Advertised => 2,
            CandidateKind::Guess => 3,
        };
        ranked.push((rank, a));
    }
    if let Some(a) = advertised {
        ranked.push((4, a));
    }
    ranked.sort_by_key(|(r, _)| *r);
    let mut out = Vec::new();
    for (_, a) in ranked {
        if a.ip().is_loopback() || a.ip().is_unspecified() {
            continue;
        }
        if !out.contains(&a) {
            out.push(a);
        }
    }
    out
}

/// Peers due for a punch attempt right now, from the initiator's side of the
/// deterministic rule. Requires an established link (configured + heartbeat-live,
/// i.e. the wg handshake completed over TCP) with its fallback bridge in hand.
pub fn due_for_punch(peers: &PeerTable, self_id: &MemberId, now: u64) -> Vec<MemberId> {
    peers
        .all()
        .filter(|p| {
            p.configured
                && p.live
                && p.bridge_addr.is_some()
                && p.transport == LinkTransport::Tcp
                && is_initiator(self_id, &p.member_id)
                && now >= p.punch.next_retry_ms
        })
        .map(|p| p.member_id)
        .collect()
}

/// Where this node's packets egress from — needed to build our GUESS candidate.
#[async_trait]
pub trait EgressSource: Send + Sync {
    async fn egress_ip(&self) -> Result<IpAddr>;
}

/// Egress IP from a DNS lookup of the provider-issued per-app gateway hostname.
/// Gateway apexes are not required to have an A record. Cached briefly; NAT
/// mappings outlive this.
pub struct DnsEgress {
    domain: String,
    cache: tokio::sync::Mutex<Option<(IpAddr, std::time::Instant)>>,
}

const EGRESS_TTL: Duration = Duration::from_secs(60);

impl DnsEgress {
    pub fn new(domain: String) -> Self {
        Self {
            domain,
            cache: tokio::sync::Mutex::new(None),
        }
    }
}

#[async_trait]
impl EgressSource for DnsEgress {
    async fn egress_ip(&self) -> Result<IpAddr> {
        let mut cache = self.cache.lock().await;
        if let Some((ip, at)) = *cache {
            if at.elapsed() < EGRESS_TTL {
                return Ok(ip);
            }
        }
        let addrs = tokio::net::lookup_host((self.domain.as_str(), 443))
            .await
            .with_context(|| format!("DNS lookup {}", self.domain))?;
        let ip = addrs
            .filter(|a| a.is_ipv4())
            .map(|a| a.ip())
            .next()
            .ok_or_else(|| anyhow::anyhow!("no A record for {}", self.domain))?;
        *cache = Some((ip, std::time::Instant::now()));
        Ok(ip)
    }
}

/// Fixed egress IP — tests, and fleets that know their address out of band.
pub struct StaticEgress(pub IpAddr);

#[async_trait]
impl EgressSource for StaticEgress {
    async fn egress_ip(&self) -> Result<IpAddr> {
        Ok(self.0)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PunchOutcome {
    pub success: bool,
    /// On success: the peer's source address as our wg device saw it (its
    /// precise external mapping — the peer-reflexive candidate for next time).
    pub observed: Option<SocketAddr>,
}

/// The punch itself: at T0 retarget the wg peer endpoint to the best candidate
/// and watch the device for authenticated activity on a non-loopback endpoint
/// within the window. A fresh handshake or an increase in received bytes proves
/// the peer reached us; checking RX matters because WireGuard may reuse the
/// existing session after an endpoint change without immediately rekeying.
/// Activity whose endpoint is loopback means the peer is still reaching us
/// through the TCP ingress — that is NOT a punched path.
/// On timeout the endpoint is restored to the TCP bridge (`revert_to`).
pub async fn execute_punch(
    wg: &dyn MeshControl,
    peer_wg_pub: &[u8; 32],
    targets: &[SocketAddr],
    revert_to: SocketAddr,
    t0_ms: u64,
    timeout: Duration,
) -> Result<PunchOutcome> {
    let Some(target) = targets.first() else {
        anyhow::bail!("no punch targets");
    };

    let now = now_ms();
    if t0_ms > now {
        tokio::time::sleep(Duration::from_millis(t0_ms - now)).await;
    }

    let baseline_rx = wg
        .peer_status(peer_wg_pub)
        .await?
        .map(|st| st.rx_bytes)
        .unwrap_or(0);

    wg.set_peer_endpoint(peer_wg_pub, &target.to_string())
        .await
        .context("retarget wg endpoint to punch candidate")?;

    let deadline = tokio::time::Instant::now() + timeout;
    while tokio::time::Instant::now() < deadline {
        if let Some(st) = wg.peer_status(peer_wg_pub).await? {
            let fresh = st.last_handshake_unix >= t0_ms / 1000 && st.last_handshake_unix > 0;
            let authenticated_rx = st.rx_bytes > baseline_rx;
            if fresh || authenticated_rx {
                if let Some(ep) = st.endpoint {
                    if !ep.ip().is_loopback() {
                        if !targets.iter().any(|t| t.ip() == ep.ip()) {
                            tracing::debug!(observed = %ep,
                                "punched endpoint outside candidate set (NAT re-mapped); accepting roamed path");
                        }
                        return Ok(PunchOutcome {
                            success: true,
                            observed: Some(ep),
                        });
                    }
                }
            }
        }
        tokio::time::sleep(POLL_INTERVAL).await;
    }

    wg.set_peer_endpoint(peer_wg_pub, &revert_to.to_string())
        .await
        .context("revert wg endpoint to TCP bridge after punch timeout")?;
    Ok(PunchOutcome {
        success: false,
        observed: None,
    })
}

/// Owns candidate discovery, the punch scheduler, the executor, and the
/// UDP-path watchdog. One instance per node, shared by the initiator loop and
/// the `PeerControl` responder handlers.
pub struct Puncher {
    shared: Arc<Shared>,
    wg: Arc<dyn MeshControl>,
    cfg: PunchConfig,
    egress: Arc<dyn EgressSource>,
}

impl Puncher {
    pub fn new(
        shared: Arc<Shared>,
        wg: Arc<dyn MeshControl>,
        cfg: PunchConfig,
        egress: Arc<dyn EgressSource>,
    ) -> Arc<Self> {
        Arc::new(Self {
            shared,
            wg,
            cfg,
            egress,
        })
    }

    /// Our candidates for `peer` to fire at: the peer-reported reflexive mapping
    /// first (precise), then the egress-IP + port-preservation guess.
    pub async fn self_candidates(&self, peer: &MemberId) -> Vec<Candidate> {
        let mut out = Vec::new();
        let reflexive = self
            .shared
            .peers
            .lock()
            .await
            .get(peer)
            .and_then(|p| p.punch.self_reflexive);
        if let Some(a) = reflexive {
            out.push(candidate(a, CandidateKind::PeerReflexive));
        }
        if let Ok(ip) = self.egress.egress_ip().await {
            let guess = SocketAddr::new(ip, self.cfg.wg_listen_port);
            if !out.iter().any(|c| to_socket_addr(c) == Some(guess)) {
                out.push(candidate(guess, CandidateKind::Guess));
            }
        }
        out
    }

    /// The optional ADVERTISED candidate carried in our PeerEndpoint envelope
    /// (cuts one negotiation round trip for the peer).
    pub async fn advertised_udp_candidate(&self) -> Option<(String, u16)> {
        if !self.cfg.enabled {
            return None;
        }
        self.egress
            .egress_ip()
            .await
            .ok()
            .map(|ip| (ip.to_string(), self.cfg.wg_listen_port))
    }

    /// Responder side of `NegotiatePunch`. Validates the offer, agrees a T0,
    /// fires its own executor toward the offered candidates, and returns our
    /// candidate set. Errors map to clean degradation on the initiator.
    pub async fn handle_offer(self: Arc<Self>, offer: PunchOffer) -> Result<PunchAccept, Status> {
        if !self.cfg.enabled {
            // Same surface an old sidecar presents: the peer settles on TCP.
            return Err(Status::unimplemented("punch upgrade disabled"));
        }
        let peer_id: MemberId = offer
            .requester_member_id
            .as_slice()
            .try_into()
            .map_err(|_| Status::invalid_argument("requester_member_id must be 32 bytes"))?;
        let nonce: [u8; 16] = offer
            .nonce
            .as_slice()
            .try_into()
            .map_err(|_| Status::invalid_argument("nonce must be 16 bytes"))?;
        if !is_initiator(&peer_id, &self.shared.self_member_id) {
            return Err(Status::invalid_argument(
                "initiator rule: the lexically lower memberId sends PunchOffer",
            ));
        }

        let now = now_ms();
        let t0 = offer.start_at_ms.max(now + MIN_LEAD_MS);
        let targets;
        {
            let mut peers = self.shared.peers.lock().await;
            let p = peers
                .get(&peer_id)
                .ok_or_else(|| Status::permission_denied("unknown member"))?;
            if !p.configured || p.bridge_addr.is_none() {
                return Err(Status::failed_precondition(
                    "link not established over TCP yet",
                ));
            }
            if matches!(p.transport, LinkTransport::Punching { .. }) {
                return Err(Status::aborted("punch already in flight"));
            }
            targets = rank_targets(&offer.candidates, p.punch.peer_reflexive, p.advertised_udp);
            if targets.is_empty() {
                return Err(Status::invalid_argument("no usable candidates"));
            }
            if let Some(ps) = peers.punch_mut(&peer_id) {
                ps.nonce = Some(nonce);
            }
            peers.set_transport(&peer_id, LinkTransport::Punching { since_ms: now });
        }

        let mine = self.self_candidates(&peer_id).await;
        if mine.is_empty() {
            let mut peers = self.shared.peers.lock().await;
            peers.set_transport(&peer_id, LinkTransport::Tcp);
            if let Some(ps) = peers.punch_mut(&peer_id) {
                ps.status = PunchStatus::BackingOff;
            }
            return Err(Status::failed_precondition(
                "no local candidates (egress IP unknown)",
            ));
        }

        tokio::spawn(self.clone().run_punch(peer_id, targets, t0));
        Ok(PunchAccept {
            candidates: mine,
            start_at_ms: t0,
        })
    }

    /// `ReportPunch` handler: absorb the peer-reflexive observation (what the
    /// peer's wg device saw as our source — our precise external mapping toward
    /// it), keyed by negotiation nonce. Unknown nonces are stale and harmless.
    pub async fn handle_report(&self, report: PunchReport) -> Result<(), Status> {
        if report.nonce.len() != 16 {
            return Err(Status::invalid_argument("nonce must be 16 bytes"));
        }
        let observed = report.observed_source.as_ref().and_then(to_socket_addr);
        let mut peers = self.shared.peers.lock().await;
        let Some(member_id) = peers.by_punch_nonce(&report.nonce).map(|p| p.member_id) else {
            tracing::debug!("PunchReport with unknown nonce ignored");
            return Ok(());
        };
        if observed.is_some() {
            if let Some(ps) = peers.punch_mut(&member_id) {
                ps.self_reflexive = observed;
            }
        }
        tracing::debug!(peer = %hex::encode(member_id), success = report.success,
            observed = ?observed, "PunchReport absorbed");
        Ok(())
    }

    /// Initiator loop: scan for due peers (lexically-lower rule) and negotiate.
    pub async fn run_initiator(self: Arc<Self>) {
        if !self.cfg.enabled {
            return;
        }
        loop {
            let due = {
                let peers = self.shared.peers.lock().await;
                due_for_punch(&peers, &self.shared.self_member_id, now_ms())
            };
            for peer in due {
                tokio::spawn(self.clone().initiate(peer));
            }
            tokio::time::sleep(INITIATOR_TICK).await;
        }
    }

    async fn initiate(self: Arc<Self>, peer: MemberId) {
        let now = now_ms();
        let mine = self.self_candidates(&peer).await;
        if mine.is_empty() {
            // Environment problem (DNS), not the peer's fault — plain backoff.
            self.fail_backoff(&peer, "no local candidates (egress IP unknown)")
                .await;
            return;
        }
        let nonce: [u8; 16] = rand::random();
        let t0 = now + PUNCH_LEAD_MS;

        let mesh_ip = {
            let mut peers = self.shared.peers.lock().await;
            // Re-check under the lock: the responder path may have raced us.
            if peers.transport_of(&peer) != Some(LinkTransport::Tcp) {
                return;
            }
            let Some(p) = peers.get(&peer) else { return };
            let mesh_ip = p.mesh_ip;
            if let Some(ps) = peers.punch_mut(&peer) {
                ps.nonce = Some(nonce);
            }
            peers.set_transport(&peer, LinkTransport::Punching { since_ms: now });
            mesh_ip
        };

        let url = format!(
            "http://{}:{}",
            cidr::fmt_ipv4(mesh_ip),
            crate::bringup::PEER_GRPC_PORT
        );
        let offer = PunchOffer {
            requester_member_id: self.shared.self_member_id.to_vec(),
            candidates: mine,
            start_at_ms: t0,
            nonce: nonce.to_vec(),
        };
        let accept = async {
            let mut client = PeerControlClient::connect(url)
                .await
                .map_err(|e| Status::unavailable(e.to_string()))?;
            client.negotiate_punch(offer).await.map(|r| r.into_inner())
        }
        .await;

        match accept {
            Err(s) if s.code() == Code::Unimplemented => {
                self.on_unimplemented(&peer).await;
            }
            Err(s) => {
                self.fail_backoff(&peer, &format!("negotiation failed: {s}"))
                    .await;
            }
            Ok(accept) => {
                let targets = {
                    let peers = self.shared.peers.lock().await;
                    let (reflexive, advertised) = peers
                        .get(&peer)
                        .map(|p| (p.punch.peer_reflexive, p.advertised_udp))
                        .unwrap_or((None, None));
                    rank_targets(&accept.candidates, reflexive, advertised)
                };
                if targets.is_empty() {
                    self.fail_backoff(&peer, "peer returned no usable candidates")
                        .await;
                    return;
                }
                let t0 = accept.start_at_ms.max(t0);
                self.run_punch(peer, targets, t0).await;
            }
        }
    }

    /// Execute the punch at T0, then latch the result into the peer table and
    /// report the observation back to the peer (best effort).
    async fn run_punch(self: Arc<Self>, peer: MemberId, targets: Vec<SocketAddr>, t0_ms: u64) {
        let (wg_pub, bridge, mesh_ip, nonce) = {
            let peers = self.shared.peers.lock().await;
            match peers.get(&peer) {
                Some(p) => (p.wg_pub, p.bridge_addr, p.mesh_ip, p.punch.nonce),
                None => return,
            }
        };
        let Some(bridge) = bridge else {
            // Never fire without the fallback in hand.
            let mut peers = self.shared.peers.lock().await;
            peers.set_transport(&peer, LinkTransport::Tcp);
            if let Some(ps) = peers.punch_mut(&peer) {
                ps.status = PunchStatus::BackingOff;
            }
            return;
        };

        self.shared
            .punch_metrics
            .attempts_total
            .fetch_add(1, Relaxed);
        let outcome = match execute_punch(
            self.wg.as_ref(),
            &wg_pub,
            &targets,
            bridge,
            t0_ms,
            self.cfg.timeout,
        )
        .await
        {
            Ok(o) => o,
            Err(e) => {
                // The endpoint may still be latched on a dead candidate —
                // keep forcing the revert until it lands.
                tracing::warn!(peer = %hex::encode(peer), error = ?e,
                    "punch executor failed; forcing revert to TCP bridge");
                for _ in 0..3 {
                    if self
                        .wg
                        .set_peer_endpoint(&wg_pub, &bridge.to_string())
                        .await
                        .is_ok()
                    {
                        break;
                    }
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
                PunchOutcome {
                    success: false,
                    observed: None,
                }
            }
        };

        let now = now_ms();
        {
            let mut peers = self.shared.peers.lock().await;
            if let Some(ep) = outcome.observed.filter(|_| outcome.success) {
                peers.set_transport(&peer, LinkTransport::Udp { endpoint: ep });
                if let Some(ps) = peers.punch_mut(&peer) {
                    ps.status = PunchStatus::Udp;
                    ps.attempts = 0;
                    ps.unsupported = false;
                    ps.peer_reflexive = Some(ep);
                }
            } else {
                peers.set_transport(&peer, LinkTransport::Tcp);
                if let Some(ps) = peers.punch_mut(&peer) {
                    ps.status = PunchStatus::BackingOff;
                    ps.attempts = ps.attempts.saturating_add(1);
                    ps.next_retry_ms = now + backoff_ms(&self.cfg, ps.attempts);
                }
            }
        }
        if outcome.success {
            self.shared
                .punch_metrics
                .success_total
                .fetch_add(1, Relaxed);
            tracing::info!(peer = %hex::encode(peer), endpoint = ?outcome.observed,
                "link upgraded to punched UDP");
        } else {
            tracing::debug!(peer = %hex::encode(peer),
                "punch attempt failed; reverted to gateway TCP");
        }

        // Best-effort report: carries the peer-reflexive observation that makes
        // the next attempt precise. Loss only costs a round trip later.
        if let Some(nonce) = nonce {
            let report = PunchReport {
                nonce: nonce.to_vec(),
                success: outcome.success,
                observed_source: outcome
                    .observed
                    .map(|a| candidate(a, CandidateKind::PeerReflexive)),
            };
            let url = format!(
                "http://{}:{}",
                cidr::fmt_ipv4(mesh_ip),
                crate::bringup::PEER_GRPC_PORT
            );
            if let Ok(mut client) = PeerControlClient::connect(url).await {
                let _ = client.report_punch(report).await;
            }
        }
    }

    /// Clean degradation (Must-Have): the peer runs an older sidecar with no
    /// punch RPCs. Stay on TCP and re-probe only at the backoff cap, in case
    /// the peer upgrades later. No log spam, no churn.
    async fn on_unimplemented(&self, peer: &MemberId) {
        let mut peers = self.shared.peers.lock().await;
        peers.set_transport(peer, LinkTransport::Tcp);
        if let Some(ps) = peers.punch_mut(peer) {
            ps.status = PunchStatus::Unsupported;
            ps.unsupported = true;
            ps.next_retry_ms = now_ms() + BACKOFF_CAP_SECS * 1000;
        }
        tracing::info!(peer = %hex::encode(peer),
            "peer sidecar has no punch support; staying on gateway TCP");
    }

    async fn fail_backoff(&self, peer: &MemberId, why: &str) {
        let now = now_ms();
        let mut peers = self.shared.peers.lock().await;
        peers.set_transport(peer, LinkTransport::Tcp);
        if let Some(ps) = peers.punch_mut(peer) {
            ps.status = PunchStatus::BackingOff;
            ps.attempts = ps.attempts.saturating_add(1);
            ps.next_retry_ms = now + backoff_ms(&self.cfg, ps.attempts);
            tracing::debug!(peer = %hex::encode(peer), attempts = ps.attempts, why,
                "punch attempt abandoned; backing off");
        }
    }

    /// UDP-path watchdog: revert any UDP link whose handshake went stale (or
    /// whose endpoint roamed back to loopback — the peer fell back to TCP), and
    /// reset any punch stuck in `Punching`. Returns the number of UDP reverts.
    pub async fn watchdog_pass(&self, now: u64) -> usize {
        let snapshot: Vec<(MemberId, [u8; 32], Option<SocketAddr>, LinkTransport)> = {
            self.shared
                .peers
                .lock()
                .await
                .all()
                .map(|p| (p.member_id, p.wg_pub, p.bridge_addr, p.transport))
                .collect()
        };
        let mut reverts = 0;
        for (id, wg_pub, bridge, transport) in snapshot {
            match transport {
                LinkTransport::Udp { .. } => {
                    let dead = match self.wg.peer_status(&wg_pub).await {
                        Ok(Some(st)) => {
                            st.last_handshake_unix == 0
                                || now.saturating_sub(st.last_handshake_unix * 1000)
                                    > WG_REKEY_TIMEOUT_SECS * 1000
                                || st.endpoint.map(|e| e.ip().is_loopback()).unwrap_or(true)
                        }
                        Ok(None) => true,
                        // Transient device-read failure: don't churn the link.
                        Err(_) => false,
                    };
                    if dead {
                        tracing::info!(peer = %hex::encode(id),
                            "UDP path dead; reverting to gateway TCP and scheduling re-punch");
                        self.revert_to_tcp(&id, &wg_pub, bridge, now, PunchStatus::Reverted)
                            .await;
                        self.shared
                            .punch_metrics
                            .udp_reverts_total
                            .fetch_add(1, Relaxed);
                        reverts += 1;
                    }
                }
                LinkTransport::Punching { since_ms }
                    if now.saturating_sub(since_ms) > stale_punching_ms(&self.cfg) =>
                {
                    tracing::warn!(peer = %hex::encode(id),
                        "punch stuck in PUNCHING; resetting to TCP");
                    self.revert_to_tcp(&id, &wg_pub, bridge, now, PunchStatus::BackingOff)
                        .await;
                }
                _ => {}
            }
        }
        reverts
    }

    async fn revert_to_tcp(
        &self,
        id: &MemberId,
        wg_pub: &[u8; 32],
        bridge: Option<SocketAddr>,
        now: u64,
        status: PunchStatus,
    ) {
        if let Some(b) = bridge {
            if let Err(e) = self.wg.set_peer_endpoint(wg_pub, &b.to_string()).await {
                tracing::warn!(peer = %hex::encode(id), error = ?e,
                    "endpoint revert failed; next watchdog pass retries");
            }
        }
        let mut peers = self.shared.peers.lock().await;
        peers.set_transport(id, LinkTransport::Tcp);
        if let Some(ps) = peers.punch_mut(id) {
            ps.status = status;
            ps.attempts = ps.attempts.saturating_add(1);
            ps.next_retry_ms = now + backoff_ms(&self.cfg, ps.attempts);
        }
    }

    pub async fn run_watchdog(self: Arc<Self>) {
        if !self.cfg.enabled {
            return;
        }
        loop {
            self.watchdog_pass(now_ms()).await;
            tokio::time::sleep(WATCHDOG_TICK).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dstack::MockDstack;
    use crate::wg::{MockWg, WgPeerStatus};
    use alloy::primitives::Address;

    fn test_cfg() -> PunchConfig {
        PunchConfig {
            enabled: true,
            timeout: Duration::from_millis(500),
            backoff_initial_secs: 30,
            wg_listen_port: 51821,
        }
    }

    async fn test_shared() -> Arc<Shared> {
        let dstack = MockDstack::from_label("punch-test");
        let keys = Arc::new(crate::keys::derive_all(&dstack).await.unwrap());
        Shared::new(
            keys,
            Address::repeat_byte(0x11),
            Address::repeat_byte(0x22),
            0x0a0d0000,
            16,
            51821,
        )
    }

    /// memberId one below `self_id` lexically (the peer that must initiate
    /// toward us) — exact 256-bit decrement.
    fn id_below(self_id: &MemberId) -> MemberId {
        let mut x = *self_id;
        for b in x.iter_mut().rev() {
            if *b > 0 {
                *b -= 1;
                return x;
            }
            *b = 0xff;
        }
        x
    }

    /// memberId one above `self_id` lexically (the peer we initiate toward).
    fn id_above(self_id: &MemberId) -> MemberId {
        let mut x = *self_id;
        for b in x.iter_mut().rev() {
            if *b < 0xff {
                *b += 1;
                return x;
            }
            *b = 0;
        }
        x
    }

    async fn add_established_peer(shared: &Shared, id: MemberId, wg_pub: [u8; 32]) {
        let mut peers = shared.peers.lock().await;
        peers.ensure_chain(id, 0x0a0d0009, wg_pub);
        peers.set_bridge_addr(&id, "127.0.0.1:40000".parse().unwrap());
        peers.mark_configured(&id);
        peers.set_live(&id, true);
    }

    fn puncher(shared: Arc<Shared>, wg: Arc<MockWg>, cfg: PunchConfig) -> Arc<Puncher> {
        Puncher::new(
            shared,
            wg,
            cfg,
            Arc::new(StaticEgress("198.51.100.4".parse().unwrap())),
        )
    }

    fn cand(ip: &str, port: u32, kind: CandidateKind) -> Candidate {
        Candidate {
            ip: ip.into(),
            port,
            kind: kind as i32,
        }
    }

    async fn wait_for_transport(shared: &Shared, id: &MemberId, want: &str) -> LinkTransport {
        for _ in 0..200 {
            let t = shared.peers.lock().await.transport_of(id).unwrap();
            if t.as_str() == want {
                return t;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        panic!("transport never reached {want}");
    }

    #[test]
    fn initiator_rule_is_lexical_lower() {
        let lo = [1u8; 32];
        let hi = [2u8; 32];
        assert!(is_initiator(&lo, &hi));
        assert!(!is_initiator(&hi, &lo));
        assert!(!is_initiator(&lo, &lo), "never self-initiate");
    }

    #[test]
    fn backoff_doubles_and_caps() {
        let cfg = test_cfg();
        assert_eq!(backoff_ms(&cfg, 1), 30_000);
        assert_eq!(backoff_ms(&cfg, 2), 60_000);
        assert_eq!(backoff_ms(&cfg, 3), 120_000);
        assert_eq!(backoff_ms(&cfg, 8), 3_600_000, "30*2^7=3840s caps at 3600s");
        assert_eq!(
            backoff_ms(&cfg, 64),
            3_600_000,
            "huge attempt counts stay capped"
        );
    }

    #[test]
    fn rank_targets_orders_dedups_and_sanitizes() {
        let reflexive: SocketAddr = "203.0.113.1:5001".parse().unwrap();
        let advertised: SocketAddr = "203.0.113.4:5004".parse().unwrap();
        let offered = vec![
            cand("203.0.113.3", 5003, CandidateKind::Guess),
            cand("203.0.113.2", 5002, CandidateKind::PeerReflexive),
            cand("203.0.113.1", 5001, CandidateKind::Guess), // dup of stored reflexive
            cand("127.0.0.1", 5005, CandidateKind::Guess),   // loopback: dropped
            cand("0.0.0.0", 5006, CandidateKind::Guess),     // unspecified: dropped
            cand("not-an-ip", 5007, CandidateKind::Guess),   // garbage: dropped
            cand("203.0.113.9", 0, CandidateKind::Guess),    // port 0: dropped
            cand("203.0.113.9", 70_000, CandidateKind::Guess), // port overflow: dropped
        ];
        let t = rank_targets(&offered, Some(reflexive), Some(advertised));
        assert_eq!(
            t,
            vec![
                "203.0.113.1:5001".parse::<SocketAddr>().unwrap(), // stored reflexive first
                "203.0.113.2:5002".parse().unwrap(),               // offered reflexive
                "203.0.113.3:5003".parse().unwrap(),               // guess
                "203.0.113.4:5004".parse().unwrap(),               // envelope-advertised last
            ]
        );
        assert!(rank_targets(&[], None, None).is_empty());
    }

    #[tokio::test]
    async fn due_for_punch_applies_every_gate() {
        let shared = test_shared().await;
        let me = shared.self_member_id;
        let now = now_ms();

        let due_peer = id_above(&me);
        add_established_peer(&shared, due_peer, [1u8; 32]).await;

        // peer below us: it is the initiator, not us
        let below = id_below(&me);
        add_established_peer(&shared, below, [2u8; 32]).await;

        // build remaining fixtures inline
        {
            let mut peers = shared.peers.lock().await;

            // configured but heartbeat-dead: link not established yet
            let mut dead = due_peer;
            dead[31] = dead[31].wrapping_add(1);
            peers.ensure_chain(dead, 1, [3u8; 32]);
            peers.set_bridge_addr(&dead, "127.0.0.1:40001".parse().unwrap());
            peers.mark_configured(&dead);

            // already on UDP
            let mut on_udp = due_peer;
            on_udp[31] = on_udp[31].wrapping_add(2);
            peers.ensure_chain(on_udp, 2, [4u8; 32]);
            peers.set_bridge_addr(&on_udp, "127.0.0.1:40002".parse().unwrap());
            peers.mark_configured(&on_udp);
            peers.set_live(&on_udp, true);
            peers.set_transport(
                &on_udp,
                LinkTransport::Udp {
                    endpoint: "203.0.113.1:1".parse().unwrap(),
                },
            );

            // backing off until the future
            let mut backing_off = due_peer;
            backing_off[31] = backing_off[31].wrapping_add(3);
            peers.ensure_chain(backing_off, 3, [5u8; 32]);
            peers.set_bridge_addr(&backing_off, "127.0.0.1:40003".parse().unwrap());
            peers.mark_configured(&backing_off);
            peers.set_live(&backing_off, true);
            peers.punch_mut(&backing_off).unwrap().next_retry_ms = now + 60_000;

            // live but no bridge recorded: nothing to revert to — never punch
            let mut no_bridge = due_peer;
            no_bridge[31] = no_bridge[31].wrapping_add(4);
            peers.ensure_chain(no_bridge, 4, [6u8; 32]);
            peers.mark_configured(&no_bridge);
            peers.set_live(&no_bridge, true);
        }

        let peers = shared.peers.lock().await;
        let due = due_for_punch(&peers, &me, now);
        assert_eq!(
            due,
            vec![due_peer],
            "exactly the established, due, lexically-higher peer"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn execute_punch_latches_fresh_handshake_on_candidate_path() {
        let wg = MockWg::default();
        let key = [9u8; 32];
        let target: SocketAddr = "203.0.113.7:51821".parse().unwrap();
        let bridge: SocketAddr = "127.0.0.1:40000".parse().unwrap();
        let t0 = now_ms();
        // NAT re-mapped the port: observed differs from the candidate's port.
        let observed: SocketAddr = "203.0.113.7:60123".parse().unwrap();
        wg.script_status(
            key,
            WgPeerStatus {
                endpoint: Some(observed),
                last_handshake_unix: t0 / 1000 + 1,
                rx_bytes: 0,
            },
        );

        let out = execute_punch(&wg, &key, &[target], bridge, t0, Duration::from_secs(2))
            .await
            .unwrap();
        assert!(out.success);
        assert_eq!(
            out.observed,
            Some(observed),
            "peer-reflexive mapping captured"
        );
        assert_eq!(wg.last_endpoint_of(&key).unwrap(), target.to_string());
    }

    /// A fresh handshake whose endpoint is loopback means the peer is still
    /// reaching us through the TCP ingress — that must NOT count as a punched
    /// path, and the endpoint must be reverted to the bridge.
    #[tokio::test(start_paused = true)]
    async fn execute_punch_rejects_loopback_handshake_and_reverts() {
        let wg = MockWg::default();
        let key = [9u8; 32];
        let target: SocketAddr = "203.0.113.7:51821".parse().unwrap();
        let bridge: SocketAddr = "127.0.0.1:40000".parse().unwrap();
        let t0 = now_ms();
        wg.script_status(
            key,
            WgPeerStatus {
                endpoint: Some("127.0.0.1:55555".parse().unwrap()),
                last_handshake_unix: t0 / 1000 + 1,
                rx_bytes: 0,
            },
        );

        let out = execute_punch(&wg, &key, &[target], bridge, t0, Duration::from_millis(300))
            .await
            .unwrap();
        assert!(!out.success);
        assert_eq!(
            wg.last_endpoint_of(&key).unwrap(),
            bridge.to_string(),
            "reverted to the TCP bridge"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn execute_punch_times_out_on_stale_handshake_and_reverts() {
        let wg = MockWg::default();
        let key = [9u8; 32];
        let target: SocketAddr = "203.0.113.7:51821".parse().unwrap();
        let bridge: SocketAddr = "127.0.0.1:40000".parse().unwrap();
        let t0 = now_ms();
        // handshake exists but predates T0 (it's the old TCP-path session)
        wg.script_status(
            key,
            WgPeerStatus {
                endpoint: Some(target),
                last_handshake_unix: t0 / 1000 - 60,
                rx_bytes: 0,
            },
        );

        let out = execute_punch(&wg, &key, &[target], bridge, t0, Duration::from_millis(300))
            .await
            .unwrap();
        assert!(!out.success);
        assert_eq!(wg.last_endpoint_of(&key).unwrap(), bridge.to_string());
    }

    #[tokio::test(start_paused = true)]
    async fn execute_punch_latches_authenticated_rx_without_rekey() {
        let wg = Arc::new(MockWg::default());
        let key = [8u8; 32];
        let target: SocketAddr = "203.0.113.8:51821".parse().unwrap();
        let bridge: SocketAddr = "127.0.0.1:40000".parse().unwrap();
        let t0 = now_ms();

        let task = {
            let wg = wg.clone();
            tokio::spawn(async move {
                execute_punch(&*wg, &key, &[target], bridge, t0, Duration::from_secs(2)).await
            })
        };
        tokio::task::yield_now().await;
        wg.script_status(
            key,
            WgPeerStatus {
                endpoint: Some(target),
                // The TCP-path handshake predates the endpoint swap.
                last_handshake_unix: t0 / 1000 - 60,
                // Authenticated receive traffic arrived on the direct endpoint.
                rx_bytes: 128,
            },
        );
        tokio::time::advance(POLL_INTERVAL).await;

        let out = task.await.unwrap().unwrap();
        assert!(out.success);
        assert_eq!(out.observed, Some(target));
    }

    #[tokio::test]
    async fn handle_offer_validates_inputs() {
        let shared = test_shared().await;
        let wg = Arc::new(MockWg::default());
        let p = puncher(shared.clone(), wg, test_cfg());
        let me = shared.self_member_id;
        let initiator = id_below(&me);
        add_established_peer(&shared, initiator, [1u8; 32]).await;

        let base = PunchOffer {
            requester_member_id: initiator.to_vec(),
            candidates: vec![cand("203.0.113.9", 51821, CandidateKind::Guess)],
            start_at_ms: now_ms() + 1000,
            nonce: vec![7u8; 16],
        };

        // malformed member id
        let mut o = base.clone();
        o.requester_member_id = vec![1u8; 7];
        let e = p.clone().handle_offer(o).await.unwrap_err();
        assert_eq!(e.code(), Code::InvalidArgument);

        // malformed nonce
        let mut o = base.clone();
        o.nonce = vec![7u8; 3];
        let e = p.clone().handle_offer(o).await.unwrap_err();
        assert_eq!(e.code(), Code::InvalidArgument);

        // initiator rule violated: a lexically higher peer must not offer
        let mut o = base.clone();
        o.requester_member_id = id_above(&me).to_vec();
        let e = p.clone().handle_offer(o).await.unwrap_err();
        assert_eq!(e.code(), Code::InvalidArgument);

        // unknown member (me-2: lexically below us, not in the table yet)
        let stranger = id_below(&initiator);
        let mut o = base.clone();
        o.requester_member_id = stranger.to_vec();
        let e = p.clone().handle_offer(o).await.unwrap_err();
        assert_eq!(e.code(), Code::PermissionDenied);

        // no usable candidates
        let mut o = base.clone();
        o.candidates = vec![cand("127.0.0.1", 1, CandidateKind::Guess)];
        let e = p.clone().handle_offer(o).await.unwrap_err();
        assert_eq!(e.code(), Code::InvalidArgument);

        // link not established (the same id, now known but with no bridge yet)
        {
            let mut peers = shared.peers.lock().await;
            peers.ensure_chain(stranger, 5, [8u8; 32]);
            peers.mark_configured(&stranger);
            drop(peers);
            let mut o = base.clone();
            o.requester_member_id = stranger.to_vec();
            let e = p.clone().handle_offer(o).await.unwrap_err();
            assert_eq!(e.code(), Code::FailedPrecondition);
        }

        // disabled puncher presents the old-sidecar surface
        let off = puncher(
            shared.clone(),
            Arc::new(MockWg::default()),
            PunchConfig {
                enabled: false,
                ..test_cfg()
            },
        );
        let e = off.handle_offer(base).await.unwrap_err();
        assert_eq!(e.code(), Code::Unimplemented);
    }

    /// Full responder flow: accept the offer, fire the executor at T0, observe
    /// the (mock-scripted) fresh handshake, latch UDP, and record the
    /// peer-reflexive mapping. Then kill the scripted path and watch the
    /// watchdog revert to the bridge.
    #[tokio::test(start_paused = true)]
    async fn responder_punch_latches_udp_then_watchdog_reverts() {
        let shared = test_shared().await;
        let wg = Arc::new(MockWg::default());
        let p = puncher(shared.clone(), wg.clone(), test_cfg());
        let me = shared.self_member_id;
        let initiator = id_below(&me);
        let wg_pub = [1u8; 32];
        add_established_peer(&shared, initiator, wg_pub).await;

        let observed: SocketAddr = "203.0.113.9:60001".parse().unwrap();
        wg.script_status(
            wg_pub,
            WgPeerStatus {
                endpoint: Some(observed),
                last_handshake_unix: now_ms() / 1000 + 2,
                rx_bytes: 0,
            },
        );

        let offer = PunchOffer {
            requester_member_id: initiator.to_vec(),
            candidates: vec![cand("203.0.113.9", 51821, CandidateKind::Guess)],
            start_at_ms: now_ms() + 100,
            nonce: vec![7u8; 16],
        };
        let accept = p.clone().handle_offer(offer).await.unwrap();

        // our candidate set: the StaticEgress GUESS with our listen port
        assert_eq!(accept.candidates.len(), 1);
        assert_eq!(accept.candidates[0].ip, "198.51.100.4");
        assert_eq!(accept.candidates[0].port, 51821);
        assert_eq!(accept.candidates[0].kind(), CandidateKind::Guess);
        assert!(
            accept.start_at_ms >= now_ms(),
            "agreed T0 is not in the past"
        );

        // immediately after accept: PUNCHING
        assert_eq!(
            shared
                .peers
                .lock()
                .await
                .transport_of(&initiator)
                .unwrap()
                .as_str(),
            "punching"
        );

        // executor observes the scripted fresh handshake and latches UDP
        let t = wait_for_transport(&shared, &initiator, "udp").await;
        assert_eq!(t, LinkTransport::Udp { endpoint: observed });
        {
            let peers = shared.peers.lock().await;
            let info = peers.get(&initiator).unwrap();
            assert_eq!(info.punch.peer_reflexive, Some(observed));
            assert_eq!(info.punch.attempts, 0);
            assert_eq!(info.punch.status, PunchStatus::Udp);
        }
        let (attempts, success, reverts) = shared.punch_metrics.snapshot();
        assert_eq!((attempts, success, reverts), (1, 1, 0));

        // UDP path dies: handshake goes stale -> watchdog reverts to the bridge
        wg.script_status(
            wg_pub,
            WgPeerStatus {
                endpoint: Some(observed),
                last_handshake_unix: now_ms() / 1000 - 300,
                rx_bytes: 0,
            },
        );
        let reverted = p.watchdog_pass(now_ms()).await;
        assert_eq!(reverted, 1);
        {
            let peers = shared.peers.lock().await;
            let info = peers.get(&initiator).unwrap();
            assert_eq!(info.transport, LinkTransport::Tcp);
            assert_eq!(info.punch.attempts, 1);
            assert_eq!(info.punch.status, PunchStatus::Reverted);
            assert!(
                info.punch.next_retry_ms > now_ms(),
                "re-punch scheduled with backoff"
            );
        }
        assert_eq!(wg.last_endpoint_of(&wg_pub).unwrap(), "127.0.0.1:40000");
        let (_, _, reverts) = shared.punch_metrics.snapshot();
        assert_eq!(reverts, 1);
    }

    /// Failed punch: no handshake within the window -> revert, backoff, count.
    #[tokio::test(start_paused = true)]
    async fn responder_punch_failure_reverts_and_backs_off() {
        let shared = test_shared().await;
        let wg = Arc::new(MockWg::default()); // no scripted status: device never handshakes
        let p = puncher(shared.clone(), wg.clone(), test_cfg());
        let initiator = id_below(&shared.self_member_id);
        let wg_pub = [1u8; 32];
        add_established_peer(&shared, initiator, wg_pub).await;

        let offer = PunchOffer {
            requester_member_id: initiator.to_vec(),
            candidates: vec![cand("203.0.113.9", 51821, CandidateKind::Guess)],
            start_at_ms: now_ms() + 100,
            nonce: vec![7u8; 16],
        };
        p.clone().handle_offer(offer).await.unwrap();

        wait_for_transport(&shared, &initiator, "tcp").await;
        {
            let peers = shared.peers.lock().await;
            let info = peers.get(&initiator).unwrap();
            assert_eq!(info.punch.attempts, 1);
            assert!(info.punch.next_retry_ms >= now_ms());
        }
        assert_eq!(
            wg.last_endpoint_of(&wg_pub).unwrap(),
            "127.0.0.1:40000",
            "endpoint restored to the TCP bridge"
        );
        let (attempts, success, _) = shared.punch_metrics.snapshot();
        assert_eq!((attempts, success), (1, 0));

        // a second offer while settled on TCP is accepted (retry path)
        let offer = PunchOffer {
            requester_member_id: initiator.to_vec(),
            candidates: vec![cand("203.0.113.9", 51821, CandidateKind::Guess)],
            start_at_ms: now_ms() + 100,
            nonce: vec![8u8; 16],
        };
        p.clone().handle_offer(offer).await.unwrap();
        // ...but a third while PUNCHING is refused (glare/duplicate guard)
        let offer = PunchOffer {
            requester_member_id: initiator.to_vec(),
            candidates: vec![cand("203.0.113.9", 51821, CandidateKind::Guess)],
            start_at_ms: now_ms() + 100,
            nonce: vec![9u8; 16],
        };
        let e = p.clone().handle_offer(offer).await.unwrap_err();
        assert_eq!(e.code(), Code::Aborted);
    }

    #[tokio::test]
    async fn handle_report_stores_self_reflexive_by_nonce() {
        let shared = test_shared().await;
        let p = puncher(shared.clone(), Arc::new(MockWg::default()), test_cfg());
        let peer = id_below(&shared.self_member_id);
        add_established_peer(&shared, peer, [1u8; 32]).await;
        shared.peers.lock().await.punch_mut(&peer).unwrap().nonce = Some([7u8; 16]);

        // bad nonce length
        let e = p
            .handle_report(PunchReport {
                nonce: vec![7u8; 3],
                success: true,
                observed_source: None,
            })
            .await
            .unwrap_err();
        assert_eq!(e.code(), Code::InvalidArgument);

        // unknown nonce: stale, ignored without error
        p.handle_report(PunchReport {
            nonce: vec![9u8; 16],
            success: false,
            observed_source: None,
        })
        .await
        .unwrap();

        // known nonce: the observation becomes our reflexive candidate
        p.handle_report(PunchReport {
            nonce: vec![7u8; 16],
            success: true,
            observed_source: Some(cand("198.51.100.4", 60777, CandidateKind::PeerReflexive)),
        })
        .await
        .unwrap();
        let peers = shared.peers.lock().await;
        assert_eq!(
            peers.get(&peer).unwrap().punch.self_reflexive,
            Some("198.51.100.4:60777".parse().unwrap())
        );
    }

    #[tokio::test]
    async fn unimplemented_marks_peer_unsupported_at_cap_cadence() {
        let shared = test_shared().await;
        let p = puncher(shared.clone(), Arc::new(MockWg::default()), test_cfg());
        let peer = id_above(&shared.self_member_id);
        add_established_peer(&shared, peer, [1u8; 32]).await;

        p.on_unimplemented(&peer).await;
        let peers = shared.peers.lock().await;
        let info = peers.get(&peer).unwrap();
        assert_eq!(info.transport, LinkTransport::Tcp);
        assert!(info.punch.unsupported);
        assert_eq!(info.punch.status, PunchStatus::Unsupported);
        // re-probe no sooner than the cap (allow scheduling slack)
        assert!(info.punch.next_retry_ms >= now_ms() + (BACKOFF_CAP_SECS - 5) * 1000);
        // and the scheduler now skips it
        assert!(due_for_punch(&peers, &shared.self_member_id, now_ms()).is_empty());
    }

    /// Watchdog keeps healthy UDP links alone, reverts a link whose endpoint
    /// roamed back to loopback (peer fell back to TCP through our ingress),
    /// and resets a punch stuck in PUNCHING.
    #[tokio::test]
    async fn watchdog_distinguishes_alive_loopback_and_stale() {
        let shared = test_shared().await;
        let wg = Arc::new(MockWg::default());
        let p = puncher(shared.clone(), wg.clone(), test_cfg());
        let me = shared.self_member_id;
        let now = now_ms();

        let healthy = id_above(&me);
        let healthy_pub = [1u8; 32];
        add_established_peer(&shared, healthy, healthy_pub).await;
        let healthy_ep: SocketAddr = "203.0.113.1:1000".parse().unwrap();
        wg.script_status(
            healthy_pub,
            WgPeerStatus {
                endpoint: Some(healthy_ep),
                last_handshake_unix: now / 1000 - 10,
                rx_bytes: 0,
            },
        );

        let mut roamed = healthy;
        roamed[31] = roamed[31].wrapping_add(1);
        let roamed_pub = [2u8; 32];
        add_established_peer(&shared, roamed, roamed_pub).await;
        wg.script_status(
            roamed_pub,
            WgPeerStatus {
                endpoint: Some("127.0.0.1:50000".parse().unwrap()),
                last_handshake_unix: now / 1000 - 10, // fresh, but via the TCP ingress
                rx_bytes: 0,
            },
        );

        let mut stuck = healthy;
        stuck[31] = stuck[31].wrapping_add(2);
        let stuck_pub = [3u8; 32];
        add_established_peer(&shared, stuck, stuck_pub).await;

        {
            let mut peers = shared.peers.lock().await;
            peers.set_transport(
                &healthy,
                LinkTransport::Udp {
                    endpoint: healthy_ep,
                },
            );
            peers.set_transport(
                &roamed,
                LinkTransport::Udp {
                    endpoint: "203.0.113.2:2000".parse().unwrap(),
                },
            );
            peers.set_transport(&stuck, LinkTransport::Punching { since_ms: 0 });
        }

        let reverts = p.watchdog_pass(now).await;
        assert_eq!(
            reverts, 1,
            "only the loopback-roamed link counts as a UDP revert"
        );

        let peers = shared.peers.lock().await;
        assert_eq!(
            peers.transport_of(&healthy).unwrap(),
            LinkTransport::Udp {
                endpoint: healthy_ep
            },
            "healthy UDP link untouched"
        );
        assert_eq!(peers.transport_of(&roamed).unwrap(), LinkTransport::Tcp);
        assert_eq!(
            peers.get(&roamed).unwrap().punch.status,
            PunchStatus::Reverted
        );
        assert_eq!(
            peers.transport_of(&stuck).unwrap(),
            LinkTransport::Tcp,
            "stale PUNCHING reset to TCP"
        );
        assert_eq!(
            peers.get(&stuck).unwrap().punch.status,
            PunchStatus::BackingOff
        );
        assert!(wg.last_endpoint_of(&healthy_pub).is_none());
        assert_eq!(wg.last_endpoint_of(&roamed_pub).unwrap(), "127.0.0.1:40000");
        let (_, _, reverts_total) = shared.punch_metrics.snapshot();
        assert_eq!(reverts_total, 1);
    }

    /// The negotiation messages survive a prost round trip — the wire contract
    /// an old/new sidecar pair depends on.
    #[test]
    fn punch_proto_round_trips() {
        use prost::Message;

        let offer = PunchOffer {
            requester_member_id: vec![7u8; 32],
            candidates: vec![
                cand("203.0.113.7", 51821, CandidateKind::Guess),
                cand("203.0.113.7", 60123, CandidateKind::PeerReflexive),
                cand("198.51.100.4", 51821, CandidateKind::Advertised),
            ],
            start_at_ms: 1765432100123,
            nonce: vec![9u8; 16],
        };
        let back = PunchOffer::decode(offer.encode_to_vec().as_slice()).unwrap();
        assert_eq!(back, offer);
        assert_eq!(back.candidates[1].kind(), CandidateKind::PeerReflexive);

        let accept = PunchAccept {
            candidates: vec![cand("198.51.100.4", 51821, CandidateKind::Guess)],
            start_at_ms: 1765432101000,
        };
        assert_eq!(
            PunchAccept::decode(accept.encode_to_vec().as_slice()).unwrap(),
            accept
        );

        let report = PunchReport {
            nonce: vec![9u8; 16],
            success: true,
            observed_source: Some(cand("203.0.113.7", 60123, CandidateKind::PeerReflexive)),
        };
        let back = PunchReport::decode(report.encode_to_vec().as_slice()).unwrap();
        assert_eq!(back, report);
        assert_eq!(
            to_socket_addr(back.observed_source.as_ref().unwrap()),
            Some("203.0.113.7:60123".parse().unwrap())
        );
    }
}
