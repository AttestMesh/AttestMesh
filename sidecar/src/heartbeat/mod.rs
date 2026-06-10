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

async fn send_loop(shared: Arc<Shared>) -> anyhow::Result<()> {
    let sock = UdpSocket::bind(("0.0.0.0", 0)).await?;
    loop {
        let (mut connected, targets) = {
            let p = shared.peers.lock().await;
            (
                p.connected_ids(),
                p.all().map(|x| x.mesh_ip).collect::<Vec<_>>(),
            )
        };
        // The connected view includes the sender itself (liveness convergence
        // compares each node's view against the live set, which contains everyone).
        connected.push(shared.self_member_id);
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
        let mut connected = shared.peers.lock().await.connected_ids();
        connected.push(shared.self_member_id); // self is part of our own view
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
