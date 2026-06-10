// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { MemberStorage } from "../storage/MemberStorage.sol";

/// @title IAttest — attestation-method-agnostic member registry (contracts spec §5.1).
/// @dev ERC-165 support for the cluster is provided by the SolidStateDiamond base;
///      this interface intentionally does not extend IERC165 so facets can inherit
///      it without re-registering the `supportsInterface` selector.
interface IAttest {
    // ── Member registry view surface ──────────────────────────────────────────
    function isClusterMember(address account) external view returns (bool);
    function memberIdOf(address account) external view returns (bytes32);
    function memberOf(address account) external view returns (MemberStorage.MemberRecord memory);
    function memberById(bytes32 memberId) external view returns (MemberStorage.MemberRecord memory);
    function xPubKeyOf(bytes32 memberId) external view returns (bytes32);
    function wgPubKeyOf(bytes32 memberId) external view returns (bytes32);
    function listMembers() external view returns (bytes32[] memory);
    function memberCount() external view returns (uint256);

    // ── Cluster-wide config readers ───────────────────────────────────────────
    function clusterOwner() external view returns (address);
    function pendingClusterOwner() external view returns (address);
    function meshCidr() external view returns (uint32 ip, uint8 prefix);
    function meshIpOf(bytes32 memberId) external view returns (uint32);

    // ── CSK commitment ────────────────────────────────────────────────────────
    function setCskCommitment(bytes32 commitment) external;
    function cskCommitment() external view returns (bytes32);

    // ── Cluster-ownership management ──────────────────────────────────────────
    function transferClusterOwnership(address newOwner) external;
    function acceptClusterOwnership() external;
    function transferBothOwners(address newOwner) external;
    function acceptBothOwners() external;

    // ── Internal write surface (gated msg.sender == address(this)) ────────────
    function _addMember(MemberStorage.MemberRecord calldata rec) external returns (bytes32 memberId);
    function _setWgMirror(bytes32 memberId, bytes32 wgPubKey) external;

    // ── Events ────────────────────────────────────────────────────────────────
    event MemberRegistered(
        bytes32 indexed memberId,
        address indexed memberContract,
        bytes32 indexed attestorId,
        bytes32 xPubKey,
        bytes32 wgPubKey
    );
    event CskCommitmentSet(bytes32 commitment);
    event ClusterOwnershipTransferProposed(address indexed pending);
    event ClusterOwnershipTransferAccepted(address indexed newOwner);
    event BothOwnersTransferProposed(address indexed pending);
    event BothOwnersTransferAccepted(address indexed newOwner);
}
