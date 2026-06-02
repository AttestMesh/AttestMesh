// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IClusterMemberFactory — provenance lookups for ClusterMember addresses.
interface IClusterMemberFactory {
    function deployMember(address cluster_, bytes32 salt) external returns (address member);
    function predictMemberAddress(address cluster_, bytes32 salt) external view returns (address);
    function isOurMember(address account) external view returns (bool);

    event MemberDeployed(address indexed member, address indexed cluster, bytes32 salt);
}
