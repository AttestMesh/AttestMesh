# TeeMesh Contracts — Component Spec

**Status**: Draft v0.1
**Parent spec**: [`teemesh-coordination-layer.md`](./teemesh-coordination-layer.md)
**Component**: `contracts/`
**Last updated**: 2026-05-30

---

## 1. Purpose

This spec defines the on-chain layer of TeeMesh v1: every Solidity contract under `contracts/`, the ERC-7201 storage layout, the external ABI of every facet, errors, events, and the bring-up flow.

Code generation works from this spec. The parent spec defines *what* the system does at the architectural level; this spec defines *what gets compiled*.

---

## 2. Toolchain

- **Foundry** (`forge`, `cast`). Solc `0.8.24`. EVM version `cancun`. Optimizer 200 runs.
- **Dependencies** (`lib/` submodules):
  - `OpenZeppelin/openzeppelin-contracts` — for `IERC165`, `IERC1271`, ECDSA recovery utilities.
  - `OpenZeppelin/openzeppelin-contracts-upgradeable` — for `Initializable`, `UUPSUpgradeable` on `ClusterMember`.
  - `solidstate-network/solidstate-solidity` — diamond base (`SolidStateDiamond`, `DiamondWritable`, `DiamondReadable`, `ERC165Base`, `SafeOwnable`).
  - `eth-infinitism/account-abstraction` — EIP-4337 v0.7 interfaces (`IAccount`, `PackedUserOperation`, `IEntryPoint`).
- **EntryPoint v0.7** (canonical, identical address on every supported chain): `0x0000000071727De22E5E9d8BAf0edAc6f37da032`. Hardcoded into `ClusterMember` as a `constant`; not deployed by us.
- **Targets**:
  - v1: Base Sepolia (chain id 84532).
  - Milestone B: Base mainnet (chain id 8453).

---

## 3. File layout

```
contracts/
├── foundry.toml
├── remappings.txt
├── lib/                                  # forge submodules
├── src/
│   ├── ClusterDiamond.sol                # ERC-2535 proxy
│   ├── DiamondInit.sol                   # one-shot atomic init contract
│   ├── facets/
│   │   ├── core/
│   │   │   ├── AttestFacet.sol           # member registry + isClusterMember
│   │   │   ├── MessageFacet.sol          # encrypted member messaging
│   │   │   └── NetworkFacet.sol          # wireguard pubkey publication
│   │   └── platform/
│   │       └── DstackFacet.sol           # dstack platform facet + IAppAuthBasicManagement
│   ├── members/
│   │   ├── ClusterMember.sol             # UUPS per-CVM contract: dstack passthrough + EIP-4337 smart wallet
│   │   └── ClusterMemberFactory.sol      # deterministic CREATE2 deployer
│   ├── factory/
│   │   └── ClusterDiamondFactory.sol     # CREATE2 atomic ClusterDiamond + DiamondInit deployer
│   ├── registry/
│   │   └── IndexerRegistry.sol           # per-chain Indexer lookup
│   ├── storage/
│   │   ├── MemberStorage.sol             # ERC-7201 layout for AttestFacet
│   │   ├── MessageStorage.sol            # ERC-7201 layout for MessageFacet
│   │   ├── NetworkStorage.sol            # ERC-7201 layout for NetworkFacet
│   │   ├── DstackStorage.sol             # ERC-7201 layout for DstackFacet
│   │   └── _namespaces.txt               # source strings for every namespace
│   ├── libraries/
│   │   └── DstackSigChain.sol            # KMS sig-chain verification (k256)
│   ├── interfaces/
│   │   ├── IAttest.sol
│   │   ├── IMessage.sol
│   │   ├── INetwork.sol
│   │   ├── IDstackFacet.sol
│   │   ├── IAppAuth.sol                  # mirrored from dstack
│   │   ├── IAppAuthBasicManagement.sol   # mirrored from dstack
│   │   └── IIndexerRegistry.sol
│   └── errors/
│       └── Errors.sol                    # canonical error declarations
├── script/
│   ├── DeployInfra.s.sol                 # one-shot per-chain: facets, factories, IndexerRegistry
│   ├── DeployCluster.s.sol               # per-cluster: atomic ClusterDiamond + DiamondInit via the factory
│   └── DeployMember.s.sol                # per-CVM: deploy ClusterMember via the factory
└── test/
    ├── unit/
    ├── integration/
    └── helpers/
```

`script/DeployCluster.s.sol` takes a JSON config (cluster Safe, KMS root signer, initial compose hashes, initial device ids, facet cut list) and emits the constructor calldata for `ClusterDiamond` — see §11.

---

## 4. ERC-7201 storage namespaces

Every facet reads / writes through a single ERC-7201 namespace; no facet ever touches another's slot. Listed by source string (the literal passed to `keccak256` per the EIP).

| Layout struct | Source string | Owner facet |
|---|---|---|
| `MemberStorage.Layout` | `teemesh.storage.Member` | AttestFacet (shared write from platform facets via internal selector) |
| `MessageStorage.Layout` | `teemesh.storage.Message` | MessageFacet |
| `NetworkStorage.Layout` | `teemesh.storage.Network` | NetworkFacet (shared write from AttestFacet for the wg-pubkey mirror) |
| `DstackStorage.Layout` | `teemesh.storage.Dstack` | DstackFacet |

