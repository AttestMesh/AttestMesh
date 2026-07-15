// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";

import { IERC2535DiamondCut } from "@solidstate/contracts/interfaces/IERC2535DiamondCut.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";

import {
    PrepareDstackFacetPathASafe,
    IPathADiamondLoupe
} from "../../script/PrepareDstackFacetPathASafe.s.sol";
import { AttestFacet } from "../../src/facets/core/AttestFacet.sol";
import { MessageFacet } from "../../src/facets/core/MessageFacet.sol";
import { NetworkFacet } from "../../src/facets/core/NetworkFacet.sol";
import { DstackFacet } from "../../src/facets/attestor/DstackFacet.sol";
import { DiamondInit } from "../../src/DiamondInit.sol";
import { ClusterMember } from "../../src/members/ClusterMember.sol";
import { ClusterMemberFactory } from "../../src/members/ClusterMemberFactory.sol";
import { ClusterDiamondFactory } from "../../src/factory/ClusterDiamondFactory.sol";

contract MockPathASafe {
    uint256 internal immutable _threshold;
    address[] internal _owners;

    constructor(uint256 threshold_, address[] memory owners_) {
        _threshold = threshold_;
        _owners = owners_;
    }

    function getThreshold() external view returns (uint256) {
        return _threshold;
    }

    function getOwners() external view returns (address[] memory) {
        return _owners;
    }

    function execute(address target, bytes calldata data) external returns (bytes memory result) {
        (bool ok, bytes memory returnData) = target.call(data);
        if (!ok) {
            assembly ("memory-safe") {
                revert(add(returnData, 0x20), mload(returnData))
            }
        }
        return returnData;
    }
}

contract MockPathAClusterTopology {
    address public owner;
    address public clusterOwner;
    mapping(bytes4 selector => address facet) internal _facets;

    constructor(address owner_) {
        owner = owner_;
        clusterOwner = owner_;
    }

    function setClusterOwner(address owner_) external {
        clusterOwner = owner_;
    }

    function setFacet(bytes4 selector, address facet) external {
        _facets[selector] = facet;
    }

    function facetAddress(bytes4 selector) external view returns (address) {
        return _facets[selector];
    }
}

