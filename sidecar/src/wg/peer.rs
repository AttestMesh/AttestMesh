//! Peer table + wireguard peer config (sidecar spec §10.2) and the per-peer link
//! transport state machine (docs/specs/udp-transport-upgrade.md).

use std::collections::HashMap;
use std::net::SocketAddr;

pub type MemberId = [u8; 32];

/// A single wireguard peer's configuration.
#[derive(Debug, Clone)]
pub struct WgPeerConfig {
    pub public_key: [u8; 32],
    pub endpoint: String, // "host:port"
    pub allowed_ip: u32,  // mesh /32
    pub persistent_keepalive: u16,
}

/// Which path the kernel wg endpoint for this peer currently points at.
/// TCP is the bootstrap path and permanent fallback; the bridge task stays
/// alive (idle) while on UDP precisely so reverting is one endpoint swap.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LinkTransport {
    Tcp,
    Punching { since_ms: u64 },
    Udp { endpoint: SocketAddr },
}

impl LinkTransport {
    pub fn as_str(&self) -> &'static str {
        match self {
            LinkTransport::Tcp => "tcp",
            LinkTransport::Punching { .. } => "punching",
            LinkTransport::Udp { .. } => "udp",
        }
    }
}

/// Punch bookkeeping per peer. No persistent state: NAT mappings don't survive
/// reboots, and the TCP bootstrap makes rediscovery cheap.
#[derive(Debug, Clone, Default)]
pub struct PunchState {
    pub attempts: u32,
    pub next_retry_ms: u64,
    /// Our external mapping toward this peer, as the peer's wg observed it
    /// (learned from its `PunchReport.observed_source`). Offered as our
    /// PEER_REFLEXIVE candidate on the next attempt.
    pub self_reflexive: Option<SocketAddr>,
    /// The peer's external mapping as our wg observed it (endpoint roam on the
    /// first authenticated packet). Tried first on the next attempt.
    pub peer_reflexive: Option<SocketAddr>,
    /// Nonce of the in-flight negotiation (correlates offer/result).
    pub nonce: Option<[u8; 16]>,
    /// Peer answered UNIMPLEMENTED — an older sidecar. Stay on TCP; re-probe
    /// only at the backoff cap in case it upgrades.
    pub unsupported: bool,
}

/// Everything the sidecar tracks about a peer across wg / heartbeat / app surfaces.
#[derive(Debug, Clone)]
pub struct PeerInfo {
    pub member_id: MemberId,
    pub mesh_ip: u32,
    pub wg_pub: [u8; 32],
    pub ed25519_pub: Option<[u8; 32]>,
    pub endpoint: Option<String>,
    pub configured: bool,
    pub live: bool,
    /// Loopback address of this peer's TCP bridge — the revert target.
    pub bridge_addr: Option<SocketAddr>,
    /// Peer's self-advertised UDP candidate from its PeerEndpoint envelope.
    pub advertised_udp: Option<SocketAddr>,
    pub transport: LinkTransport,
    pub punch: PunchState,
}

#[derive(Default)]
pub struct PeerTable {
    peers: HashMap<MemberId, PeerInfo>,
}

impl PeerTable {
    pub fn new() -> Self {
        Self::default()
    }

    fn entry(&mut self, member_id: MemberId, mesh_ip: u32, wg_pub: [u8; 32]) -> &mut PeerInfo {
        self.peers.entry(member_id).or_insert_with(|| PeerInfo {
            member_id,
            mesh_ip,
            wg_pub,
            ed25519_pub: None,
            endpoint: None,
            configured: false,
            live: false,
            bridge_addr: None,
            advertised_udp: None,
            transport: LinkTransport::Tcp,
            punch: PunchState::default(),
        })
    }

    /// Insert/update a peer from a received PeerEndpoint.
    pub fn upsert_endpoint(
        &mut self,
        member_id: MemberId,
        mesh_ip: u32,
        wg_pub: [u8; 32],
        ed25519_pub: [u8; 32],
        endpoint: String,
    ) {
        let e = self.entry(member_id, mesh_ip, wg_pub);
        e.mesh_ip = mesh_ip;
        e.wg_pub = wg_pub;
        e.ed25519_pub = Some(ed25519_pub);
        e.endpoint = Some(endpoint);
    }

    /// Insert/refresh a peer from chain enumeration alone (no Ed25519/endpoint yet —
    /// those arrive via the PeerEndpoint envelope; existing values are preserved).
    pub fn ensure_chain(&mut self, member_id: MemberId, mesh_ip: u32, wg_pub: [u8; 32]) {
        let e = self.entry(member_id, mesh_ip, wg_pub);
        e.mesh_ip = mesh_ip;
        e.wg_pub = wg_pub;
    }

    /// Record a peer's Ed25519 heartbeat key. Returns true if it was newly set/changed.
    pub fn set_ed25519(&mut self, member_id: &MemberId, ed: [u8; 32]) -> bool {
        match self.peers.get_mut(member_id) {
            Some(p) if p.ed25519_pub != Some(ed) => {
                p.ed25519_pub = Some(ed);
                true
            }
            _ => false,
        }
    }

    pub fn set_endpoint(&mut self, member_id: &MemberId, endpoint: String) {
        if let Some(p) = self.peers.get_mut(member_id) {
            p.endpoint = Some(endpoint);
        }
    }

