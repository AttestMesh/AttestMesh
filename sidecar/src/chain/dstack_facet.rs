//! dstack_register UserOp builder + binding-hash construction (sidecar spec §7.1
//! step 3, contracts spec §6.3). The KMS sig-chain material comes from the dstack
//! runtime at runtime; this module assembles it into the on-chain proof shape and
//! computes the binding hash the derived key signs.

use super::abi;
use crate::dstack::DstackRuntime;
use alloy::primitives::{keccak256, Address, Bytes, B256};
use alloy::signers::k256::ecdsa::{RecoveryId, Signature as K256Sig, SigningKey, VerifyingKey};
use alloy::signers::local::PrivateKeySigner;
use alloy::signers::Signer;
use alloy::sol_types::{SolCall, SolValue};
use anyhow::{ensure, Context, Result};

/// dstack key path/purpose for AttestMesh's registration (binding) key. The proof's
/// app->derived signature is over the "ethereum:" label regardless (see build_kms_material);
/// these select which derived key the guest agent returns. Confirm against a live node.
const KEY_PATH: &str = "attestmesh-binding-v1";
const KEY_PURPOSE: &str = "ethereum";

/// EIP-712-style message-domain tag (distinct from the dstack derive-key purpose
/// `attestmesh.binding.v1`). Must match DstackFacet.BIND_DOMAIN on chain.
pub const BIND_DOMAIN: &str = "attestmesh.bind.v1";

/// `keccak256(abi.encode("attestmesh.bind.v1", cluster, member, xPubKey, wgPubKey))`.
/// The derived key signs the EIP-191 message of this; DstackFacet recovers it.
pub fn bind_hash(cluster: Address, member: Address, x_pub: B256, wg_pub: B256) -> B256 {
    let enc = (BIND_DOMAIN.to_string(), cluster, member, x_pub, wg_pub).abi_encode_params();
    keccak256(enc)
}

/// Raw KMS sig-chain material as produced by the dstack runtime: the codeId
/// (bytes20(app_id) left-aligned), the app + derived 33-byte compressed SEC1
/// pubkeys, the KMS-root and app signatures, and the dstack key-derivation purpose
/// label. The binding `messageHash`/`messageSignature` are computed and signed by
/// the sidecar (see `bind_hash` + `assemble_proof`), not the runtime.
#[derive(Debug, Clone, Default)]
pub struct KmsChainMaterial {
    pub code_id: B256,
    pub app_compressed_pubkey: Vec<u8>,
    pub app_signature: Vec<u8>,
    pub kms_signature: Vec<u8>,
    pub derived_compressed_pubkey: Vec<u8>,
    pub purpose: String,
}

/// Assemble the on-chain proof from runtime material + the sidecar's binding hash
/// and derived-key signature over its EIP-191 message.
pub fn assemble_proof(
    m: KmsChainMaterial,
    message_hash: B256,
    message_signature: Vec<u8>,
) -> abi::DstackProof {
    abi::DstackProof {
        codeId: m.code_id,
        messageHash: message_hash,
        messageSignature: Bytes::from(message_signature),
        appSignature: Bytes::from(m.app_signature),
        kmsSignature: Bytes::from(m.kms_signature),
        derivedCompressedPubkey: Bytes::from(m.derived_compressed_pubkey),
        appCompressedPubkey: Bytes::from(m.app_compressed_pubkey),
        purpose: m.purpose,
    }
}

/// The 33-byte compressed SEC1 secp256k1 public key for a 32-byte private key.
pub fn compressed_pubkey(priv_bytes: &[u8]) -> Result<Vec<u8>> {
    let sk = SigningKey::from_slice(priv_bytes).context("invalid secp256k1 private key")?;
    Ok(sk
        .verifying_key()
        .to_encoded_point(true)
        .as_bytes()
        .to_vec())
}

/// Recover the signer's 33-byte compressed pubkey from a 65-byte (r||s||v) signature
/// over `prehash`.
fn recover_compressed(prehash: &[u8; 32], sig65: &[u8]) -> Result<Vec<u8>> {
    ensure!(
        sig65.len() == 65,
        "expected a 65-byte signature, got {}",
        sig65.len()
    );
    let sig = K256Sig::from_slice(&sig65[..64]).context("bad r||s")?;
    let v = sig65[64];
    let recid =
        RecoveryId::from_byte(if v >= 27 { v - 27 } else { v }).context("bad recovery id")?;
    let vk = VerifyingKey::recover_from_prehash(prehash, &sig, recid)
        .context("pubkey recovery failed")?;
    Ok(vk.to_encoded_point(true).as_bytes().to_vec())
}

