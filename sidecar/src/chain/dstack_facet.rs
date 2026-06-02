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

/// Raw KMS sig-chain material as produced by the dstack runtime (one-to-one with
/// the on-chain DstackProof, minus the binding signature which the sidecar adds).
#[derive(Debug, Clone, Default)]
pub struct KmsChainMaterial {
    pub kms_root_pubkey: Vec<u8>,
    pub app_key: Vec<u8>,
    pub app_key_sig: Vec<u8>,
    pub app_compose_hash: B256,
    pub derived_pubkey: Vec<u8>,
    pub derived_key_sig: Vec<u8>,
    pub derived_instance_id: B256,
    pub derived_device_id: B256,
    pub tcb_status: String,
    pub advisory_ids: Vec<String>,
}

/// Assemble the on-chain proof from runtime material + the sidecar's binding sig.
pub fn assemble_proof(m: KmsChainMaterial, binding_sig: Vec<u8>) -> abi::DstackProof {
    abi::DstackProof {
        kmsRootPubKey: Bytes::from(m.kms_root_pubkey),
        appKey: Bytes::from(m.app_key),
        appKeySig: Bytes::from(m.app_key_sig),
        appComposeHash: m.app_compose_hash,
        derivedPubKey: Bytes::from(m.derived_pubkey),
        derivedKeySig: Bytes::from(m.derived_key_sig),
        derivedInstanceId: m.derived_instance_id,
        derivedDeviceId: m.derived_device_id,
        tcbStatus: m.tcb_status,
        advisoryIds: m.advisory_ids,
        bindingSig: Bytes::from(binding_sig),
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
