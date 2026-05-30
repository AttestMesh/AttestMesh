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
│   │   ├── ClusterMember.sol             # UUPS per-CVM passthrough proxy impl
│   │   └── ClusterMemberFactory.sol      # deterministic CREATE2 deployer
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
│   ├── DeployIndexerRegistry.s.sol
│   ├── DeployClusterMemberFactory.s.sol
│   └── DeployCluster.s.sol               # atomic ClusterDiamond + DiamondInit
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

```solidity
struct MemberRecord {
    bytes32 platformId;      // keccak256("teemesh.platform.dstack") etc.
    address memberContract;  // ClusterMember proxy address
    bytes32 xPubKey;         // x25519 public key for sealed-box
    bytes32 wgPubKey;        // wireguard public key (mirror)
    uint64 registeredAt;     // block.timestamp
    bytes32 metadata;        // application-defined; opaque to TeeMesh
}

struct Layout {
    mapping(bytes32 memberId => MemberRecord) members;
    mapping(address memberAddr => bytes32 memberId) memberIdOf;
    bytes32[] memberIds;     // enumeration
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
    // ── View surface ────────────────────────────────────────────
    function isClusterMember(address account) external view returns (bool);
    function memberOf(address account) external view returns (MemberStorage.MemberRecord memory);
    function memberById(bytes32 memberId) external view returns (MemberStorage.MemberRecord memory);
    function xPubKeyOf(bytes32 memberId) external view returns (bytes32);
    function wgPubKeyOf(bytes32 memberId) external view returns (bytes32);
    function listMembers() external view returns (bytes32[] memory);
    function memberCount() external view returns (uint256);

    // ── Events ──────────────────────────────────────────────────
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

        // Cluster owner is recorded in MemberStorage so every facet can read it
        MemberStorage.layout().clusterOwner = args.clusterOwner;
    }
}
```

(`MemberStorage.Layout` gets an additional `address clusterOwner` field for this; corrects §4.1 above.)

For milestone B / multi-platform clusters, `InitArgs` extends with per-platform-facet init blobs. v1 is dstack-only.

---

## 9. ClusterMember + ClusterMemberFactory

### 9.1 ClusterMember

```solidity
contract ClusterMember is Initializable, UUPSUpgradeable, IAppAuth, IAppAuthBasicManagement {
    function initialize(address cluster_) external initializer;
    function cluster() external view returns (address);

    // IAppAuth — forwards to DstackFacet
    function isAppAllowed(AppBootInfo calldata bootInfo)
        external view returns (bool, string memory);

    // IAppAuthBasicManagement — all forwards
    function addComposeHash(bytes32) external;
    // ... (full set forwarded to DstackFacet)
}
```

Storage: just `address cluster` in a single ERC-7201 slot (`teemesh.storage.ClusterMember`).
Upgrade authority: `_authorizeUpgrade` checks `msg.sender == IAttest(cluster).clusterOwner()` (read from the diamond on each call so Safe rotations propagate). Same pattern as dstackgres's `DstackMember`.

### 9.2 ClusterMemberFactory

