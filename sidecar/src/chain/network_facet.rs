//! publishWgKey / publishEd25519Key UserOp builders (sidecar spec §7.1 step 5).

use super::abi;
use alloy::primitives::{Bytes, B256};
use alloy::sol_types::SolCall;

pub fn build_publish_wg_calldata(wg_pub: B256) -> Bytes {
    Bytes::from(abi::publishWgKeyCall { wgPubKey: wg_pub }.abi_encode())
}

/// publishEd25519Key UserOp builder: a member publishes its heartbeat key on chain
/// so peers read it instead of exchanging a sponsored PeerEndpoint envelope
/// (ed25519-onchain-key spec).
pub fn build_publish_ed25519_calldata(ed25519_pub: B256) -> Bytes {
    Bytes::from(
        abi::publishEd25519KeyCall {
            ed25519Key: ed25519_pub,
        }
        .abi_encode(),
    )
}
