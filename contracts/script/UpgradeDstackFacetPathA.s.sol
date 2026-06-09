// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";

import {
    IERC2535DiamondCut
} from "@solidstate/contracts/interfaces/IERC2535DiamondCut.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";
import { ISafeOwnable } from "@solidstate/contracts/access/ownable/ISafeOwnable.sol";

import { DstackFacet } from "../src/facets/attestor/DstackFacet.sol";
import { ClusterMember } from "../src/members/ClusterMember.sol";
import { ClusterCut } from "../src/libraries/ClusterCut.sol";

interface IOwner {
    function owner() external view returns (address);
}

/// @notice Path A upgrade (surgical). Replaces the live cluster's DstackFacet with one whose
///         `dstack_register` also accepts owner-allowlisted app_ids (so a dstack-provisioned
///         DstackApp upgraded to ClusterMember can register), and deploys a fresh
///         ClusterMember implementation to use as that UUPS upgrade target. The cluster and
///         factory addresses are preserved, so the gas-webhook config is unchanged.
///
/// The DstackFacet external ABI is unchanged by the edit, so the cut is a pure REPLACE of the
/// existing 19 dstack selectors (ClusterCut._dstackSelectors via buildFacetCuts index 3).
///
/// Env: PRIVATE_KEY (broadcaster = the cluster nominee/owner), CLUSTER (the ClusterDiamond).
contract UpgradeDstackFacetPathA is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address me = vm.addr(pk);
        address cluster = vm.envAddress("CLUSTER");

        vm.startBroadcast(pk);

        // Become the diamond's solidstate owner (the factory nominated us at deploy time).
        if (IOwner(cluster).owner() != me) {
            ISafeOwnable(cluster).acceptOwnership();
        }

        // Path A DstackFacet: dstack_register also accepts allowedAppIds[memberContract].
        DstackFacet dstackFacet = new DstackFacet();

        // Replace exactly the live dstack selector set (unchanged by the edit).
        IERC2535DiamondCutInternal.FacetCut[] memory built =
            ClusterCut.buildFacetCuts(address(1), address(1), address(1), address(dstackFacet));
        IERC2535DiamondCutInternal.FacetCut[] memory cut =
            new IERC2535DiamondCutInternal.FacetCut[](1);
        cut[0] = IERC2535DiamondCutInternal.FacetCut({
            target: address(dstackFacet),
            action: IERC2535DiamondCutInternal.FacetCutAction.REPLACE,
            selectors: built[3].selectors // index 3 = dstack (see ClusterCut.buildFacetCuts)
        });
        IERC2535DiamondCut(cluster).diamondCut(cut, address(0), "");

        // Path A ClusterMember impl — the UUPS upgrade target for the dstack app proxy.
        ClusterMember memberImpl = new ClusterMember();

        vm.stopBroadcast();

        console2.log("cluster:                ", cluster);
        console2.log("new DstackFacet:        ", address(dstackFacet));
        console2.log("new ClusterMember impl: ", address(memberImpl));
    }
}
