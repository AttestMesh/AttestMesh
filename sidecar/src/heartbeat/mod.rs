//! Heartbeat protocol (sidecar spec §11): wire format, sign/verify, liveness view,
//! and the UDP send/recv loops that drive first-convergence.

pub mod liveness;
pub mod packet;

use crate::state::{AppPeerEvent, Phase, Shared};
use ed25519_dalek::VerifyingKey;
use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr, SocketAddrV4};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::net::UdpSocket;
use tokio::task::JoinHandle;

pub const HEARTBEAT_PORT: u16 = 51820;
const INTERVAL: Duration = Duration::from_secs(2);
/// Heartbeats arrive every two seconds; ten seconds tolerates modest clock skew
/// and network delay without allowing old signed views to refresh liveness.
const TIMESTAMP_TOLERANCE_MS: u64 = 10_000;
const MAX_BIND_RETRY_SECS: u64 = 30;

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Spawn the send + receive loops. Returns their join handles.
pub fn spawn(shared: Arc<Shared>) -> Vec<JoinHandle<()>> {
    let s1 = shared.clone();
    let send = tokio::spawn(async move {
        if let Err(e) = send_loop(s1).await {
            tracing::warn!(error = %e, "heartbeat send loop exited");
        }
    });
    let recv = tokio::spawn(async move {
        if let Err(e) = recv_loop(shared).await {
            tracing::warn!(error = %e, "heartbeat recv loop exited");
        }
    });
    vec![send, recv]
}

/// The connected view a node reports (and records for itself): its configured
/// peers PLUS itself. Liveness convergence compares each node's view against the
/// live set — which contains everyone — so omitting self makes convergence
/// unsatisfiable (live-found bug: a 2-node mesh could never converge).
pub fn connected_view(peers: &crate::wg::peer::PeerTable, self_id: [u8; 32]) -> Vec<[u8; 32]> {
    let mut connected = peers.connected_ids();
    connected.push(self_id);
    connected
}

async fn send_loop(shared: Arc<Shared>) -> anyhow::Result<()> {
    let sock = UdpSocket::bind(("0.0.0.0", 0)).await?;
    loop {
        let (connected, targets) = {
            let p = shared.peers.lock().await;
            (
                connected_view(&p, shared.self_member_id),
                p.all().map(|x| x.mesh_ip).collect::<Vec<_>>(),
            )
        };
        let payload = packet::HeartbeatPayload {
            version: packet::HEARTBEAT_VERSION,
            sender_member_id: shared.self_member_id,
            timestamp_ms: now_ms(),
            connected_member_ids: connected,
        };
        let hb = packet::sign(&shared.keys.ed_signing, payload);
        if let Ok(bytes) = packet::encode(&hb) {
            for ip in targets {
                let addr = SocketAddrV4::new(Ipv4Addr::from(ip), HEARTBEAT_PORT);
                let _ = sock.send_to(&bytes, addr).await;
            }
        }
        tokio::time::sleep(INTERVAL).await;
    }
}

fn heartbeat_metadata_acceptable(
    source_ip: IpAddr,
    sender_mesh_ip: u32,
    timestamp_ms: u64,
    now_ms: u64,
    last_accepted_timestamp_ms: Option<u64>,
) -> bool {
    if source_ip != IpAddr::V4(Ipv4Addr::from(sender_mesh_ip)) {
        return false;
    }
    if timestamp_ms.abs_diff(now_ms) > TIMESTAMP_TOLERANCE_MS {
        return false;
    }
    last_accepted_timestamp_ms
        .map(|last| timestamp_ms > last)
        .unwrap_or(true)
}

async fn bind_recv_socket(mesh_ip: u32) -> UdpSocket {
    let addr = SocketAddrV4::new(Ipv4Addr::from(mesh_ip), HEARTBEAT_PORT);
    let mut retry_secs = 1;
    loop {
        match UdpSocket::bind(addr).await {
            Ok(sock) => return sock,
            Err(e) => {
                // The heartbeat task is spawned before the mesh address is guaranteed
                // to be up. Retry here so that race cannot block the rest of bring-up.
                tracing::warn!(error = %e, %addr, retry_secs, "heartbeat bind failed; retrying");
                tokio::time::sleep(Duration::from_secs(retry_secs)).await;
                retry_secs = (retry_secs * 2).min(MAX_BIND_RETRY_SECS);
            }
        }
    }
}