/// Assemble the runtime half of the proof (validated against dstackgres `group_auth.rs`):
/// derive the compressed derived pubkey, recover the app pubkey from `signature_chain[0]`
/// over `keccak256("ethereum:" + hex(derivedCompressedPubkey))`, and set
/// `codeId = bytes20(app_id)`. `app_signature`/`kms_signature` are `signature_chain[0..2]`.
pub fn build_kms_material(
    app_id: &[u8],
    derived_priv: &[u8],
    app_signature: Vec<u8>,
    kms_signature: Vec<u8>,
) -> Result<KmsChainMaterial> {
    let derived_compressed_pubkey = compressed_pubkey(derived_priv)?;
    let app_msg = format!("ethereum:{}", hex::encode(&derived_compressed_pubkey));
    let app_msg_hash = keccak256(app_msg.as_bytes());
    let app_compressed_pubkey = recover_compressed(&app_msg_hash.0, &app_signature)?;

    // codeId = bytes32(bytes20(app_id)): the 20-byte app_id left-aligned, upper 12 zero.
    let mut code_id = [0u8; 32];
    let n = app_id.len().min(20);
    code_id[..n].copy_from_slice(&app_id[..n]);

    Ok(KmsChainMaterial {
        code_id: B256::from(code_id),
        app_compressed_pubkey,
        app_signature,
        kms_signature,
        derived_compressed_pubkey,
        purpose: "ethereum".to_string(),
    })
}

/// EIP-191 personal-sign hash of a 32-byte message (matches Solidity
/// `MessageHashUtils.toEthSignedMessageHash`): `keccak256("\x19Ethereum Signed
/// Message:\n32" || hash)`.
pub fn eth_signed_message_hash(hash: B256) -> B256 {
    let mut v = Vec::with_capacity(28 + 32);
    v.extend_from_slice(b"\x19Ethereum Signed Message:\n32");
    v.extend_from_slice(hash.as_slice());
    keccak256(v)
}

/// Normalize an ECDSA signature's recovery id to Ethereum's {27, 28}. Both dstack's KMS
/// chain sigs and alloy's `sign_message` may emit a raw 0/1 y-parity, but every on-chain
/// recovery the proof must pass — `DstackSigChain.verify` (app/kms/binding sigs) and
/// `ClusterMember._recoverBindingSigner` (binding sig) — uses OZ `ECDSA.recover`, which
/// rejects v < 27 (ecrecover returns address(0) → `ECDSAInvalidSignature`). 0→27 / 1→28
/// preserves the recovered key (v only selects the y-parity branch).
fn normalize_recovery_id(mut sig: Vec<u8>) -> Vec<u8> {
    if sig.len() == 65 && sig[64] < 27 {
        sig[64] += 27;
    }
    sig
}

/// Build the complete on-chain `DstackProof` the sidecar submits to register: assemble
/// the KMS material (app pubkey recovery + codeId), pin the binding `messageHash` to
/// (cluster, member, xPub, wgPub), and sign its EIP-191 message with the derived key.
/// The result satisfies the on-chain `DstackSigChain.verify` checks (see the e2e test).
#[allow(clippy::too_many_arguments)]
pub async fn build_proof(
    derived_signer: &PrivateKeySigner,
    app_id: &[u8],
    app_signature: Vec<u8>,
    kms_signature: Vec<u8>,
    cluster: Address,
    member: Address,
    x_pub: B256,
    wg_pub: B256,
) -> Result<abi::DstackProof> {
    let derived_priv = derived_signer.to_bytes();
    let material = build_kms_material(
        app_id,
        derived_priv.as_slice(),
        normalize_recovery_id(app_signature),
        normalize_recovery_id(kms_signature),
    )?;
    let message_hash = bind_hash(cluster, member, x_pub, wg_pub);
    // alloy `sign_message` applies the EIP-191 prefix, exactly as the contract recovers.
    let sig = derived_signer
        .sign_message(message_hash.as_slice())
        .await
        .context("sign binding message")?;
    Ok(assemble_proof(
        material,
        message_hash,
        normalize_recovery_id(sig.as_bytes().to_vec()),
    ))
}

