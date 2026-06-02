//! dstack guest-agent runtime client (sidecar spec §6, §2.1).
//!
//! The real dstack runtime exposes `derive_key`, `seal`, `unseal`, and `get_quote`
//! over a unix-domain socket. We vendor a small trait here (the `dstack-sdk` crate is
//! not crates.io-published) with two implementations: a best-effort UDS HTTP client
//! for production and an in-memory mock for tests. All attestation-method-specific
//! detail stays behind this boundary.

use anyhow::{Context, Result};
use async_trait::async_trait;
use sha3::{Digest, Keccak256};
use std::collections::HashMap;
use std::sync::Mutex;
use zeroize::Zeroizing;

/// The dstack runtime surface the sidecar depends on. Deterministic per TEE state:
/// the same purpose/subkey yields the same bytes across restarts of the same CVM,
/// but differs across CVMs (dstack keys by app_id).
#[async_trait]
pub trait DstackRuntime: Send + Sync {
    /// Derive 32 bytes of attestation-bound key material for `purpose`/`subkey`.
    async fn derive_key(&self, purpose: &str, subkey: &str) -> Result<Zeroizing<[u8; 32]>>;

    /// Request a TEE quote whose user-data slot commits to `report_data` (64 bytes).
    async fn get_quote(&self, report_data: [u8; 64]) -> Result<Vec<u8>>;

    /// Seal `data` to the node's persistent sealed store under `label`.
    async fn seal(&self, label: &str, data: &[u8]) -> Result<()>;

    /// Unseal previously sealed bytes, or `None` if nothing was sealed under `label`.
    async fn unseal(&self, label: &str) -> Result<Option<Vec<u8>>>;
}

/// In-memory mock for tests. `root_seed` stands in for the per-CVM TEE state, so two
/// mocks with different seeds derive different keys (matching dstack's per-app_id
/// behavior) while one mock reused across "restarts" is deterministic.
pub struct MockDstack {
    root_seed: [u8; 32],
    store: Mutex<HashMap<String, Vec<u8>>>,
}

impl MockDstack {
    pub fn new(root_seed: [u8; 32]) -> Self {
        Self {
            root_seed,
            store: Mutex::new(HashMap::new()),
        }
    }

    pub fn from_label(label: &str) -> Self {
        let mut seed = [0u8; 32];
        let h = Keccak256::digest(label.as_bytes());
        seed.copy_from_slice(&h);
        Self::new(seed)
    }
}

#[async_trait]
impl DstackRuntime for MockDstack {
    async fn derive_key(&self, purpose: &str, subkey: &str) -> Result<Zeroizing<[u8; 32]>> {
        let mut hasher = Keccak256::new();
        hasher.update(b"attestmesh.dstack.derive.v1");
        hasher.update(self.root_seed);
        hasher.update((purpose.len() as u32).to_be_bytes());
        hasher.update(purpose.as_bytes());
        hasher.update((subkey.len() as u32).to_be_bytes());
        hasher.update(subkey.as_bytes());
        let mut out = [0u8; 32];
        out.copy_from_slice(&hasher.finalize());
        Ok(Zeroizing::new(out))
    }

    async fn get_quote(&self, report_data: [u8; 64]) -> Result<Vec<u8>> {
        // A mock quote: a tag plus the report_data so a verifier can at least check
        // the binding. A real quote is the TEE-signed attestation blob.
        let mut q = Vec::with_capacity(80);
        q.extend_from_slice(b"MOCKQUOTE-v1");
        q.extend_from_slice(&report_data);
        Ok(q)
    }

    async fn seal(&self, label: &str, data: &[u8]) -> Result<()> {
        self.store
            .lock()
            .unwrap()
            .insert(label.to_string(), data.to_vec());
        Ok(())
    }

    async fn unseal(&self, label: &str) -> Result<Option<Vec<u8>>> {
        Ok(self.store.lock().unwrap().get(label).cloned())
    }
}

/// Best-effort production client speaking JSON over the dstack guest-agent UDS.
/// The exact wire protocol is dstack-version-specific; this is structured to be
/// swapped for the real `dstack-sdk` once vendored. Not exercised by unit tests.
pub struct UnixSocketDstack {
    socket_path: String,
}

impl UnixSocketDstack {
    pub fn new(socket_path: impl Into<String>) -> Self {
        Self {
            socket_path: socket_path.into(),
        }
    }
}

#[async_trait]
impl DstackRuntime for UnixSocketDstack {
    async fn derive_key(&self, purpose: &str, subkey: &str) -> Result<Zeroizing<[u8; 32]>> {
        let body = serde_json::json!({ "path": purpose, "purpose": subkey });
        let resp = self.request("/DeriveKey", &body).await?;
        let key_hex = resp
            .get("key")
            .and_then(|v| v.as_str())
            .context("missing key")?;
        let bytes = hex::decode(key_hex.trim_start_matches("0x"))?;
        let mut out = [0u8; 32];
        anyhow::ensure!(bytes.len() >= 32, "short key");
        out.copy_from_slice(&bytes[..32]);
        Ok(Zeroizing::new(out))
    }

    async fn get_quote(&self, report_data: [u8; 64]) -> Result<Vec<u8>> {
        let body = serde_json::json!({ "report_data": hex::encode(report_data) });
        let resp = self.request("/GetQuote", &body).await?;
        let q = resp
            .get("quote")
            .and_then(|v| v.as_str())
            .context("missing quote")?;
        Ok(hex::decode(q.trim_start_matches("0x"))?)
    }

    async fn seal(&self, label: &str, data: &[u8]) -> Result<()> {
        let body = serde_json::json!({ "label": label, "data": hex::encode(data) });
        self.request("/Seal", &body).await?;
        Ok(())
    }

    async fn unseal(&self, label: &str) -> Result<Option<Vec<u8>>> {
        let body = serde_json::json!({ "label": label });
        let resp = self.request("/Unseal", &body).await?;
        match resp.get("data").and_then(|v| v.as_str()) {
            Some(d) if !d.is_empty() => Ok(Some(hex::decode(d.trim_start_matches("0x"))?)),
            _ => Ok(None),
        }
    }
}

impl UnixSocketDstack {
    async fn request(&self, path: &str, body: &serde_json::Value) -> Result<serde_json::Value> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let mut stream = tokio::net::UnixStream::connect(&self.socket_path)
            .await
            .with_context(|| format!("connect dstack socket {}", self.socket_path))?;
        let payload = serde_json::to_vec(body)?;
        let req = format!(
            "POST {} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            path,
            payload.len()
        );
        stream.write_all(req.as_bytes()).await?;
        stream.write_all(&payload).await?;
        let mut resp = Vec::new();
        stream.read_to_end(&mut resp).await?;
        let body_start = resp
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .map(|p| p + 4)
            .context("malformed dstack response")?;
        Ok(serde_json::from_slice(&resp[body_start..])?)
    }
}
