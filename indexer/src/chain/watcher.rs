//! Block-watcher loop + event classification (spec §7.1, §7.3).
//!
//! The watcher polls `eth_getLogs` for the set of known cluster diamonds, filtered to
//! the allowlist of event topics we care about. Each returned log is classified into
//! an [`IndexedLog`] carrying the cluster address, position, and the per-member
//! relevance metadata used by the dispatch loop.

use super::{
    ClusterDeployed, CskCommitmentSet, HttpProvider, MemberRegistered, MessageSent, WgKeyPublished,
};
use crate::query::ClusterDeployment;
use alloy::primitives::{Address, B256};
use alloy::providers::Provider;
use alloy::rpc::types::eth::{Filter, Log};
use alloy::sol_types::SolEvent;
use anyhow::{Context, Result};
use std::time::Duration;

/// Which cluster event a log represents, with the indexed fields needed for the
/// per-member relevance filter (spec §7.3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EventKind {
    /// All cluster members care (someone joined).
    MemberRegistered {
        member_id: B256,
        member_contract: Address,
        attestor_id: B256,
        x_pub_key: B256,
        wg_pub_key: B256,
    },
    /// All cluster members care (a peer (re)published its wireguard key).
    WgKeyPublished {
        member_id: B256,
        wg_pub_key: B256,
    },
    /// Only the recipient member cares; never leaked to others (spec §7.3).
    MessageSent {
        sender_member_id: B256,
        recipient_member_id: B256,
        envelope_id: B256,
    },
    CskCommitmentSet {
        commitment: B256,
    },
}

impl EventKind {
    /// "Relevant for this member?" per spec §7.3 / master spec §6.2.
    pub fn is_relevant_for(&self, member_id: &B256) -> bool {
        match self {
            // Membership/key events go to every member of the cluster.
            EventKind::MemberRegistered { .. }
            | EventKind::WgKeyPublished { .. }
            | EventKind::CskCommitmentSet { .. } => true,
            // A message is delivered only to its recipient — the indexer must never
            // leak the existence of a message to a non-recipient member.
            EventKind::MessageSent {
                recipient_member_id,
                ..
            } => recipient_member_id == member_id,
        }
    }

    /// Human-readable label for metrics (`indexer_events_pushed_total{event}`).
    pub fn label(&self) -> &'static str {
        match self {
            EventKind::MemberRegistered { .. } => "MemberRegistered",
            EventKind::WgKeyPublished { .. } => "WgKeyPublished",
            EventKind::MessageSent { .. } => "MessageSent",
            EventKind::CskCommitmentSet { .. } => "CskCommitmentSet",
        }
    }
}

/// A classified, chain-ordered log ready to push. Holds enough of the raw log to
/// rebuild the verifiable `event_data` and the repro stub.
#[derive(Debug, Clone)]
pub struct IndexedLog {
    pub cluster_addr: Address,
    pub block_number: u64,
    pub tx_hash: B256,
    pub log_index: u64,
    pub topics: Vec<B256>,
    pub data: Vec<u8>,
    pub kind: EventKind,
}

impl IndexedLog {
    pub fn is_relevant_for(&self, member_id: &B256) -> bool {
        self.kind.is_relevant_for(member_id)
    }
}

/// The topic0 allowlist for the cluster-event filter (spec §7.1). `MemberRemoved` and
/// allowlist/owner mutations are milestone-B only and intentionally omitted from v1.
pub fn cluster_event_topics() -> Vec<B256> {
    vec![
        MemberRegistered::SIGNATURE_HASH,
        WgKeyPublished::SIGNATURE_HASH,
        MessageSent::SIGNATURE_HASH,
        CskCommitmentSet::SIGNATURE_HASH,
    ]
}

/// Build the `eth_getLogs` filter for a block range over the known clusters,
/// restricted to the event allowlist (spec §7.1).
pub fn cluster_filter(clusters: &[Address], from_block: u64, to_block: u64) -> Filter {
    Filter::new()
        .address(clusters.to_vec())
        .event_signature(cluster_event_topics())
        .from_block(from_block)
        .to_block(to_block)
}

/// Build the `ClusterDeployed` discovery filter over the factory (spec §7.2).
pub fn factory_filter(factory: Address, from_block: u64, to_block: u64) -> Filter {
    Filter::new()
        .address(factory)
        .event_signature(ClusterDeployed::SIGNATURE_HASH)
        .from_block(from_block)
        .to_block(to_block)
}

