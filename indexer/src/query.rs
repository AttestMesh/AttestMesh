//! Public mesh-state read model served by the indexer HTTP listener.
//!
//! This is the Rust replacement for `services/mesh-state-api`: it projects the
//! same factory + cluster events the indexer already scans, then serves frontend
//! JSON without issuing per-request chain reads.

use alloy::primitives::{keccak256, Address, B256};
use serde::Serialize;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

#[derive(Debug, Clone)]
pub struct ClusterDeployment {
    pub cluster: Address,
    pub cluster_owner: Address,
    pub salt: B256,
    pub deployed_at_block: u64,
    pub deployment_tx_hash: B256,
    pub deployment_log_index: u64,
}

#[derive(Debug, Clone, Copy)]
pub struct MeshCidr {
    pub ip: u32,
    pub prefix: u8,
}

#[derive(Debug, Clone)]
pub struct IndexedMember {
    pub member_id: B256,
    pub member_contract: Address,
    pub attestor_id: B256,
    pub x_pub_key: B256,
    pub wg_pub_key: B256,
    pub registered_at: u64,
    pub block: u64,
    pub tx_hash: B256,
    pub log_index: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct TimelineEvent {
    #[serde(rename = "type")]
    pub kind: String,
    pub block: u64,
    #[serde(rename = "txHash")]
    pub tx_hash: String,
    #[serde(rename = "logIndex")]
    pub log_index: u64,
    #[serde(flatten)]
    pub args: serde_json::Map<String, serde_json::Value>,
}

#[derive(Debug, Clone)]
struct IndexedCluster {
    deployment: ClusterDeployment,
    cidr: MeshCidr,
    csk_commitment: Option<B256>,
    scanned_to_block: u64,
    members: Vec<IndexedMember>,
    events: Vec<TimelineEvent>,
}

#[derive(Debug, Default)]
struct Inner {
    clusters: HashMap<Address, IndexedCluster>,
    order: Vec<Address>,
    scanned_to_block: u64,
}

#[derive(Debug, Clone, Default)]
pub struct ReadModel {
    inner: Arc<RwLock<Inner>>,
}

impl ReadModel {
    pub fn new() -> Self {
        Self::default()
    }

    pub async fn add_cluster(&self, deployment: ClusterDeployment, cidr: MeshCidr) -> bool {
        let mut g = self.inner.write().await;
        if g.clusters.contains_key(&deployment.cluster) {
            return false;
        }
        let event = TimelineEvent {
            kind: "ClusterDeployed".into(),
            block: deployment.deployed_at_block,
            tx_hash: b256_hex(deployment.deployment_tx_hash),
            log_index: deployment.deployment_log_index,
            args: serde_json::Map::from_iter([
                ("cluster".into(), addr_json(deployment.cluster)),
                ("clusterOwner".into(), addr_json(deployment.cluster_owner)),
                ("salt".into(), b256_json(deployment.salt)),
            ]),
        };
        g.order.push(deployment.cluster);
        g.clusters.insert(
            deployment.cluster,
            IndexedCluster {
                scanned_to_block: deployment.deployed_at_block.saturating_sub(1),
                deployment,
                cidr,
                csk_commitment: None,
                members: Vec::new(),
                events: vec![event],
            },
        );
        let sort_keys = g
            .clusters
            .iter()
            .map(|(addr, c)| {
                (
                    *addr,
                    (
                        c.deployment.deployed_at_block,
                        c.deployment.deployment_log_index,
                    ),
                )
            })
            .collect::<HashMap<_, _>>();
        g.order.sort_by_key(|cluster| {
            sort_keys
                .get(cluster)
                .copied()
                .unwrap_or((u64::MAX, u64::MAX))
        });
        true
    }

    pub async fn contains_cluster(&self, cluster: Address) -> bool {
        self.inner.read().await.clusters.contains_key(&cluster)
    }

