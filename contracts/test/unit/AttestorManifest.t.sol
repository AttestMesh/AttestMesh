// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";

import { ClusterCut } from "../../src/libraries/ClusterCut.sol";
import { DstackFacet } from "../../src/facets/attestor/DstackFacet.sol";
import { OperatorFacet } from "../../src/facets/attestor/OperatorFacet.sol";
import { IAttestorFacet, AttestorConfig } from "../../src/interfaces/IAttestorFacet.sol";
import { IOperatorFacet } from "../../src/interfaces/IOperatorFacet.sol";

/// @notice The multi-attestor framework's cut invariants (multi-attestor spec):
///         a facet's `selectorManifest()` IS its cut, so the dstack manifest must
///         equal the v1 hand-maintained list bit-for-bit (regression against the
///         deployed v1 lineage), manifests must not collide with the core cuts or
///         each other, and `buildAttestorCuts` must mirror the manifests exactly.
contract AttestorManifestTest is Test {
    DstackFacet internal dstackFacet;
    OperatorFacet internal operatorFacet;

    function setUp() public {
        dstackFacet = new DstackFacet();
        operatorFacet = new OperatorFacet();
    }

    /// The load-bearing regression: DstackFacet.selectorManifest() == the v1
    /// hardcoded selector list (ClusterCut.buildFacetCuts cuts[3]), bit-for-bit.
    function test_dstackManifestMatchesV1CutBitForBit() public view {
        IERC2535DiamondCutInternal.FacetCut[] memory v1 = ClusterCut.buildFacetCuts(
            address(0xA), address(0xB), address(0xC), address(dstackFacet)
        );
        bytes4[] memory v1Selectors = v1[3].selectors;
        bytes4[] memory manifest = dstackFacet.selectorManifest();

        assertEq(manifest.length, v1Selectors.length, "manifest length must equal v1 list");
        for (uint256 i; i < manifest.length; ++i) {
            assertEq(manifest[i], v1Selectors[i], "manifest selector drifted from the v1 cut");
        }
    }

    function test_operatorManifestIsExactlyItsSurface() public view {
        bytes4[] memory m = operatorFacet.selectorManifest();
        assertEq(m.length, 5);
        assertEq(m[0], IOperatorFacet.operator_register.selector);
        assertEq(m[1], IOperatorFacet.addOperatorSigner.selector);
        assertEq(m[2], IOperatorFacet.removeOperatorSigner.selector);
        assertEq(m[3], IOperatorFacet.operatorSigners.selector);
        assertEq(m[4], IOperatorFacet.isOperatorSigner.selector);
    }

    /// IAttestorFacet's own selectors are impl-level and must never be cut into a
    /// diamond (a second attestor facet would collide on them).
    function test_manifestsExcludeAttestorConventionSelectors() public view {
        bytes4[3] memory convention = [
            IAttestorFacet.attestorId.selector,
            IAttestorFacet.selectorManifest.selector,
            IAttestorFacet.initAttestor.selector
        ];
        bytes4[] memory dm = dstackFacet.selectorManifest();
        bytes4[] memory om = operatorFacet.selectorManifest();
        for (uint256 c; c < convention.length; ++c) {
            for (uint256 i; i < dm.length; ++i) {
                assertTrue(dm[i] != convention[c], "dstack manifest leaks a convention selector");
            }
            for (uint256 i; i < om.length; ++i) {
                assertTrue(om[i] != convention[c], "operator manifest leaks a convention selector");
            }
        }
    }

    /// Installing both facets in one diamond must be collision-free: the two
    /// manifests are disjoint, and neither overlaps the fixed core cuts.
    function test_manifestsAreDisjointFromEachOtherAndCore() public view {
        bytes4[] memory dm = dstackFacet.selectorManifest();
        bytes4[] memory om = operatorFacet.selectorManifest();
        for (uint256 i; i < dm.length; ++i) {
            for (uint256 j; j < om.length; ++j) {
                assertTrue(dm[i] != om[j], "dstack/operator manifests collide");
            }
        }

        IERC2535DiamondCutInternal.FacetCut[] memory core =
            ClusterCut.buildCoreCuts(address(0xA), address(0xB), address(0xC));
        for (uint256 c; c < core.length; ++c) {
            bytes4[] memory cs = core[c].selectors;
            for (uint256 i; i < cs.length; ++i) {
                for (uint256 j; j < dm.length; ++j) {
                    assertTrue(cs[i] != dm[j], "core/dstack selectors collide");
                }
                for (uint256 j; j < om.length; ++j) {
                    assertTrue(cs[i] != om[j], "core/operator selectors collide");
                }
            }
        }
    }

    /// v2 core cuts must be the v1 core cuts unchanged (the framework only made the
    /// attestor seam pluggable).
    function test_coreCutsMatchV1() public view {
        IERC2535DiamondCutInternal.FacetCut[] memory v1 = ClusterCut.buildFacetCuts(
            address(0xA), address(0xB), address(0xC), address(dstackFacet)
        );
        IERC2535DiamondCutInternal.FacetCut[] memory core =
            ClusterCut.buildCoreCuts(address(0xA), address(0xB), address(0xC));
        assertEq(core.length, 3);
        for (uint256 c; c < 3; ++c) {
            assertEq(core[c].target, v1[c].target);
            assertEq(core[c].selectors.length, v1[c].selectors.length);
            for (uint256 i; i < core[c].selectors.length; ++i) {
                assertEq(core[c].selectors[i], v1[c].selectors[i]);
            }
        }
    }

    /// buildAttestorCuts mirrors each facet's manifest exactly (self-describing cut).
    function test_buildAttestorCutsMirrorsManifests() public view {
        AttestorConfig[] memory configs = new AttestorConfig[](2);
        configs[0] = AttestorConfig({ facet: address(dstackFacet), initData: "" });
        configs[1] = AttestorConfig({ facet: address(operatorFacet), initData: "" });

        IERC2535DiamondCutInternal.FacetCut[] memory cuts = ClusterCut.buildAttestorCuts(configs);
        assertEq(cuts.length, 2);
        assertEq(cuts[0].target, address(dstackFacet));
        assertEq(cuts[1].target, address(operatorFacet));

        bytes4[] memory dm = dstackFacet.selectorManifest();
        assertEq(cuts[0].selectors.length, dm.length);
        for (uint256 i; i < dm.length; ++i) {
            assertEq(cuts[0].selectors[i], dm[i]);
        }
        bytes4[] memory om = operatorFacet.selectorManifest();
        assertEq(cuts[1].selectors.length, om.length);
        for (uint256 i; i < om.length; ++i) {
            assertEq(cuts[1].selectors[i], om[i]);
        }
    }

    /// attestorId constants are derivable off-chain; pin both literals.
    function test_attestorIdsArePinned() public view {
        assertEq(dstackFacet.attestorId(), keccak256("attestmesh.attestor.dstack"));
        assertEq(operatorFacet.attestorId(), keccak256("attestmesh.attestor.operator"));
        assertEq(dstackFacet.attestorId(), dstackFacet.DSTACK_ATTESTOR_ID());
        assertEq(operatorFacet.attestorId(), operatorFacet.OPERATOR_ATTESTOR_ID());
    }
}
