//! dstack_register UserOp builder + binding-hash construction (sidecar spec §7.1
//! step 3, contracts spec §6.3). The KMS sig-chain material comes from the dstack
//! runtime at runtime; this module assembles it into the on-chain proof shape and
//! computes the binding hash the derived key signs.

use super::abi;
use alloy::primitives::{keccak256, Address, Bytes, B256};
use alloy::sol_types::{SolCall, SolValue};

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
}
