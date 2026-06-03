//! dstack_register UserOp builder + binding-hash construction (sidecar spec §7.1
//! step 3, contracts spec §6.3). The KMS sig-chain material comes from the dstack
//! runtime at runtime; this module assembles it into the on-chain proof shape and
//! computes the binding hash the derived key signs.

use super::abi;
use alloy::primitives::{keccak256, Address, Bytes, B256};
use alloy::signers::k256::ecdsa::{RecoveryId, Signature as K256Sig, SigningKey, VerifyingKey};
use alloy::sol_types::{SolCall, SolValue};
use anyhow::{ensure, Context, Result};

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
}
