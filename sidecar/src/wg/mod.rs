//! Wireguard interface + peer management (sidecar spec §10).
//!
//! The spec calls for `defguard_wireguard_rs` netlink control. For build portability
//! and test isolation we put control behind a `MeshControl` trait with a command-based
//! impl (`CommandWg`, shells out to `ip`/`wg` — runtime needs NET_ADMIN) and an
//! in-memory `MockWg` for tests. The netlink impl is a drop-in replacement.

pub mod cidr;
pub mod peer;

use anyhow::{Context, Result};
use async_trait::async_trait;
use base64::Engine;
use peer::WgPeerConfig;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Mutex;

pub const IFACE: &str = "attestmesh0";
pub const MTU: u32 = 1420;

fn b64(key: &[u8; 32]) -> String {
    base64::engine::general_purpose::STANDARD.encode(key)
}

/// Live per-peer view from the wg device — what the punch executor and the
/// UDP-path watchdog judge success/death by.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WgPeerStatus {
    /// Current endpoint as the device sees it (roams on the first
    /// authenticated packet from a new source).
    pub endpoint: Option<SocketAddr>,
    /// Unix seconds of the latest completed handshake; 0 = never.
    pub last_handshake_unix: u64,
    /// Authenticated bytes received from this peer. An increase proves that
    /// the current WireGuard session carried peer traffic even when no new
    /// handshake was needed after an endpoint change.
    pub rx_bytes: u64,
}

#[async_trait]
pub trait MeshControl: Send + Sync {
    async fn create_interface(
        &self,
        private_key: &[u8; 32],
        listen_port: u16,
        self_ip: u32,
        prefix: u8,
    ) -> Result<()>;
    async fn add_peer(&self, peer: &WgPeerConfig) -> Result<()>;
    async fn remove_peer(&self, public_key: &[u8; 32]) -> Result<()>;
    /// Retarget an existing peer's endpoint in place (the punch upgrade /
    /// revert primitive). The wg session survives the swap — same keys, same
    /// session; at worst it re-handshakes.
    async fn set_peer_endpoint(&self, public_key: &[u8; 32], endpoint: &str) -> Result<()>;
    /// Read the device's live view of one peer (`None` if unknown to the device).
    async fn peer_status(&self, public_key: &[u8; 32]) -> Result<Option<WgPeerStatus>>;
}

/// Production control via the `wg` / `ip` userspace tools.
pub struct CommandWg;

#[async_trait]
impl MeshControl for CommandWg {
    async fn create_interface(
        &self,
        private_key: &[u8; 32],
        listen_port: u16,
        self_ip: u32,
        prefix: u8,
    ) -> Result<()> {
        use tokio::process::Command;
        run(Command::new("ip").args(["link", "add", IFACE, "type", "wireguard"]))
            .await
            .ok();
        // private key via stdin to `wg set`
        let key_b64 = b64(private_key);
        let tmp = std::env::temp_dir().join(format!("{IFACE}.key"));
        tokio::fs::write(&tmp, &key_b64)
            .await
            .context("write wg key")?;
        run(Command::new("wg").args([
            "set",
            IFACE,
            "listen-port",
            &listen_port.to_string(),
            "private-key",
            tmp.to_str().unwrap(),
        ]))
        .await?;
        let ip = cidr::fmt_ipv4(self_ip);
        run(Command::new("ip").args(["address", "add", &format!("{ip}/{prefix}"), "dev", IFACE]))
            .await
            .ok();
        run(Command::new("ip").args(["link", "set", "mtu", &MTU.to_string(), "up", "dev", IFACE]))
            .await?;
        let _ = tokio::fs::remove_file(&tmp).await;
        Ok(())
    }

    async fn add_peer(&self, peer: &WgPeerConfig) -> Result<()> {
        use tokio::process::Command;
        let allowed = format!("{}/32", cidr::fmt_ipv4(peer.allowed_ip));
        run(Command::new("wg").args([
            "set",
            IFACE,
            "peer",
            &b64(&peer.public_key),
            "endpoint",
            &peer.endpoint,
            "allowed-ips",
            &allowed,
            "persistent-keepalive",
            &peer.persistent_keepalive.to_string(),
        ]))
        .await
    }

    async fn remove_peer(&self, public_key: &[u8; 32]) -> Result<()> {
        use tokio::process::Command;
        run(Command::new("wg").args(["set", IFACE, "peer", &b64(public_key), "remove"])).await
    }

    async fn set_peer_endpoint(&self, public_key: &[u8; 32], endpoint: &str) -> Result<()> {
        use tokio::process::Command;
        run(Command::new("wg").args(["set", IFACE, "peer", &b64(public_key), "endpoint", endpoint]))
            .await
    }

    async fn peer_status(&self, public_key: &[u8; 32]) -> Result<Option<WgPeerStatus>> {
        use tokio::process::Command;
        let out = Command::new("wg")
            .args(["show", IFACE, "dump"])
            .output()
            .await
            .context("spawn wg show dump")?;
        if !out.status.success() {
            anyhow::bail!(
                "wg show dump failed: {}",
                String::from_utf8_lossy(&out.stderr)
            );
        }
        Ok(parse_wg_dump(
            &String::from_utf8_lossy(&out.stdout),
            &b64(public_key),
        ))
    }
}

