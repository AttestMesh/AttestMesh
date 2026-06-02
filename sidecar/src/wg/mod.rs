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
use std::sync::Mutex;

pub const IFACE: &str = "attestmesh0";
pub const MTU: u32 = 1420;

fn b64(key: &[u8; 32]) -> String {
    base64::engine::general_purpose::STANDARD.encode(key)
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
}

async fn run(cmd: &mut tokio::process::Command) -> Result<()> {
    let out = cmd.output().await.context("spawn wg/ip")?;
    if !out.status.success() {
        anyhow::bail!("command failed: {}", String::from_utf8_lossy(&out.stderr));
    }
    Ok(())
}

/// In-memory control for tests — records configured peers.
#[derive(Default)]
pub struct MockWg {
    pub created: Mutex<bool>,
    pub peers: Mutex<Vec<WgPeerConfig>>,
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
}
