// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IOperatorFacet — operator-signature attestor facet surface (multi-attestor spec).
/// @notice TRUST DISCLOSURE: members admitted via `operator_register` are vouched for
///         by an allowlisted operator key, NOT by hardware attestation. Installing
///         this facet changes the cluster's trust model — `isClusterMember` treats
///         all members identically regardless of admission method (master spec §5.1:
///         a cluster picks its attestation policy by picking its facets).
interface IOperatorFacet {
    /// @notice The voucher an allowlisted operator signs to admit a member. The
    ///         signature is an EIP-191 personal-sign over the bind hash
    ///         `keccak256(abi.encode("attestmesh.operator.bind.v1", cluster,
    ///         memberContract, xPubKey, wgPubKey, ownerKey, expiry))` — the same
    ///         binding surface dstack's KMS chain commits to, plus the 4337 owner
    ///         and a deadline bounding the voucher's lifetime.
    struct OperatorProof {
        address signer; // must be in OperatorStorage.signers
        address ownerKey; // becomes the member's 4337 owner
        uint64 expiry; // voucher deadline (block.timestamp)
        bytes signature; // 65-byte ECDSA by `signer` over EIP-191(bindHash)
    }

    function operator_register(
        OperatorProof calldata proof,
        address memberContract,
        bytes32 xPubKey,
        bytes32 wgPubKey
    ) external returns (bytes32 memberId);

    // Owner-managed operator signer allowlist.
    function addOperatorSigner(address signer) external;
    function removeOperatorSigner(address signer) external;
    function operatorSigners() external view returns (address[] memory);
    function isOperatorSigner(address signer) external view returns (bool);

    event OperatorMemberRegistered(
        bytes32 indexed memberId, address indexed signer, address ownerKey
    );
    event OperatorSignerAdded(address indexed signer);
    event OperatorSignerRemoved(address indexed signer);
}