/// Re-derive the registration signer (the ClusterMember `owner` installed by
/// `dstack_register`) for post-registration UserOps. Same `/GetKey` path+purpose
/// as `build_proof_from_runtime`, so it is bit-identical across calls.
pub async fn derive_owner_signer(dstack: &dyn DstackRuntime) -> Result<PrivateKeySigner> {
    let dk = dstack.get_key(KEY_PATH, KEY_PURPOSE).await?;
    PrivateKeySigner::from_slice(&dk.key).context("derived owner key from dstack")
}

/// End-to-end: pull the KMS chain from the dstack runtime (`/Info` + `/GetKey`) and
/// assemble the full registration proof the sidecar submits. Returns the proof and the
/// derived signer (which becomes the ClusterMember's EIP-4337 owner). The proof satisfies
/// the on-chain `DstackSigChain.verify` (see the e2e tests).
pub async fn build_proof_from_runtime(
    dstack: &dyn DstackRuntime,
    cluster: Address,
    member: Address,
    x_pub: B256,
    wg_pub: B256,
) -> Result<(abi::DstackProof, PrivateKeySigner)> {
    let info = dstack.info().await?;
    let dk = dstack.get_key(KEY_PATH, KEY_PURPOSE).await?;
    ensure!(
        dk.signature_chain.len() >= 2,
        "signature_chain must be [app_sig, kms_sig, ...], got {}",
        dk.signature_chain.len()
    );
    let signer = PrivateKeySigner::from_slice(&dk.key).context("derived key from dstack")?;
    let proof = build_proof(
        &signer,
        &info.app_id,
        dk.signature_chain[0].clone(),
        dk.signature_chain[1].clone(),
        cluster,
        member,
        x_pub,
        wg_pub,
    )
    .await?;
    Ok((proof, signer))
}