/// Parse `wg show <iface> dump` output for one peer. Line 1 is the interface;
/// peer lines are `pubkey psk endpoint allowed-ips latest-handshake rx tx keepalive`
/// (tab-separated, endpoint `(none)` when unset).
fn parse_wg_dump(dump: &str, peer_b64: &str) -> Option<WgPeerStatus> {
    for line in dump.lines().skip(1) {
        let f: Vec<&str> = line.split('\t').collect();
        if f.len() < 5 || f[0] != peer_b64 {
            continue;
        }
        return Some(WgPeerStatus {
            endpoint: f[2].parse().ok(),
            last_handshake_unix: f[4].parse().unwrap_or(0),
            rx_bytes: f.get(5).and_then(|v| v.parse().ok()).unwrap_or(0),
        });
    }
    None
}

async fn run(cmd: &mut tokio::process::Command) -> Result<()> {
    let out = cmd.output().await.context("spawn wg/ip")?;
    if !out.status.success() {
        anyhow::bail!("command failed: {}", String::from_utf8_lossy(&out.stderr));
    }
    Ok(())
}

/// In-memory control for tests — records configured peers, endpoint swaps, and
/// serves scripted `WgPeerStatus` so punch/watchdog logic is testable without a
/// kernel device.
#[derive(Default)]
pub struct MockWg {
    pub created: Mutex<bool>,
    pub peers: Mutex<Vec<WgPeerConfig>>,
    /// Every `set_peer_endpoint` call in order: (public_key, endpoint).
    pub endpoint_sets: Mutex<Vec<([u8; 32], String)>>,
    /// Scripted device view per peer (tests set this to simulate handshakes).
    pub statuses: Mutex<HashMap<[u8; 32], WgPeerStatus>>,
}

impl MockWg {
    pub fn script_status(&self, public_key: [u8; 32], status: WgPeerStatus) {
        self.statuses.lock().unwrap().insert(public_key, status);
    }

    pub fn last_endpoint_of(&self, public_key: &[u8; 32]) -> Option<String> {
        self.endpoint_sets
            .lock()
            .unwrap()
            .iter()
            .rev()
            .find(|(k, _)| k == public_key)
            .map(|(_, e)| e.clone())
    }
}

#[async_trait]
impl MeshControl for MockWg {
    async fn create_interface(
        &self,
        _private_key: &[u8; 32],
        _listen_port: u16,
        _self_ip: u32,
        _prefix: u8,
    ) -> Result<()> {
        *self.created.lock().unwrap() = true;
        Ok(())
    }

    async fn add_peer(&self, peer: &WgPeerConfig) -> Result<()> {
        self.peers.lock().unwrap().push(peer.clone());
        Ok(())
    }

    async fn remove_peer(&self, public_key: &[u8; 32]) -> Result<()> {
        self.peers
            .lock()
            .unwrap()
            .retain(|p| &p.public_key != public_key);
        Ok(())
    }

    async fn set_peer_endpoint(&self, public_key: &[u8; 32], endpoint: &str) -> Result<()> {
        self.endpoint_sets
            .lock()
            .unwrap()
            .push((*public_key, endpoint.to_string()));
        Ok(())
    }

    async fn peer_status(&self, public_key: &[u8; 32]) -> Result<Option<WgPeerStatus>> {
        Ok(self.statuses.lock().unwrap().get(public_key).cloned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn mock_wg_records_peers() {
        let wg = MockWg::default();
        wg.create_interface(&[1u8; 32], 51820, 0x0a0d0001, 16)
            .await
            .unwrap();
        assert!(*wg.created.lock().unwrap());
        wg.add_peer(&WgPeerConfig {
            public_key: [9u8; 32],
            endpoint: "h:51820".into(),
            allowed_ip: 0x0a0d0002,
            persistent_keepalive: 25,
        })
        .await
        .unwrap();
        assert_eq!(wg.peers.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn mock_wg_records_endpoint_swaps_and_serves_scripted_status() {
        let wg = MockWg::default();
        let key = [9u8; 32];
        assert_eq!(wg.peer_status(&key).await.unwrap(), None);

        wg.set_peer_endpoint(&key, "127.0.0.1:1000").await.unwrap();
        wg.set_peer_endpoint(&key, "203.0.113.7:51821")
            .await
            .unwrap();
        assert_eq!(wg.last_endpoint_of(&key).unwrap(), "203.0.113.7:51821");

        let st = WgPeerStatus {
            endpoint: Some("203.0.113.7:51821".parse().unwrap()),
            last_handshake_unix: 1234,
            rx_bytes: 100,
        };
        wg.script_status(key, st.clone());
        assert_eq!(wg.peer_status(&key).await.unwrap(), Some(st));
    }

    #[test]
    fn parse_wg_dump_finds_peer_endpoint_and_handshake() {
        let pk = "HIgo9xNzJMWLKASShiTqIybxZ0U3wGLiUeJ1PKf8ykw=";
        let other = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg=";
        let dump = format!(
            "privkey\tselfpub\t51821\toff\n\
             {other}\t(none)\t(none)\t10.13.0.2/32\t0\t0\t0\t25\n\
             {pk}\t(none)\t203.0.113.7:51821\t10.13.0.3/32\t1765432100\t100\t200\t25\n"
        );
        let st = parse_wg_dump(&dump, pk).unwrap();
        assert_eq!(st.endpoint, Some("203.0.113.7:51821".parse().unwrap()));
        assert_eq!(st.last_handshake_unix, 1765432100);
        assert_eq!(st.rx_bytes, 100);

        // peer with no endpoint / no handshake yet
        let st = parse_wg_dump(&dump, other).unwrap();
        assert_eq!(st.endpoint, None);
        assert_eq!(st.last_handshake_unix, 0);
        assert_eq!(st.rx_bytes, 0);

        assert!(parse_wg_dump(&dump, "missing").is_none());
        // a peer pubkey never matches the interface line
        assert!(parse_wg_dump("privkey\tselfpub\t51821\toff\n", "selfpub").is_none());
    }
}
