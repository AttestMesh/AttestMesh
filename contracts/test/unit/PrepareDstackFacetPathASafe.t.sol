// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";

import { IERC2535DiamondCut } from "@solidstate/contracts/interfaces/IERC2535DiamondCut.sol";
import { IERC2535DiamondLoupe } from "@solidstate/contracts/interfaces/IERC2535DiamondLoupe.sol";
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
import { ClusterDiamond } from "../../src/ClusterDiamond.sol";
import { ClusterCut } from "../../src/libraries/ClusterCut.sol";

contract TestablePrepareDstackFacetPathASafe is PrepareDstackFacetPathASafe {
    function _validateApprovedSafe(address expectedSafe) internal view override {
        _validateSafeBoundary(expectedSafe);
    }

    function _validateCanonicalCluster(address) internal view override { }
}

contract FactoryCheckingPrepareDstackFacetPathASafe is PrepareDstackFacetPathASafe {
    function _validateApprovedSafe(address expectedSafe) internal view override {
        _validateSafeBoundary(expectedSafe);
    }
}

contract BoundaryCheckingPrepareDstackFacetPathASafe is PrepareDstackFacetPathASafe {
    function validateSafeExecutionSurface(
        address safe,
        address expectedHandler,
        bytes32 expectedHandlerCodehash
    ) external view {
        _validateSafeExecutionSurface(safe, expectedHandler, expectedHandlerCodehash);
    }

    function validateExactTopology(address cluster, address dstackFacet) external view {
        _validateExactClusterTopology(cluster, dstackFacet);
    }
}