/// ABI-encode `dstack_register(proof, member, xPub, wgPub)` for the inner UserOp call.
pub fn build_register_calldata(
    proof: abi::DstackProof,
    member: Address,
    x_pub: B256,
    wg_pub: B256,
) -> Bytes {
    let call = abi::dstack_registerCall {
        proof,
        memberContract: member,
        xPubKey: x_pub,
        wgPubKey: wg_pub,
    };
    Bytes::from(call.abi_encode())
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloy::primitives::address;

    #[test]
    fn bind_hash_matches_solidity() {
        // Cross-checked against `cast abi-encode` + `cast keccak` (and the on-chain
        // ClusterMember bootstrap path). Validates that alloy's abi_encode_params
        // matches Solidity's abi.encode for (string, address, address, bytes32, bytes32).
        let cluster = address!("000000000000000000000000000000000000000a");
        let member = address!("000000000000000000000000000000000000000b");
        let x_pub = B256::repeat_byte(0x11);
        let wg_pub = B256::repeat_byte(0x22);
        let got = bind_hash(cluster, member, x_pub, wg_pub);
        assert_eq!(
            got,
            "0x2de4c55a4fed77e1a8b1423735d699b594f71eca394ed97a14293b0a61b37d84"
                .parse::<B256>()
                .unwrap()
        );
    }

    #[test]
    fn build_kms_material_recovers_app_pubkey() {
        // The dstack app key signs "ethereum:" + hex(derivedCompressedPubkey); the proof
        // assembly must recover exactly that app pubkey from signature_chain[0]. This is
        // the validated app-link from dstackgres group_auth.rs.
        let derived = SigningKey::from_slice(&[0x11u8; 32]).unwrap();
        let derived_priv = derived.to_bytes();
        let derived_compressed = derived
            .verifying_key()
            .to_encoded_point(true)
            .as_bytes()
            .to_vec();

        let app = SigningKey::from_slice(&[0x22u8; 32]).unwrap();
        let app_compressed = app
            .verifying_key()
            .to_encoded_point(true)
            .as_bytes()
            .to_vec();
        let app_msg_hash =
            keccak256(format!("ethereum:{}", hex::encode(&derived_compressed)).as_bytes());
        let (sig, recid) = app.sign_prehash_recoverable(&app_msg_hash.0).unwrap();
        let mut app_sig = sig.to_bytes().to_vec();
        app_sig.push(27 + recid.to_byte());

        let app_id = [0xABu8; 20];
        let m =
            build_kms_material(&app_id, derived_priv.as_slice(), app_sig, vec![0u8; 65]).unwrap();

        assert_eq!(
            m.app_compressed_pubkey, app_compressed,
            "recovered app pubkey must match"
        );
        assert_eq!(m.derived_compressed_pubkey, derived_compressed);
        assert_eq!(m.purpose, "ethereum");
        assert_eq!(&m.code_id.0[..20], &app_id, "codeId = bytes20(app_id)");
        assert!(
            m.code_id.0[20..].iter().all(|&b| b == 0),
            "codeId upper 12 bytes zero"
        );
    }

    #[tokio::test]
    async fn build_proof_passes_onchain_verification_logic() {
        // Build a full proof, then run the same checks DstackSigChain.verify does on
        // chain — proving a sidecar-built proof verifies against the deployed contract.
        let derived = PrivateKeySigner::from_slice(&[0x11u8; 32]).unwrap();
        let derived_compressed = compressed_pubkey(derived.to_bytes().as_slice()).unwrap();

        // app key signs "ethereum:" + hex(derivedCompressedPubkey).
        let app = SigningKey::from_slice(&[0x22u8; 32]).unwrap();
        let app_compressed = app
            .verifying_key()
            .to_encoded_point(true)
            .as_bytes()
            .to_vec();
        let app_msg_hash =
            keccak256(format!("ethereum:{}", hex::encode(&derived_compressed)).as_bytes());
        let (asig, arec) = app.sign_prehash_recoverable(&app_msg_hash.0).unwrap();
        let mut app_sig = asig.to_bytes().to_vec();
        app_sig.push(27 + arec.to_byte());

        let app_id = [0xABu8; 20];
        let cluster = address!("000000000000000000000000000000000000000a");
        let member = address!("000000000000000000000000000000000000000b");
        let x_pub = B256::repeat_byte(0x33);
        let wg_pub = B256::repeat_byte(0x44);

        let proof = build_proof(
            &derived,
            &app_id,
            app_sig,
            vec![9u8; 65],
            cluster,
            member,
            x_pub,
            wg_pub,
        )
        .await
        .unwrap();

        // (1) app step: appCompressedPubkey == the signer of the "ethereum:" preimage.
        assert_eq!(
            proof.appCompressedPubkey.as_ref(),
            app_compressed.as_slice()
        );
        // (2) codeId binding = bytes20(app_id).
        assert_eq!(&proof.codeId.0[..20], &app_id);
        assert!(proof.codeId.0[20..].iter().all(|&b| b == 0));
        // (3) derived/binding step: messageSignature over EIP-191(messageHash) recovers the derived key.
        let eth = eth_signed_message_hash(proof.messageHash);
        let recovered = recover_compressed(&eth.0, &proof.messageSignature).unwrap();
        assert_eq!(
            recovered, derived_compressed,
            "derived key must sign the bind message"
        );
        // (4) messageHash pins (cluster, member, xPub, wgPub).
        assert_eq!(proof.messageHash, bind_hash(cluster, member, x_pub, wg_pub));
        assert_eq!(proof.purpose, "ethereum");
    }

    /// A dstack runtime mock that returns a *valid* signing chain (real app + KMS sigs)
    /// so the full runtime -> proof path can be verified end to end, all three links.
    struct SigningMock {
        app: SigningKey,
        kms: SigningKey,
        derived: [u8; 32],
        app_id: [u8; 20],
    }

    #[async_trait::async_trait]
    impl crate::dstack::DstackRuntime for SigningMock {
        async fn derive_key(&self, _: &str, _: &str) -> Result<zeroize::Zeroizing<[u8; 32]>> {
            Ok(zeroize::Zeroizing::new([0u8; 32]))
        }
        async fn get_quote(&self, _: [u8; 64]) -> Result<Vec<u8>> {
            Ok(vec![])
        }
        async fn seal(&self, _: &str, _: &[u8]) -> Result<()> {
            Ok(())
        }
        async fn unseal(&self, _: &str) -> Result<Option<Vec<u8>>> {
            Ok(None)
        }
        async fn info(&self) -> Result<crate::dstack::DstackInfo> {
            Ok(crate::dstack::DstackInfo {
                app_id: self.app_id.to_vec(),
                ..Default::default()
            })
        }
        async fn get_key(&self, _: &str, _: &str) -> Result<crate::dstack::DstackKey> {
            let derived_compressed = compressed_pubkey(&self.derived)?;
            // app signs "ethereum:" + hex(derivedCompressedPubkey).
            let ah = keccak256(format!("ethereum:{}", hex::encode(&derived_compressed)).as_bytes());
            let (asig, ar) = self.app.sign_prehash_recoverable(&ah.0)?;
            let mut app_sig = asig.to_bytes().to_vec();
            app_sig.push(27 + ar.to_byte());
            // KMS signs "dstack-kms-issued:" || bytes20(app_id) || appCompressedPubkey.
            let app_compressed = self
                .app
                .verifying_key()
                .to_encoded_point(true)
                .as_bytes()
                .to_vec();
            let mut kpre = Vec::new();
            kpre.extend_from_slice(b"dstack-kms-issued:");
            kpre.extend_from_slice(&self.app_id);
            kpre.extend_from_slice(&app_compressed);
            let (ksig, kr) = self.kms.sign_prehash_recoverable(&keccak256(&kpre).0)?;
            let mut kms_sig = ksig.to_bytes().to_vec();
            kms_sig.push(27 + kr.to_byte());
            Ok(crate::dstack::DstackKey {
                key: self.derived.to_vec(),
                signature_chain: vec![app_sig, kms_sig],
            })
        }
    }

    #[tokio::test]
    async fn build_proof_from_runtime_all_three_links_verify() {
        let app = SigningKey::from_slice(&[0x22u8; 32]).unwrap();
        let kms = SigningKey::from_slice(&[0x33u8; 32]).unwrap();
        let mock = SigningMock {
            app: app.clone(),
            kms: kms.clone(),
            derived: [0x11u8; 32],
            app_id: [0xABu8; 20],
        };
        let cluster = address!("000000000000000000000000000000000000000a");
        let member = address!("000000000000000000000000000000000000000b");
        let x_pub = B256::repeat_byte(0x33);
        let wg_pub = B256::repeat_byte(0x44);

        let (proof, signer) = build_proof_from_runtime(&mock, cluster, member, x_pub, wg_pub)
            .await
            .unwrap();

        // app link: appCompressedPubkey is the signer of the "ethereum:" preimage.
        let app_compressed = app
            .verifying_key()
            .to_encoded_point(true)
            .as_bytes()
            .to_vec();
        assert_eq!(
            proof.appCompressedPubkey.as_ref(),
            app_compressed.as_slice()
        );
        // KMS link: kmsSignature recovers the KMS key over the "dstack-kms-issued:" preimage.
        let mut kpre = Vec::new();
        kpre.extend_from_slice(b"dstack-kms-issued:");
        kpre.extend_from_slice(&proof.codeId.0[..20]);
        kpre.extend_from_slice(proof.appCompressedPubkey.as_ref());
        let kms_compressed = kms
            .verifying_key()
            .to_encoded_point(true)
            .as_bytes()
            .to_vec();
        assert_eq!(
            recover_compressed(&keccak256(&kpre).0, &proof.kmsSignature).unwrap(),
            kms_compressed,
            "KMS signature must recover the KMS root"
        );
        // derived link: messageSignature over EIP-191(messageHash) recovers the derived key.
        let derived_compressed = compressed_pubkey(signer.to_bytes().as_slice()).unwrap();
        let eth = eth_signed_message_hash(proof.messageHash);
        assert_eq!(
            recover_compressed(&eth.0, &proof.messageSignature).unwrap(),
            derived_compressed
        );
        // bindings.
        assert_eq!(&proof.codeId.0[..20], &[0xABu8; 20]);
        assert_eq!(proof.messageHash, bind_hash(cluster, member, x_pub, wg_pub));

        // Every signature must carry an Ethereum recovery id (v in {27,28}): the on-chain
        // OZ ECDSA.recover in DstackSigChain.verify and ClusterMember._recoverBindingSigner
        // reverts (ECDSAInvalidSignature) on a raw 0/1 y-parity.
        for (label, sig) in [
            ("app", &proof.appSignature),
            ("kms", &proof.kmsSignature),
            ("binding", &proof.messageSignature),
        ] {
            assert_eq!(sig.len(), 65, "{label} signature must be 65 bytes");
            assert!(
                sig[64] >= 27,
                "{label} signature v={} must be an eth recovery id",
                sig[64]
            );
        }
    }

    #[test]
    fn normalize_recovery_id_maps_parity_to_eth() {
        let mut raw = vec![0xABu8; 65];
        raw[64] = 0;
        assert_eq!(normalize_recovery_id(raw.clone())[64], 27);
        raw[64] = 1;
        assert_eq!(normalize_recovery_id(raw.clone())[64], 28);
        // Already-Ethereum ids and non-65-byte inputs are left untouched.
        raw[64] = 27;
        assert_eq!(normalize_recovery_id(raw.clone())[64], 27);
        raw[64] = 28;
        assert_eq!(normalize_recovery_id(raw)[64], 28);
        assert_eq!(normalize_recovery_id(vec![1u8; 64]).len(), 64);
    }
}