    pub async fn ingest(
        &self,
        log: &crate::chain::watcher::IndexedLog,
        registered_at: Option<u64>,
    ) {
        use crate::chain::watcher::EventKind;

        let mut g = self.inner.write().await;
        let Some(cluster) = g.clusters.get_mut(&log.cluster_addr) else {
            return;
        };
        cluster.scanned_to_block = cluster.scanned_to_block.max(log.block_number);

        match &log.kind {
            EventKind::MemberRegistered {
                member_id,
                member_contract,
                attestor_id,
                x_pub_key,
                wg_pub_key,
            } => {
                if !cluster.members.iter().any(|m| m.member_id == *member_id) {
                    cluster.members.push(IndexedMember {
                        member_id: *member_id,
                        member_contract: *member_contract,
                        attestor_id: *attestor_id,
                        x_pub_key: *x_pub_key,
                        wg_pub_key: *wg_pub_key,
                        registered_at: registered_at.unwrap_or(log.block_number),
                        block: log.block_number,
                        tx_hash: log.tx_hash,
                        log_index: log.log_index,
                    });
                    cluster.members.sort_by_key(|m| (m.block, m.log_index));
                }
                cluster.events.push(timeline_event(
                    "MemberRegistered",
                    log,
                    [
                        ("memberId", b256_json(*member_id)),
                        ("memberContract", addr_json(*member_contract)),
                        ("attestorId", b256_json(*attestor_id)),
                        ("xPubKey", b256_json(*x_pub_key)),
                        ("wgPubKey", b256_json(*wg_pub_key)),
                    ],
                ));
            }
            EventKind::WgKeyPublished {
                member_id,
                wg_pub_key,
            } => {
                if let Some(member) = cluster
                    .members
                    .iter_mut()
                    .find(|m| m.member_id == *member_id)
                {
                    member.wg_pub_key = *wg_pub_key;
                }
                cluster.events.push(timeline_event(
                    "WgKeyPublished",
                    log,
                    [
                        ("memberId", b256_json(*member_id)),
                        ("wgPubKey", b256_json(*wg_pub_key)),
                    ],
                ));
            }
            EventKind::MessageSent {
                sender_member_id,
                recipient_member_id,
                envelope_id,
            } => {
                cluster.events.push(timeline_event(
                    "MessageSent",
                    log,
                    [
                        ("senderMemberId", b256_json(*sender_member_id)),
                        ("recipientMemberId", b256_json(*recipient_member_id)),
                        ("envelopeId", b256_json(*envelope_id)),
                    ],
                ));
            }
            EventKind::CskCommitmentSet { commitment } => {
                cluster.csk_commitment = Some(*commitment);
                cluster.events.push(timeline_event(
                    "CskCommitmentSet",
                    log,
                    [("commitment", b256_json(*commitment))],
                ));
            }
        }
        cluster.events.sort_by_key(|e| (e.block, e.log_index));
    }

    pub async fn set_scanned_to_block(&self, block: u64) {
        self.inner.write().await.scanned_to_block = block;
    }

    pub async fn snapshots(&self, chain_id: u64, gateway_domain: Option<&str>) -> Vec<SnapshotOut> {
        let g = self.inner.read().await;
        g.order
            .iter()
            .filter_map(|addr| g.clusters.get(addr))
            .map(|cluster| snapshot_out(cluster, chain_id, gateway_domain))
            .collect()
    }

