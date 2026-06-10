//! `OperatorProvider` — the operator-signature attestation method (multi-attestor
//! spec).
//!
//! ╔════════════════════════════════════════════════════════════════════════╗
//! ║ TRUST DISCLOSURE: this method is NOT hardware attestation. The node's   ║
//! ║ keys derive from a local seed file readable by anyone with host access, ║
//! ║ and admission rests entirely on an allowlisted operator's signature     ║
//! ║ (the OPERATOR_VOUCHER). Use for dev/test clusters and operator-vouched  ║
//! ║ non-TEE nodes only.                                                     ║
//! ╚════════════════════════════════════════════════════════════════════════╝
//!
//! Keys derive from `ATTESTOR_SEED_PATH` (hex, generated on first boot, mode
//! 0600) under the same AttestMesh purpose strings as every other method. The
//! registration voucher comes from `OPERATOR_VOUCHER` (hex CBOR or JSON), minted
//! by the `mesh-voucher` CLI — which creates seed + voucher together at provision
//! time, or signs over pubkeys taken from a node's first-boot log.

use super::{AttestationProvider, RegisterCall};
use crate::chain::abi;
use crate::config::Config;
use crate::keys::{self, KeyMaterial, PURPOSE_BINDING, PURPOSE_IDENTITY, PURPOSE_WIREGUARD};
use alloy::primitives::{keccak256, Address, Bytes, B256};
use alloy::signers::local::PrivateKeySigner;
use alloy::sol_types::{SolCall, SolValue};
use anyhow::{bail, ensure, Context, Result};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use sha3::{Digest, Keccak256};
use zeroize::Zeroizing;

/// Must match OperatorFacet.OPERATOR_BIND_DOMAIN on chain.
pub const OPERATOR_BIND_DOMAIN: &str = "attestmesh.operator.bind.v1";
pub const OPERATOR_ATTESTOR_ID: &[u8] = b"attestmesh.attestor.operator";

/// The pre-signed registration voucher (`OPERATOR_VOUCHER`). All byte fields are
/// 0x-hex strings so the same shape round-trips through JSON and CBOR and stays
/// human-debuggable. Not secret: it is useless without the node's seed.
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct OperatorVoucher {
    pub cluster: String,
    pub member_contract: String,
    pub x_pub: String,
    pub wg_pub: String,
    pub signer: String,
    pub owner_key: String,
    pub expiry: u64,
    pub signature: String,
}

/// `keccak256(abi.encode("attestmesh.operator.bind.v1", cluster, member, xPubKey,
/// wgPubKey, ownerKey, expiry))` — the preimage the operator signs (EIP-191) and
/// OperatorFacet recovers.
pub fn operator_bind_hash(
    cluster: Address,
    member: Address,
    x_pub: B256,
    wg_pub: B256,
    owner_key: Address,
    expiry: u64,
) -> B256 {
    let enc = (
        OPERATOR_BIND_DOMAIN.to_string(),
        cluster,
        member,
        x_pub,
        wg_pub,
        owner_key,
        expiry,
    )
        .abi_encode_params();
    keccak256(enc)
}

/// Parse `OPERATOR_VOUCHER`: JSON when it looks like an object, hex CBOR otherwise.
pub fn parse_voucher(raw: &str) -> Result<OperatorVoucher> {
    let raw = raw.trim();
    if raw.starts_with('{') {
        return serde_json::from_str(raw).context("OPERATOR_VOUCHER: bad JSON");
    }
    let bytes = hex::decode(raw.trim_start_matches("0x"))
        .context("OPERATOR_VOUCHER: not JSON and not hex")?;
    ciborium::from_reader(bytes.as_slice()).context("OPERATOR_VOUCHER: bad CBOR")
}

/// Encode a voucher as the hex-CBOR form `mesh-voucher` emits.
pub fn encode_voucher_hex(v: &OperatorVoucher) -> Result<String> {
    let mut buf = Vec::new();
    ciborium::into_writer(v, &mut buf).context("encode voucher CBOR")?;
    Ok(hex::encode(buf))
}