contract PrepareDstackFacetPathASafeTest is Test {
    PrepareDstackFacetPathASafe internal preparer;
    MockPathASafe internal safe;
    ClusterDiamondFactory internal clusterFactory;

    address internal cluster;
    address internal initialDstackFacet;

    function setUp() public {
        preparer = new PrepareDstackFacetPathASafe();
        safe = _newSafe(1);

        address memberImplementation = address(new ClusterMember());
        ClusterMemberFactory memberFactory =
            new ClusterMemberFactory(memberImplementation, address(safe));

        initialDstackFacet = address(new DstackFacet());
        clusterFactory = new ClusterDiamondFactory(
            address(safe),
            address(new DiamondInit()),
            address(new AttestFacet()),
            address(new MessageFacet()),
            address(new NetworkFacet()),
            initialDstackFacet
        );

        bytes32[] memory composes = new bytes32[](1);
        composes[0] = keccak256("patha-safe-test-compose");
        bytes32[] memory devices = new bytes32[](1);
        devices[0] = keccak256("patha-safe-test-device");
        DiamondInit.InitArgs memory args = DiamondInit.InitArgs({
            clusterOwner: address(safe),
            kmsRootSigner: address(0xBEEF),
            initialComposeHashes: composes,
            initialDeviceIds: devices,
            allowAnyDevice: false,
            requireTcbUpToDate: true,
            meshCidrIp: 0x0a170000,
            meshCidrPrefix: 16,
            memberFactory: address(memberFactory)
        });
        cluster = clusterFactory.deployCluster(args, keccak256("patha-safe-test-cluster"));

        // The factory nominated the Safe. The Safe must accept before preparation is allowed.
        safe.execute(cluster, abi.encodeWithSignature("acceptOwnership()"));
    }

    function test_buildsExactSingleReplaceCutAndCalldata() public {
        address replacement = address(new DstackFacet());
        IERC2535DiamondCutInternal.FacetCut[] memory cut = preparer.buildDiamondCut(replacement);

        assertEq(cut.length, 1);
        assertEq(cut[0].target, replacement);
        assertEq(uint256(cut[0].action), uint256(IERC2535DiamondCutInternal.FacetCutAction.REPLACE));
        assertEq(cut[0].selectors.length, 19);
        assertEq(cut[0].selectors[14], bytes4(0x537d491c));

        for (uint256 i; i < cut[0].selectors.length; ++i) {
            for (uint256 j = i + 1; j < cut[0].selectors.length; ++j) {
                assertTrue(cut[0].selectors[i] != cut[0].selectors[j], "duplicate selector");
            }
        }

        bytes memory expected =
            abi.encodeCall(IERC2535DiamondCut.diamondCut, (cut, address(0), bytes("")));
        bytes memory actual = preparer.buildSafeCalldata(replacement);
        assertEq(actual, expected);
        assertEq(_selector(actual), IERC2535DiamondCut.diamondCut.selector);
    }

    function test_safePayloadReplacesEveryDstackSelector() public {
        address current = preparer.validateCluster(cluster, address(safe));
        assertEq(current, initialDstackFacet);

        address replacement = address(new DstackFacet());
        IERC2535DiamondCutInternal.FacetCut[] memory cut = preparer.buildDiamondCut(replacement);
        safe.execute(cluster, preparer.buildSafeCalldata(replacement));

        for (uint256 i; i < cut[0].selectors.length; ++i) {
            assertEq(IPathADiamondLoupe(cluster).facetAddress(cut[0].selectors[i]), replacement);
        }
        assertEq(preparer.validateCluster(cluster, address(safe)), replacement);
    }

    function test_runWritesExactBundleWithoutMutatingCluster() public {
        uint256 broadcasterKey = 0xB0B;
        address broadcaster = vm.addr(broadcasterKey);
        vm.deal(broadcaster, 10 ether);

        string memory bundleFile = "script/deployments/patha-safe-unit-test.json";
        vm.setEnv("PRIVATE_KEY", vm.toString(broadcasterKey));
        vm.setEnv("CLUSTER", vm.toString(cluster));
        vm.setEnv("EXPECTED_SAFE_OWNER", vm.toString(address(safe)));
        vm.setEnv("PATHA_BUNDLE_FILE", bundleFile);

        (address replacement, address memberImplementation, bytes memory safeCalldata) =
            preparer.run();

        assertGt(replacement.code.length, 0);
        assertGt(memberImplementation.code.length, 0);
        assertEq(preparer.validateCluster(cluster, address(safe)), initialDstackFacet);

        string memory json = vm.readFile(bundleFile);
        assertEq(vm.parseJsonUint(json, ".schemaVersion"), 1);
        assertEq(vm.parseJsonUint(json, ".chainId"), block.chainid);
        assertEq(vm.parseJsonAddress(json, ".broadcaster"), broadcaster);
        assertEq(vm.parseJsonAddress(json, ".safeOwner"), address(safe));
        assertEq(vm.parseJsonAddress(json, ".cluster"), cluster);
        assertEq(vm.parseJsonAddress(json, ".target"), cluster);
        assertEq(vm.parseJsonAddress(json, ".currentDstackFacet"), initialDstackFacet);
        assertEq(vm.parseJsonAddress(json, ".dstackFacet"), replacement);
        assertEq(vm.parseJsonAddress(json, ".clusterMemberImplementation"), memberImplementation);
        assertEq(vm.parseJsonUint(json, ".value"), 0);
        assertEq(vm.parseJsonBytes(json, ".data"), safeCalldata);
        assertEq(vm.parseJsonBytes32(json, ".calldataHash"), keccak256(safeCalldata));
        vm.removeFile(bundleFile);
    }

    function test_rejectsEoaAsExpectedSafe() public {
        address eoa = address(0xA11CE);
        vm.expectRevert(
            abi.encodeWithSelector(PrepareDstackFacetPathASafe.ExpectedSafeHasNoCode.selector, eoa)
        );
        preparer.validateCluster(cluster, eoa);
    }

    function test_rejectsUninitializedSafeBoundary() public {
        address[] memory noOwners = new address[](0);
        MockPathASafe invalidSafe = new MockPathASafe(0, noOwners);
        MockPathAClusterTopology topology = new MockPathAClusterTopology(address(invalidSafe));

        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.InvalidSafeConfiguration.selector,
                address(invalidSafe),
                0,
                0
            )
        );
        preparer.validateCluster(address(topology), address(invalidSafe));
    }

    function test_rejectsDuplicateSafeBoundaryOwners() public {
        address[] memory owners = new address[](2);
        owners[0] = address(this);
        owners[1] = address(this);
        MockPathASafe invalidSafe = new MockPathASafe(1, owners);
        MockPathAClusterTopology topology = new MockPathAClusterTopology(address(invalidSafe));

        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.InvalidSafeConfiguration.selector,
                address(invalidSafe),
                1,
                2
            )
        );
        preparer.validateCluster(address(topology), address(invalidSafe));
    }

    function test_rejectsSafeThatHasNotAcceptedSolidstateOwnership() public {
        MockPathASafe otherSafe = _newSafe(1);
        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.UnexpectedClusterOwner.selector,
                address(otherSafe),
                address(safe)
            )
        );
        preparer.validateCluster(cluster, address(otherSafe));
    }

    function test_rejectsDifferentClusterPolicyOwner() public {
        MockPathAClusterTopology topology = new MockPathAClusterTopology(address(safe));
        MockPathASafe otherSafe = _newSafe(1);
        topology.setClusterOwner(address(otherSafe));

        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.UnexpectedClusterPolicyOwner.selector,
                address(safe),
                address(otherSafe)
            )
        );
        preparer.validateCluster(address(topology), address(safe));
    }

    function test_rejectsMissingDstackSelector() public {
        MockPathAClusterTopology topology = new MockPathAClusterTopology(address(safe));
        address liveFacet = address(new DstackFacet());
        IERC2535DiamondCutInternal.FacetCut[] memory cut = preparer.buildDiamondCut(liveFacet);
        for (uint256 i; i + 1 < cut[0].selectors.length; ++i) {
            topology.setFacet(cut[0].selectors[i], liveFacet);
        }

        bytes4 missing = cut[0].selectors[cut[0].selectors.length - 1];
        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.MissingDstackSelector.selector, missing
            )
        );
        preparer.validateCluster(address(topology), address(safe));
    }

    function test_rejectsSplitDstackSelectorTopology() public {
        MockPathAClusterTopology topology = new MockPathAClusterTopology(address(safe));
        address firstFacet = address(new DstackFacet());
        address secondFacet = address(new DstackFacet());
        IERC2535DiamondCutInternal.FacetCut[] memory cut = preparer.buildDiamondCut(firstFacet);
        for (uint256 i; i < cut[0].selectors.length; ++i) {
            topology.setFacet(cut[0].selectors[i], firstFacet);
        }
        bytes4 splitSelector = cut[0].selectors[7];
        topology.setFacet(splitSelector, secondFacet);

        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.SplitDstackSelector.selector,
                splitSelector,
                firstFacet,
                secondFacet
            )
        );
        preparer.validateCluster(address(topology), address(safe));
    }

    function test_rejectsZeroReplacementFacet() public {
        vm.expectRevert(PrepareDstackFacetPathASafe.NewDstackFacetIsZero.selector);
        preparer.buildDiamondCut(address(0));
    }

    function _newSafe(uint256 threshold) internal returns (MockPathASafe created) {
        address[] memory owners = new address[](1);
        owners[0] = address(this);
        created = new MockPathASafe(threshold, owners);
    }

    function _selector(bytes memory data) internal pure returns (bytes4 selector) {
        assembly ("memory-safe") {
            selector := mload(add(data, 0x20))
        }
    }
}
