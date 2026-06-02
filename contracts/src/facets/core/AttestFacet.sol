// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { IAttest } from "../../interfaces/IAttest.sol";
import { MemberStorage } from "../../storage/MemberStorage.sol";
import { ClusterAccess } from "../../access/ClusterAccess.sol";
import { OwnableStorage } from "@solidstate/contracts/access/ownable/OwnableStorage.sol";
import { SafeOwnableStorage } from "@solidstate/contracts/access/ownable/SafeOwnableStorage.sol";
import {
    AlreadyRegistered,
    NotOriginator,
    CskCommitmentAlreadySet,
    NotClusterOwner
} from "../../errors/Errors.sol";

/// @title AttestFacet — attestation-method-agnostic member registry (contracts spec §5.1).
/// @notice Owns the shared MemberStorage namespace. Attestor facets write into it
///         via the internal `_addMember` selector; core facets read membership and
///         cluster-wide config from it.
contract AttestFacet is IAttest, ClusterAccess {
    // ── Member registry views ─────────────────────────────────────────────────

    function isClusterMember(address account) external view returns (bool) {
        return MemberStorage.layout().memberIdOf[account] != bytes32(0);
    }

    function memberIdOf(address account) external view returns (bytes32) {
        return MemberStorage.layout().memberIdOf[account];
    }

    function memberOf(address account) external view returns (MemberStorage.MemberRecord memory) {
        MemberStorage.Layout storage l = MemberStorage.layout();
        return l.members[l.memberIdOf[account]];
    }

    function memberById(bytes32 memberId)
        external
        view
        returns (MemberStorage.MemberRecord memory)
    {
        return MemberStorage.layout().members[memberId];
    }

    function xPubKeyOf(bytes32 memberId) external view returns (bytes32) {
        return MemberStorage.layout().members[memberId].xPubKey;
    }

    function wgPubKeyOf(bytes32 memberId) external view returns (bytes32) {
        return MemberStorage.layout().members[memberId].wgPubKey;
    }

    function listMembers() external view returns (bytes32[] memory) {
        return MemberStorage.layout().memberIds;
    }

    function memberCount() external view returns (uint256) {
        return MemberStorage.layout().memberIds.length;
    }

    // ── Cluster-wide config readers ───────────────────────────────────────────

    function clusterOwner() external view returns (address) {
        return MemberStorage.layout().clusterOwner;
    }

    function pendingClusterOwner() external view returns (address) {
        return MemberStorage.layout().pendingClusterOwner;
    }

    function meshCidr() external view returns (uint32 ip, uint8 prefix) {
        MemberStorage.Layout storage l = MemberStorage.layout();
        return (l.meshCidrIp, l.meshCidrPrefix);
    }

    /// @notice On-chain mesh IP derivation (master spec §7.3):
    ///         ip = network | ((uint32(keccak256(memberId)) % (hostCount - 2)) + 1)
    function meshIpOf(bytes32 memberId) external view returns (uint32) {
        MemberStorage.Layout storage l = MemberStorage.layout();
        uint256 hostCount = uint256(1) << (32 - l.meshCidrPrefix);
        uint32 low = uint32(uint256(keccak256(abi.encodePacked(memberId))));
        uint32 offset = uint32((uint256(low) % (hostCount - 2)) + 1);
        return l.meshCidrIp | offset;
    }

    // ── CSK commitment ────────────────────────────────────────────────────────

    /// @notice Publish keccak256(CSK). Originator-only (first registrant), set-once.
    function setCskCommitment(bytes32 commitment) external {
        MemberStorage.Layout storage l = MemberStorage.layout();
        if (l.memberIds.length == 0 || l.memberIdOf[msg.sender] != l.memberIds[0]) {
            revert NotOriginator();
        }
        if (l.cskCommitment != bytes32(0)) revert CskCommitmentAlreadySet();
        l.cskCommitment = commitment;
        emit CskCommitmentSet(commitment);
    }

    function cskCommitment() external view returns (bytes32) {
        return MemberStorage.layout().cskCommitment;
    }

    // ── Internal write surface (only callable from a sibling facet) ───────────

    /// @notice Add a member. Called by an attestor facet via IAttest(address(this)).
    ///         memberId = keccak256(abi.encode(cluster, memberContract, attestorId)).
    function _addMember(MemberStorage.MemberRecord calldata rec)
        external
        onlyInternal
        returns (bytes32 memberId)
    {
        MemberStorage.Layout storage l = MemberStorage.layout();
        if (l.memberIdOf[rec.memberContract] != bytes32(0)) revert AlreadyRegistered();

        memberId = keccak256(abi.encode(address(this), rec.memberContract, rec.attestorId));
        l.members[memberId] = rec;
        l.memberIdOf[rec.memberContract] = memberId;
        l.memberIds.push(memberId);

        emit MemberRegistered(
            memberId, rec.memberContract, rec.attestorId, rec.xPubKey, rec.wgPubKey
        );
    }

    /// @notice Update the denormalized wg-pubkey mirror in MemberStorage.
    ///         Called by NetworkFacet (which owns the canonical NetworkStorage value).
    function _setWgMirror(bytes32 memberId, bytes32 wgPubKey) external onlyInternal {
        MemberStorage.layout().members[memberId].wgPubKey = wgPubKey;
    }

    // ── Cluster-ownership management (contracts spec §5.1) ────────────────────

    function transferClusterOwnership(address newOwner) external onlyClusterOwner {
        MemberStorage.layout().pendingClusterOwner = newOwner;
        emit ClusterOwnershipTransferProposed(newOwner);
    }

    function acceptClusterOwnership() external {
        MemberStorage.Layout storage l = MemberStorage.layout();
        if (msg.sender != l.pendingClusterOwner) revert NotClusterOwner();
        l.clusterOwner = msg.sender;
        l.pendingClusterOwner = address(0);
        emit ClusterOwnershipTransferAccepted(msg.sender);
    }

    /// @notice Fused proposal of BOTH the solidstate owner (DiamondCut authority)
    ///         and the cluster owner (allowlist authority). Caller must hold both.
    function transferBothOwners(address newOwner) external {
        if (msg.sender != OwnableStorage.layout().owner) revert NotClusterOwner();
        if (msg.sender != MemberStorage.layout().clusterOwner) revert NotClusterOwner();
        SafeOwnableStorage.layout().nomineeOwner = newOwner;
        MemberStorage.layout().pendingClusterOwner = newOwner;
        emit BothOwnersTransferProposed(newOwner);
    }

    /// @notice Atomically accept BOTH owner roles. Caller must be the pending
    ///         nominee on both sides. Either both slots move or both revert.
    function acceptBothOwners() external {
        if (msg.sender != SafeOwnableStorage.layout().nomineeOwner) revert NotClusterOwner();
        if (msg.sender != MemberStorage.layout().pendingClusterOwner) revert NotClusterOwner();

        OwnableStorage.layout().owner = msg.sender;
        SafeOwnableStorage.layout().nomineeOwner = address(0);

        MemberStorage.Layout storage l = MemberStorage.layout();
        l.clusterOwner = msg.sender;
        l.pendingClusterOwner = address(0);

        emit BothOwnersTransferAccepted(msg.sender);
    }
}
