// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";

import { IERC2535DiamondCut } from "@solidstate/contracts/interfaces/IERC2535DiamondCut.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";
import { ISafeOwnable } from "@solidstate/contracts/access/ownable/ISafeOwnable.sol";

import { NetworkFacet } from "../src/facets/core/NetworkFacet.sol";
import { INetwork } from "../src/interfaces/INetwork.sol";

interface IOwner {
    function owner() external view returns (address);
}

/// @notice Upgrade a live cluster's NetworkFacet to add on-chain Ed25519 heartbeat keys
///         (ed25519-onchain-key spec). The facet's ABI GREW (publishEd25519Key, ed25519KeyOf),
///         so the cut is REPLACE of the 3 existing network selectors + ADD of the 2 new ones,
///         both targeting one freshly-deployed NetworkFacet. Storage is additive (appended
///         mapping in the same NetworkStorage slot) — no migration, no data touched. Cluster
///         and factory addresses are preserved, so the gas-webhook config is unchanged.
///
/// Env: PRIVATE_KEY (broadcaster = the cluster nominee/owner), CLUSTER (the ClusterDiamond).
contract UpgradeNetworkFacetEd25519 is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address me = vm.addr(pk);
        address cluster = vm.envAddress("CLUSTER");

        vm.startBroadcast(pk);

        // Become the diamond's solidstate owner if not already (factory nominated us).
        if (IOwner(cluster).owner() != me) {
            ISafeOwnable(cluster).acceptOwnership();
        }

        NetworkFacet networkFacet = new NetworkFacet();

        // The 3 selectors already live on chain — REPLACE onto the new facet.
        bytes4[] memory existing = new bytes4[](3);
        existing[0] = INetwork.publishWgKey.selector;
        existing[1] = INetwork._setWgPubKey.selector;
        existing[2] = INetwork.wgPubKeyOf.selector;

        // The 2 new selectors — ADD onto the new facet.
        bytes4[] memory added = new bytes4[](2);
        added[0] = INetwork.publishEd25519Key.selector;
        added[1] = INetwork.ed25519KeyOf.selector;

        IERC2535DiamondCutInternal.FacetCut[] memory cut =
            new IERC2535DiamondCutInternal.FacetCut[](2);
        cut[0] = IERC2535DiamondCutInternal.FacetCut({
            target: address(networkFacet),
            action: IERC2535DiamondCutInternal.FacetCutAction.REPLACE,
            selectors: existing
        });
        cut[1] = IERC2535DiamondCutInternal.FacetCut({
            target: address(networkFacet),
            action: IERC2535DiamondCutInternal.FacetCutAction.ADD,
            selectors: added
        });
        IERC2535DiamondCut(cluster).diamondCut(cut, address(0), "");

        vm.stopBroadcast();

        console2.log("cluster:          ", cluster);
        console2.log("new NetworkFacet: ", address(networkFacet));
    }
}