    pub async fn timelines(&self) -> Vec<TimelineOut> {
        let g = self.inner.read().await;
        g.order
            .iter()
            .filter_map(|addr| g.clusters.get(addr))
            .map(|cluster| TimelineOut {
                cluster: addr_hex(cluster.deployment.cluster),
                from_block: cluster.deployment.deployed_at_block,
                to_block: cluster.scanned_to_block,
                events: cluster.events.clone(),
            })
            .collect()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct MemberOut {
    #[serde(rename = "memberId")]
    pub member_id: String,
    #[serde(rename = "memberContract")]
    pub member_contract: String,
    #[serde(rename = "appId")]
    pub app_id: String,
    #[serde(rename = "attestorId")]
    pub attestor_id: String,
    pub attestor: String,
    #[serde(rename = "xPubKey")]
    pub x_pub_key: String,
    #[serde(rename = "wgPubKey")]
    pub wg_pub_key: String,
    #[serde(rename = "meshIp")]
    pub mesh_ip: String,
    #[serde(rename = "registeredAt")]
    pub registered_at: u64,
    #[serde(rename = "isOriginator")]
    pub is_originator: bool,
    pub endpoint: Option<String>,
    pub vm: Option<serde_json::Value>,
    pub health: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SnapshotOut {
    pub cluster: String,
    #[serde(rename = "clusterOwner")]
    pub cluster_owner: String,
    pub salt: String,
    #[serde(rename = "deployedAtBlock")]
    pub deployed_at_block: u64,
    #[serde(rename = "deploymentTxHash")]
    pub deployment_tx_hash: String,
    #[serde(rename = "deploymentLogIndex")]
    pub deployment_log_index: u64,
    #[serde(rename = "chainId")]
    pub chain_id: u64,
    #[serde(rename = "atBlock")]
    pub at_block: u64,
    #[serde(rename = "meshCidr")]
    pub mesh_cidr: String,
    #[serde(rename = "cskCommitment")]
    pub csk_commitment: Option<String>,
    #[serde(rename = "originatorMemberId")]
    pub originator_member_id: Option<String>,
    #[serde(rename = "memberCount")]
    pub member_count: usize,
    pub members: Vec<MemberOut>,
}

#[derive(Debug, Clone, Serialize)]
pub struct TopologyOut {
    pub cluster: String,
    #[serde(rename = "clusterOwner")]
    pub cluster_owner: String,
    pub salt: String,
    #[serde(rename = "deployedAtBlock")]
    pub deployed_at_block: u64,
    #[serde(rename = "deploymentTxHash")]
    pub deployment_tx_hash: String,
    #[serde(rename = "deploymentLogIndex")]
    pub deployment_log_index: u64,
    #[serde(rename = "atBlock")]
    pub at_block: u64,
    pub nodes: Vec<TopologyNode>,
    pub edges: Vec<TopologyEdge>,
}

#[derive(Debug, Clone, Serialize)]
pub struct TopologyNode {
    #[serde(rename = "memberId")]
    pub member_id: String,
    #[serde(rename = "appId")]
    pub app_id: String,
    pub label: String,
    #[serde(rename = "meshIp")]
    pub mesh_ip: String,
    #[serde(rename = "isOriginator")]
    pub is_originator: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct TopologyEdge {
    pub a: String,
    pub b: String,
    pub state: &'static str,
}

#[derive(Debug, Clone, Serialize)]
pub struct ClusterHealthOut {
    pub cluster: String,
    #[serde(rename = "clusterOwner")]
    pub cluster_owner: String,
    pub salt: String,
    #[serde(rename = "deployedAtBlock")]
    pub deployed_at_block: u64,
    #[serde(rename = "deploymentTxHash")]
    pub deployment_tx_hash: String,
    #[serde(rename = "deploymentLogIndex")]
    pub deployment_log_index: u64,
    #[serde(rename = "atBlock")]
    pub at_block: u64,
    #[serde(rename = "memberCount")]
    pub member_count: usize,
    #[serde(rename = "cskCommitted")]
    pub csk_committed: bool,
    #[serde(rename = "originatorMemberId")]
    pub originator_member_id: Option<String>,
    pub note: &'static str,
}

#[derive(Debug, Clone, Serialize)]
pub struct TimelineOut {
    pub cluster: String,
    #[serde(rename = "fromBlock")]
    pub from_block: u64,
    #[serde(rename = "toBlock")]
    pub to_block: u64,
    pub events: Vec<TimelineEvent>,
}

pub fn build_topology(s: &SnapshotOut) -> TopologyOut {
    let nodes = s
        .members
        .iter()
        .map(|m| TopologyNode {
            member_id: m.member_id.clone(),
            app_id: m.app_id.clone(),
            label: m.member_contract.clone(),
            mesh_ip: m.mesh_ip.clone(),
            is_originator: m.is_originator,
        })
        .collect::<Vec<_>>();
    let mut edges = Vec::new();
    for i in 0..s.members.len() {
        for j in (i + 1)..s.members.len() {
            edges.push(TopologyEdge {
                a: s.members[i].member_id.clone(),
                b: s.members[j].member_id.clone(),
                state: "unknown",
            });
        }
    }
    TopologyOut {
        cluster: s.cluster.clone(),
        cluster_owner: s.cluster_owner.clone(),
        salt: s.salt.clone(),
        deployed_at_block: s.deployed_at_block,
        deployment_tx_hash: s.deployment_tx_hash.clone(),
        deployment_log_index: s.deployment_log_index,
        at_block: s.at_block,
        nodes,
        edges,
    }
}

pub fn build_health(s: &SnapshotOut) -> ClusterHealthOut {
    ClusterHealthOut {
        cluster: s.cluster.clone(),
        cluster_owner: s.cluster_owner.clone(),
        salt: s.salt.clone(),
        deployed_at_block: s.deployed_at_block,
        deployment_tx_hash: s.deployment_tx_hash.clone(),
        deployment_log_index: s.deployment_log_index,
        at_block: s.at_block,
        member_count: s.member_count,
        csk_committed: s.csk_commitment.is_some(),
        originator_member_id: s.originator_member_id.clone(),
        note: "liveness (phase/live_peers) is operator-tier; null here",
    }
}

fn snapshot_out(
    cluster: &IndexedCluster,
    chain_id: u64,
    gateway_domain: Option<&str>,
) -> SnapshotOut {
    let members = cluster
        .members
        .iter()
        .enumerate()
        .map(|(i, member)| {
            let app_id = app_id_of(member.member_contract);
            MemberOut {
                member_id: b256_hex(member.member_id),
                member_contract: addr_hex(member.member_contract),
                app_id: app_id.clone(),
                attestor_id: b256_hex(member.attestor_id),
                attestor: attestor_label(member.attestor_id),
                x_pub_key: b256_hex(member.x_pub_key),
                wg_pub_key: b256_hex(member.wg_pub_key),
                mesh_ip: ip_to_dotted(derive_mesh_ip(
                    member.member_id,
                    cluster.cidr.ip,
                    cluster.cidr.prefix,
                )),
                registered_at: member.registered_at,
                is_originator: i == 0,
                endpoint: gateway_domain.map(|domain| format!("https://{app_id}-51900s.{domain}")),
                vm: None,
                health: None,
            }
        })
        .collect::<Vec<_>>();
    SnapshotOut {
        cluster: addr_hex(cluster.deployment.cluster),
        cluster_owner: addr_hex(cluster.deployment.cluster_owner),
        salt: b256_hex(cluster.deployment.salt),
        deployed_at_block: cluster.deployment.deployed_at_block,
        deployment_tx_hash: b256_hex(cluster.deployment.deployment_tx_hash),
        deployment_log_index: cluster.deployment.deployment_log_index,
        chain_id,
        at_block: cluster.scanned_to_block,
        mesh_cidr: format!("{}/{}", ip_to_dotted(cluster.cidr.ip), cluster.cidr.prefix),
        csk_commitment: cluster.csk_commitment.map(b256_hex),
        originator_member_id: members.first().map(|m| m.member_id.clone()),
        member_count: members.len(),
        members,
    }
}

fn timeline_event<const N: usize>(
    kind: &str,
    log: &crate::chain::watcher::IndexedLog,
    args: [(&str, serde_json::Value); N],
) -> TimelineEvent {
    TimelineEvent {
        kind: kind.into(),
        block: log.block_number,
        tx_hash: b256_hex(log.tx_hash),
        log_index: log.log_index,
        args: serde_json::Map::from_iter(args.into_iter().map(|(k, v)| (k.to_string(), v))),
    }
}

fn addr_json(addr: Address) -> serde_json::Value {
    serde_json::Value::String(addr_hex(addr))
}

fn b256_json(v: B256) -> serde_json::Value {
    serde_json::Value::String(b256_hex(v))
}

pub fn addr_hex(addr: Address) -> String {
    format!("{addr:#x}")
}

pub fn b256_hex(v: B256) -> String {
    format!("{v:#x}")
}

fn app_id_of(member_contract: Address) -> String {
    addr_hex(member_contract)
        .trim_start_matches("0x")
        .to_string()
}

fn ip_to_dotted(ip: u32) -> String {
    format!(
        "{}.{}.{}.{}",
        (ip >> 24) & 0xff,
        (ip >> 16) & 0xff,
        (ip >> 8) & 0xff,
        ip & 0xff
    )
}

fn derive_mesh_ip(member_id: B256, cidr_ip: u32, cidr_prefix: u8) -> u32 {
    let host_count = 1u64 << (32 - cidr_prefix);
    if host_count <= 2 {
        return cidr_ip;
    }
    let hash = keccak256(member_id.as_slice());
    let bytes = hash.as_slice();
    let low = u32::from_be_bytes(bytes[28..32].try_into().expect("slice len"));
    let offset = ((low as u64 % (host_count - 2)) + 1) as u32;
    cidr_ip | offset
}

fn attestor_label(attestor_id: B256) -> String {
    let dstack = keccak256("attestmesh.attestor.dstack".as_bytes());
    if attestor_id == dstack {
        "dstack".into()
    } else {
        let id = b256_hex(attestor_id);
        format!("unknown:{}", &id[..10])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dotted_ip_and_app_id_match_mesh_state_api_shape() {
        assert_eq!(ip_to_dotted(0x0a120001), "10.18.0.1");
        assert_eq!(
            app_id_of(Address::repeat_byte(0xab)),
            "abababababababababababababababababababab"
        );
    }
}
