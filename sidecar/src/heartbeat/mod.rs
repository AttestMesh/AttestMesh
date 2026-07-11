//! Heartbeat protocol (sidecar spec §11): wire format, sign/verify, liveness view,
//! and the UDP send/recv loops that drive first-convergence.

pub mod liveness;
pub mod packet;

use crate::state::{AppPeerEvent, Phase, Shared};
use ed25519_dalek::VerifyingKey;
use std::net::{Ipv4Addr, SocketAddrV4};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::net::UdpSocket;
use tokio::task::JoinHandle;

pub const HEARTBEAT_PORT: u16 = 51820;
const INTERVAL: Duration = Duration::from_secs(2);

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

async fn recv_loop(shared: Arc<Shared>) -> anyhow::Result<()> {
    let sock = UdpSocket::bind(("0.0.0.0", HEARTBEAT_PORT)).await?;
    let mut buf = vec![0u8; 8192];
    loop {
        let n = match sock.recv_from(&mut buf).await {
            Ok((n, _)) => n,
            Err(_) => continue,
        };
        let hb = match packet::decode(&buf[..n]) {
            Ok(h) => h,
            Err(_) => continue,
        };
        let sender = hb.payload.sender_member_id;

        // Verify against the sender's Ed25519 key (learned via PeerEndpoint).
        let ed = shared.peers.lock().await.ed25519_of(&sender);
        let Some(ed) = ed else { continue };
        let Ok(vk) = VerifyingKey::from_bytes(&ed) else {
            continue;
        };
        if !packet::verify(&vk, &hb) {
            continue;
        }

        let now = now_ms();
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
    use crate::wg::peer::PeerTable;

    fn id(n: u8) -> [u8; 32] {
        let mut b = [0u8; 32];
        b[31] = n;
        b
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
        use crate::heartbeat::liveness::Liveness;
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
