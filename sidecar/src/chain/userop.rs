//! EIP-4337 v0.7 UserOperation construction + hashing + signing (sidecar spec §8.2).
//!
//! State-mutating calls are wrapped as `ClusterMember.execute(cluster, 0, innerData)`
//! to match the gas-webhook policy, packed into a v0.7 UserOperation, and signed with
//! the binding key. The signature is over the EIP-191 message of the userOpHash, which
//! `ClusterMember.validateUserOp` recovers (contracts spec §9.1.2).

use super::abi;
use alloy::primitives::{keccak256, Address, Bytes, B256, U256};
use alloy::sol_types::{SolCall, SolValue};

/// The v0.7 UserOperation in the unpacked shape the Alchemy bundler RPC expects.
/// Hex fields are filled lazily; `paymaster*` are populated by the paymaster service.
#[derive(Debug, Clone)]
pub struct UserOperation {
    pub sender: Address,
    pub nonce: U256,
    pub call_data: Bytes,
    pub call_gas_limit: U256,
    pub verification_gas_limit: U256,
    pub pre_verification_gas: U256,
    pub max_fee_per_gas: U256,
    pub max_priority_fee_per_gas: U256,
    pub paymaster: Option<Address>,
    pub paymaster_verification_gas_limit: U256,
    pub paymaster_post_op_gas_limit: U256,
    pub paymaster_data: Bytes,
    pub signature: Bytes,
}

impl UserOperation {
    pub fn new(sender: Address, nonce: U256, call_data: Bytes) -> Self {
        Self {
            sender,
            nonce,
            call_data,
            call_gas_limit: U256::ZERO,
            verification_gas_limit: U256::ZERO,
            pre_verification_gas: U256::ZERO,
            max_fee_per_gas: U256::ZERO,
            max_priority_fee_per_gas: U256::ZERO,
            paymaster: None,
            paymaster_verification_gas_limit: U256::ZERO,
            paymaster_post_op_gas_limit: U256::ZERO,
            paymaster_data: Bytes::new(),
            signature: Bytes::new(),
        }
    }

    fn account_gas_limits(&self) -> B256 {
        pack_hi_lo(self.verification_gas_limit, self.call_gas_limit)
    }

    fn gas_fees(&self) -> B256 {
        pack_hi_lo(self.max_priority_fee_per_gas, self.max_fee_per_gas)
    }

    fn paymaster_and_data(&self) -> Bytes {
        match self.paymaster {
            None => Bytes::new(),
            Some(pm) => {
                let mut v = Vec::with_capacity(52 + self.paymaster_data.len());
                v.extend_from_slice(pm.as_slice());
                v.extend_from_slice(&u128_be16(self.paymaster_verification_gas_limit));
                v.extend_from_slice(&u128_be16(self.paymaster_post_op_gas_limit));
                v.extend_from_slice(&self.paymaster_data);
                Bytes::from(v)
            }
        }
    }

    /// EIP-4337 v0.7 userOpHash = keccak256(abi.encode(hashOf(packed), entryPoint, chainId)).
    pub fn user_op_hash(&self, entry_point: Address, chain_id: u64) -> B256 {
        let inner = (
            self.sender,
            self.nonce,
            keccak256(Bytes::new()), // initCode (empty)
            keccak256(&self.call_data),
            self.account_gas_limits(),
            self.pre_verification_gas,
            self.gas_fees(),
            keccak256(self.paymaster_and_data()),
        )
            .abi_encode_params();
        let packed_hash = keccak256(inner);
        let outer = (packed_hash, entry_point, U256::from(chain_id)).abi_encode_params();
        keccak256(outer)
    }
}

fn pack_hi_lo(hi: U256, lo: U256) -> B256 {
    let mut out = [0u8; 32];
    out[0..16].copy_from_slice(&u128_be16(hi));
    out[16..32].copy_from_slice(&u128_be16(lo));
    B256::from(out)
}

fn u128_be16(v: U256) -> [u8; 16] {
    let bytes: [u8; 32] = v.to_be_bytes();
    let mut out = [0u8; 16];
    out.copy_from_slice(&bytes[16..32]);
    out
}

/// Wrap an inner cluster call as `ClusterMember.execute(target, 0, innerData)`.
pub fn wrap_execute(cluster: Address, inner: Bytes) -> Bytes {
    let call = abi::executeCall {
        target: cluster,
        value: U256::ZERO,
        data: inner,
    };
    Bytes::from(call.abi_encode())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn account_gas_limits_packs_hi_lo() {
        let mut op = UserOperation::new(Address::ZERO, U256::ZERO, Bytes::new());
        op.verification_gas_limit = U256::from(0x1111u64);
        op.call_gas_limit = U256::from(0x2222u64);
        let packed = op.account_gas_limits();
        assert_eq!(&packed.as_slice()[14..16], &[0x11, 0x11]);
        assert_eq!(&packed.as_slice()[30..32], &[0x22, 0x22]);
    }

    #[test]
    fn user_op_hash_is_stable() {
        let op = UserOperation::new(
            Address::repeat_byte(1),
            U256::from(5u64),
            Bytes::from(vec![1, 2, 3]),
        );
        let ep = Address::repeat_byte(0xEE);
        let h1 = op.user_op_hash(ep, 84532);
        let h2 = op.user_op_hash(ep, 84532);
        assert_eq!(h1, h2);
        // Chain id is bound into the hash.
        assert_ne!(h1, op.user_op_hash(ep, 8453));
    }
}