The exact 32-byte slot per layout is computed via:

```solidity
bytes32 internal constant SLOT =
    keccak256(abi.encode(uint256(keccak256("teemesh.storage.<name>")) - 1))
    & ~bytes32(uint256(0xff));
```

`_namespaces.txt` lists every namespace string with its precomputed slot value as a build-time sanity check.

### 4.1 MemberStorage.Layout

This is the canonical layout. AttestFacet owns this namespace; platform facets write into it via the internal `_addMember` selector (§5.1).

```solidity
struct MemberRecord {
    bytes32 platformId;      // keccak256("teemesh.platform.dstack") etc.
    address memberContract;  // ClusterMember address (also the EIP-4337 sender per §9)
    bytes32 xPubKey;         // x25519 public key for sealed-box
    bytes32 wgPubKey;        // wireguard public key (mirror; canonical source is NetworkStorage)
    uint64 registeredAt;     // block.timestamp
}

struct Layout {
    mapping(bytes32 memberId => MemberRecord) members;
    mapping(address memberAddr => bytes32 memberId) memberIdOf;
    bytes32[] memberIds;     // enumeration

    // Cluster-wide config (written once by DiamondInit, updated by AttestFacet's owner-transfer selectors):
    address clusterOwner;
    address pendingClusterOwner;

    // Per-cluster wireguard mesh CIDR (DiamondInit-seeded; immutable thereafter in v1):
    uint32 meshCidrIp;       // packed network address, big-endian (e.g. 0x0a0d0000 for 10.13.0.0)
    uint8 meshCidrPrefix;    // e.g. 16 for /16
}
```

`memberId` is `keccak256(abi.encode(clusterAddr, memberContract, platformId))`.

### 4.2 MessageStorage.Layout

No on-chain storage beyond a per-channel nonce for `envelopeId` collision detection. Messages live in events. Layout:

```solidity
struct Layout {
    mapping(bytes32 channelId => mapping(bytes32 envelopeId => bool seen)) envelopeNonces;
}
```

`channelId` = recipient `memberId`.

### 4.3 NetworkStorage.Layout

```solidity
struct Layout {
    mapping(bytes32 memberId => bytes32 wgPubKey) wgPubKeys;
}
```

(`AttestFacet.MemberRecord.wgPubKey` is a denormalized mirror for one-shot reads — single source of truth lives here, mirror updates atomically.)

### 4.4 DstackStorage.Layout

```solidity
struct Layout {
    mapping(bytes32 composeHash => bool allowed) allowedComposeHashes;
    mapping(bytes32 deviceId => bool allowed) allowedDeviceIds;
    mapping(address kmsRoot => bool allowed) allowedKmsRoots;
    bool allowAnyDevice;
    bool requireTcbUpToDate;
}
```

Mirrors dstackgres's `KmsDstackStorage` + the `IAppAuthBasicManagement` set.

---

## 5. Core facets

### 5.1 AttestFacet

**Storage**: `MemberStorage`.
**Interface**: `IAttest`.

```solidity
interface IAttest is IERC165 {
    // ── Member registry view surface ──────────────────────────────
    function isClusterMember(address account) external view returns (bool);
    function memberOf(address account) external view returns (MemberStorage.MemberRecord memory);
    function memberById(bytes32 memberId) external view returns (MemberStorage.MemberRecord memory);
    function xPubKeyOf(bytes32 memberId) external view returns (bytes32);
    function wgPubKeyOf(bytes32 memberId) external view returns (bytes32);
    function listMembers() external view returns (bytes32[] memory);
    function memberCount() external view returns (uint256);

    // ── Cluster-wide config readers ───────────────────────────────
    function clusterOwner() external view returns (address);
    function pendingClusterOwner() external view returns (address);
    function meshCidr() external view returns (uint32 ip, uint8 prefix);
    function meshIpOf(bytes32 memberId) external view returns (uint32);

    // ── Events ────────────────────────────────────────────────────
    event MemberRegistered(
        bytes32 indexed memberId,
        address indexed memberContract,
        bytes32 indexed platformId,
        bytes32 xPubKey,
        bytes32 wgPubKey
    );
}
```

