//! MessageFacet.send + setCskCommitment UserOp builders (sidecar spec §7.1, §13).

use super::abi;
use alloy::primitives::{Bytes, B256};
use alloy::sol_types::SolCall;

pub fn build_send_calldata(recipient: B256, envelope: B256, ciphertext: Bytes) -> Bytes {
    Bytes::from(
        abi::sendCall {
            recipientMemberId: recipient,
            envelopeId: envelope,
            ciphertext,
        }
        .abi_encode(),
    )
}

pub fn build_set_csk_commitment_calldata(commitment: B256) -> Bytes {
    Bytes::from(abi::setCskCommitmentCall { commitment }.abi_encode())
}
