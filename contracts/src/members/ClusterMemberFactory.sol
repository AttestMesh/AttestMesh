// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { ERC1967Proxy } from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import { Create2 } from "@openzeppelin/contracts/utils/Create2.sol";

import { IClusterMemberFactory } from "../interfaces/IClusterMemberFactory.sol";
import { ClusterMember } from "./ClusterMember.sol";

/// @title ClusterMemberFactory — deterministic CREATE2 deployer for ClusterMembers
///        (contracts spec §9.2).
/// @notice One per chain, shared across all clusters. The member address depends
///         only on (factory, impl, salt, cluster) — not on any owner key — so the
///         operator can predict it before the CVM boots and wire it into the dstack
///         compose config as the `app_id`.
contract ClusterMemberFactory is IClusterMemberFactory {
    address public immutable implementation;
    address public immutable factoryOwner; // AttestMesh org Safe — gates impl swaps

    mapping(address account => bool) public deployedMembers;

    constructor(address impl, address factoryOwner_) {
        implementation = impl;
        factoryOwner = factoryOwner_;
    }

    function deployMember(address cluster_, bytes32 salt) external returns (address member) {
        member = address(new ERC1967Proxy{ salt: salt }(implementation, _initData(cluster_)));
        deployedMembers[member] = true;
        emit MemberDeployed(member, cluster_, salt);
    }

    function predictMemberAddress(address cluster_, bytes32 salt) external view returns (address) {
        bytes32 initCodeHash = keccak256(
            abi.encodePacked(
                type(ERC1967Proxy).creationCode, abi.encode(implementation, _initData(cluster_))
            )
        );
        return Create2.computeAddress(salt, initCodeHash);
    }

    function isOurMember(address account) external view returns (bool) {
        return deployedMembers[account];
    }

    function _initData(address cluster_) internal pure returns (bytes memory) {
        return abi.encodeCall(ClusterMember.initialize, (cluster_));
    }
}
