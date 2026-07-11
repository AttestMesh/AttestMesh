//! Bring-up state machine + shared runtime state (sidecar spec §7).
//!
//! `Shared` is the cross-cutting state every subsystem (indexer client, heartbeat
//! loops, gRPC servers, health) reads and updates. `run` drives the full boot
//! sequence — keys → registration → `bringup::launch` — live-proven on Base
//! mainnet (docs/deployment.md).

pub mod gates;

use crate::chain::{bundler, dstack_facet, userop, ChainClient};
use crate::config::Config;
use crate::dstack::DstackRuntime;
use crate::heartbeat::liveness::Liveness;
use crate::keys::KeyMaterial;
use crate::wg::peer::PeerTable;
use crate::wg::{self, MeshControl};
use alloy::primitives::{keccak256, Address, B256};
use alloy::sol_types::SolValue;
use anyhow::{Context, Result};

/// Canonical EIP-4337 v0.7 EntryPoint (identical on every chain).
const ENTRY_POINT: Address =
    alloy::primitives::address!("0000000071727De22E5E9d8BAf0edAc6f37da032");

/// Retry cadence for boot-time chain calls that depend on the operator finishing the
/// Path A on-chain setup (upgrade the stock proxy, allowlist the app_id, add the compose
/// hash) shortly after `phala deploy`. Also absorbs transient RPC/bundler lag.
const RETRY_DELAY: std::time::Duration = std::time::Duration::from_secs(10);
const CLUSTER_DISCOVERY_MAX_ATTEMPTS: u32 = 90; // ~15 min
const REGISTRATION_MAX_ATTEMPTS: u32 = 60; // ~10 min
use gates::Gates;
use std::sync::Arc;
use tokio::sync::{broadcast, Mutex, Notify};

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

/// Punch-upgrade counters (udp-transport-upgrade spec, Interfaces): exposed on
/// the health endpoint and `/metrics`. Purely observational — mesh health never
/// depends on punch outcomes.
#[derive(Default)]
pub struct PunchMetrics {
    pub attempts_total: std::sync::atomic::AtomicU64,
    pub success_total: std::sync::atomic::AtomicU64,
    pub udp_reverts_total: std::sync::atomic::AtomicU64,
}

impl PunchMetrics {
    pub fn snapshot(&self) -> (u64, u64, u64) {
        use std::sync::atomic::Ordering::Relaxed;
        (
            self.attempts_total.load(Relaxed),
            self.success_total.load(Relaxed),
            self.udp_reverts_total.load(Relaxed),
        )
    }
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
    pub punch_metrics: PunchMetrics,
    pub originator_member_id: Mutex<Option<[u8; 32]>>,
    /// Coalescing wake-up for the single CSK pull loop. Peer configuration and
    /// liveness changes should trigger an immediate retry instead of a fixed sleep.
    pub peer_change: Notify,

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
            punch_metrics: PunchMetrics::default(),
            originator_member_id: Mutex::new(None),
            peer_change: Notify::new(),
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

    pub async fn set_originator_member_id(&self, member_id: [u8; 32]) {
        let mut current = self.originator_member_id.lock().await;
        if *current != Some(member_id) {
            *current = Some(member_id);
            drop(current);
            self.peer_change.notify_one();
        }
    }

    pub async fn get_originator_member_id(&self) -> Option<[u8; 32]> {
        *self.originator_member_id.lock().await
    }
}

/// Boot orchestration (sidecar spec §7.1): derive keys → resolve the member
/// contract → discover the cluster → sponsored dstack_register → mesh bring-up
/// (`bringup::launch`) → health server. Validated live on Base mainnet
/// (docs/deployment.md); the §16.2 integration harness reproduces the flow
/// locally against anvil + a mock dstack runtime.
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

    // Path A (dstack base KMS): when MEMBER_CONTRACT is unset, the member contract IS this
    // CVM's app_id (a stock DstackApp `phala deploy` minted, then upgraded to ClusterMember),
    // learnable only at runtime from /Info.
    let member = resolve_member_contract(&config, dstack.as_ref()).await?;
    tracing::info!(%member, "resolved member contract");

    let chain = ChainClient::new(&config.rpc_url, config.chain_id, member, &keys)?;

    // The cluster binding lands when the operator upgrades the stock proxy to ClusterMember
    // and reinitializes it; ClusterMember.cluster() reverts until then. Retry so the sidecar
    // tolerates the upgrade landing after boot (Path A) and transient RPC lag (both paths).
    let mut attempt = 0u32;
    let cluster = loop {
        match chain.cluster_of().await {
            Ok(c) => break c,
            Err(e) => {
                attempt += 1;
                if attempt >= CLUSTER_DISCOVERY_MAX_ATTEMPTS {
                    return Err(e.context("discover cluster (ClusterMember.cluster())"));
                }
                tracing::warn!(attempt, max = CLUSTER_DISCOVERY_MAX_ATTEMPTS, error = ?e,
                    delay_s = RETRY_DELAY.as_secs(),
                    "cluster not resolvable yet (awaiting ClusterMember upgrade?); retrying");
                tokio::time::sleep(RETRY_DELAY).await;
            }
        }
    };
    tracing::info!(%cluster, "discovered cluster diamond");

    let (mesh_cidr_ip, mesh_cidr_prefix) = chain
        .mesh_cidr(cluster)
        .await
        .context("read cluster mesh CIDR")?;
    tracing::info!(
        mesh_cidr = %format!("{}/{}", wg::cidr::fmt_ipv4(mesh_cidr_ip), mesh_cidr_prefix),
        "discovered cluster mesh CIDR"
    );

    let shared = Shared::new(
        keys.clone(),
        member,
        cluster,
        mesh_cidr_ip,
        mesh_cidr_prefix,
        config.wg_listen_port,
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

    // Track D — registration. Build the dstack KMS proof and submit a sponsored
    // dstack_register UserOp. Logged verbosely so a live-CVM run pinpoints any failure
    // (which dstack call, the bundler, or the on-chain gate). Retried: in Path A the
    // operator's allowlist + compose-hash writes may land just after boot, and the bundler
    // can transiently reject. Indexer subscription, peer exchange, heartbeats, CSK, and wg
    // config follow via bringup::launch once registration lands.
    let mut reg_attempt = 0u32;
    let mut registered = false;
    loop {
        reg_attempt += 1;
        match register_on_chain(&config, dstack.as_ref(), &chain, member, cluster, &keys).await {
            Ok(tx) if tx == B256::ZERO => {
                tracing::info!("already registered on-chain; skipping");
                registered = true;
                break;
            }
            Ok(tx) => {
                tracing::info!(tx = %tx, "✔ dstack_register landed on-chain");
                registered = true;
                break;
            }
            Err(e) if reg_attempt < REGISTRATION_MAX_ATTEMPTS => {
                tracing::warn!(attempt = reg_attempt, max = REGISTRATION_MAX_ATTEMPTS, error = ?e,
                    delay_s = RETRY_DELAY.as_secs(),
                    "registration failed; retrying (awaiting allowlist/compose-hash or bundler)");
                tokio::time::sleep(RETRY_DELAY).await;
            }
            Err(e) => {
                tracing::error!(error = ?e, "✗ registration failed permanently");
                break;
            }
        }
    }

    // Mesh bring-up: peers from chain, wg over the gateway TCP leg, envelope
    // exchange, heartbeats, CSK, gRPC servers. Spawns tasks and returns. (The
    // pure-UDP transport upgrade is the remaining deferred piece — see transport.)
    if registered {
        crate::bringup::launch(
            config.clone(),
            dstack.clone(),
            Arc::new(chain),
            shared.clone(),
            wg_ctl.clone(),
        )
        .await?;
    }

    crate::health::serve(shared.clone(), config.health_http_addr.clone()).await?;
    Ok(())
}

