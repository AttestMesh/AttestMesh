//! publishWgKey UserOp builder (sidecar spec §7.1 step 5).

use super::abi;
use alloy::primitives::{Bytes, B256};
use alloy::sol_types::SolCall;

pub fn build_publish_wg_calldata(wg_pub: B256) -> Bytes {
    Bytes::from(abi::publishWgKeyCall { wgPubKey: wg_pub }.abi_encode())
}
