// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";

import { AttestFacet } from "../src/facets/core/AttestFacet.sol";
import { MessageFacet } from "../src/facets/core/MessageFacet.sol";
import { NetworkFacet } from "../src/facets/core/NetworkFacet.sol";
import { DstackFacet } from "../src/facets/attestor/DstackFacet.sol";
import { OperatorFacet } from "../src/facets/attestor/OperatorFacet.sol";
import { DiamondInitV2 } from "../src/DiamondInitV2.sol";
import { ClusterMember } from "../src/members/ClusterMember.sol";
import { ClusterMemberFactory } from "../src/members/ClusterMemberFactory.sol";
import { ClusterDiamondFactoryV2 } from "../src/factory/ClusterDiamondFactoryV2.sol";
import { IndexerRegistry } from "../src/registry/IndexerRegistry.sol";

/// @notice One-shot per-chain infra deploy (contracts spec §12.1, multi-attestor
///         generation). Run once by the AttestMesh org Safe. Deploys the v2 factory
///         lineage (ClusterDiamondFactoryV2 + DiamondInitV2 + both attestor facet
///         impls) — the v1 factory is deprecated for new deploys and is NOT deployed
///         here. Writes a chain-id-stamped JSON receipt that the
///         sidecar/indexer/gas-webhook configs read.
///
/// TRUST NOTE: deploying + approving OperatorFacet only vets its code. A cluster
/// that installs it admits members vouched for by an operator key, not hardware
/// attestation — that is the installing cluster's own policy choice.
///
/// Env:
///   PRIVATE_KEY  — deployer key (broadcaster)
///   ORG_SAFE     — AttestMesh org Safe (factory owners + registry owner). When it
///                  differs from the broadcaster, the Safe must call
///                  addApprovedAttestor for each facet itself (logged below).
contract DeployInfra is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address orgSafe = vm.envOr("ORG_SAFE", vm.addr(pk));

        vm.startBroadcast(pk);

        address attestFacet = address(new AttestFacet());
        address messageFacet = address(new MessageFacet());
        address networkFacet = address(new NetworkFacet());
        address dstackFacet = address(new DstackFacet());
        address operatorFacet = address(new OperatorFacet());
        address diamondInit = address(new DiamondInitV2());
        address memberImpl = address(new ClusterMember());

        ClusterMemberFactory memberFactory = new ClusterMemberFactory(memberImpl, orgSafe);
        ClusterDiamondFactoryV2 clusterFactory = new ClusterDiamondFactoryV2(
            orgSafe, diamondInit, attestFacet, messageFacet, networkFacet
        );
        IndexerRegistry indexerRegistry = new IndexerRegistry(orgSafe);

        if (orgSafe == vm.addr(pk)) {
            clusterFactory.addApprovedAttestor(dstackFacet);
            clusterFactory.addApprovedAttestor(operatorFacet);
        } else {
            console2.log("ORG_SAFE must approve the attestor facets itself:");
            console2.log("  addApprovedAttestor(dstackFacet)  :", dstackFacet);
            console2.log("  addApprovedAttestor(operatorFacet):", operatorFacet);
        }

        vm.stopBroadcast();

        string memory o = "infra";
        vm.serializeAddress(o, "attestFacet", attestFacet);
        vm.serializeAddress(o, "messageFacet", messageFacet);
        vm.serializeAddress(o, "networkFacet", networkFacet);
        vm.serializeAddress(o, "dstackFacet", dstackFacet);
        vm.serializeAddress(o, "operatorFacet", operatorFacet);
        vm.serializeAddress(o, "diamondInitV2", diamondInit);
        vm.serializeAddress(o, "clusterMemberImpl", memberImpl);
        vm.serializeAddress(o, "clusterMemberFactory", address(memberFactory));
        vm.serializeAddress(o, "clusterDiamondFactoryV2", address(clusterFactory));
        string memory json = vm.serializeAddress(o, "indexerRegistry", address(indexerRegistry));

        string memory path =
            string.concat("script/deployments/", vm.toString(block.chainid), ".json");
        vm.writeJson(json, path);
        console2.log("Infra deployed (v2 lineage); receipt:", path);
    }
}