    pub fn mark_configured(&mut self, member_id: &MemberId) -> bool {
        if let Some(p) = self.peers.get_mut(member_id) {
            let changed = !p.configured;
            p.configured = true;
            changed
        } else {
            false
        }
    }

    pub fn set_live(&mut self, member_id: &MemberId, live: bool) -> bool {
        if let Some(p) = self.peers.get_mut(member_id) {
            let changed = p.live != live;
            p.live = live;
            changed
        } else {
            false
        }
    }

    pub fn set_bridge_addr(&mut self, member_id: &MemberId, addr: SocketAddr) {
        if let Some(p) = self.peers.get_mut(member_id) {
            p.bridge_addr = Some(addr);
        }
    }

    pub fn set_advertised_udp(&mut self, member_id: &MemberId, addr: Option<SocketAddr>) {
        if let Some(p) = self.peers.get_mut(member_id) {
            p.advertised_udp = addr;
        }
    }

    pub fn set_transport(&mut self, member_id: &MemberId, t: LinkTransport) {
        if let Some(p) = self.peers.get_mut(member_id) {
            p.transport = t;
        }
    }

    pub fn transport_of(&self, member_id: &MemberId) -> Option<LinkTransport> {
        self.peers.get(member_id).map(|p| p.transport)
    }

    pub fn punch_mut(&mut self, member_id: &MemberId) -> Option<&mut PunchState> {
        self.peers.get_mut(member_id).map(|p| &mut p.punch)
    }

    /// Find the peer whose in-flight punch negotiation carries `nonce`.
    pub fn by_punch_nonce(&self, nonce: &[u8]) -> Option<&PeerInfo> {
        self.peers
            .values()
            .find(|p| p.punch.nonce.is_some_and(|n| n[..] == *nonce))
    }

    pub fn ed25519_of(&self, member_id: &MemberId) -> Option<[u8; 32]> {
        self.peers.get(member_id).and_then(|p| p.ed25519_pub)
    }

    pub fn get(&self, member_id: &MemberId) -> Option<&PeerInfo> {
        self.peers.get(member_id)
    }

    pub fn all(&self) -> impl Iterator<Item = &PeerInfo> {
        self.peers.values()
    }

    pub fn live_count(&self) -> usize {
        self.peers.values().filter(|p| p.live).count()
    }

    pub fn connected_ids(&self) -> Vec<MemberId> {
        self.peers
            .values()
            .filter(|p| p.configured)
            .map(|p| p.member_id)
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upsert_and_live_tracking() {
        let mut t = PeerTable::new();
        let id = [1u8; 32];
        t.upsert_endpoint(id, 0x0a0d0001, [2u8; 32], [3u8; 32], "h:1".into());
        assert_eq!(t.ed25519_of(&id), Some([3u8; 32]));
        assert!(!t.get(&id).unwrap().live);
        assert!(t.set_live(&id, true));
        assert!(!t.set_live(&id, true)); // no change
        assert_eq!(t.live_count(), 1);
    }

    #[test]
    fn new_peers_start_on_tcp_with_clean_punch_state() {
        let mut t = PeerTable::new();
        let id = [1u8; 32];
        t.ensure_chain(id, 0x0a0d0001, [2u8; 32]);
        let p = t.get(&id).unwrap();
        assert_eq!(p.transport, LinkTransport::Tcp);
        assert_eq!(p.punch.attempts, 0);
        assert!(!p.punch.unsupported);
        assert!(p.bridge_addr.is_none());
    }

    #[test]
    fn transport_swaps_and_nonce_lookup() {
        let mut t = PeerTable::new();
        let id = [1u8; 32];
        t.ensure_chain(id, 0x0a0d0001, [2u8; 32]);

        t.set_transport(&id, LinkTransport::Punching { since_ms: 42 });
        assert_eq!(t.transport_of(&id).unwrap().as_str(), "punching");

        let ep: SocketAddr = "203.0.113.7:51821".parse().unwrap();
        t.set_transport(&id, LinkTransport::Udp { endpoint: ep });
        assert_eq!(
            t.transport_of(&id),
            Some(LinkTransport::Udp { endpoint: ep })
        );

        t.punch_mut(&id).unwrap().nonce = Some([9u8; 16]);
        assert_eq!(t.by_punch_nonce(&[9u8; 16]).unwrap().member_id, id);
        assert!(t.by_punch_nonce(&[8u8; 16]).is_none());
    }

    /// PeerEndpoint re-receipt (or chain refresh) must never clobber an upgraded
    /// link's transport state or punch bookkeeping.
    #[test]
    fn upsert_preserves_transport_state() {
        let mut t = PeerTable::new();
        let id = [1u8; 32];
        t.ensure_chain(id, 0x0a0d0001, [2u8; 32]);
        let ep: SocketAddr = "203.0.113.7:51821".parse().unwrap();
        t.set_transport(&id, LinkTransport::Udp { endpoint: ep });
        t.punch_mut(&id).unwrap().attempts = 3;

        t.upsert_endpoint(id, 0x0a0d0001, [2u8; 32], [3u8; 32], "h:1".into());
        t.ensure_chain(id, 0x0a0d0001, [2u8; 32]);
        assert_eq!(
            t.transport_of(&id),
            Some(LinkTransport::Udp { endpoint: ep })
        );
        assert_eq!(t.get(&id).unwrap().punch.attempts, 3);
    }
}
