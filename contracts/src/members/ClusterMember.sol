// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Initializable } from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {
    UUPSUpgradeable
} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import { ECDSA } from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import { MessageHashUtils } from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

import { IAccount } from "@account-abstraction/contracts/interfaces/IAccount.sol";
import {
    PackedUserOperation
} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {
    SIG_VALIDATION_FAILED,
    SIG_VALIDATION_SUCCESS
} from "@account-abstraction/contracts/core/Helpers.sol";

import { IAppAuth } from "../interfaces/IAppAuth.sol";
import { IAppAuthBasicManagement } from "../interfaces/IAppAuthBasicManagement.sol";
import { IClusterMember } from "../interfaces/IClusterMember.sol";
import { IAttest } from "../interfaces/IAttest.sol";
import { IDstackFacet } from "../interfaces/IDstackFacet.sol";
import { ClusterMemberStorage } from "../storage/ClusterMemberStorage.sol";
import {
    OnlyEntryPoint,
    OnlyCluster,
    OwnerAlreadySet,
    InvalidBootstrapCall,
    NotClusterOwner
} from "../errors/Errors.sol";

/// @title ClusterMember — per-CVM dstack proxy + EIP-4337 v0.7 smart wallet (contracts spec §9).
/// @notice One address that dstack sees as `app_id`, the diamond sees as `msg.sender`,
///         and the gas webhook sees as `userOperation.sender`. UUPS-upgradeable, gated
///         on the cluster owner.
contract ClusterMember is
    Initializable,
    UUPSUpgradeable,
    IAccount,
    IAppAuth,
    IAppAuthBasicManagement,
    IClusterMember
{
    using MessageHashUtils for bytes32;

    /// Canonical v0.7 EntryPoint, identical on every supported chain.
    address public constant ENTRY_POINT = 0x0000000071727De22E5E9d8BAf0edAc6f37da032;

    bytes4 internal constant EXECUTE_SELECTOR = bytes4(keccak256("execute(address,uint256,bytes)"));
    bytes4 internal constant DSTACK_REGISTER_SELECTOR = IDstackFacet.dstack_register.selector;
    string internal constant BIND_DOMAIN = "attestmesh.bind.v1";

    constructor() {
        _disableInitializers();
    }

    /// @notice One-shot init by the factory. Owner lands later, during dstack_register.
    function initialize(address cluster_) external initializer {
        ClusterMemberStorage.layout().cluster = cluster_;
    }

    function cluster() external view returns (address) {
        return ClusterMemberStorage.layout().cluster;
    }

    /// @notice The EIP-4337 wallet owner (the dstack binding key). address(0) until
    ///         registered. NB: this also satisfies IAppAuthBasicManagement.owner();
    ///         on a ClusterMember it returns the wallet owner, not the cluster owner.
    function owner()
        public
        view
        override(IAppAuthBasicManagement, IClusterMember)
        returns (address)
    {
        return ClusterMemberStorage.layout().owner;
    }

    // ── IAccount (EIP-4337 v0.7) ──────────────────────────────────────────────

    function validateUserOp(
        PackedUserOperation calldata userOp,
        bytes32 userOpHash,
        uint256 missingAccountFunds
    ) external returns (uint256 validationData) {
        if (msg.sender != ENTRY_POINT) revert OnlyEntryPoint();

        (address recovered, ECDSA.RecoverError err,) =
            ECDSA.tryRecover(userOpHash.toEthSignedMessageHash(), userOp.signature);

        if (err != ECDSA.RecoverError.NoError) {
            validationData = SIG_VALIDATION_FAILED;
        } else {
            address ownerAddr = ClusterMemberStorage.layout().owner;
            if (ownerAddr == address(0)) {
                // Bootstrap: must be the registration call; userOp signer must equal
                // the binding signer inside the inner dstack_register calldata.
                address bindSigner = _recoverBindingSigner(userOp.callData);
                validationData = (recovered == bindSigner && recovered != address(0))
                    ? SIG_VALIDATION_SUCCESS
                    : SIG_VALIDATION_FAILED;
            } else {
                validationData =
                    (recovered == ownerAddr) ? SIG_VALIDATION_SUCCESS : SIG_VALIDATION_FAILED;
            }
        }

        if (missingAccountFunds > 0) {
            (bool ok,) = payable(ENTRY_POINT).call{ value: missingAccountFunds }("");
            ok; // EIP-4337: failure here is the EntryPoint's problem, not ours.
        }
    }

    /// @notice Execute a single cluster call. Only the EntryPoint may invoke.
    function execute(address target, uint256 value, bytes calldata data) external {
        if (msg.sender != ENTRY_POINT) revert OnlyEntryPoint();
        (bool ok, bytes memory ret) = target.call{ value: value }(data);
        if (!ok) {
            assembly {
                revert(add(ret, 0x20), mload(ret))
            }
        }
    }

    /// @notice Parse `execute(cluster, 0, dstack_register(...))` and recover the
    ///         binding signer from the inner proof's bindingSig. Reverts on any
    ///         structural deviation from the expected bootstrap shape.
    function _recoverBindingSigner(bytes calldata callData) internal view returns (address) {
        if (callData.length < 100 || bytes4(callData[0:4]) != EXECUTE_SELECTOR) {
            revert InvalidBootstrapCall();
        }
        address clusterAddr = ClusterMemberStorage.layout().cluster;
        address target = address(uint160(uint256(bytes32(callData[4:36]))));
        uint256 value = uint256(bytes32(callData[36:68]));
        if (target != clusterAddr || value != 0) revert InvalidBootstrapCall();

        // Offset to the `bytes data` argument, relative to the start of the args (callData[4:]).
        uint256 dataOffset = 4 + uint256(bytes32(callData[68:100]));
        uint256 dataLen = uint256(bytes32(callData[dataOffset:dataOffset + 32]));
        bytes calldata inner = callData[dataOffset + 32:dataOffset + 32 + dataLen];

        if (inner.length < 4 || bytes4(inner[0:4]) != DSTACK_REGISTER_SELECTOR) {
            revert InvalidBootstrapCall();
        }
        (
            IDstackFacet.DstackProof memory proof,
            address memberContract,
            bytes32 xPubKey,
            bytes32 wgPubKey
        ) = abi.decode(inner[4:], (IDstackFacet.DstackProof, address, bytes32, bytes32));
        if (memberContract != address(this)) revert InvalidBootstrapCall();

        bytes32 bindHash = keccak256(
                abi.encode(BIND_DOMAIN, clusterAddr, address(this), xPubKey, wgPubKey)
            ).toEthSignedMessageHash();
        return ECDSA.recover(bindHash, proof.messageSignature);
    }

    // ── Cluster-mediated owner setting (contracts spec §9.1.3) ────────────────

    function __setOwnerFromCluster(address newOwner) external {
        ClusterMemberStorage.Layout storage l = ClusterMemberStorage.layout();
        if (msg.sender != l.cluster) revert OnlyCluster();
        if (l.owner != address(0)) revert OwnerAlreadySet();
        l.owner = newOwner;
    }

    // ── IAppAuth boot gate (forwarded to the diamond's DstackFacet) ───────────

    function isAppAllowed(IAppAuth.AppBootInfo calldata bootInfo)
        external
        view
        returns (bool, string memory)
    {
        return IAppAuth(ClusterMemberStorage.layout().cluster).isAppAllowed(bootInfo);
    }

    // ── IAppAuthBasicManagement (forwarded to DstackFacet for phala-cli compat) ─
    // Write paths are owner-gated on the facet; management is normally performed
    // directly on the diamond by the cluster owner. These forwards exist for ABI
    // surface parity with a stock dstack app contract.

    function addComposeHash(bytes32 composeHash) external {
        IAppAuthBasicManagement(_cluster()).addComposeHash(composeHash);
    }

    function removeComposeHash(bytes32 composeHash) external {
        IAppAuthBasicManagement(_cluster()).removeComposeHash(composeHash);
    }

    function addDevice(bytes32 deviceId) external {
        IAppAuthBasicManagement(_cluster()).addDevice(deviceId);
    }

    function removeDevice(bytes32 deviceId) external {
        IAppAuthBasicManagement(_cluster()).removeDevice(deviceId);
    }

    function setAllowAnyDevice(bool allowAny) external {
        IAppAuthBasicManagement(_cluster()).setAllowAnyDevice(allowAny);
    }

    function setRequireTcbUpToDate(bool require_) external {
        IAppAuthBasicManagement(_cluster()).setRequireTcbUpToDate(require_);
    }

    function allowedComposeHashes(bytes32 composeHash) external view returns (bool) {
        return IAppAuthBasicManagement(_cluster()).allowedComposeHashes(composeHash);
    }

    function allowedDeviceIds(bytes32 deviceId) external view returns (bool) {
        return IAppAuthBasicManagement(_cluster()).allowedDeviceIds(deviceId);
    }

    function allowAnyDevice() external view returns (bool) {
        return IAppAuthBasicManagement(_cluster()).allowAnyDevice();
    }

    function requireTcbUpToDate() external view returns (bool) {
        return IAppAuthBasicManagement(_cluster()).requireTcbUpToDate();
    }

    function version() external view returns (uint256) {
        return IAppAuthBasicManagement(_cluster()).version();
    }

    // ── UUPS upgrade authority (contracts spec §9.1.4) ────────────────────────

    function _authorizeUpgrade(address) internal view override {
        if (msg.sender != IAttest(ClusterMemberStorage.layout().cluster).clusterOwner()) {
            revert NotClusterOwner();
        }
    }

    function _cluster() internal view returns (address) {
        return ClusterMemberStorage.layout().cluster;
    }

    receive() external payable { }
}
