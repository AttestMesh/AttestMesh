// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { MessageHashUtils } from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

import { IDstackFacet } from "../../interfaces/IDstackFacet.sol";
import { IAppAuth } from "../../interfaces/IAppAuth.sol";
import { IAppAuthBasicManagement } from "../../interfaces/IAppAuthBasicManagement.sol";
import { IAttest } from "../../interfaces/IAttest.sol";
import { INetwork } from "../../interfaces/INetwork.sol";
import { IClusterMember } from "../../interfaces/IClusterMember.sol";
import { IClusterMemberFactory } from "../../interfaces/IClusterMemberFactory.sol";

import { MemberStorage } from "../../storage/MemberStorage.sol";
import { DstackStorage } from "../../storage/DstackStorage.sol";
import { ClusterAccess } from "../../access/ClusterAccess.sol";
import { DstackSigChain } from "../../libraries/DstackSigChain.sol";

import {
    NotOurMember,
    KmsRootNotAllowed,
    KmsAppKeySigInvalid,
    AppKeyDerivedSigInvalid,
    ComposeHashNotAllowed,
    DeviceNotAllowed,
    TcbStale,
    BindingSigInvalid
} from "../../errors/Errors.sol";

/// @title DstackFacet — the dstack attestor facet (contracts spec §6).
/// @notice Verifies the dstack KMS 3-level secp256k1 sig chain + a binding
///         signature, then writes the member into the shared registry. Also
///         implements dstack's IAppAuth boot gate and IAppAuthBasicManagement
///         allowlist surface so existing dstack tooling works unchanged.
contract DstackFacet is IDstackFacet, IAppAuth, IAppAuthBasicManagement, ClusterAccess {
    using MessageHashUtils for bytes32;

    bytes32 public constant DSTACK_ATTESTOR_ID = keccak256("attestmesh.attestor.dstack");
    string internal constant BIND_DOMAIN = "attestmesh.bind.v1";

    // ── IAppAuthBasicManagement: allowlist admin ──────────────────────────────

    function addComposeHash(bytes32 composeHash) external onlyClusterOwner {
        DstackStorage.layout().allowedComposeHashes[composeHash] = true;
        emit ComposeHashAdded(composeHash);
    }

    function removeComposeHash(bytes32 composeHash) external onlyClusterOwner {
        DstackStorage.layout().allowedComposeHashes[composeHash] = false;
        emit ComposeHashRemoved(composeHash);
    }

    function addDevice(bytes32 deviceId) external onlyClusterOwner {
        DstackStorage.layout().allowedDeviceIds[deviceId] = true;
        emit DeviceAdded(deviceId);
    }

    function removeDevice(bytes32 deviceId) external onlyClusterOwner {
        DstackStorage.layout().allowedDeviceIds[deviceId] = false;
        emit DeviceRemoved(deviceId);
    }

    function setAllowAnyDevice(bool allowAny) external onlyClusterOwner {
        DstackStorage.layout().allowAnyDevice = allowAny;
        emit AllowAnyDeviceSet(allowAny);
    }

    function setRequireTcbUpToDate(bool require_) external onlyClusterOwner {
        DstackStorage.layout().requireTcbUpToDate = require_;
        emit RequireTcbUpToDateSet(require_);
    }

    function allowedComposeHashes(bytes32 composeHash) external view returns (bool) {
        return DstackStorage.layout().allowedComposeHashes[composeHash];
    }

    function allowedDeviceIds(bytes32 deviceId) external view returns (bool) {
        return DstackStorage.layout().allowedDeviceIds[deviceId];
    }

    function allowAnyDevice() external view returns (bool) {
        return DstackStorage.layout().allowAnyDevice;
    }

    function requireTcbUpToDate() external view returns (bool) {
        return DstackStorage.layout().requireTcbUpToDate;
    }

    /// @notice dstack expects `owner()` here; returns the cluster owner.
    function owner() external view returns (address) {
        return MemberStorage.layout().clusterOwner;
    }

    function version() external pure returns (uint256) {
        return 1;
    }

    // ── AttestMesh KMS-root allowlist admin ───────────────────────────────────

    function addAllowedKmsRoot(address kmsRoot) external onlyClusterOwner {
        DstackStorage.layout().allowedKmsRoots[kmsRoot] = true;
        emit KmsRootAdded(kmsRoot);
    }

    function removeAllowedKmsRoot(address kmsRoot) external onlyClusterOwner {
        DstackStorage.layout().allowedKmsRoots[kmsRoot] = false;
        emit KmsRootRemoved(kmsRoot);
    }

    function allowedKmsRoots(address kmsRoot) external view returns (bool) {
        return DstackStorage.layout().allowedKmsRoots[kmsRoot];
    }

    // ── IAppAuth boot gate (contracts spec §6.2) ──────────────────────────────

    function isAppAllowed(IAppAuth.AppBootInfo calldata bootInfo)
        external
        view
        returns (bool isAllowed, string memory reason)
    {
        DstackStorage.Layout storage d = DstackStorage.layout();
        if (!d.allowedComposeHashes[bootInfo.composeHash]) {
            return (false, "compose hash not allowed");
        }
        if (!d.allowedDeviceIds[bootInfo.deviceId] && !d.allowAnyDevice) {
            return (false, "device not allowed");
        }
        if (MemberStorage.layout().memberIdOf[bootInfo.appId] == bytes32(0)) {
            // appId must be one of this cluster's registered ClusterMembers.
            return (false, "appId not a cluster member");
        }
        if (
            d.requireTcbUpToDate
                && keccak256(bytes(bootInfo.tcbStatus)) != keccak256(bytes("UpToDate"))
        ) {
            return (false, "tcb not up to date");
        }
        return (true, "");
    }

    // ── Registration (contracts spec §6.3) ────────────────────────────────────

    function dstack_register(
        DstackProof calldata proof,
        address memberContract,
        bytes32 xPubKey,
        bytes32 wgPubKey
    ) external returns (bytes32 memberId) {
        DstackStorage.Layout storage d = DstackStorage.layout();

        // 1. memberContract is one of ours.
        address factory = MemberStorage.layout().memberFactory;
        if (!IClusterMemberFactory(factory).isOurMember(memberContract)) revert NotOurMember();

        // 2. KMS root allowed.
        address rootAddr = DstackSigChain.compressedToAddress(proof.kmsRootPubKey);
        if (!d.allowedKmsRoots[rootAddr]) revert KmsRootNotAllowed();

        // 3. KMS root -> app key.
        bytes32 hApp = keccak256(abi.encode("dstack.app", proof.appKey, proof.appComposeHash));
        if (DstackSigChain.recover(hApp, proof.appKeySig) != rootAddr) {
            revert KmsAppKeySigInvalid();
        }

        // 4. Compose hash allowed.
        if (!d.allowedComposeHashes[proof.appComposeHash]) revert ComposeHashNotAllowed();

        // 5. Device allowed.
        if (!d.allowedDeviceIds[proof.derivedDeviceId] && !d.allowAnyDevice) {
            revert DeviceNotAllowed();
        }

        // 6. App key -> derived key.
        address appKeyAddr = DstackSigChain.compressedToAddress(proof.appKey);
        bytes32 hDerived = keccak256(
            abi.encode(
                "dstack.instance",
                proof.derivedPubKey,
                proof.derivedInstanceId,
                proof.derivedDeviceId
            )
        );
        if (DstackSigChain.recover(hDerived, proof.derivedKeySig) != appKeyAddr) {
            revert AppKeyDerivedSigInvalid();
        }

        // 7. TCB freshness.
        if (
            d.requireTcbUpToDate
                && keccak256(bytes(proof.tcbStatus)) != keccak256(bytes("UpToDate"))
        ) {
            revert TcbStale();
        }

        // 8. Binding signature (EIP-191 prefixed bind hash).
        address derivedAddr = DstackSigChain.compressedToAddress(proof.derivedPubKey);
        bytes32 bindHash = keccak256(
                abi.encode(BIND_DOMAIN, address(this), memberContract, xPubKey, wgPubKey)
            ).toEthSignedMessageHash();
        if (DstackSigChain.recover(bindHash, proof.bindingSig) != derivedAddr) {
            revert BindingSigInvalid();
        }

        // 9. Write member + canonical wg key (folded for atomicity).
        memberId = IAttest(address(this))
            ._addMember(
                MemberStorage.MemberRecord({
                attestorId: DSTACK_ATTESTOR_ID,
                memberContract: memberContract,
                xPubKey: xPubKey,
                wgPubKey: wgPubKey,
                registeredAt: uint64(block.timestamp)
            })
            );
        INetwork(address(this))._setWgPubKey(memberId, wgPubKey);

        // 9.5. Install the binding key as the ClusterMember's EIP-4337 owner.
        IClusterMember(memberContract).__setOwnerFromCluster(derivedAddr);

        // 10. Emit dstack-specific event (MemberRegistered already emitted by _addMember).
        emit DstackMemberRegistered(memberId, proof.appComposeHash, proof.derivedDeviceId);
    }
}
