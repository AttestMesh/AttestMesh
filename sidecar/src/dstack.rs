//! dstack guest-agent runtime client (sidecar spec §6, §2.1).
//!
//! The deployed dstack runtime exposes deterministic key derivation and attestation
//! over a unix-domain socket. Durable application data is the caller's responsibility;
//! the live guest agent has no `Seal` / `Unseal` service. All attestation-method-
//! specific detail stays behind this boundary.

use anyhow::{ensure, Context, Result};
use async_trait::async_trait;
use sha3::{Digest, Keccak256};
use zeroize::Zeroizing;

/// A dstack-derived key plus its KMS signature chain (guest-agent `/GetKey`).
/// Validated against dstackgres `group_auth.rs`: `signature_chain` is
/// `[app_signature, kms_signature, ...]`. The app key signs
/// `"ethereum:" + hex(compressed_pubkey(key))`; the KMS root signs
/// `"dstack-kms-issued:" || bytes20(app_id) || app_compressed_pubkey`.
#[derive(Debug, Clone, Default)]
pub struct DstackKey {
    /// The derived secp256k1 private key, raw bytes (>= 32).
    pub key: Vec<u8>,
    /// `[app_signature, kms_signature, ...]`, each raw signature bytes.
    pub signature_chain: Vec<Vec<u8>>,
}

/// CVM identity from the guest-agent `/Info`. `app_id` is the dstack app_id — in
/// AttestMesh it equals the ClusterMember contract address (and `codeId = bytes20(app_id)`).
#[derive(Debug, Clone, Default)]
pub struct DstackInfo {
    pub app_id: Vec<u8>,
    pub compose_hash: Vec<u8>,
    pub instance_id: Vec<u8>,
    pub device_id: Vec<u8>,
    pub tcb_status: String,
}

/// The dstack runtime surface the sidecar depends on. Deterministic per TEE state:
/// the same purpose/subkey yields the same bytes across restarts of the same CVM,
/// but differs across CVMs (dstack keys by app_id).
#[async_trait]
pub trait DstackRuntime: Send + Sync {
    /// Derive 32 bytes of attestation-bound key material for `purpose`/`subkey`.
    async fn derive_key(&self, purpose: &str, subkey: &str) -> Result<Zeroizing<[u8; 32]>>;

    /// Request a TEE quote whose user-data slot commits to `report_data` (64 bytes).
    async fn get_quote(&self, report_data: [u8; 64]) -> Result<Vec<u8>>;

    /// Fetch a dstack-derived key + its KMS signature chain (guest-agent `/GetKey`).
    /// This is the registration-proof material: the derived key signs the binding
    /// message, and `signature_chain` carries the app + KMS signatures the on-chain
    /// `DstackFacet.dstack_register` verifies.
    async fn get_key(&self, path: &str, purpose: &str) -> Result<DstackKey>;

    /// CVM identity from the guest-agent `/Info` (app_id, compose_hash, instance/device id, tcb).
    async fn info(&self) -> Result<DstackInfo>;
}

/// In-memory mock for tests. `root_seed` stands in for the per-CVM TEE state, so two
/// mocks with different seeds derive different keys (matching dstack's per-app_id
/// behavior) while one mock reused across "restarts" is deterministic.
pub struct MockDstack {
    root_seed: [u8; 32],
}