fn parse_addr(label: &str, s: &str) -> Result<Address> {
    s.trim()
        .parse()
        .with_context(|| format!("voucher {label}: bad address {s:?}"))
}

fn parse_b256(label: &str, s: &str) -> Result<B256> {
    s.trim()
        .parse()
        .with_context(|| format!("voucher {label}: bad 32-byte hex {s:?}"))
}

// ── seed-file key derivation (shared with the mesh-voucher CLI) ──────────────

/// Domain-separated subseed: keccak256("attestmesh.operator.derive.v1" || seed ||
/// len(purpose) || purpose). Mirrors the dstack mock's shape; purposes are the
/// AttestMesh-scoped strings from keys.rs.
fn subseed(master: &[u8; 32], purpose: &str) -> Zeroizing<[u8; 32]> {
    let mut hasher = Keccak256::new();
    hasher.update(b"attestmesh.operator.derive.v1");
    hasher.update(master);
    hasher.update((purpose.len() as u32).to_be_bytes());
    hasher.update(purpose.as_bytes());
    let mut out = [0u8; 32];
    out.copy_from_slice(&hasher.finalize());
    Zeroizing::new(out)
}

/// The full identity key set from a master seed. Deterministic: the same seed
/// yields the same keys on every boot (and inside `mesh-voucher`).
pub fn key_material_from_seed(master: &[u8; 32]) -> KeyMaterial {
    keys::from_seeds(
        &subseed(master, PURPOSE_IDENTITY),
        &subseed(master, PURPOSE_WIREGUARD),
        &subseed(master, PURPOSE_BINDING),
    )
}

/// The secp256k1 signer derived from the seed's binding purpose: the bootstrap
/// signer, the voucher's `owner_key`, and the member's 4337 owner.
pub fn owner_signer_from_seed(master: &[u8; 32]) -> Result<PrivateKeySigner> {
    PrivateKeySigner::from_slice(subseed(master, PURPOSE_BINDING).as_slice())
        .context("binding subseed is not a valid secp256k1 scalar (regenerate the seed)")
}

/// Read the hex seed at `path`, or generate one (mode 0600) on first boot.
pub fn load_or_create_seed(path: &str) -> Result<[u8; 32]> {
    if std::path::Path::new(path).exists() {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("read ATTESTOR_SEED_PATH {path}"))?;
        let bytes = hex::decode(text.trim().trim_start_matches("0x"))
            .with_context(|| format!("ATTESTOR_SEED_PATH {path}: not hex"))?;
        ensure!(
            bytes.len() == 32,
            "ATTESTOR_SEED_PATH {path}: want 32 bytes, got {}",
            bytes.len()
        );
        let mut seed = [0u8; 32];
        seed.copy_from_slice(&bytes);
        return Ok(seed);
    }

    let mut seed = [0u8; 32];
    rand::RngCore::fill_bytes(&mut rand::rngs::OsRng, &mut seed);
    write_seed_file(path, &seed)?;
    tracing::warn!(
        path,
        "generated a NEW operator seed (first boot); mint the \
        OPERATOR_VOUCHER against this node's pubkeys"
    );
    Ok(seed)
}

/// Write the seed hex-encoded with owner-only permissions (0600).
pub fn write_seed_file(path: &str, seed: &[u8; 32]) -> Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    if let Some(dir) = std::path::Path::new(path).parent() {
        std::fs::create_dir_all(dir).with_context(|| format!("create seed dir {dir:?}"))?;
    }
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("create seed file {path}"))?;
    f.write_all(hex::encode(seed).as_bytes())?;
    Ok(())
}

// ── provider ──────────────────────────────────────────────────────────────────

pub struct OperatorProvider {
    seed: Zeroizing<[u8; 32]>,
    voucher_raw: Option<String>,
}