contract MockPathASafe {
    uint256 internal immutable _threshold;
    address[] internal _owners;
    address[] internal _modules;
    address internal _guard;
    address internal _fallbackHandler;

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

    function setModule(address module) external {
        delete _modules;
        if (module != address(0)) _modules.push(module);
    }

    function setGuard(address guard_) external {
        _guard = guard_;
    }

    function setFallbackHandler(address handler_) external {
        _fallbackHandler = handler_;
    }

    function getModulesPaginated(address, uint256)
        external
        view
        returns (address[] memory modules, address next)
    {
        return (_modules, address(1));
    }

    function getStorageAt(uint256 offset, uint256) external view returns (bytes memory) {
        bytes32 guardSlot = 0x4a204f620c8c5ccdca3fd54d003badd85ba500436a431f0cbda4f558c93c34c8;
        bytes32 fallbackSlot = 0x6c9a6c4a39284e37ed1cf53d337577d14212a4870fb976a4366c693b939918d5;
        if (bytes32(offset) == guardSlot) {
            return abi.encode(bytes32(uint256(uint160(_guard))));
        }
        if (bytes32(offset) == fallbackSlot) {
            return abi.encode(bytes32(uint256(uint160(_fallbackHandler))));
        }
        return abi.encode(bytes32(0));
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
    TestablePrepareDstackFacetPathASafe internal preparer;
    PrepareDstackFacetPathASafe internal strictPreparer;
    MockPathASafe internal safe;
    ClusterDiamondFactory internal clusterFactory;

    address internal cluster;
    address internal initialDstackFacet;

    function setUp() public {
        vm.chainId(8453);
        preparer = new TestablePrepareDstackFacetPathASafe();
        strictPreparer = new PrepareDstackFacetPathASafe();
        safe = _newSafe(1);

        // Canonical deterministic deployment proxy, pinned by production codehash.
        vm.etch(
            preparer.CREATE2_DEPLOYER(),
            hex"7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffe03601600081602082378035828234f58015156039578182fd5b8082525050506014600cf3"
        );

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
        bytes4[19] memory expectedSelectors = [
            bytes4(0xdfc77223),
            bytes4(0x67b3f22c),
            bytes4(0x2a819728),
            bytes4(0x1d266200),
            bytes4(0x7c4beeb8),
            bytes4(0x6e4c7422),
            bytes4(0x2f6622e5),
            bytes4(0xbf8b211b),
            bytes4(0x3440a16a),
            bytes4(0x0aa58c83),
            bytes4(0x54fd4d50),
            bytes4(0x2514ce2d),
            bytes4(0x7e02756a),
            bytes4(0x12c604da),
            bytes4(0x537d491c),
            bytes4(0x1e079198),
            bytes4(0x64e985f8),
            bytes4(0x4bc7cbb7),
            bytes4(0x875f31fb)
        ];
        for (uint256 i; i < expectedSelectors.length; ++i) {
            assertEq(cut[0].selectors[i], expectedSelectors[i], "reviewed selector/order drift");
        }

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

    function test_runRecoversPartialDeploymentWritesExactBundleAndIsIdempotent() public {
        bytes memory initCode = type(DstackFacet).creationCode;
        (bool deployedPartial,) = preparer.CREATE2_DEPLOYER()
            .call(abi.encodePacked(preparer.DSTACK_FACET_SALT(), initCode));
        assertTrue(deployedPartial);
        assertGt(preparer.DETERMINISTIC_DSTACK_FACET().code.length, 0);
        assertEq(preparer.DETERMINISTIC_MEMBER_IMPLEMENTATION().code.length, 0);

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
        assertEq(vm.parseJsonUint(json, ".safeModuleCount"), 0);
        assertEq(vm.parseJsonAddress(json, ".safeModulesNext"), address(1));
        assertEq(vm.parseJsonAddress(json, ".safeGuard"), address(0));
        assertEq(
            vm.parseJsonAddress(json, ".safeFallbackHandler"),
            preparer.APPROVED_SAFE_FALLBACK_HANDLER()
        );
        assertEq(
            vm.parseJsonBytes32(json, ".safeFallbackHandlerCodeHash"),
            preparer.APPROVED_SAFE_FALLBACK_HANDLER_CODEHASH()
        );
        assertEq(vm.parseJsonAddress(json, ".create2Deployer"), preparer.CREATE2_DEPLOYER());
        assertEq(vm.parseJsonAddress(json, ".cluster"), cluster);
        assertEq(vm.parseJsonAddress(json, ".target"), cluster);
        assertEq(vm.parseJsonUint(json, ".clusterFacetCount"), 5);
        assertEq(vm.parseJsonUint(json, ".clusterSelectorCount"), 54);
        assertEq(vm.parseJsonUint(json, ".memberCount"), 0);
        assertEq(vm.parseJsonBytes32(json, ".cskCommitment"), bytes32(0));
        assertEq(vm.parseJsonAddress(json, ".currentDstackFacet"), initialDstackFacet);
        assertEq(vm.parseJsonAddress(json, ".dstackFacet"), replacement);
        assertEq(replacement, preparer.DETERMINISTIC_DSTACK_FACET());
        assertEq(
            vm.parseJsonBytes32(json, ".dstackFacetRuntimeCodeHash"),
            preparer.DSTACK_FACET_RUNTIME_CODEHASH()
        );
        assertEq(vm.parseJsonAddress(json, ".clusterMemberImplementation"), memberImplementation);
        assertEq(memberImplementation, preparer.DETERMINISTIC_MEMBER_IMPLEMENTATION());
        assertEq(
            vm.parseJsonBytes32(json, ".clusterMemberImplementationRuntimeCodeHash"),
            preparer.MEMBER_IMPLEMENTATION_RUNTIME_CODEHASH()
        );
        assertEq(vm.parseJsonUint(json, ".value"), 0);
        assertEq(vm.parseJsonBytes(json, ".data"), safeCalldata);
        assertEq(vm.parseJsonBytes32(json, ".calldataHash"), keccak256(safeCalldata));

        bytes32 facetHash = replacement.codehash;
        bytes32 memberHash = memberImplementation.codehash;
        (address secondFacet, address secondMember,) = preparer.run();
        assertEq(secondFacet, replacement);
        assertEq(secondMember, memberImplementation);
        assertEq(secondFacet.codehash, facetHash);
        assertEq(secondMember.codehash, memberHash);
        vm.removeFile(bundleFile);
    }

    function test_safeExecutionSurfaceRejectsModulesGuardHandlerAndCodeDrift() public {
        BoundaryCheckingPrepareDstackFacetPathASafe checker =
            new BoundaryCheckingPrepareDstackFacetPathASafe();
        address handler = address(new DstackFacet());
        safe.setFallbackHandler(handler);
        checker.validateSafeExecutionSurface(address(safe), handler, handler.codehash);

        safe.setModule(address(0xBEEF));
        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.UnexpectedSafeModules.selector, uint256(1), address(1)
            )
        );
        checker.validateSafeExecutionSurface(address(safe), handler, handler.codehash);
        safe.setModule(address(0));

        safe.setGuard(address(0xCAFE));
        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.UnexpectedSafeGuard.selector, address(0xCAFE)
            )
        );
        checker.validateSafeExecutionSurface(address(safe), handler, handler.codehash);
        safe.setGuard(address(0));

        safe.setFallbackHandler(address(0xDEAD));
        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.UnexpectedSafeFallbackHandler.selector,
                handler,
                address(0xDEAD)
            )
        );
        checker.validateSafeExecutionSurface(address(safe), handler, handler.codehash);
        safe.setFallbackHandler(handler);

        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.UnexpectedCodeHash.selector,
                handler,
                bytes32(uint256(1)),
                handler.codehash
            )
        );
        checker.validateSafeExecutionSurface(address(safe), handler, bytes32(uint256(1)));
    }

    function test_exactFactoryTopologyRejectsExtraFacet() public {
        BoundaryCheckingPrepareDstackFacetPathASafe checker =
            new BoundaryCheckingPrepareDstackFacetPathASafe();
        address oldDstackFacet = checker.FACTORY_DSTACK_FACET();
        vm.etch(checker.ATTEST_FACET(), hex"00");
        vm.etch(checker.MESSAGE_FACET(), hex"00");
        vm.etch(checker.FACTORY_NETWORK_FACET(), hex"00");
        vm.etch(oldDstackFacet, hex"00");

        IERC2535DiamondCutInternal.FacetCut[] memory cut = ClusterCut.buildFacetCuts(
            checker.ATTEST_FACET(),
            checker.MESSAGE_FACET(),
            checker.FACTORY_NETWORK_FACET(),
            oldDstackFacet
        );
        bytes4[] memory oldNetworkSelectors = new bytes4[](3);
        for (uint256 i; i < oldNetworkSelectors.length; ++i) {
            oldNetworkSelectors[i] = cut[2].selectors[i];
        }
        cut[2].selectors = oldNetworkSelectors;
        ClusterDiamond topology = new ClusterDiamond(cut, address(0), bytes(""));
        checker.validateExactTopology(address(topology), oldDstackFacet);
        assertEq(IPathADiamondLoupe(address(topology)).facetAddress(0xcedc29c2), address(0));
        assertEq(IPathADiamondLoupe(address(topology)).facetAddress(0x14342a70), address(0));

        IERC2535DiamondCutInternal.FacetCut[] memory extra =
            new IERC2535DiamondCutInternal.FacetCut[](1);
        bytes4[] memory extraSelector = new bytes4[](1);
        extraSelector[0] = 0xdeadbeef;
        extra[0] = IERC2535DiamondCutInternal.FacetCut({
            target: checker.FACTORY_NETWORK_FACET(),
            action: IERC2535DiamondCutInternal.FacetCutAction.ADD,
            selectors: extraSelector
        });
        IERC2535DiamondCut(address(topology)).diamondCut(extra, address(0), bytes(""));
        assertEq(
            IERC2535DiamondLoupe(address(topology))
            .facetFunctionSelectors(checker.FACTORY_NETWORK_FACET())
            .length,
            4
        );
        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.UnexpectedClusterFacetSet.selector,
                checker.FACTORY_NETWORK_FACET()
            )
        );
        checker.validateExactTopology(address(topology), oldDstackFacet);
    }

    function test_strictValidationRejectsSafeCompatibleImpostor() public {
        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.UnexpectedApprovedSafe.selector,
                strictPreparer.APPROVED_SAFE(),
                address(safe)
            )
        );
        strictPreparer.validateCluster(cluster, address(safe));
    }

    function test_strictValidationRejectsUnreviewedFactoryCode() public {
        FactoryCheckingPrepareDstackFacetPathASafe factoryChecking =
            new FactoryCheckingPrepareDstackFacetPathASafe();
        vm.etch(factoryChecking.CANONICAL_CLUSTER_FACTORY(), hex"60006000");
        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.UnexpectedCodeHash.selector,
                factoryChecking.CANONICAL_CLUSTER_FACTORY(),
                factoryChecking.CANONICAL_CLUSTER_FACTORY_CODEHASH(),
                factoryChecking.CANONICAL_CLUSTER_FACTORY().codehash
            )
        );
        factoryChecking.validateCluster(cluster, address(safe));
    }

    function test_strictValidationRejectsWrongChain() public {
        address approvedSafe = strictPreparer.APPROVED_SAFE();
        vm.chainId(1);
        vm.expectRevert(
            abi.encodeWithSelector(
                PrepareDstackFacetPathASafe.UnexpectedChain.selector, uint256(8453), uint256(1)
            )
        );
        strictPreparer.validateCluster(cluster, approvedSafe);
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
