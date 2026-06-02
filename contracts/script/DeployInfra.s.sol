// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";

import { AttestFacet } from "../src/facets/core/AttestFacet.sol";
import { MessageFacet } from "../src/facets/core/MessageFacet.sol";
import { NetworkFacet } from "../src/facets/core/NetworkFacet.sol";
import { DstackFacet } from "../src/facets/attestor/DstackFacet.sol";
import { DiamondInit } from "../src/DiamondInit.sol";
import { ClusterMember } from "../src/members/ClusterMember.sol";
import { ClusterMemberFactory } from "../src/members/ClusterMemberFactory.sol";
import { ClusterDiamondFactory } from "../src/factory/ClusterDiamondFactory.sol";
import { IndexerRegistry } from "../src/registry/IndexerRegistry.sol";

/// @notice One-shot per-chain infra deploy (contracts spec §12.1). Run once by the
///         AttestMesh org Safe. Writes a chain-id-stamped JSON receipt that the
///         sidecar/indexer/gas-webhook configs read.
///
/// Env:
///   PRIVATE_KEY  — deployer key (broadcaster)
///   ORG_SAFE     — AttestMesh org Safe (factory owners + registry owner)
contract DeployInfra is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address orgSafe = vm.envOr("ORG_SAFE", vm.addr(pk));

        vm.startBroadcast(pk);

        address attestFacet = address(new AttestFacet());
        address messageFacet = address(new MessageFacet());
        address networkFacet = address(new NetworkFacet());
        address dstackFacet = address(new DstackFacet());
        address diamondInit = address(new DiamondInit());
        address memberImpl = address(new ClusterMember());

        ClusterMemberFactory memberFactory = new ClusterMemberFactory(memberImpl, orgSafe);
        ClusterDiamondFactory clusterFactory = new ClusterDiamondFactory(
            orgSafe, diamondInit, attestFacet, messageFacet, networkFacet, dstackFacet
        );
        IndexerRegistry indexerRegistry = new IndexerRegistry(orgSafe);

        vm.stopBroadcast();

        string memory o = "infra";
        vm.serializeAddress(o, "attestFacet", attestFacet);
        vm.serializeAddress(o, "messageFacet", messageFacet);
        vm.serializeAddress(o, "networkFacet", networkFacet);
        vm.serializeAddress(o, "dstackFacet", dstackFacet);
        vm.serializeAddress(o, "diamondInit", diamondInit);
        vm.serializeAddress(o, "clusterMemberImpl", memberImpl);
        vm.serializeAddress(o, "clusterMemberFactory", address(memberFactory));
        vm.serializeAddress(o, "clusterDiamondFactory", address(clusterFactory));
        string memory json = vm.serializeAddress(o, "indexerRegistry", address(indexerRegistry));

        string memory path =
            string.concat("script/deployments/", vm.toString(block.chainid), ".json");
        vm.writeJson(json, path);
        console2.log("Infra deployed; receipt:", path);
    }
}