impl OperatorProvider {
    pub fn from_config(config: &Config) -> Result<Self> {
        tracing::warn!("════════════════════════════════════════════════════════════════════");
        tracing::warn!(
            "ATTESTOR=operator: this node is NOT hardware-attested. Its keys come \
             from a local seed file ({}), and cluster admission rests on an \
             allowlisted operator's signature alone.",
            config.attestor_seed_path
        );
        tracing::warn!("════════════════════════════════════════════════════════════════════");
        let seed = load_or_create_seed(&config.attestor_seed_path)?;
        let provider = Self {
            seed: Zeroizing::new(seed),
            voucher_raw: config.operator_voucher.clone(),
        };
        // First-boot affordance: log the pubkeys an externally-minted voucher
        // must sign over (mesh-voucher --x-pub/--wg-pub/--owner flow).
        let km = key_material_from_seed(&provider.seed);
        let owner = owner_signer_from_seed(&provider.seed)?;
        tracing::info!(
            x_pub = %hex::encode(km.x_pub),
            wg_pub = %hex::encode(km.wg_pub),
            owner_key = %owner.address(),
            "operator-method identity (voucher must bind these)"
        );
        Ok(provider)
    }

    fn voucher(&self) -> Result<OperatorVoucher> {
        let Some(raw) = &self.voucher_raw else {
            bail!(
                "OPERATOR_VOUCHER is not set — mint one with mesh-voucher (it must \
                 sign over this node's pubkeys, logged at boot) and restart"
            );
        };
        parse_voucher(raw)
    }
}

#[async_trait]
impl AttestationProvider for OperatorProvider {
    fn attestor_id(&self) -> [u8; 32] {
        keccak256(OPERATOR_ATTESTOR_ID).0
    }

    async fn derive_keys(&self) -> Result<KeyMaterial> {
        Ok(key_material_from_seed(&self.seed))
    }

    async fn build_register_call(
        &self,
        cluster: Address,
        member: Address,
        x_pub: [u8; 32],
        wg_pub: [u8; 32],
    ) -> Result<RegisterCall> {
        let v = self.voucher()?;
        let signer_addr = parse_addr("signer", &v.signer)?;
        let owner_key = parse_addr("owner_key", &v.owner_key)?;
        let v_cluster = parse_addr("cluster", &v.cluster)?;
        let v_member = parse_addr("member_contract", &v.member_contract)?;
        let v_x_pub = parse_b256("x_pub", &v.x_pub)?;
        let v_wg_pub = parse_b256("wg_pub", &v.wg_pub)?;

        // The voucher binds (cluster, member, xPub, wgPub, ownerKey, expiry); any
        // mismatch with what this node is about to register guarantees an on-chain
        // BindingMismatch, so fail fast with a useful message instead.
        ensure!(
            v_cluster == cluster,
            "voucher cluster {v_cluster} != ours {cluster}"
        );
        ensure!(
            v_member == member,
            "voucher member {v_member} != ours {member}"
        );
        ensure!(
            v_x_pub == B256::from(x_pub),
            "voucher x_pub does not match our derived key"
        );
        ensure!(
            v_wg_pub == B256::from(wg_pub),
            "voucher wg_pub does not match our derived key"
        );

        // The bootstrap UserOp must be signed by the voucher's ownerKey
        // (ClusterMember validates exactly that in owner-unset mode).
        let signer = owner_signer_from_seed(&self.seed)?;
        ensure!(
            owner_key == signer.address(),
            "voucher owner_key {owner_key} is not this seed's owner {} — voucher \
             was minted for a different node",
            signer.address()
        );

        let signature = hex::decode(v.signature.trim().trim_start_matches("0x"))
            .context("voucher signature: bad hex")?;
        let call = abi::operator_registerCall {
            proof: abi::OperatorProof {
                signer: signer_addr,
                ownerKey: owner_key,
                expiry: v.expiry,
                signature: Bytes::from(signature),
            },
            memberContract: member,
            xPubKey: B256::from(x_pub),
            wgPubKey: B256::from(wg_pub),
        };
        Ok(RegisterCall {
            calldata: Bytes::from(call.abi_encode()),
            signer,
        })
    }

    async fn owner_signer(&self) -> Result<PrivateKeySigner> {
        owner_signer_from_seed(&self.seed)
    }