impl MockDstack {
    pub fn new(root_seed: [u8; 32]) -> Self {
        Self { root_seed }
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

    async fn get_key(&self, path: &str, purpose: &str) -> Result<DstackKey> {
        let k = self.derive_key(path, purpose).await?;
        // Shape-valid placeholder chain so callers can exercise the [app_sig, kms_sig]
        // layout. Real signatures come from a live KMS; the contract-side MockKmsChain
        // exercises real-format verification.
        let app_sig = Keccak256::digest(
            [
                b"mock-app-sig:".as_ref(),
                self.root_seed.as_slice(),
                k.as_slice(),
            ]
            .concat(),
        )
        .to_vec();
        let kms_sig =
            Keccak256::digest([b"mock-kms-sig:".as_ref(), self.root_seed.as_slice()].concat())
                .to_vec();
        Ok(DstackKey {
            key: k.as_slice().to_vec(),
            signature_chain: vec![app_sig, kms_sig],
        })
    }

    async fn info(&self) -> Result<DstackInfo> {
        Ok(DstackInfo {
            app_id: self.root_seed[..20].to_vec(),
            compose_hash: Keccak256::digest(
                [b"mock-compose:".as_ref(), self.root_seed.as_slice()].concat(),
            )
            .to_vec(),
            instance_id: Keccak256::digest(
                [b"mock-instance:".as_ref(), self.root_seed.as_slice()].concat(),
            )[..20]
                .to_vec(),
            device_id: Keccak256::digest(b"mock-device").to_vec(),
            tcb_status: "UpToDate".to_string(),
        })
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
        // dstack 0.5.x exposes only /GetKey (the /DeriveKey endpoint was removed). It
        // returns 32 bytes of HKDF-derived key material for (path, purpose) plus a KMS
        // signature chain we ignore here — derive_key wants only the seed bytes.
        let body = serde_json::json!({ "path": purpose, "purpose": subkey });
        let resp = self.request("/GetKey", &body).await?;
        // On the error path the response carries no key, so it is safe to log it verbatim
        // to pinpoint a guest-agent/API mismatch on a live CVM (deploy logging directive).
        let key_hex = resp.get("key").and_then(|v| v.as_str()).with_context(|| {
            format!("/GetKey response missing 'key' (path={purpose}, purpose={subkey}): {resp}")
        })?;
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

    async fn get_key(&self, path: &str, purpose: &str) -> Result<DstackKey> {
        let body = serde_json::json!({ "path": path, "purpose": purpose });
        let resp = self.request("/GetKey", &body).await?;
        // Error path has no key → safe to log the whole response.
        let key_hex = resp.get("key").and_then(|v| v.as_str()).with_context(|| {
            format!("GetKey: missing 'key' (path={path}, purpose={purpose}): {resp}")
        })?;
        let key = hex::decode(key_hex.trim_start_matches("0x")).context("GetKey: bad key hex")?;
        // The key IS present here, so never log the full response — only its field names.
        let signature_chain = resp
            .get("signature_chain")
            .and_then(|v| v.as_array())
            .with_context(|| {
                let fields: Vec<&String> = resp
                    .as_object()
                    .map(|o| o.keys().collect())
                    .unwrap_or_default();
                format!("GetKey: missing 'signature_chain'; response fields = {fields:?}")
            })?
            .iter()
            .map(|s| {
                let h = s.as_str().context("signature_chain entry not a string")?;
                hex::decode(h.trim_start_matches("0x")).context("signature_chain: bad hex")
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(DstackKey {
            key,
            signature_chain,
        })
    }

    async fn info(&self) -> Result<DstackInfo> {
        let resp = self.request("/Info", &serde_json::json!({})).await?;
        // Strict hex field: the field must be present and valid hex, otherwise the
        // whole `/Info` is rejected. Silently defaulting a missing/garbled field to
        // empty bytes would let a malformed guest-agent response masquerade as a
        // valid CVM identity (and produce an all-zero code_id downstream).
        let hexf = |k: &str| -> Result<Vec<u8>> {
            let s = resp
                .get(k)
                .and_then(|v| v.as_str())
                .with_context(|| format!("/Info missing string field '{k}'"))?;
            hex::decode(s.trim_start_matches("0x"))
                .with_context(|| format!("/Info field '{k}' is not valid hex"))
        };
        let app_id = hexf("app_id")?;
        ensure!(
            app_id.len() == 20,
            "/Info app_id is {} bytes, expected 20 bytes",
            app_id.len()
        );
        let compose_hash = hexf("compose_hash")?;
        ensure!(
            compose_hash.len() == 32,
            "/Info compose_hash is {} bytes, expected 32 bytes",
            compose_hash.len()
        );
        let instance_id = hexf("instance_id")?;
        ensure!(
            instance_id.len() == 20,
            "/Info instance_id is {} bytes, expected 20 bytes",
            instance_id.len()
        );
        let device_id = hexf("device_id")?;
        ensure!(
            device_id.len() == 32,
            "/Info device_id is {} bytes, expected 32 bytes",
            device_id.len()
        );
        let tcb_status = resp
            .get("tcb_status")
            .and_then(|v| v.as_str())
            .context("/Info missing 'tcb_status'")?
            .to_string();
        ensure!(!tcb_status.trim().is_empty(), "/Info tcb_status is empty");
        Ok(DstackInfo {
            app_id,
            compose_hash,
            instance_id,
            device_id,
            tcb_status,
        })
    }
}

impl UnixSocketDstack {
    async fn request(&self, path: &str, body: &serde_json::Value) -> Result<serde_json::Value> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let exchange = async {
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

            // Some dstack 0.5.x guest agents keep the UDS HTTP connection alive even
            // when the request says `Connection: close`. Waiting for EOF therefore
            // wedges first-boot key derivation forever. Read until Content-Length is
            // satisfied (or EOF for older agents) and bound the whole exchange.
            let mut resp = Vec::new();
            let mut buf = [0u8; 4096];
            let mut expected_total = None;
            loop {
                let n = stream.read(&mut buf).await?;
                if n == 0 {
                    break;
                }
                resp.extend_from_slice(&buf[..n]);
                if expected_total.is_none() {
                    if let Some(header_end) = resp.windows(4).position(|w| w == b"\r\n\r\n") {
                        let headers = std::str::from_utf8(&resp[..header_end])?;
                        let content_length = headers.lines().find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())
                                .flatten()
                        });
                        expected_total = content_length.map(|len| header_end + 4 + len);
                    }
                }
                if expected_total.is_some_and(|total| resp.len() >= total) {
                    break;
                }
            }
            Ok::<Vec<u8>, anyhow::Error>(resp)
        };
        let resp = tokio::time::timeout(std::time::Duration::from_secs(20), exchange)
            .await
            .with_context(|| format!("dstack request {path} timed out"))??;
        let header_end = resp
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .context("malformed dstack response")?;
        // Reject non-2xx responses instead of parsing an error body as if it were a
        // valid reply. The status line is `HTTP/1.1 <code> <reason>`; a guest-agent
        // 4xx/5xx must surface as an error, not a silently-empty `/Info` or a missing
        // quote that later reads as "successful".
        let headers =
            std::str::from_utf8(&resp[..header_end]).context("non-utf8 dstack headers")?;
        let status_line = headers.lines().next().context("empty dstack response")?;
        let status_code: u16 = status_line
            .split_whitespace()
            .nth(1)
            .and_then(|c| c.parse().ok())
            .with_context(|| format!("malformed dstack status line: {status_line:?}"))?;
        anyhow::ensure!(
            (200..300).contains(&status_code),
            "dstack {path} returned HTTP {status_code}"
        );
        let body_start = header_end + 4;
        Ok(serde_json::from_slice(&resp[body_start..])?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn mock_get_key_and_info_shapes() {
        // Validates the dstack /GetKey + /Info shapes the registration proof is built
        // from (signature_chain = [app_sig, kms_sig]; app_id is a 20-byte address).
        let d = MockDstack::from_label("cvm-1");
        let k = d.get_key("attestmesh", "ethereum").await.unwrap();
        assert_eq!(
            k.signature_chain.len(),
            2,
            "signature_chain = [app_sig, kms_sig]"
        );
        assert_eq!(k.key.len(), 32);
        assert!(!k.signature_chain[0].is_empty() && !k.signature_chain[1].is_empty());
        // Deterministic per CVM state (same seed → same key).
        assert_eq!(
            k.key,
            d.get_key("attestmesh", "ethereum").await.unwrap().key
        );

        let info = d.info().await.unwrap();
        assert_eq!(info.app_id.len(), 20, "app_id is a 20-byte address");
        assert_eq!(info.compose_hash.len(), 32, "compose_hash is bytes32");
        assert_eq!(info.instance_id.len(), 20, "instance_id is an address");
        assert_eq!(info.device_id.len(), 32, "device_id is bytes32");
        assert_eq!(info.tcb_status, "UpToDate");
    }

    fn http_response(status: &str, body: &str) -> String {
        format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    }

    async fn spawn_uds_response(
        response: String,
    ) -> (tempfile::TempDir, String, tokio::task::JoinHandle<()>) {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("agent.sock");
        let listener = tokio::net::UnixListener::bind(&socket).unwrap();
        let socket_path = socket.to_string_lossy().into_owned();
        let task = tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 4096];
            let _ = stream.read(&mut buf).await.unwrap();
            stream.write_all(response.as_bytes()).await.unwrap();
        });
        (dir, socket_path, task)
    }

    /// Issue [P2]: a guest-agent HTTP error body must not be parsed as successful
    /// `/Info` evidence. `request()` rejects non-2xx before JSON field parsing.
    #[tokio::test]
    async fn unix_info_rejects_http_error_status() {
        let (_dir, socket_path, task) = spawn_uds_response(http_response(
            "500 Internal Server Error",
            "{\"error\":\"bad\"}",
        ))
        .await;
        let d = UnixSocketDstack::new(socket_path);

        let err = d.info().await.unwrap_err().to_string();
        assert!(err.contains("HTTP 500"), "unexpected error: {err}");
        task.await.unwrap();
    }

    /// Issue [P2]: present-but-malformed `/Info` fields must fail. In particular,
    /// fixed-size identity fields are length-checked instead of defaulting to empty
    /// vectors and later producing a zero-padded `code_id`.
    #[tokio::test]
    async fn unix_info_rejects_wrong_field_lengths() {
        let body = serde_json::json!({
            "app_id": format!("0x{}", hex::encode([0x11u8; 20])),
            "compose_hash": "0x01",
            "instance_id": format!("0x{}", hex::encode([0x22u8; 20])),
            "device_id": format!("0x{}", hex::encode([0x33u8; 32])),
            "tcb_status": "UpToDate",
        })
        .to_string();
        let (_dir, socket_path, task) = spawn_uds_response(http_response("200 OK", &body)).await;
        let d = UnixSocketDstack::new(socket_path);

        let err = d.info().await.unwrap_err().to_string();
        assert!(err.contains("compose_hash"), "unexpected error: {err}");
        assert!(err.contains("expected 32 bytes"), "unexpected error: {err}");
        task.await.unwrap();
    }
}