/// Classify a raw cluster log into an [`IndexedLog`], or `None` if its topic0 is not
/// in the allowlist or it is missing positional metadata (pending logs).
pub fn classify(log: &Log) -> Option<IndexedLog> {
    let topic0 = log.topic0().copied()?;
    let topics = log.topics().to_vec();
    let data = log.data().data.to_vec();

    let kind = if topic0 == MemberRegistered::SIGNATURE_HASH {
        let ev = MemberRegistered::decode_log_data(log.data(), true).ok()?;
        EventKind::MemberRegistered {
            member_id: ev.memberId,
            member_contract: ev.memberContract,
            attestor_id: ev.attestorId,
            x_pub_key: ev.xPubKey,
            wg_pub_key: ev.wgPubKey,
        }
    } else if topic0 == WgKeyPublished::SIGNATURE_HASH {
        let ev = WgKeyPublished::decode_log_data(log.data(), true).ok()?;
        EventKind::WgKeyPublished {
            member_id: ev.memberId,
            wg_pub_key: ev.wgPubKey,
        }
    } else if topic0 == MessageSent::SIGNATURE_HASH {
        let ev = MessageSent::decode_log_data(log.data(), true).ok()?;
        EventKind::MessageSent {
            sender_member_id: ev.senderMemberId,
            recipient_member_id: ev.recipientMemberId,
            envelope_id: ev.envelopeId,
        }
    } else if topic0 == CskCommitmentSet::SIGNATURE_HASH {
        let ev = CskCommitmentSet::decode_log_data(log.data(), true).ok()?;
        EventKind::CskCommitmentSet {
            commitment: ev.commitment,
        }
    } else {
        return None;
    };

    Some(IndexedLog {
        cluster_addr: log.address(),
        block_number: log.block_number?,
        tx_hash: log.transaction_hash?,
        log_index: log.log_index?,
        topics,
        data,
        kind,
    })
}

/// Decode a `ClusterDeployed` log to its deployment metadata (spec §7.2).
pub fn decode_cluster_deployed(log: &Log) -> Option<ClusterDeployment> {
    if log.topic0().copied()? != ClusterDeployed::SIGNATURE_HASH {
        return None;
    }
    let ev = ClusterDeployed::decode_log_data(log.data(), true).ok()?;
    Some(ClusterDeployment {
        cluster: ev.cluster,
        cluster_owner: ev.clusterOwner,
        salt: ev.salt,
        deployed_at_block: log.block_number?,
        deployment_tx_hash: log.transaction_hash?,
        deployment_log_index: log.log_index?,
    })
}

/// One catch-up / steady-state poll: fetch and classify logs in `[from_block,
/// to_block]` for the known clusters, in chain order. Returns the classified logs.
pub async fn poll_cluster_logs(
    provider: &HttpProvider,
    clusters: &[Address],
    from_block: u64,
    to_block: u64,
) -> Result<Vec<IndexedLog>> {
    if clusters.is_empty() || from_block > to_block {
        return Ok(Vec::new());
    }
    let filter = cluster_filter(clusters, from_block, to_block);
    let logs = get_logs_with_retry(provider, filter, "eth_getLogs(clusters)").await?;
    Ok(logs.iter().filter_map(classify).collect())
}

/// One discovery poll: fetch `ClusterDeployed` logs from the factory and return the
/// newly-seen cluster addresses (spec §7.2).
pub async fn poll_new_clusters(
    provider: &HttpProvider,
    factory: Address,
    from_block: u64,
    to_block: u64,
) -> Result<Vec<ClusterDeployment>> {
    if from_block > to_block {
        return Ok(Vec::new());
    }
    let filter = factory_filter(factory, from_block, to_block);
    let logs = get_logs_with_retry(provider, filter, "eth_getLogs(factory)").await?;
    Ok(logs.iter().filter_map(decode_cluster_deployed).collect())
}