/// Resolve the ClusterMember contract address. Uses `MEMBER_CONTRACT` when set (factory /
/// custom-app-id KMS path); otherwise self-discovers it from the dstack `/Info` app_id
/// (Path A: the member contract IS this CVM's provisioned app_id, unknown until runtime).
async fn resolve_member_contract(config: &Config, dstack: &dyn DstackRuntime) -> Result<Address> {
    if let Some(m) = config.member_contract {
        if m != Address::ZERO {
            return Ok(m);
        }
    }
    let info = dstack
        .info()
        .await
        .context("dstack /Info for app_id self-discovery (MEMBER_CONTRACT unset)")?;
    if info.app_id.len() != 20 {
        anyhow::bail!(
            "dstack app_id is {} bytes, expected a 20-byte address",
            info.app_id.len()
        );
    }
    let member = Address::from_slice(&info.app_id);
    tracing::info!(%member,
        "MEMBER_CONTRACT unset; self-discovered member contract from dstack /Info app_id (Path A)");
    Ok(member)
}

/// Build the dstack KMS proof from the runtime and submit a sponsored `dstack_register`
/// UserOp (bootstrap flow: ClusterMember.validateUserOp recovers the binding key from the
/// inner proof). Returns the tx hash, or `B256::ZERO` if the node is already a member.
///
/// NOTE: the registration signer is the `/GetKey`-derived key returned by
/// `build_proof_from_runtime` (the one the KMS sig-chain attests), NOT
/// `keys.binding_seed` (a separate `derive_key` value). The ClusterMember owner is set to
/// this signer, so every later UserOp must use it too — unify on it as bring-up grows.
async fn register_on_chain(
    config: &Config,
    dstack: &dyn DstackRuntime,
    chain: &ChainClient,
    member: Address,
    cluster: Address,
    keys: &KeyMaterial,
) -> Result<B256> {
    // Restart-safe: skip if already registered.
    if chain
        .member_of(cluster)
        .await
        .context("read memberOf")?
        .exists()
    {
        return Ok(B256::ZERO);
    }

    let x_pub = B256::from(keys.x_pub);
    let wg_pub = B256::from(keys.wg_pub);
    tracing::info!(%member, %cluster, %x_pub, %wg_pub,
        "registration: building proof from dstack runtime (/Info + /GetKey)");

    let (proof, binding_signer) =
        dstack_facet::build_proof_from_runtime(dstack, cluster, member, x_pub, wg_pub)
            .await
            .context("build_proof_from_runtime (/Info + /GetKey -> DstackProof)")?;
    tracing::info!(owner = %binding_signer.address(), code_id = %proof.codeId,
        purpose = %proof.purpose, "registration: proof built; derived key is the member owner");

    let inner = dstack_facet::build_register_calldata(proof, member, x_pub, wg_pub);
    let outer = userop::wrap_execute(cluster, inner);
    let op = userop::UserOperation::new(member, Default::default(), outer);

    let bundler = bundler::BundlerClient::new(
        config.bundler_url.clone(),
        config.rpc_url.clone(),
        ENTRY_POINT,
        config.chain_id,
        config.gas_policy_id.clone(),
    );
    tracing::info!(policy = %config.gas_policy_id,
        "registration: submitting sponsored dstack_register UserOp (bootstrap mode)");
    bundler
        .submit(&binding_signer, op)
        .await
        .context("bundler.submit(dstack_register)")
}
