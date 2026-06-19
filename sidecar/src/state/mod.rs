//! Bring-up state machine + shared runtime state (sidecar spec §7).
//!
//! `Shared` is the cross-cutting state every subsystem (indexer client, heartbeat
//! loops, gRPC servers, health) reads and updates. `run` drives the full boot
//! sequence — keys → registration → `bringup::launch` — live-proven on Base
//! mainnet (docs/deployment.md).

pub mod gates;

use crate::attestor::AttestationProvider;
use crate::chain::{bundler, userop, ChainClient};
use crate::config::Config;
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

/// memberId = keccak256(abi.encode(cluster, memberContract, keccak256(attestorId))),
/// for the dstack method. Kept for callers/tests that predate the provider seam.
pub fn compute_member_id(cluster: Address, member: Address) -> [u8; 32] {
    compute_member_id_for(cluster, member, keccak256(DSTACK_ATTESTOR_ID).0)
}

/// memberId = keccak256(abi.encode(cluster, memberContract, attestorId)) — the
/// method-agnostic form (`attestorId` comes from the provider).
pub fn compute_member_id_for(cluster: Address, member: Address, attestor_id: [u8; 32]) -> [u8; 32] {
    let enc = (cluster, member, B256::from(attestor_id)).abi_encode_params();
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
        attestor_id: [u8; 32],
    ) -> Arc<Self> {
        let self_member_id = compute_member_id_for(cluster, member_contract, attestor_id);
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

/// Boot orchestration (sidecar spec §7.1): derive keys → resolve the member
/// contract → discover the cluster → sponsored register UserOp → mesh bring-up
/// (`bringup::launch`) → health server. All attestation-method detail lives behind
/// the provider (`ATTESTOR=dstack|operator`); the dstack flow is validated live on
/// Base mainnet (docs/deployment.md).
pub async fn run(config: Config) -> Result<()> {
    let provider = crate::attestor::make_provider(&config)?;
    let keys = Arc::new(provider.derive_keys().await?);
    tracing::info!(
        x_pub = %hex::encode(keys.x_pub),
        ed25519_pub = %hex::encode(keys.ed25519_pub),
        wg_pub = %hex::encode(keys.wg_pub),
        "derived identity keys"
    );

    // Path A (dstack base KMS): when MEMBER_CONTRACT is unset, the member contract IS this
    // CVM's app_id (a stock DstackApp `phala deploy` minted, then upgraded to ClusterMember),
    // learnable only at runtime from /Info. Other methods require MEMBER_CONTRACT.
    let member = resolve_member_contract(&config, provider.as_ref()).await?;
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

    // Default CIDR for v1; a production build reads AttestFacet.meshCidr().
    let shared = Shared::new(
        keys.clone(),
        member,
        cluster,
        0x0a0d0000,
        16,
        config.wg_listen_port,
        provider.attestor_id(),
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

    // Track D — registration. Build this method's register call via the provider and
    // submit it as a sponsored UserOp. Logged verbosely so a live run pinpoints any
    // failure (the provider, the bundler, or the on-chain gate). Retried: the
    // operator-side on-chain prep (allowlists, compose hashes, signer set) may land
    // just after boot, and the bundler can transiently reject. Indexer subscription,
    // peer exchange, heartbeats, CSK, and wg config follow via bringup::launch.
    let mut reg_attempt = 0u32;
    let mut registered = false;
    loop {
        reg_attempt += 1;
        match register_on_chain(&config, provider.as_ref(), &chain, member, cluster, &keys).await {
            Ok(tx) if tx == B256::ZERO => {
                tracing::info!("already registered on-chain; skipping");
                registered = true;
                break;
            }
            Ok(tx) => {
                tracing::info!(tx = %tx, "✔ registration landed on-chain");
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
            provider.clone(),
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
/// custom-app-id KMS path); otherwise asks the provider to self-discover it (dstack
/// Path A reads its `/Info` app_id; methods without self-discovery fail fast).
async fn resolve_member_contract(
    config: &Config,
    provider: &dyn AttestationProvider,
) -> Result<Address> {
    if let Some(m) = config.member_contract {
        if m != Address::ZERO {
            return Ok(m);
        }
    }
    let member = provider.self_member_contract().await?;
    tracing::info!(%member, "MEMBER_CONTRACT unset; provider self-discovered the member contract");
    Ok(member)
}

/// Build this method's register call via the provider and submit it as a sponsored
/// UserOp (bootstrap flow: ClusterMember.validateUserOp checks the userOp signer
/// against the method's bootstrap signer inside the inner calldata). Returns the tx
/// hash, or `B256::ZERO` if the node is already a member.
///
/// NOTE (dstack): the registration signer is the `/GetKey`-derived key the KMS
/// sig-chain attests, NOT `keys.binding_seed` (a separate `derive_key` value). The
/// ClusterMember owner is set to the provider's signer, so every later UserOp must
/// use it too (`AttestationProvider::owner_signer`).
async fn register_on_chain(
    config: &Config,
    provider: &dyn AttestationProvider,
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
        "registration: building register call via the attestation provider");

    let reg = provider
        .build_register_call(cluster, member, keys.x_pub, keys.wg_pub)
        .await
        .context("provider.build_register_call")?;
    let outer = userop::wrap_execute(cluster, reg.calldata);
    let op = userop::UserOperation::new(member, Default::default(), outer);

    let bundler = bundler::BundlerClient::new(
        config.bundler_url.clone(),
        ENTRY_POINT,
        config.chain_id,
        config.gas_policy_id.clone(),
    );
    tracing::info!(policy = %config.gas_policy_id,
        "registration: submitting sponsored register UserOp (bootstrap mode)");
    bundler
        .submit(&reg.signer, op)
        .await
        .context("bundler.submit(register)")
}
