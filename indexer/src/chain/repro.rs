//! RPC repro-stub generation (spec §10).
//!
//! For every push the indexer attaches the exact `eth_getLogs` call that reproduces
//! the underlying log against any RPC provider the member trusts. The member runs it,
//! finds the entry with the matching `logIndex`, and confirms the bytes match
//! `event_data`. Any mismatch is grounds to drop the subscription (spec §10).

use super::watcher::IndexedLog;
use alloy::primitives::{Address, B256};
use serde::Serialize;

/// The repro stub (mirrors the proto `RpcReproStub`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReproStub {
    pub method: String,
    pub params_json: String,
}

#[derive(Serialize)]
struct GetLogsParam {
    address: String,
    #[serde(rename = "fromBlock")]
    from_block: String,
    #[serde(rename = "toBlock")]
    to_block: String,
    topics: Vec<String>,
}

fn hex_block(n: u64) -> String {
    format!("0x{n:x}")
}

fn hex_addr(a: Address) -> String {
    format!("0x{}", hex::encode(a.as_slice()))
}

fn hex_topic(t: &B256) -> String {
    format!("0x{}", hex::encode(t.as_slice()))
}

/// Build the `eth_getLogs` repro stub for a single indexed log (spec §10). The block
/// range is the single block the log lives in; the topics are its full topic set so a
/// provider returns exactly this event (plus any same-block siblings the member
/// disambiguates by `logIndex`).
pub fn build_stub(log: &IndexedLog) -> ReproStub {
    let param = GetLogsParam {
        address: hex_addr(log.cluster_addr),
        from_block: hex_block(log.block_number),
        to_block: hex_block(log.block_number),
        topics: log.topics.iter().map(hex_topic).collect(),
    };
    // A JSON array of one filter object, exactly as `eth_getLogs` expects.
    let params_json = serde_json::to_string(&[param]).expect("serialize repro params");
    ReproStub {
        method: "eth_getLogs".to_string(),
        params_json,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::chain::watcher::EventKind;

    fn fixture_log() -> IndexedLog {
        IndexedLog {
            cluster_addr: Address::repeat_byte(0xc1),
            block_number: 0x1234,
            tx_hash: B256::repeat_byte(0xbb),
            log_index: 7,
            topics: vec![B256::repeat_byte(0xaa), B256::repeat_byte(0x01)],
            data: vec![1, 2, 3],
            kind: EventKind::WgKeyPublished {
                member_id: B256::repeat_byte(0x01),
                wg_pub_key: B256::repeat_byte(0x02),
            },
        }
    }

    #[test]
    fn build_stub_shape() {
        let stub = build_stub(&fixture_log());
        assert_eq!(stub.method, "eth_getLogs");

        let v: serde_json::Value = serde_json::from_str(&stub.params_json).unwrap();
        let filter = &v[0];
        assert_eq!(filter["address"], "0x".to_string() + &"c1".repeat(20));
        assert_eq!(filter["fromBlock"], "0x1234");
        assert_eq!(filter["toBlock"], "0x1234");
        assert_eq!(filter["topics"][0], "0x".to_string() + &"aa".repeat(32));
        assert_eq!(filter["topics"][1], "0x".to_string() + &"01".repeat(32));
    }

    #[test]
    fn build_stub_block_range_is_single_block() {
        let stub = build_stub(&fixture_log());
        let v: serde_json::Value = serde_json::from_str(&stub.params_json).unwrap();
        assert_eq!(v[0]["fromBlock"], v[0]["toBlock"]);
    }
}
