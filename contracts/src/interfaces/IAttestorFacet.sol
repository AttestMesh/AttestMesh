// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice One pluggable attestor facet in a cluster deploy: the approved facet
///         implementation address plus the ABI-encoded blob its `initAttestor`
///         consumes (multi-attestor spec, Design §Architecture).
struct AttestorConfig {
    address facet;
    bytes initData;
}

/// @title IAttestorFacet — the convention every attestor facet implements on its
///        stateless implementation contract (multi-attestor spec, Must Have #1).
/// @notice `attestorId` and `selectorManifest` are `pure` on the implementation, so
///         the diamond cut is self-describing: ClusterCut builds attestor cuts by
///         staticcalling `selectorManifest()` against the implementation address,
///         making manifest/cut drift impossible by construction. Neither selector is
///         part of the manifest itself (mirroring DSTACK_ATTESTOR_ID's precedent:
///         derivable off-chain, and registering them per-facet would collide once a
///         cluster installs a second attestor). `initAttestor` runs only as a
///         delegatecall in a diamond's storage context — at deploy time from
///         DiamondInitV2, or from the `diamondCut` init step when an owner adds the
///         facet to a live cluster — and is likewise never cut into the diamond.
interface IAttestorFacet {
    /// @notice The method's stable identifier, e.g. keccak256("attestmesh.attestor.dstack").
    function attestorId() external pure returns (bytes32);

    /// @notice Exactly the selectors to cut into a diamond for this facet.
    function selectorManifest() external pure returns (bytes4[] memory);

    /// @notice Seed this facet's ERC-7201 namespace from an ABI-encoded init blob.
    ///         Delegatecall-only: reverts unless executing in an initialized
    ///         diamond's storage context (core storage seeded first).
    function initAttestor(bytes calldata initData) external;
}