async fn recv_loop(shared: Arc<Shared>) -> anyhow::Result<()> {
    let sock = bind_recv_socket(shared.self_mesh_ip).await;
    let mut buf = vec![0u8; 8192];
    let mut last_accepted_timestamps = HashMap::new();
    loop {
        let (n, src) = match sock.recv_from(&mut buf).await {
            Ok(received) => received,
            Err(_) => continue,
        };
        let hb = match packet::decode(&buf[..n]) {
            Ok(h) => h,
            Err(_) => continue,
        };
        let sender = hb.payload.sender_member_id;

        let peer = {
            let peers = shared.peers.lock().await;
            peers
                .get(&sender)
                .map(|peer| (peer.mesh_ip, peer.ed25519_pub))
        };
        let Some((sender_mesh_ip, Some(ed))) = peer else {
            continue;
        };
        let now = now_ms();
        // WireGuard cryptokey routing pins each peer to its mesh /32, making this
        // exact source check the authoritative barrier against member replays.
        if !heartbeat_metadata_acceptable(
            src.ip(),
            sender_mesh_ip,
            hb.payload.timestamp_ms,
            now,
            last_accepted_timestamps.get(&sender).copied(),
        ) {
            continue;
        }

        // Verify against the sender's Ed25519 key published on chain.
        let Ok(vk) = VerifyingKey::from_bytes(&ed) else {
            continue;
        };
        if !packet::verify(&vk, &hb) {
            continue;
        }
        last_accepted_timestamps.insert(sender, hb.payload.timestamp_ms);

        let connected = connected_view(&*shared.peers.lock().await, shared.self_member_id);
        {
            let mut lv = shared.liveness.lock().await;
            lv.on_heartbeat(sender, &hb.payload.connected_member_ids, now);
            lv.record_self_view(&connected, now);
            if lv.is_converged(now) && !shared.gates.first_converged() {
                shared.gates.latch_first_converged();
                tracing::info!("first convergence observed");
            }
        }
        if shared.peers.lock().await.set_live(&sender, true) {
            shared.peer_change.notify_one();
            let _ = shared.peer_event_tx.send(AppPeerEvent::Liveness {
                member_id: sender,
                up: true,
            });
        }
        if shared.gates.healthy() {
            shared.set_phase(Phase::Healthy).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::heartbeat::liveness::Liveness;
    use crate::wg::peer::PeerTable;

    fn id(n: u8) -> [u8; 32] {
        let mut b = [0u8; 32];
        b[31] = n;
        b
    }

    fn apply_if_metadata_acceptable(
        source_ip: IpAddr,
        sender_mesh_ip: u32,
        sender: [u8; 32],
        timestamp_ms: u64,
        now_ms: u64,
        last_accepted_timestamps: &mut HashMap<[u8; 32], u64>,
        liveness: &mut Liveness,
    ) -> bool {
        if !heartbeat_metadata_acceptable(
            source_ip,
            sender_mesh_ip,
            timestamp_ms,
            now_ms,
            last_accepted_timestamps.get(&sender).copied(),
        ) {
            return false;
        }
        last_accepted_timestamps.insert(sender, timestamp_ms);
        liveness.on_heartbeat(sender, &[sender], now_ms);
        true
    }

    #[test]
    fn heartbeat_with_mismatched_source_is_rejected() {
        let me = id(1);
        let sender = id(2);
        let sender_mesh_ip = 0x0a0d0002;
        let now = 50_000;
        let mut last_accepted_timestamps = HashMap::new();
        let mut liveness = Liveness::new(me);

        assert!(!apply_if_metadata_acceptable(
            IpAddr::V4(Ipv4Addr::new(10, 13, 0, 3)),
            sender_mesh_ip,
            sender,
            now,
            now,
            &mut last_accepted_timestamps,
            &mut liveness,
        ));
        assert!(!liveness.is_up(&sender, now));
        assert!(!last_accepted_timestamps.contains_key(&sender));
    }

    #[test]
    fn replayed_or_out_of_window_heartbeat_is_rejected() {
        let me = id(1);
        let sender = id(2);
        let sender_mesh_ip = 0x0a0d0002;
        let source_ip = IpAddr::V4(Ipv4Addr::from(sender_mesh_ip));
        let first_timestamp = 100_000;
        let mut last_accepted_timestamps = HashMap::new();
        let mut liveness = Liveness::new(me);

        assert!(apply_if_metadata_acceptable(
            source_ip,
            sender_mesh_ip,
            sender,
            first_timestamp,
            first_timestamp,
            &mut last_accepted_timestamps,
            &mut liveness,
        ));
        assert!(!apply_if_metadata_acceptable(
            source_ip,
            sender_mesh_ip,
            sender,
            first_timestamp,
            first_timestamp + 1_000,
            &mut last_accepted_timestamps,
            &mut liveness,
        ));

        let stale_now = first_timestamp + TIMESTAMP_TOLERANCE_MS + 2;
        assert!(!apply_if_metadata_acceptable(
            source_ip,
            sender_mesh_ip,
            sender,
            first_timestamp + 1,
            stale_now,
            &mut last_accepted_timestamps,
            &mut liveness,
        ));
        assert!(!apply_if_metadata_acceptable(
            source_ip,
            sender_mesh_ip,
            sender,
            stale_now + TIMESTAMP_TOLERANCE_MS + 1,
            stale_now,
            &mut last_accepted_timestamps,
            &mut liveness,
        ));

        assert_eq!(
            last_accepted_timestamps.get(&sender),
            Some(&first_timestamp)
        );
        assert!(!liveness.is_up(&sender, stale_now));
    }

    #[test]
    fn fresh_correctly_sourced_heartbeat_is_accepted() {
        let me = id(1);
        let sender = id(2);
        let sender_mesh_ip = 0x0a0d0002;
        let now = 50_000;
        let mut last_accepted_timestamps = HashMap::new();
        let mut liveness = Liveness::new(me);

        assert!(apply_if_metadata_acceptable(
            IpAddr::V4(Ipv4Addr::from(sender_mesh_ip)),
            sender_mesh_ip,
            sender,
            now - 1_000,
            now,
            &mut last_accepted_timestamps,
            &mut liveness,
        ));
        assert!(liveness.is_up(&sender, now));
        assert_eq!(last_accepted_timestamps.get(&sender), Some(&(now - 1_000)));
    }

    /// Regression (live bug 5 in docs/deployment.md): the reported view must
    /// include the sender itself, or `view == live_set` can never hold and
    /// first-convergence never latches on a real mesh.
    #[test]
    fn connected_view_includes_self_and_configured_peers() {
        let me = id(1);
        let mut peers = PeerTable::new();
        peers.ensure_chain(id(2), 0x0a0d0002, [2u8; 32]);
        peers.mark_configured(&id(2));
        peers.ensure_chain(id(3), 0x0a0d0003, [3u8; 32]); // known but NOT configured

        let view = connected_view(&peers, me);
        assert!(view.contains(&me), "self must be in the reported view");
        assert!(view.contains(&id(2)), "configured peers are in the view");
        assert!(!view.contains(&id(3)), "unconfigured peers are not");
        assert_eq!(view.len(), 2);
    }

    /// The exact two-node shape that deadlocked live: each node's view (peer +
    /// self) must satisfy the liveness convergence check.
    #[test]
    fn two_node_views_converge() {
        let (a, b) = (id(1), id(2));

        let mut peers_a = PeerTable::new();
        peers_a.ensure_chain(b, 2, [2u8; 32]);
        peers_a.mark_configured(&b);
        let view_a = connected_view(&peers_a, a);

        let mut peers_b = PeerTable::new();
        peers_b.ensure_chain(a, 1, [1u8; 32]);
        peers_b.mark_configured(&a);
        let view_b = connected_view(&peers_b, b);

        let mut lv = Liveness::with_params(a, 1000, 3);
        lv.record_self_view(&view_a, 1000);
        lv.on_heartbeat(b, &view_b, 1000);
        assert!(lv.is_converged(1000), "peer+self views must converge");
    }
}
