// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";

import { ClusterMemberFactory } from "../src/members/ClusterMemberFactory.sol";

/// @notice Per-node ClusterMember deploy (contracts spec §12.3). The predicted-and-
///         confirmed address is what gets written into the dstack compose config as
///         the CVM's `app_id`.
///
/// Env:
///   PRIVATE_KEY     — broadcaster
///   MEMBER_FACTORY  — ClusterMemberFactory address
///   CLUSTER         — target ClusterDiamond address
///   MEMBER_SALT     — CREATE2 salt (bytes32)
contract DeployMember is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        ClusterMemberFactory factory = ClusterMemberFactory(vm.envAddress("MEMBER_FACTORY"));
        address cluster = vm.envAddress("CLUSTER");
        bytes32 salt = vm.envBytes32("MEMBER_SALT");

        address predicted = factory.predictMemberAddress(cluster, salt);

        vm.startBroadcast(pk);
        address member = factory.deployMember(cluster, salt);
        vm.stopBroadcast();

        require(member == predicted, "address mismatch");
        console2.log("ClusterMember deployed (use as dstack app_id):", member);
    }
}