async fn get_logs_with_retry(
    provider: &HttpProvider,
    filter: Filter,
    label: &'static str,
) -> Result<Vec<Log>> {
    let mut delay = Duration::from_millis(500);
    let mut last_error = None;
    for attempt in 1..=8 {
        match provider.get_logs(&filter).await {
            Ok(logs) => return Ok(logs),
            Err(e) => {
                last_error = Some(e);
                if attempt == 8 {
                    break;
                }
                tracing::warn!(label, attempt, error = ?last_error, "RPC read failed; retrying");
                tokio::time::sleep(delay).await;
                delay = (delay * 2).min(Duration::from_secs(10));
            }
        }
    }
    Err(anyhow::Error::new(last_error.expect("retry loop ran"))).context(label)
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloy::primitives::{Address, Bytes, LogData, B256};

    fn raw_log(address: Address, topics: Vec<B256>, data: Vec<u8>, log_index: u64) -> Log {
        let inner = alloy::primitives::Log {
            address,
            data: LogData::new_unchecked(topics, Bytes::from(data)),
        };
        Log {
            inner,
            block_hash: Some(B256::repeat_byte(0xaa)),
            block_number: Some(100),
            block_timestamp: None,
            transaction_hash: Some(B256::repeat_byte(0xbb)),
            transaction_index: Some(0),
            log_index: Some(log_index),
            removed: false,
        }
    }

    fn member_registered_log(member_id: B256) -> Log {
        // indexed: memberId, memberContract, attestorId; data: xPubKey, wgPubKey.
        let member_contract = B256::left_padding_from(Address::repeat_byte(0x11).as_slice());
        let attestor_id = B256::repeat_byte(0x22);
        let mut data = Vec::new();
        data.extend_from_slice(B256::repeat_byte(0x33).as_slice()); // xPubKey
        data.extend_from_slice(B256::repeat_byte(0x44).as_slice()); // wgPubKey
        raw_log(
            Address::repeat_byte(0xc1),
            vec![
                MemberRegistered::SIGNATURE_HASH,
                member_id,
                member_contract,
                attestor_id,
            ],
            data,
            0,
        )
    }

    fn wg_key_published_log(member_id: B256) -> Log {
        let mut data = Vec::new();
        data.extend_from_slice(B256::repeat_byte(0x55).as_slice()); // wgPubKey
        raw_log(
            Address::repeat_byte(0xc1),
            vec![WgKeyPublished::SIGNATURE_HASH, member_id],
            data,
            1,
        )
    }

    fn message_sent_log(sender: B256, recipient: B256, envelope: B256) -> Log {
        // dynamic `bytes ciphertext`: head offset (32) + len + padded payload.
        let payload = b"ciphertext-bytes";
        let mut data = Vec::new();
        data.extend_from_slice(&B256::from(alloy::primitives::U256::from(32)).0); // offset
        data.extend_from_slice(&B256::from(alloy::primitives::U256::from(payload.len())).0); // len
        let mut padded = payload.to_vec();
        padded.resize(32, 0);
        data.extend_from_slice(&padded);
        raw_log(
            Address::repeat_byte(0xc1),
            vec![MessageSent::SIGNATURE_HASH, sender, recipient, envelope],
            data,
            2,
        )
    }

    #[test]
    fn classifies_member_registered() {
        let mid = B256::repeat_byte(0xa1);
        let il = classify(&member_registered_log(mid)).expect("classified");
        assert_eq!(
            il.kind,
            EventKind::MemberRegistered {
                member_id: mid,
                member_contract: Address::repeat_byte(0x11),
                attestor_id: B256::repeat_byte(0x22),
                x_pub_key: B256::repeat_byte(0x33),
                wg_pub_key: B256::repeat_byte(0x44),
            }
        );
        assert_eq!(il.cluster_addr, Address::repeat_byte(0xc1));
        assert_eq!(il.block_number, 100);
        assert_eq!(il.log_index, 0);
    }

    #[test]
    fn member_registered_is_relevant_to_everyone() {
        let il = classify(&member_registered_log(B256::repeat_byte(0xa1))).unwrap();
        assert!(il.is_relevant_for(&B256::repeat_byte(0xde)));
        assert!(il.is_relevant_for(&B256::repeat_byte(0xad)));
    }

    #[test]
    fn wg_key_published_is_relevant_to_everyone() {
        let il = classify(&wg_key_published_log(B256::repeat_byte(0xb2))).unwrap();
        assert!(il.is_relevant_for(&B256::repeat_byte(0x01)));
        assert!(il.is_relevant_for(&B256::repeat_byte(0x02)));
    }

    #[test]
    fn message_sent_is_relevant_only_to_recipient() {
        let recipient = B256::repeat_byte(0xcc);
        let other = B256::repeat_byte(0xdd);
        let il = classify(&message_sent_log(
            B256::repeat_byte(0xaa),
            recipient,
            B256::repeat_byte(0xee),
        ))
        .unwrap();
        assert!(
            il.is_relevant_for(&recipient),
            "recipient must receive its message"
        );
        assert!(
            !il.is_relevant_for(&other),
            "non-recipient must NOT see the message"
        );
    }

    #[test]
    fn unknown_topic_is_dropped() {
        let l = raw_log(
            Address::repeat_byte(0xc1),
            vec![B256::repeat_byte(0xff)],
            vec![],
            0,
        );
        assert!(classify(&l).is_none());
    }

    #[test]
    fn cluster_deployed_decodes_address() {
        let cluster = Address::repeat_byte(0x9a);
        let owner = Address::repeat_byte(0x9b);
        let l = raw_log(
            Address::repeat_byte(0xfa),
            vec![
                ClusterDeployed::SIGNATURE_HASH,
                B256::left_padding_from(cluster.as_slice()),
                B256::left_padding_from(owner.as_slice()),
            ],
            B256::repeat_byte(0x01).to_vec(),
            0,
        );
        let d = decode_cluster_deployed(&l).expect("decoded deployment");
        assert_eq!(d.cluster, cluster);
        assert_eq!(d.cluster_owner, owner);
        assert_eq!(d.salt, B256::repeat_byte(0x01));
    }
}