    async fn self_member_contract(&self) -> Result<Address> {
        bail!(
            "MEMBER_CONTRACT is required for ATTESTOR=operator (only dstack Path A \
             can self-discover its member contract from the CVM app_id)"
        )
    }

    fn supports_csk_origination(&self) -> bool {
        false // v1 gates CSK origination to dstack (KMS-derived; spec CSK note)
    }

    async fn derive_csk_originator(&self) -> Result<Zeroizing<[u8; 32]>> {
        bail!(
            "an operator-admitted node must not originate the CSK in v1 — it can \
             only pull it from a dstack peer (multi-attestor spec, CSK note)"
        )
    }

    async fn csk_seal(&self, _csk: &[u8; 32]) -> Result<()> {
        // No sealed store without a TEE; the node re-pulls the CSK after restart.
        tracing::debug!("operator method has no sealed store; CSK will be re-pulled on restart");
        Ok(())
    }

    async fn csk_unseal(&self) -> Result<Option<Zeroizing<[u8; 32]>>> {
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloy::primitives::address;
    use alloy::signers::SignerSync;

    fn seeded_provider(dir: &tempfile::TempDir, voucher: Option<String>) -> OperatorProvider {
        let path = dir.path().join("seed").to_str().unwrap().to_string();
        let seed = load_or_create_seed(&path).unwrap();
        OperatorProvider {
            seed: Zeroizing::new(seed),
            voucher_raw: voucher,
        }
    }

    #[test]
    fn seed_file_round_trips_and_is_0600() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("seed").to_str().unwrap().to_string();

        let s1 = load_or_create_seed(&path).unwrap();
        let s2 = load_or_create_seed(&path).unwrap();
        assert_eq!(s1, s2, "restart must re-load the same seed");

        let mode = std::fs::metadata(&path).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600, "seed file must be owner-only");
    }

    #[test]
    fn keys_are_deterministic_per_seed_and_distinct() {
        let k1 = key_material_from_seed(&[7u8; 32]);
        let k1b = key_material_from_seed(&[7u8; 32]);
        assert_eq!(k1.x_pub, k1b.x_pub);
        assert_eq!(k1.wg_pub, k1b.wg_pub);
        assert_eq!(k1.ed25519_pub, k1b.ed25519_pub);
        assert_ne!(k1.x_pub, k1.wg_pub, "purpose separation");

        let k2 = key_material_from_seed(&[8u8; 32]);
        assert_ne!(k1.x_pub, k2.x_pub, "different seed → different keys");
    }

    #[test]
    fn voucher_parses_from_json_and_hex_cbor() {
        let v = OperatorVoucher {
            cluster: "0x000000000000000000000000000000000000000a".into(),
            member_contract: "0x000000000000000000000000000000000000000b".into(),
            x_pub: format!("0x{}", hex::encode([0x11u8; 32])),
            wg_pub: format!("0x{}", hex::encode([0x22u8; 32])),
            signer: "0x000000000000000000000000000000000000000c".into(),
            owner_key: "0x000000000000000000000000000000000000000d".into(),
            expiry: 1234567890,
            signature: format!("0x{}", hex::encode([0x33u8; 65])),
        };
        let json = serde_json::to_string(&v).unwrap();
        let from_json = parse_voucher(&json).unwrap();
        assert_eq!(from_json.expiry, v.expiry);

        let hex_cbor = encode_voucher_hex(&v).unwrap();
        let from_cbor = parse_voucher(&hex_cbor).unwrap();
        assert_eq!(from_cbor.owner_key, v.owner_key);
        assert_eq!(from_cbor.signature, v.signature);
    }

    /// The bind hash must be exactly Solidity's
    /// keccak256(abi.encode(string,address,address,bytes32,bytes32,address,uint64)).
    /// Vector cross-checked with `cast abi-encode` + `cast keccak` (and the
    /// OperatorFacet preimage on chain).
    #[test]
    fn operator_bind_hash_matches_solidity() {
        let h1 = operator_bind_hash(
            address!("000000000000000000000000000000000000000a"),
            address!("000000000000000000000000000000000000000b"),
            B256::repeat_byte(0x11),
            B256::repeat_byte(0x22),
            address!("000000000000000000000000000000000000000d"),
            1000,
        );
        assert_eq!(
            h1,
            "0x07676eaf539da47f4192973b1183aadfe5284aca19cf536153de084bea91ab2f"
                .parse::<B256>()
                .unwrap()
        );
        let h2 = operator_bind_hash(
            address!("000000000000000000000000000000000000000a"),
            address!("000000000000000000000000000000000000000b"),
            B256::repeat_byte(0x11),
            B256::repeat_byte(0x22),
            address!("000000000000000000000000000000000000000d"),
            1001,
        );
        assert_ne!(h1, h2, "expiry is part of the binding");
    }

    #[tokio::test]
    async fn build_register_call_validates_and_encodes() {
        use alloy::sol_types::SolCall;

        let dir = tempfile::tempdir().unwrap();
        let cluster = address!("000000000000000000000000000000000000000a");

        // Mint a consistent voucher for this provider's seed (what mesh-voucher does).
        let mut p = seeded_provider(&dir, None);
        let km = key_material_from_seed(&p.seed);
        let owner = owner_signer_from_seed(&p.seed).unwrap();
        let member = address!("000000000000000000000000000000000000000b");
        let op_signer = PrivateKeySigner::from_slice(&[0x42u8; 32]).unwrap();
        let expiry = 4_000_000_000u64;
        let bind = operator_bind_hash(
            cluster,
            member,
            B256::from(km.x_pub),
            B256::from(km.wg_pub),
            owner.address(),
            expiry,
        );
        let sig = op_signer.sign_message_sync(bind.as_slice()).unwrap();
        let voucher = OperatorVoucher {
            cluster: cluster.to_string(),
            member_contract: member.to_string(),
            x_pub: format!("0x{}", hex::encode(km.x_pub)),
            wg_pub: format!("0x{}", hex::encode(km.wg_pub)),
            signer: op_signer.address().to_string(),
            owner_key: owner.address().to_string(),
            expiry,
            signature: format!("0x{}", hex::encode(sig.as_bytes())),
        };
        p.voucher_raw = Some(encode_voucher_hex(&voucher).unwrap());

        let call = p
            .build_register_call(cluster, member, km.x_pub, km.wg_pub)
            .await
            .unwrap();
        assert_eq!(
            &call.calldata[..4],
            abi::operator_registerCall::SELECTOR.as_slice(),
            "inner call must be operator_register"
        );
        assert_eq!(
            call.signer.address(),
            owner.address(),
            "bootstrap signer is the voucher's ownerKey"
        );

        // A voucher for different keys is rejected before it can hit the chain.
        let err = p
            .build_register_call(cluster, member, [9u8; 32], km.wg_pub)
            .await
            .unwrap_err();
        assert!(err.to_string().contains("x_pub"), "got: {err}");

        // Wrong cluster likewise.
        let other = address!("00000000000000000000000000000000000000ff");
        let err = p
            .build_register_call(other, member, km.x_pub, km.wg_pub)
            .await
            .unwrap_err();
        assert!(err.to_string().contains("cluster"), "got: {err}");
    }

    #[tokio::test]
    async fn operator_provider_gates_csk_and_self_discovery() {
        let dir = tempfile::tempdir().unwrap();
        let p = seeded_provider(&dir, None);
        assert!(!p.supports_csk_origination());
        assert!(p.derive_csk_originator().await.is_err());
        assert!(p.csk_unseal().await.unwrap().is_none(), "no sealed store");
        p.csk_seal(&[1u8; 32]).await.unwrap(); // no-op, never errors
        assert!(
            p.csk_unseal().await.unwrap().is_none(),
            "seal is a documented no-op"
        );
        assert!(
            p.self_member_contract().await.is_err(),
            "MEMBER_CONTRACT required"
        );
        assert_eq!(
            p.attestor_id(),
            keccak256(b"attestmesh.attestor.operator").0
        );
    }
}
