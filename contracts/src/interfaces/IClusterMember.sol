// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IClusterMember — the cluster-mediated owner-set callback (contracts spec §9.1.3).
interface IClusterMember {
    /// @notice Set the EIP-4337 owner. Gated to `msg.sender == cluster` and only
    ///         while `owner == address(0)`. Invoked atomically from dstack_register.
    function __setOwnerFromCluster(address newOwner) external;

    function cluster() external view returns (address);
    function owner() external view returns (address);
}
