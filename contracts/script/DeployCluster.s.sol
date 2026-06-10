// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";
import { stdJson } from "forge-std/StdJson.sol";

import { ClusterDiamondFactory } from "../src/factory/ClusterDiamondFactory.sol";
import { DiamondInit } from "../src/DiamondInit.sol";

/// @notice Per-cluster deploy (contracts spec §12.2). Reads a JSON config and calls
///         the factory to atomically deploy a ClusterDiamond + apply the default cut
///         + delegatecall DiamondInit.
///
/// Env:
///   PRIVATE_KEY      — broadcaster
///   CLUSTER_FACTORY  — ClusterDiamondFactory address (from the infra receipt)
///   MEMBER_FACTORY   — ClusterMemberFactory address (from the infra receipt)
///   CLUSTER_CONFIG   — path to the cluster JSON config (see contracts spec §12.2)
contract DeployCluster is Script {
    using stdJson for string;

    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        ClusterDiamondFactory factory = ClusterDiamondFactory(vm.envAddress("CLUSTER_FACTORY"));
        address memberFactory = vm.envAddress("MEMBER_FACTORY");

        string memory cfg = vm.readFile(vm.envString("CLUSTER_CONFIG"));

        DiamondInit.InitArgs memory args = DiamondInit.InitArgs({
            clusterOwner: cfg.readAddress(".clusterOwner"),
            kmsRootSigner: cfg.readAddress(".kmsRootSigner"),
            initialComposeHashes: cfg.readBytes32Array(".initialComposeHashes"),
            initialDeviceIds: cfg.readBytes32Array(".initialDeviceIds"),
            allowAnyDevice: cfg.readBool(".allowAnyDevice"),
            requireTcbUpToDate: cfg.readBool(".requireTcbUpToDate"),
            meshCidrIp: uint32(cfg.readUint(".meshCidrIp")),
            meshCidrPrefix: uint8(cfg.readUint(".meshCidrPrefix")),
            memberFactory: memberFactory
        });
        bytes32 salt = cfg.readBytes32(".salt");

        vm.startBroadcast(pk);
        address cluster = factory.deployCluster(args, salt);
        vm.stopBroadcast();

        console2.log("Cluster deployed:", cluster);
        console2.log("  clusterOwner (must acceptOwnership):", args.clusterOwner);
    }
}
