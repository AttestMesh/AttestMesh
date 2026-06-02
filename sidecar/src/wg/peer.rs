//! Peer table + wireguard peer config (sidecar spec §10.2).

use std::collections::HashMap;

pub type MemberId = [u8; 32];

/// A single wireguard peer's configuration.
#[derive(Debug, Clone)]
pub struct WgPeerConfig {
    pub public_key: [u8; 32],
    pub endpoint: String, // "host:port"
    pub allowed_ip: u32,  // mesh /32
    pub persistent_keepalive: u16,
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
}

#[derive(Default)]
pub struct PeerTable {
    peers: HashMap<MemberId, PeerInfo>,
}

impl PeerTable {
    pub fn new() -> Self {
        Self::default()
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
        let e = self.peers.entry(member_id).or_insert_with(|| PeerInfo {
            member_id,
            mesh_ip,
            wg_pub,
            ed25519_pub: None,
            endpoint: None,
            configured: false,
            live: false,
        });
        e.mesh_ip = mesh_ip;
        e.wg_pub = wg_pub;
        e.ed25519_pub = Some(ed25519_pub);
        e.endpoint = Some(endpoint);
    }

    pub fn mark_configured(&mut self, member_id: &MemberId) {
        if let Some(p) = self.peers.get_mut(member_id) {
            p.configured = true;
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
}
