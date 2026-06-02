//! Bring-up state machine + shared runtime state (sidecar spec §7).
//!
//! `Shared` is the cross-cutting state every subsystem (indexer client, heartbeat
//! loops, gRPC servers, health) reads and updates. `run` drives the boot sequence.

pub mod gates;

use crate::chain::ChainClient;
use crate::config::Config;
use crate::heartbeat::liveness::Liveness;
use crate::keys::KeyMaterial;
use crate::wg::peer::PeerTable;
use crate::wg::{self, MeshControl};
use alloy::primitives::{keccak256, Address};
use alloy::sol_types::SolValue;
use anyhow::Result;
use gates::Gates;
use std::sync::Arc;
use tokio::sync::{broadcast, Mutex};

pub const DSTACK_ATTESTOR_ID: &[u8] = b"attestmesh.attestor.dstack";

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Phase {
    Booting,
    Registering,
    Subscribing,
    WaitingPeers,
    PullingCsk,
    WgConfiguring,
    Heartbeating,
    Healthy,
}

impl Phase {
    pub fn as_str(&self) -> &'static str {
        match self {
            Phase::Booting => "booting",
            Phase::Registering => "registering",
            Phase::Subscribing => "subscribing",
            Phase::WaitingPeers => "waiting-peers",
            Phase::PullingCsk => "pulling-csk",
            Phase::WgConfiguring => "wg-configuring",
            Phase::Heartbeating => "heartbeating",
            Phase::Healthy => "healthy",
        }
    }
}

/// Decrypted, addressed-to-us message handed to the app stream.
#[derive(Clone, Debug)]
pub struct AppIncoming {
    pub sender_member_id: [u8; 32],
    pub payload: Vec<u8>,
    pub block_number: u64,
}

/// Peer lifecycle event for the app stream.
#[derive(Clone, Debug)]
pub enum AppPeerEvent {
    Joined { member_id: [u8; 32], mesh_ip: u32 },
    Liveness { member_id: [u8; 32], up: bool },
}

pub struct Shared {
    pub keys: Arc<KeyMaterial>,
    pub member_contract: Address,
    pub cluster: Address,
    pub self_member_id: [u8; 32],
    pub mesh_cidr_ip: u32,
    pub mesh_cidr_prefix: u8,
    pub wg_listen_port: u16,
    pub self_mesh_ip: u32,

    pub peers: Mutex<PeerTable>,
    pub liveness: Mutex<Liveness>,
    pub csk: Mutex<Option<[u8; 32]>>,
    pub phase: Mutex<Phase>,
    pub gates: Gates,

    pub incoming_tx: broadcast::Sender<AppIncoming>,
    pub peer_event_tx: broadcast::Sender<AppPeerEvent>,
}

/// memberId = keccak256(abi.encode(cluster, memberContract, keccak256(attestorId))).
pub fn compute_member_id(cluster: Address, member: Address) -> [u8; 32] {
    let attestor_id = keccak256(DSTACK_ATTESTOR_ID);
    let enc = (cluster, member, attestor_id).abi_encode_params();
    keccak256(enc).0
}

impl Shared {
    pub fn new(
        keys: Arc<KeyMaterial>,
        member_contract: Address,
        cluster: Address,
        mesh_cidr_ip: u32,
        mesh_cidr_prefix: u8,
        wg_listen_port: u16,
    ) -> Arc<Self> {
        let self_member_id = compute_member_id(cluster, member_contract);
        let self_mesh_ip = wg::cidr::derive_ip(&self_member_id, mesh_cidr_ip, mesh_cidr_prefix);
        let (incoming_tx, _) = broadcast::channel(1024);
        let (peer_event_tx, _) = broadcast::channel(1024);
        Arc::new(Self {
            keys,
            member_contract,
            cluster,
            self_member_id,
            mesh_cidr_ip,
            mesh_cidr_prefix,
            wg_listen_port,
            self_mesh_ip,
            peers: Mutex::new(PeerTable::new()),
            liveness: Mutex::new(Liveness::new(self_member_id)),
            csk: Mutex::new(None),
            phase: Mutex::new(Phase::Booting),
            gates: Gates::new(),
            incoming_tx,
            peer_event_tx,
        })
    }

    pub async fn set_phase(&self, p: Phase) {
        let mut g = self.phase.lock().await;
        if *g != p {
            tracing::info!(phase = p.as_str(), "phase transition");
            *g = p;
        }
    }

    pub async fn current_phase(&self) -> Phase {
        *self.phase.lock().await
    }

    pub fn x_pub(&self) -> [u8; 32] {
        self.keys.x_pub
    }

    pub fn mesh_ip_for(&self, member_id: &[u8; 32]) -> u32 {
        wg::cidr::derive_ip(member_id, self.mesh_cidr_ip, self.mesh_cidr_prefix)
    }
}

/// Boot orchestration (sidecar spec §7.1). High-level wiring of the modules; the
/// full end-to-end run additionally needs a live dstack runtime, bundler, Indexer,
/// and peers (exercised by the §16.2 integration harness).
pub async fn run(config: Config) -> Result<()> {
    use crate::dstack::{DstackRuntime, UnixSocketDstack};

    let dstack: Arc<dyn DstackRuntime> =
        Arc::new(UnixSocketDstack::new(config.dstack_socket.clone()));
    let keys = Arc::new(crate::keys::derive_all(dstack.as_ref()).await?);
    tracing::info!(
        x_pub = %hex::encode(keys.x_pub),
        ed25519_pub = %hex::encode(keys.ed25519_pub),
        wg_pub = %hex::encode(keys.wg_pub),
        "derived identity keys"
    );

    let chain = ChainClient::new(
        &config.rpc_url,
        config.chain_id,
        config.member_contract,
        &keys,
    )?;
    let cluster = chain.cluster_of().await?;
    tracing::info!(%cluster, "discovered cluster diamond");

    // Default CIDR for v1; a production build reads AttestFacet.meshCidr().
    let shared = Shared::new(
        keys.clone(),
        config.member_contract,
        cluster,
        0x0a0d0000,
        16,
        51820,
    );

    let wg_ctl: Arc<dyn MeshControl> = Arc::new(wg::CommandWg);
    wg_ctl
        .create_interface(
            &keys.wg_secret.to_bytes(),
            shared.wg_listen_port,
            shared.self_mesh_ip,
            shared.mesh_cidr_prefix,
        )
        .await
        .ok();

    shared.set_phase(Phase::Registering).await;
    // Registration, Indexer subscription, peer exchange, heartbeats, CSK, and the
    // health server are spawned here in the full build. The integration harness
    // (§16.2) drives them against anvil + a mock dstack + a mock Indexer.
    crate::health::serve(shared.clone(), config.health_http_addr.clone()).await?;
    Ok(())
}
