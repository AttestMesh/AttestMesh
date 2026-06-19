// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IClusterMember — the cluster-mediated owner-set callback (contracts spec §9.1.3).
interface IClusterMember {
    /// @notice Set the EIP-4337 owner. Gated to `msg.sender == cluster`. Invoked
    ///         atomically from each attestor facet's register call. Skip-if-set
    ///         (multi-attestor spec): when an owner already exists the call succeeds
    ///         without touching it and emits {OwnerSetSkipped}, so a second-method
    ///         registration's ignored ownerKey stays observable.
    function __setOwnerFromCluster(address newOwner) external;

    function cluster() external view returns (address);
    function owner() external view returns (address);

    /// @notice An owner-set request arrived while an owner was already installed;
    ///         the existing owner was kept and `proposedOwner` ignored.
    event OwnerSetSkipped(address indexed existingOwner, address indexed proposedOwner);
}
