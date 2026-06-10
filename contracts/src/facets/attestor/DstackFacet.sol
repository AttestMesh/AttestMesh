// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

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

import { NotOurMember, CodeIdMismatch, BindingMismatch } from "../../errors/Errors.sol";

/// @title DstackFacet — the dstack attestor facet (contracts spec §6).
/// @notice Verifies the dstack KMS signature chain (KMS root -> app key -> derived
///         key -> registration message) via the DstackSigChain library, binds the
///         attested app_id to the member contract, then writes the member into the
///         shared registry. Also implements dstack's IAppAuth boot gate and
///         IAppAuthBasicManagement allowlist surface so existing dstack tooling works
///         unchanged — the allowlist is the boot-gate policy the KMS enforces, not a
///         registration-time self-assertion.
contract DstackFacet is IDstackFacet, IAppAuth, IAppAuthBasicManagement, ClusterAccess {
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

    // ── Owner-seeded app_id allowlist (boot-gate, contracts spec §6.2) ─────────
    // The operator pre-approves a member's app_id (its ClusterMember address, known
    // ahead of boot via the factory's predictMemberAddress) so the KMS boot gate can
    // admit it before it has registered. Owner-operated directly (own gas).

    function addAllowedAppId(address appId) external onlyClusterOwner {
        DstackStorage.layout().allowedAppIds[appId] = true;
        emit AppIdAllowed(appId);
    }

    function removeAllowedAppId(address appId) external onlyClusterOwner {
        DstackStorage.layout().allowedAppIds[appId] = false;
        emit AppIdDisallowed(appId);
    }

    function allowedAppIds(address appId) external view returns (bool) {
        return DstackStorage.layout().allowedAppIds[appId];
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
        if (
            !d.allowedAppIds[bootInfo.appId]
                && MemberStorage.layout().memberIdOf[bootInfo.appId] == bytes32(0)
        ) {
            // appId must be an owner-allowlisted app_id (approved before first boot)
            // or an already-registered member. This breaks the cold-start deadlock:
            // the operator pre-approves the predicted ClusterMember address so the KMS
            // releases keys at first boot, before the node has registered.
            return (false, "appId not allowlisted");
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
        // 1. memberContract is one of ours: either factory-deployed (custom-app-id KMS
        //    path), or an owner-allowlisted app_id (Path A: a dstack-provisioned DstackApp
        //    upgraded to ClusterMember, whose address IS the attested app_id). Both anchors
        //    are owner-controlled; step 2 pins the attested app_id to exactly this address
        //    and step 3 binds the derived key to these keys, so neither can be spoofed.
        address factory = MemberStorage.layout().memberFactory;
        if (
            !IClusterMemberFactory(factory).isOurMember(memberContract)
                && !DstackStorage.layout().allowedAppIds[memberContract]
        ) revert NotOurMember();

        // 2. The KMS-attested app_id (codeId) must be exactly this member contract.
        if (proof.codeId != bytes32(bytes20(memberContract))) revert CodeIdMismatch();

        // 3. The signed registration message must bind exactly this
        //    (cluster, member, xPubKey, wgPubKey). The derived key signs the EIP-191
        //    message of it (checked inside verify); here we pin the preimage so the
        //    proof can't be replayed for a different member or different keys.
        bytes32 expectedMsg =
            keccak256(abi.encode(BIND_DOMAIN, address(this), memberContract, xPubKey, wgPubKey));
        if (proof.messageHash != expectedMsg) revert BindingMismatch();

        // 4. Verify the dstack KMS sig chain: a trusted KMS root issued the app key,
        //    the app key authorised the derived key, and the derived key signed the
        //    registration message. Compose hash / device / TCB are the boot-gate
        //    policy (isAppAllowed), enforced by the KMS before the CVM boots — not
        //    re-asserted here (the sig chain proves the node passed that gate).
        (, address derivedKey) = DstackSigChain.verify(proof, IDstackFacet(address(this)));

        // 5. Write the member + canonical wg key (folded for atomicity).
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

        // 6. Install the derived key as the ClusterMember's EIP-4337 owner (decision #3).
        IClusterMember(memberContract).__setOwnerFromCluster(derivedKey);

        // 7. Emit dstack-specific event (MemberRegistered already emitted by _addMember).
        emit DstackMemberRegistered(memberId, proof.codeId, derivedKey);
    }
}