**Internal write surface** (callable only when `msg.sender == address(this)` — i.e. from another facet running in the diamond's storage context):

```solidity
function _addMember(MemberStorage.MemberRecord calldata rec) external returns (bytes32 memberId);
function _setWgPubKey(bytes32 memberId, bytes32 wgPubKey) external;
```

`_addMember` reverts if `memberIdOf[rec.memberContract] != 0` (no double-registration).

**Cluster-ownership management.** Two distinct ownership concepts live on the diamond — the solidstate owner (DiamondCut authority, exposed by `SolidStateDiamond`'s SafeOwnable) and the cluster owner (allowlist + admin authority, stored in `MemberStorage.clusterOwner`). In production both are typically the same Safe; the runbook for rotating that Safe needs to update both. AttestFacet exposes both an independent transfer for the cluster-owner side and a fused helper for the common case where both should move together.

```solidity
// Independent cluster-owner transfer (two-step, mirrors SafeOwnable's shape).
// pendingClusterOwner() / clusterOwner() are declared on IAttest above.
function transferClusterOwnership(address newOwner) external;   // onlyClusterOwner
function acceptClusterOwnership() external;                     // only the pending owner

// Fused two-step transfer of BOTH owners in lockstep — what the runbook uses
// when rotating the cluster Safe in the typical case where one Safe holds both
// roles. Requires the caller to currently hold *both* roles. The accept side
// (called by the new Safe) atomically writes both slots inside one tx, so the
// inconsistency window collapses to zero.
function transferBothOwners(address newOwner) external;         // requires caller == both current owners
function acceptBothOwners() external;                           // requires caller == both pending owners

event ClusterOwnershipTransferProposed(address indexed pending);
event ClusterOwnershipTransferAccepted(address indexed newOwner);
event BothOwnersTransferProposed(address indexed pending);
event BothOwnersTransferAccepted(address indexed newOwner);
```

Implementation notes:

- `transferBothOwners` internally calls `SafeOwnable.transferOwnership` (queues the solidstate-side pending owner) and writes `MemberStorage.layout().pendingClusterOwner`. Both proposals are revocable until accepted — `transferBothOwners(address(0))` clears them.
- `acceptBothOwners` calls `SafeOwnable.acceptOwnership()` and writes `MemberStorage.layout().clusterOwner` in the same tx. Either both updates land or both revert (atomic).
- Anyone who wants the owners to diverge (e.g. a council Safe for DiamondCut, an ops Safe for allowlists) can use the independent paths — the fused helper does not foreclose that flexibility.

The `clusterOwner` / `pendingClusterOwner` storage slots live in `MemberStorage.Layout` (§4.1) and are read by every facet's `onlyClusterOwner` modifier.

### 5.2 MessageFacet

**Storage**: `MessageStorage`.
**Interface**: `IMessage`.

```solidity
interface IMessage is IERC165 {
    function send(
        bytes32 recipientMemberId,
        bytes32 envelopeId,
        bytes calldata ciphertext
    ) external;

    event MessageSent(
        bytes32 indexed senderMemberId,
        bytes32 indexed recipientMemberId,
        bytes32 indexed envelopeId,
        bytes ciphertext
    );
}
```

`send` reverts if:
- `msg.sender` is not a cluster member (resolved via `AttestFacet.isClusterMember`).
- `recipientMemberId` is not in MemberStorage.
- `envelopeNonces[recipientMemberId][envelopeId]` is already set (duplicate envelope).

On success, marks the nonce and emits `MessageSent`. Ciphertext bytes are emitted as event data only — never stored.

**Reserved envelope ids** (TeeMesh-internal protocol messages — the contract treats them as opaque but the sidecar handles them specially):

| `envelopeId` source string | Purpose | Sender | Recipient |
|---|---|---|---|
| `"teemesh.csk.onboarding.v1"` | Cluster Shared Key onboarding (master spec §8) | Any existing member | A newly-registered onboardee |
| `"teemesh.peer-endpoint.v1"` | Wireguard peer-endpoint exchange (master spec §7.1 step 7) | Any cluster member | Any cluster member |

The `DuplicateEnvelope` revert is what implements the CSK-onboarding-dedup race semantics: multiple existing members racing to onboard a new member will all pass the local "any prior onboarding tx?" check inside the racy window, both send, the first lands and the second reverts at zero protocol cost. v1 takes the dedup at face value — no rate-limiting beyond it.

### 5.3 NetworkFacet

**Storage**: `NetworkStorage` + writes to `MemberStorage` (via AttestFacet's internal `_setWgPubKey`).
**Interface**: `INetwork`.

```solidity
interface INetwork is IERC165 {
    function publishWgKey(bytes32 wgPubKey) external;
    function wgPubKeyOf(bytes32 memberId) external view returns (bytes32);

    event WgKeyPublished(bytes32 indexed memberId, bytes32 wgPubKey);
}
```

`publishWgKey`:
- Reverts if `msg.sender` is not a cluster member.
- Writes `wgPubKey` to `NetworkStorage.wgPubKeys[senderMemberId]`.
- Calls `AttestFacet._setWgPubKey(senderMemberId, wgPubKey)` to update the mirror.
- Emits `WgKeyPublished`.

---

## 6. Platform facet: DstackFacet

**Storage**: `DstackStorage`.
**Interfaces**: `IDstackFacet`, `IAppAuth`, `IAppAuthBasicManagement`.

### 6.1 Allowlist management (IAppAuthBasicManagement)

Mirrors dstack's interface exactly — no rewording, so phala-cli and the dstack KMS see the standard ABI:

```solidity
function addComposeHash(bytes32 composeHash) external;        // onlyClusterOwner
function removeComposeHash(bytes32 composeHash) external;     // onlyClusterOwner
function addDevice(bytes32 deviceId) external;                // onlyClusterOwner
function removeDevice(bytes32 deviceId) external;             // onlyClusterOwner
function setAllowAnyDevice(bool allowAny) external;           // onlyClusterOwner
function setRequireTcbUpToDate(bool require_) external;       // onlyClusterOwner

function allowedComposeHashes(bytes32) external view returns (bool);
function allowedDeviceIds(bytes32) external view returns (bool);
function allowAnyDevice() external view returns (bool);
function requireTcbUpToDate() external view returns (bool);
function owner() external view returns (address);             // returns cluster owner from MemberStorage
function version() external view returns (uint256);           // returns 1 in v1

event ComposeHashAdded(bytes32 indexed composeHash);
event ComposeHashRemoved(bytes32 composeHash);
event DeviceAdded(bytes32 deviceId);
event DeviceRemoved(bytes32 deviceId);
event AllowAnyDeviceSet(bool allowAny);
event RequireTcbUpToDateSet(bool requireUpToDate);
```

Plus TeeMesh-specific additions (admin only):

```solidity
function addAllowedKmsRoot(address kmsRoot) external;         // onlyClusterOwner
function removeAllowedKmsRoot(address kmsRoot) external;      // onlyClusterOwner
function allowedKmsRoots(address) external view returns (bool);

event KmsRootAdded(address indexed kmsRoot);
event KmsRootRemoved(address indexed kmsRoot);
```

### 6.2 Boot gate (IAppAuth)

```solidity
function isAppAllowed(IAppAuth.AppBootInfo calldata bootInfo)
    external
    view
    returns (bool isAllowed, string memory reason);
```

Returns `(true, "")` iff:
- `bootInfo.composeHash` is in `allowedComposeHashes`, AND
- `bootInfo.deviceId` is in `allowedDeviceIds` or `allowAnyDevice` is true, AND
- `bootInfo.appId` is registered as one of this cluster's ClusterMember addresses (looked up via `MemberStorage.memberIdOf`), AND
- if `requireTcbUpToDate`, then `bootInfo.tcbStatus == "UpToDate"`.

This is the call the dstack KMS makes at CVM boot. Implemented as a view because dstack expects it that way.

### 6.3 Registration

```solidity
struct DstackProof {
    // KMS sig chain
    bytes kmsRootPubKey;          // compressed secp256k1
    bytes appKey;                 // compressed secp256k1 derived for compose-hash X
    bytes appKeySig;              // KMS root signature over appKey + binding
    bytes32 appComposeHash;       // compose hash the KMS bound to
    bytes derivedPubKey;          // compressed secp256k1 — the one-shot binding signer
    bytes derivedKeySig;          // app key signature over derivedPubKey + binding
    bytes32 derivedInstanceId;    // instance id the app key bound to
    bytes32 derivedDeviceId;      // device id the app key bound to
    string tcbStatus;
    string[] advisoryIds;

    // Binding signature from the derived key over the registration message
    bytes bindingSig;             // secp256k1 sig over keccak256(REGISTRATION_DOMAIN)
}

function dstack_register(
    DstackProof calldata proof,
    address memberContract,       // ClusterMember proxy address
    bytes32 xPubKey,
    bytes32 wgPubKey
) external returns (bytes32 memberId);
```

Internal verification order (each step reverts with a named error on failure):

1. **`memberContract` is one of ours** — look up `ClusterMemberFactory.isOurMember(memberContract)`. Reverts `NotOurMember()`.
2. **KMS root allowed** — `allowedKmsRoots[deriveAddress(proof.kmsRootPubKey)]` must be true. Reverts `KmsRootNotAllowed()`.
3. **KMS root → app key** — verify `proof.appKeySig` is a valid secp256k1 signature by `proof.kmsRootPubKey` over `keccak256(abi.encode("dstack.app", proof.appKey, proof.appComposeHash))`. Reverts `KmsAppKeySigInvalid()`.
4. **Compose hash allowed** — `allowedComposeHashes[proof.appComposeHash]` must be true. Reverts `ComposeHashNotAllowed()`.
5. **Device allowed** — `allowedDeviceIds[proof.derivedDeviceId] || allowAnyDevice` must be true. Reverts `DeviceNotAllowed()`.
6. **App key → derived key** — verify `proof.derivedKeySig` is a valid secp256k1 signature by `proof.appKey` over `keccak256(abi.encode("dstack.instance", proof.derivedPubKey, proof.derivedInstanceId, proof.derivedDeviceId))`. Reverts `AppKeyDerivedSigInvalid()`.
7. **TCB freshness** — if `requireTcbUpToDate`, then `keccak256(bytes(proof.tcbStatus)) == keccak256(bytes("UpToDate"))`. Reverts `TcbStale()`.
8. **Binding** — compute `bindHash = keccak256(abi.encode(BIND_DOMAIN, address(this), memberContract, xPubKey, wgPubKey))` where `BIND_DOMAIN = "teemesh.bind.v1"`. Recover signer from `proof.bindingSig` over the EIP-191 prefixed bindHash. Recovered address must equal `deriveAddress(proof.derivedPubKey)`. Reverts `BindingSigInvalid()`.
9. **Write member** — construct `MemberRecord`, call `IAttest(address(this))._addMember(rec)`, capture returned `memberId`, call `INetwork(address(this))._setWgPubKey(memberId, wgPubKey)` (folded for atomicity — see boot-flow note in master spec §7.1 step 5).
9.5. **Set the ClusterMember's owner** — call `ClusterMember(memberContract).__setOwnerFromCluster(deriveAddress(proof.derivedPubKey))`. This closes the EIP-4337 bootstrap window for this member: every subsequent UserOp will be validated against `owner == bindingKeyAddress` in standard LightAccount mode. Reverts if `__setOwnerFromCluster` is rejected (which would only happen if the owner is somehow already set — unreachable in normal flow).
10. **Emit** — `MemberRegistered` is emitted by `_addMember`. DstackFacet additionally emits `DstackMemberRegistered(memberId, proof.appComposeHash, proof.derivedDeviceId)` for indexer convenience.

`DstackSigChain.sol` library provides the secp256k1 verification primitives (`recover`, `compressedToAddress`).

### 6.4 platformId

```solidity
bytes32 constant DSTACK_PLATFORM_ID = keccak256("teemesh.platform.dstack");
```

Stamped on every `MemberRecord.platformId` written by DstackFacet.

---

## 7. ClusterDiamond

```solidity
contract ClusterDiamond is SolidStateDiamond {
    constructor(
        FacetCut[] memory facetCuts,
        address init,
        bytes memory initCalldata
    ) SolidStateDiamond() {
        _diamondCut(facetCuts, init, initCalldata);
    }
}
```

Identical to dstackgres's pattern. SolidStateDiamond registers built-in selectors (DiamondCut, loupe, ERC-165, SafeOwnable) and `_setOwner(msg.sender)` (the deployer becomes the *solidstate owner* — distinct from the *cluster owner* in MemberStorage). The deployer must `transferOwnership` to the cluster Safe immediately after deploy; the Safe `acceptOwnership` and then controls all future `diamondCut` calls.

**Two owners**, intentionally:

1. **Solidstate owner** — controls `diamondCut`. Set by SolidStateDiamond's constructor.
2. **Cluster owner** — controls allowlists, admin selectors. Lives in DstackStorage / future per-facet storage, set by DiamondInit.

In production both are the same Safe; the runbook covers both transfers.

---

## 8. DiamondInit

A one-shot init contract `delegatecall`'d from `ClusterDiamond`'s constructor. Runs in the diamond's storage context, seeds every facet's namespace in one transaction.

```solidity
contract DiamondInit {
    struct InitArgs {
        address clusterOwner;             // Safe address — written into per-facet ownership slots
        address kmsRootSigner;            // initial allowed KMS root (added to DstackStorage)
        bytes32[] initialComposeHashes;   // seeded into DstackStorage
        bytes32[] initialDeviceIds;       // seeded into DstackStorage
        bool allowAnyDevice;
        bool requireTcbUpToDate;
        uint32 meshCidrIp;                // network address of the cluster's wireguard CIDR (e.g. 10.13.0.0 → 0x0a0d0000)
        uint8 meshCidrPrefix;             // prefix length (e.g. 16 for /16). Sidecars compute peer IPs deterministically.
    }

    function init(InitArgs calldata args) external {
        DstackStorage.Layout storage d = DstackStorage.layout();
        d.allowedKmsRoots[args.kmsRootSigner] = true;
        for (uint256 i; i < args.initialComposeHashes.length; ++i) {
            d.allowedComposeHashes[args.initialComposeHashes[i]] = true;
        }
        for (uint256 i; i < args.initialDeviceIds.length; ++i) {
            d.allowedDeviceIds[args.initialDeviceIds[i]] = true;
        }
        d.allowAnyDevice = args.allowAnyDevice;
        d.requireTcbUpToDate = args.requireTcbUpToDate;

        MemberStorage.Layout storage m = MemberStorage.layout();
        m.clusterOwner = args.clusterOwner;
        m.meshCidrIp = args.meshCidrIp;
        m.meshCidrPrefix = args.meshCidrPrefix;
    }
}
```

All cluster-wide config — `clusterOwner`, `pendingClusterOwner`, `meshCidrIp`, `meshCidrPrefix` — lives in `MemberStorage` per the canonical layout in §4.1. AttestFacet's `meshCidr()` and `meshIpOf(bytes32)` view selectors (declared on `IAttest`, §5.1) are the read paths; `meshIpOf` performs the master-spec §7.3 derivation on chain for clients that don't want to re-implement it.

For milestone B / multi-platform clusters, `InitArgs` extends with per-platform-facet init blobs. v1 is dstack-only.

---

## 9. ClusterMember + ClusterMemberFactory

ClusterMember is the per-CVM contract that does double duty: dstack-style app proxy (so dstack's KMS recognizes the CVM via `IAppAuth` at boot) **and** EIP-4337 smart wallet (so the sidecar can submit gasless UserOps via Alchemy's bundler with the Cloudflare-Worker-validated paymaster — master spec §13 item 18). One contract per CVM; one address that dstack sees as `app_id` and that the cluster diamond sees as `msg.sender` on every member operation.

### 9.1 ClusterMember

```solidity
contract ClusterMember is
    Initializable,
    UUPSUpgradeable,
    IAccount,                 // EIP-4337 v0.7
    IAppAuth,                 // dstack KMS boot gate
    IAppAuthBasicManagement   // phala-cli compat (forwarded to DstackFacet)
{
    /// Canonical v0.7 EntryPoint, identical on every supported chain.
    address public constant ENTRY_POINT = 0x0000000071727De22E5E9d8BAf0edAc6f37da032;

    /// One-shot init by the factory. Owner is *not* set here; it lands during
    /// dstack_register via a diamond-mediated callback (see §6.3 + §9.1.3).
    function initialize(address cluster_) external initializer;

    function cluster() external view returns (address);
    function owner() external view returns (address);   // address(0) until registered

    // ── IAccount (EIP-4337) ─────────────────────────────────────────────────
    function validateUserOp(
        PackedUserOperation calldata userOp,
        bytes32 userOpHash,
        uint256 missingAccountFunds
    ) external returns (uint256 validationData);

    function execute(address target, uint256 value, bytes calldata data) external;
    // executeBatch is intentionally omitted from v1 — one call per UserOp.

    // ── IAppAuth ────────────────────────────────────────────────────────────
    function isAppAllowed(AppBootInfo calldata bootInfo)
        external view returns (bool, string memory);

    // ── IAppAuthBasicManagement — all forwarded to DstackFacet ──────────────
    function addComposeHash(bytes32) external;
    // ... (full IAppAuthBasicManagement surface forwarded)

    // ── Cluster-mediated owner setting (see §9.1.3) ─────────────────────────
    function __setOwnerFromCluster(address newOwner) external;
}
```

#### 9.1.1 Storage

`teemesh.storage.ClusterMember` ERC-7201 namespace:

```solidity
struct Layout {
    address cluster;          // the ClusterDiamond this member belongs to
    address owner;            // dstack-derived secp256k1 address; address(0) until first dstack_register
    // EIP-4337 v0.7 nonces are tracked in the EntryPoint, not here.
}
```

#### 9.1.2 EIP-4337 validateUserOp

Two-mode validation, gated on whether `owner` has been set:

```
if owner == address(0):
    // Bootstrap mode: this is the very first call, must be the registration call.
    // 1. The userOp.callData MUST be execute(target=cluster, value=0, data=<dstack_register selector + args>).
    //    Reject otherwise.
    // 2. Recover signer from userOp.signature against userOpHash (standard EIP-191 / ERC-1271).
    // 3. Decode the inner dstack_register calldata. Recover the signer of the proof's bindingSig
    //    against the registration bind-hash (the same recovery DstackFacet will do).
    // 4. Require: the userOp signer (from step 2) == the bindingSig signer (from step 3).
    //    This proves the UserOp is authorized by the same key that will validate the proof.
    // 5. Accept. Validation data = 0 (no time window, no aggregator).

if owner != address(0):
    // Standard mode: signature must be from owner.
    // ECDSA-recover from userOp.signature against userOpHash; require recovered == owner.
    // Validation data = 0.
```

In both modes: if `missingAccountFunds > 0` (no paymaster sponsoring), transfer that amount back to the EntryPoint. v1 assumes paymaster sponsorship always — funds will always be 0 — but the conditional is required by EIP-4337 v0.7.

`execute(target, value, data)` is gated on `msg.sender == ENTRY_POINT`. No other caller may invoke it.

#### 9.1.3 Owner setting

`__setOwnerFromCluster(newOwner)` is gated on `msg.sender == cluster` AND `owner == address(0)`. It can only be called once, only by the cluster diamond, and only if the owner has not yet been set. It is invoked by `DstackFacet.dstack_register` (§6.3 step 9.5 — added below) as part of the same transaction that validates the registration proof. Together with the bootstrap-mode validateUserOp check, this means:

- The bootstrap UserOp authenticates the binding key (via the bindingSig recovery).
- DstackFacet verifies the dstack KMS chain proves that binding key was granted to this CVM.
- DstackFacet calls `__setOwnerFromCluster(bindingKeyAddress)` atomically.
- All subsequent UserOps from this ClusterMember are validated against `owner` in standard mode.

The bootstrap window is open for exactly one UserOp. After the registration tx mines, `owner` is set forever.

#### 9.1.4 Upgrade authority

`_authorizeUpgrade(newImpl)` checks `msg.sender == IAttest(cluster).clusterOwner()` — read fresh from the diamond on each call, so cluster-Safe rotations propagate without any per-member action. Same pattern as dstackgres's `DstackMember`.

### 9.2 ClusterMemberFactory

```solidity
contract ClusterMemberFactory {
    address public immutable implementation;
    address public immutable factoryOwner;   // TeeMesh org Safe — gates impl swaps

    function deployMember(address cluster_, bytes32 salt)
        external
        returns (address member);

    function predictMemberAddress(address cluster_, bytes32 salt)
        external
        view
        returns (address);

    function isOurMember(address account) external view returns (bool);

    event MemberDeployed(address indexed member, address indexed cluster, bytes32 salt);
}
```

CREATE2 deploy of an ERC1967Proxy pointing at `implementation` with init calldata `initialize(cluster_)`. The salt is operator-chosen; conventionally `keccak256(abi.encode(cluster_, instanceSequenceNumber))`. The CREATE2 address depends only on `(factory, impl, salt, cluster)` — not on the owner key — so the operator can predict it before the CVM ever boots, and dstack's `app_id` can be set to this predicted address as part of the compose config.

`isOurMember(account)` is the boolean lookup DstackFacet uses in step 1 of registration and the gas webhook uses for sender-provenance — a simple `deployedMembers[account]` flag set during `deployMember`. The webhook reads this view at the bundler's verification step to decide whether to sponsor a UserOp.

For v1, the factory is **per-org** (one factory deployed by the TeeMesh org Safe, shared across all clusters on the chain). Member impls can be upgraded by deploying a new implementation contract and registering it; existing members continue to point at their original impl until UUPS-upgraded individually.

---

## 10. ClusterDiamondFactory

The canonical per-chain factory for deploying ClusterDiamonds. Two consumers care about it:

1. **Operators** call `deployCluster(InitArgs)` to atomically deploy a ClusterDiamond + DiamondInit and apply the initial facet cuts.
2. **The gas-sponsorship webhook** calls `isDeployedCluster(address)` over RPC to decide whether a UserOp's target is a TeeMesh cluster the operator is willing to sponsor.

```solidity
contract ClusterDiamondFactory {
    address public immutable factoryOwner;          // TeeMesh org Safe — gates upgrades
    address public immutable diamondInitImpl;       // canonical DiamondInit contract
    address public immutable attestFacet;           // canonical AttestFacet impl
    address public immutable messageFacet;          // canonical MessageFacet impl
    address public immutable networkFacet;          // canonical NetworkFacet impl
    address public immutable dstackFacet;           // canonical DstackFacet impl
    // Adding additional platform facets later is a factory upgrade (or a new factory).

    function deployCluster(DiamondInit.InitArgs calldata args, bytes32 salt)
        external
        returns (address cluster);

    function predictClusterAddress(bytes32 salt)
        external
        view
        returns (address);

    function isDeployedCluster(address account) external view returns (bool);

    event ClusterDeployed(address indexed cluster, address indexed clusterOwner, bytes32 salt);
}
```

`deployCluster(args, salt)`:

1. Builds the standard FacetCut array (AttestFacet selectors, MessageFacet, NetworkFacet, DstackFacet — the v1 default cut).
2. CREATE2-deploys `ClusterDiamond` at `predictClusterAddress(salt)` with the facet cuts + DiamondInit address + ABI-encoded `init(args)` calldata.
3. ClusterDiamond's constructor delegatecalls DiamondInit, seeding the dstack KMS root, compose-hash allowlist, device allowlist, allowAnyDevice / requireTcbUpToDate flags, mesh CIDR, and cluster owner — all atomic per master spec §13 item 8.
4. Marks `deployedClusters[address(diamond)] = true`.
5. Calls `diamond.transferOwnership(args.clusterOwner)` — the cluster Safe must `acceptOwnership` separately.
6. Emits `ClusterDeployed`.

`isDeployedCluster(account)` is the read the gas webhook uses to validate UserOp targets. Cached in Cloudflare KV for 24h per the gas-webhook spec (see `docs/specs/gas-webhook.md`).

`factoryOwner` controls only future factory upgrades (e.g. swapping in a new default facet set). It does **not** retain any authority over already-deployed clusters — each cluster is independently owned by its own Safe after `transferOwnership` lands.

For v1, one ClusterDiamondFactory is deployed per chain (Sepolia for v1, mainnet for milestone B). The address is hardcoded into the gas webhook's env config and the sidecar binary (via the IndexerRegistry pattern — see §11).

---

## 11. IndexerRegistry

A tiny per-chain registry that the CVM sidecar reads at startup to discover the Indexer.

```solidity
contract IndexerRegistry {
    struct IndexerRecord {
        string endpoint;          // "https://indexer.teesql.io:443" or similar
        bytes32 codeId;           // attested Indexer code identifier
        bytes32 pubKey;           // ed25519 pubkey the Indexer signs envelopes with
        uint64 updatedAt;
    }

    address public immutable owner;       // TeeMesh org Safe
    IndexerRecord public current;

    function setIndexer(IndexerRecord calldata rec) external;   // onlyOwner

    event IndexerUpdated(IndexerRecord rec);
}
```

One deployed instance per chain (on Sepolia for v1; on Base mainnet for milestone B). Address is hard-coded into the sidecar binary per chain id, so it doesn't need any other discovery.

A future v2 may key the registry by chain id and expose `indexerOf(uint256 chainId)`. v1 is single-record.

---

## 12. Deployment scripts

The per-chain infrastructure (IndexerRegistry, ClusterMemberFactory, ClusterMember impl, DiamondInit impl, core facets, DstackFacet impl, ClusterDiamondFactory) is deployed once by the TeeMesh org Safe. Per-cluster deploys then go through the factory.

### 12.1 DeployInfra.s.sol

One-shot, run once per chain by the TeeMesh org Safe. Deploys (in order):

1. The four core/platform facet impls (`AttestFacet`, `MessageFacet`, `NetworkFacet`, `DstackFacet`).
2. The `DiamondInit` impl.
3. The `ClusterMember` impl.
4. `ClusterMemberFactory(impl = ClusterMember)`.
5. `ClusterDiamondFactory(diamondInitImpl, attestFacet, messageFacet, networkFacet, dstackFacet)`.
6. `IndexerRegistry(owner = org Safe)`.

Logs every address into a chain-id-stamped JSON receipt under `script/deployments/<chainId>.json`. The sidecar binary hardcodes these per chain id; the gas webhook reads them from env config.

### 12.2 DeployCluster.s.sol

Per-cluster deploy. Reads a JSON config:

```json
{
  "clusterOwner": "0x...",          // Safe address
  "kmsRootSigner": "0x...",         // Phala managed KMS root
  "initialComposeHashes": ["0x..."],
  "initialDeviceIds": ["0x..."],
  "allowAnyDevice": false,
  "requireTcbUpToDate": true,
  "meshCidrIp": 167903232,          // 10.13.0.0 packed
  "meshCidrPrefix": 16,
  "salt": "0x..."
}
```

Pipeline:

1. Construct `DiamondInit.InitArgs` from the JSON.
2. Call `ClusterDiamondFactory.deployCluster(initArgs, salt)`. The factory atomically deploys ClusterDiamond + applies the default facet cut + delegatecalls DiamondInit + transfers solidstate ownership to the cluster Safe (the cluster Safe must `acceptOwnership` separately).
3. Log the deployed cluster address.

Output: a JSON receipt for downstream tooling (sidecar config, dstack compose-config app_id seeding, etc.).

### 12.3 DeployMember.s.sol

Per-CVM deploy. Reads `(clusterAddr, salt)`:

1. Call `ClusterMemberFactory.deployMember(cluster_, salt)`. ClusterMember lands at the predicted CREATE2 address, initialized with `cluster_` and `owner = address(0)`.
2. Log the predicted-and-confirmed address. This is what gets written into the dstack compose config as the CVM's `app_id`.

---

## 13. Errors

All errors live in `src/errors/Errors.sol` and are imported where used so revert sigids are stable across compilations.

Categories:

- **Membership**: `NotOurMember()`, `AlreadyRegistered()`, `NotClusterMember()`.
- **Dstack KMS chain**: `KmsRootNotAllowed()`, `KmsAppKeySigInvalid()`, `AppKeyDerivedSigInvalid()`.
- **Dstack allowlist**: `ComposeHashNotAllowed()`, `DeviceNotAllowed()`, `TcbStale()`.
- **Binding**: `BindingSigInvalid()`.
- **Messaging**: `DuplicateEnvelope()`, `RecipientNotMember()`.
- **Admin**: `NotClusterOwner()`, `ClusterDestroyed()` (reserved for milestone B; not used in v1).

---

## 14. Events

Every facet emits the events listed in its section. The Indexer (separate spec) consumes all of them. The minimum event set for v1 demo to be useful:

- `MemberRegistered`
- `WgKeyPublished`
- `MessageSent`

The rest (allowlist mutations, etc.) ride the same pipeline but aren't required for the demo.

---

## 15. Tests (v1 scope)

`test/` is split into:

- **unit/** — per-facet, mocked diamond storage. One test file per facet.
- **integration/** — full bring-up through `DeployCluster.s.sol`, with a mock dstack KMS chain (sigs produced in-test). Three-CVM scenarios:
  1. Three members register successfully, exchange test messages, every pubkey readable.
  2. A fourth member tries to register with a non-allowed compose hash → reverts.
  3. A non-member tries to send a message → reverts.
  4. A member tries to re-register → reverts.
  5. The cluster owner removes a compose hash → existing members still work, new registration with that hash reverts.
- **helpers/** — mock KMS chain signer, sealed-box utilities for test encryption.

No fuzz / invariant tests for v1. Add in milestone B.

---

## 16. Open questions (component-level)

1. *(Resolved)* `MemberStorage.clusterOwner` and solidstate's owner are intentionally separate slots so DiamondCut authority and allowlist authority can diverge in principle. v1 ships the fused `transferBothOwners` / `acceptBothOwners` pair on AttestFacet for the common case where they should rotate together (a single cluster Safe), plus independent `transferClusterOwnership` / `acceptClusterOwnership` for the diverging case. See AttestFacet ownership-management section.
2. **MessageFacet duplicate-envelope storage cost.** Tracking `envelopeNonces` is one cold SSTORE per send (~20k gas). For a noisy cluster this dominates per-send cost. Alternatives: drop the duplicate check entirely (let readers dedupe), use a bitmap, or bound the lookback window. v1 ships the strict check; revisit if gas becomes a demo blocker.
3. **TCB status comparison.** Currently a `keccak256(bytes(tcbStatus)) == keccak256(bytes("UpToDate"))` check. dstack may emit other accepted strings (e.g. `"SWHardeningNeeded"` with whitelisted advisory ids). v1 is strict; revisit when we have a real TDX deployment to feel out the policy.