```solidity
contract ClusterMemberFactory {
    address public immutable implementation;
    address public immutable owner;       // TeeMesh org Safe — controls which impls are deployed

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

CREATE2 deploy of an ERC1967Proxy pointing at `implementation` (the ClusterMember impl) with the initializer calldata `initialize(cluster_)`. The salt is whatever the operator picks; conventionally `keccak256(abi.encode(cluster_, instanceSequenceNumber))`.

`isOurMember` is the lookup DstackFacet uses in step 1 of registration — a simple `deployedMembers[account]` flag set during `deployMember`.

For v1, the factory is **per-org** (one factory deployed by the TeeMesh org Safe, shared across all clusters on the chain). Member impls can be upgraded by deploying a new implementation contract and registering it; existing members continue to point at their original impl until UUPS-upgraded individually.

---

## 10. IndexerRegistry

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

## 11. Deployment scripts

### 11.1 DeployIndexerRegistry.s.sol

One-shot deploy of the per-chain IndexerRegistry. Owner is the TeeMesh org Safe.

### 11.2 DeployClusterMemberFactory.s.sol

One-shot deploy of the per-chain `ClusterMemberFactory` + initial `ClusterMember` implementation. Owner is the TeeMesh org Safe.

### 11.3 DeployCluster.s.sol

Per-cluster deploy. Reads a JSON config:

```json
{
  "clusterOwner": "0x...",          // Safe address
  "kmsRootSigner": "0x...",         // Phala managed KMS root
  "initialComposeHashes": ["0x..."],
  "initialDeviceIds": ["0x..."],
  "allowAnyDevice": false,
  "requireTcbUpToDate": true,
  "platformFacets": ["DstackFacet"]
}
```

Pipeline:

1. Deploy `DiamondInit`.
2. Deploy all platform facets listed in `platformFacets` (DstackFacet only for v1).
3. Deploy all core facets (AttestFacet, MessageFacet, NetworkFacet).
4. Build the facetCuts array.
5. ABI-encode `DiamondInit.init(InitArgs)` calldata.
6. Deploy `ClusterDiamond(facetCuts, address(diamondInit), initCalldata)` — atomic.
7. `transferOwnership(clusterOwner)` on the diamond. (Safe must `acceptOwnership` separately.)
8. Log all addresses.

Output: a JSON receipt with every address for downstream tooling (sidecar config, IndexerRegistry seeding, etc.).

---

## 12. Errors

All errors live in `src/errors/Errors.sol` and are imported where used so revert sigids are stable across compilations.

Categories:

- **Membership**: `NotOurMember()`, `AlreadyRegistered()`, `NotClusterMember()`.
- **Dstack KMS chain**: `KmsRootNotAllowed()`, `KmsAppKeySigInvalid()`, `AppKeyDerivedSigInvalid()`.
- **Dstack allowlist**: `ComposeHashNotAllowed()`, `DeviceNotAllowed()`, `TcbStale()`.
- **Binding**: `BindingSigInvalid()`.
- **Messaging**: `DuplicateEnvelope()`, `RecipientNotMember()`.
- **Admin**: `NotClusterOwner()`, `ClusterDestroyed()` (reserved for milestone B; not used in v1).

---

## 13. Events

Every facet emits the events listed in its section. The Indexer (separate spec) consumes all of them. The minimum event set for v1 demo to be useful:

- `MemberRegistered`
- `WgKeyPublished`
- `MessageSent`

The rest (allowlist mutations, etc.) ride the same pipeline but aren't required for the demo.

---

## 14. Tests (v1 scope)

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

## 15. Open questions (component-level)

1. **MemberStorage `clusterOwner` field vs solidstate owner.** The spec puts a `clusterOwner` field in MemberStorage so every facet's `onlyClusterOwner` modifier can read it from one place. Alternative: each facet reads from solidstate's `OwnableStorage`. The latter unifies ownership but conflates `diamondCut` authority with allowlist authority — they may want to diverge (e.g. a council Safe for diamondCut, an ops Safe for allowlist mutations). v1 keeps them separate.
2. **MessageFacet duplicate-envelope storage cost.** Tracking `envelopeNonces` is one cold SSTORE per send (~20k gas). For a noisy cluster this dominates per-send cost. Alternatives: drop the duplicate check entirely (let readers dedupe), use a bitmap, or bound the lookback window. v1 ships the strict check; revisit if gas becomes a demo blocker.
3. **TCB status comparison.** Currently a `keccak256(bytes(tcbStatus)) == keccak256(bytes("UpToDate"))` check. dstack may emit other accepted strings (e.g. `"SWHardeningNeeded"` with whitelisted advisory ids). v1 is strict; revisit when we have a real TDX deployment to feel out the policy.
