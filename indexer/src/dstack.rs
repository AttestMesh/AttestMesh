//! Vendored dstack guest-agent runtime client (spec §6, §2.1).
//!
//! Same shape as the sidecar's `dstack.rs`: a small trait with a deterministic
//! in-memory mock for tests and a best-effort UDS client for production. All
//! attestation-method-specific detail stays behind this boundary so the rest of the
//! indexer is attestation-method-agnostic.

use anyhow::{Context, Result};
use async_trait::async_trait;
use sha3::{Digest, Keccak256};
use zeroize::Zeroizing;

/// The dstack runtime surface the indexer depends on. Deterministic per TEE state:
/// the same purpose/subkey yields the same bytes across restarts of the same CVM,
/// but differs across CVMs (dstack keys by app_id). The indexer only needs key
/// derivation and quote generation — it stores no sealed secrets (spec §6, §9.2).
#[async_trait]
pub trait DstackRuntime: Send + Sync {
    /// Derive 32 bytes of attestation-bound key material for `purpose`/`subkey`.
    async fn derive_key(&self, purpose: &str, subkey: &str) -> Result<Zeroizing<[u8; 32]>>;

    /// Request a TEE quote whose user-data slot commits to `report_data` (64 bytes).
    async fn get_quote(&self, report_data: [u8; 64]) -> Result<Vec<u8>>;
}

/// In-memory mock for tests. `root_seed` stands in for the per-CVM TEE state, so two
/// mocks with different seeds derive different keys (matching dstack's per-app_id
/// behaviour) while one mock reused across "restarts" is deterministic.
pub struct MockDstack {
    root_seed: [u8; 32],
}

impl MockDstack {
    pub fn new(root_seed: [u8; 32]) -> Self {
        Self { root_seed }
    }

    pub fn from_label(label: &str) -> Self {
        let mut seed = [0u8; 32];
        seed.copy_from_slice(&Keccak256::digest(label.as_bytes()));
        Self::new(seed)
    }
}

#[async_trait]
impl DstackRuntime for MockDstack {
    async fn derive_key(&self, purpose: &str, subkey: &str) -> Result<Zeroizing<[u8; 32]>> {
        // Identical derivation scheme to the sidecar mock so cross-component test
        // fixtures line up if ever shared.
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
}

/// Best-effort production client speaking JSON over the dstack guest-agent UDS.
/// The exact wire protocol is dstack-version-specific; structured to be swapped for
/// the real `dstack-sdk` once vendored. Not exercised by unit tests.
pub struct UnixSocketDstack {
    socket_path: String,
}

impl UnixSocketDstack {
    pub fn new(socket_path: impl Into<String>) -> Self {
        Self {
            socket_path: socket_path.into(),
        }
    }

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

#[async_trait]
impl DstackRuntime for UnixSocketDstack {
    async fn derive_key(&self, purpose: &str, subkey: &str) -> Result<Zeroizing<[u8; 32]>> {
        // dstack 0.5.x exposes only /GetKey (the /DeriveKey endpoint was removed) —
        // same live-only fix as the sidecar's dstack client. The KMS signature chain
        // in the response is ignored here; derive_key wants only the seed bytes.
        let body = serde_json::json!({ "path": purpose, "purpose": subkey });
        let resp = self.request("/GetKey", &body).await?;
        // On the error path the response carries no key, so it is safe to log it
        // verbatim to pinpoint a guest-agent/API mismatch on a live CVM.
        let key_hex = resp.get("key").and_then(|v| v.as_str()).with_context(|| {
            format!("/GetKey response missing 'key' (path={purpose}, purpose={subkey}): {resp}")
        })?;
        let bytes = hex::decode(key_hex.trim_start_matches("0x"))?;
        anyhow::ensure!(bytes.len() >= 32, "short key");
        let mut out = [0u8; 32];
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
}
