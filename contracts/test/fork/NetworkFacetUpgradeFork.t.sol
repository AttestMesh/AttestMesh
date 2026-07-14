// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";

import { IERC2535DiamondCut } from "@solidstate/contracts/interfaces/IERC2535DiamondCut.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";

import { NetworkFacet } from "../../src/facets/core/NetworkFacet.sol";
import { INetwork } from "../../src/interfaces/INetwork.sol";
import { IAttest } from "../../src/interfaces/IAttest.sol";

/// @notice Mainnet-fork rehearsal of the Ed25519 NetworkFacet upgrade on the LIVE C3 diamond.
///         Runs the exact cut (REPLACE 3 + ADD 2), then proves: existing storage survives, the
///         existing wg selectors still work, the new selectors resolve, and unrelated facets are
///         untouched. Runs only when BASE_RPC is set (skipped in normal CI).
///
/// Usage: BASE_RPC=<base-mainnet-rpc> forge test --match-contract NetworkFacetUpgradeFork -vv
contract NetworkFacetUpgradeForkTest is Test {
    // Live C3 (contracts spec deployment 8453).
    address constant CLUSTER = 0x5ab4706fCa998A0792E5c06432b13c73c54E4557;
    address constant OWNER = 0x60b174704AdAf2b0BF87B426B364D6EbD81818E1;

    // Synclave: a real registered member — its ClusterMember contract (msg.sender for
    // member-gated calls) and memberId, with its currently-published wg key.
    address constant SYNCLAVE_MEMBER = 0x2e5527Ac5B3b2376Ec42dF9f3960DcEbb81A5e21;
    bytes32 constant SYNCLAVE_ID =
        0x3ee3f25b9b04da71cfd99b04cdf39930448542d431470d57d8f93a29aced40f0;
    bytes32 constant SYNCLAVE_WG =
        0x6e808d089af6d7a4c2566f416482688736bb05237058eb209be2deb4c826a042;

    function _forkOrSkip() internal returns (bool) {
        string memory rpc = vm.envOr("BASE_RPC", string(""));
        if (bytes(rpc).length == 0) return false;
        vm.createSelectFork(rpc);
        return true;
    }

    function _applyCut() internal {
        NetworkFacet nf = new NetworkFacet();

        bytes4[] memory existing = new bytes4[](3);
        existing[0] = INetwork.publishWgKey.selector;
        existing[1] = INetwork._setWgPubKey.selector;
        existing[2] = INetwork.wgPubKeyOf.selector;

        bytes4[] memory added = new bytes4[](2);
        added[0] = INetwork.publishEd25519Key.selector;
        added[1] = INetwork.ed25519KeyOf.selector;

        IERC2535DiamondCutInternal.FacetCut[] memory cut =
            new IERC2535DiamondCutInternal.FacetCut[](2);
        cut[0] = IERC2535DiamondCutInternal.FacetCut(
            address(nf), IERC2535DiamondCutInternal.FacetCutAction.REPLACE, existing
        );
        cut[1] = IERC2535DiamondCutInternal.FacetCut(
            address(nf), IERC2535DiamondCutInternal.FacetCutAction.ADD, added
        );

        vm.prank(OWNER);
        IERC2535DiamondCut(CLUSTER).diamondCut(cut, address(0), "");
    }

    function test_fork_cutPreservesStorageAndAddsEd25519() external {
        if (!_forkOrSkip()) {
            emit log("BASE_RPC unset - skipping fork test");
            return;
        }

        // Pre-cut snapshot of live state that MUST survive.
        uint256 countBefore = IAttest(CLUSTER).memberCount();
        bytes32 wgBefore = INetwork(CLUSTER).wgPubKeyOf(SYNCLAVE_ID);
        assertEq(wgBefore, SYNCLAVE_WG, "precondition: synclave wg key readable pre-cut");
        assertGt(countBefore, 0, "precondition: cluster has members");

        // The new selector must NOT resolve yet.
        (bool okBefore,) =
            CLUSTER.staticcall(abi.encodeWithSelector(INetwork.ed25519KeyOf.selector, SYNCLAVE_ID));
        assertFalse(okBefore, "ed25519KeyOf must be absent pre-cut");

        _applyCut();

        // 1. Existing storage intact — the appended mapping did not corrupt wgPubKeys.
        assertEq(INetwork(CLUSTER).wgPubKeyOf(SYNCLAVE_ID), SYNCLAVE_WG, "wg key survives the cut");
        // 2. Unrelated facet reads untouched.
        assertEq(IAttest(CLUSTER).memberCount(), countBefore, "memberCount unchanged");
        // 3. New members' ed25519 default to zero.
        assertEq(
            INetwork(CLUSTER).ed25519KeyOf(SYNCLAVE_ID), bytes32(0), "ed25519 zero pre-publish"
        );

        // 4. Existing wg selector still WRITABLE by a real member (REPLACE kept behaviour).
        vm.prank(SYNCLAVE_MEMBER);
        INetwork(CLUSTER).publishWgKey(bytes32("wg-rotated"));
        assertEq(
            INetwork(CLUSTER).wgPubKeyOf(SYNCLAVE_ID), bytes32("wg-rotated"), "wg rotate works"
        );

        // 5. New selector works end-to-end for a real member.
        vm.prank(SYNCLAVE_MEMBER);
        INetwork(CLUSTER).publishEd25519Key(bytes32("ed-live"));
        assertEq(
            INetwork(CLUSTER).ed25519KeyOf(SYNCLAVE_ID), bytes32("ed-live"), "ed publish works"
        );

        // 6. Access control preserved on the new selector.
        vm.prank(address(0xDEAD));
        (bool okBad,) = CLUSTER.call(
            abi.encodeWithSelector(INetwork.publishEd25519Key.selector, bytes32("x"))
        );
        assertFalse(okBad, "non-member publishEd25519Key reverts");
    }
}
