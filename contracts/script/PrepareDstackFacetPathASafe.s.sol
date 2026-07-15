// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";

import { IERC2535DiamondCut } from "@solidstate/contracts/interfaces/IERC2535DiamondCut.sol";
import { IERC2535DiamondLoupe } from "@solidstate/contracts/interfaces/IERC2535DiamondLoupe.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";

import { DstackFacet } from "../src/facets/attestor/DstackFacet.sol";
import { ClusterMember } from "../src/members/ClusterMember.sol";
import { ClusterCut } from "../src/libraries/ClusterCut.sol";

import { Create2 } from "@openzeppelin/contracts/utils/Create2.sol";

interface IPathAClusterOwner {
    function owner() external view returns (address);

    function clusterOwner() external view returns (address);

    function memberCount() external view returns (uint256);

    function cskCommitment() external view returns (bytes32);
}

interface IPathADiamondLoupe {
    function facetAddress(bytes4 selector) external view returns (address);
}

interface IPathASafeBoundary {
    function masterCopy() external view returns (address);

    function VERSION() external view returns (string memory);

    function getThreshold() external view returns (uint256);

    function getOwners() external view returns (address[] memory);

    function getModulesPaginated(address start, uint256 pageSize)
        external
        view
        returns (address[] memory modules, address next);

    function getStorageAt(uint256 offset, uint256 length) external view returns (bytes memory);
}

interface IPathAClusterFactory {
    function factoryOwner() external view returns (address);

    function diamondInitImpl() external view returns (address);

    function attestFacet() external view returns (address);

    function messageFacet() external view returns (address);

    function networkFacet() external view returns (address);

    function dstackFacet() external view returns (address);

    function deployedClusters(address cluster) external view returns (bool);
}

interface IPathAMemberFactory {
    function factoryOwner() external view returns (address);

    function implementation() external view returns (address);
}

/// @notice Prepares, but deliberately does not execute, the Path-A diamond upgrade for a
///         Safe-owned cluster. An arbitrary funded EOA may broadcast the two implementation
///         deployments. The cluster's Safe must separately review and execute the emitted
///         target/value/calldata tuple.
///
/// The script fails closed unless:
/// - the chain, Safe proxy/singleton, owners, threshold, modules, guard, and fallback handler
///   match the reviewed Base trust anchors;
/// - the immutable factories and every component runtime match their reviewed code hashes;
/// - CLUSTER is a fresh canonical factory deployment owned on both surfaces by that Safe; and
/// - its complete five-facet/54-selector topology is the exact pre-Path-A factory topology.
///
/// Env:
/// - PRIVATE_KEY: arbitrary broadcaster used only to deploy the new implementations;
/// - CLUSTER: ClusterDiamond to prepare the cut for;
/// - EXPECTED_SAFE_OWNER: accepted Safe owner of CLUSTER;
/// - PATHA_BUNDLE_FILE: output JSON path under contracts/script/deployments/.
contract PrepareDstackFacetPathASafe is Script {
    uint256 public constant REVIEWED_CHAIN_ID = 8453;

    address public constant APPROVED_SAFE = 0xD97b5e3Fc685e29825d76b4F90B7B3ACAE7D66f0;
    bytes32 public constant APPROVED_SAFE_PROXY_CODEHASH =
        0xd7d408ebcd99b2b70be43e20253d6d92a8ea8fab29bd3be7f55b10032331fb4c;
    address public constant APPROVED_SAFE_SINGLETON = 0x41675C099F32341bf84BFc5382aF534df5C7461a;
    bytes32 public constant APPROVED_SAFE_SINGLETON_CODEHASH =
        0x1fe2df852ba3299d6534ef416eefa406e56ced995bca886ab7a553e6d0c5e1c4;
    uint256 public constant APPROVED_SAFE_THRESHOLD = 1;
    address public constant APPROVED_SAFE_OWNER_0 = 0x37f5761218D30E90CeeF54CcA1b71208115Acc4F;
    address public constant APPROVED_SAFE_OWNER_1 = 0x890e54A378b07483a6e04AD3D2264609bD3fd07E;
    bytes32 public constant SAFE_GUARD_STORAGE_SLOT =
        0x4a204f620c8c5ccdca3fd54d003badd85ba500436a431f0cbda4f558c93c34c8;
    bytes32 public constant SAFE_FALLBACK_HANDLER_STORAGE_SLOT =
        0x6c9a6c4a39284e37ed1cf53d337577d14212a4870fb976a4366c693b939918d5;
    address public constant APPROVED_SAFE_FALLBACK_HANDLER =
        0xfd0732Dc9E303f09fCEf3a7388Ad10A83459Ec99;
    bytes32 public constant APPROVED_SAFE_FALLBACK_HANDLER_CODEHASH =
        0x7c6007a5d711cea8dfd5d91f5940ec29c7f200fe511eb1fc1397b367af3c42f9;

    address public constant CANONICAL_CLUSTER_FACTORY = 0xf6E85fD138E3208d3AAE63ce4E2A33f20e82b9fb;
    bytes32 public constant CANONICAL_CLUSTER_FACTORY_CODEHASH =
        0x9cd0a5b30b384cc625105713ef346fc086d723c6801e3a0cb07e47fc91e4e71b;
    address public constant CANONICAL_MEMBER_FACTORY = 0xFf9f438EFdAa197f4ae59D1d711C4f09640b8417;
    bytes32 public constant CANONICAL_MEMBER_FACTORY_CODEHASH =
        0x46b41e453a4437bba466f4491c38c33f99d92c0b129756d45d269d35241e4718;
    address public constant FACTORY_OWNER = 0x60b174704AdAf2b0BF87B426B364D6EbD81818E1;
    address public constant DIAMOND_INIT = 0xe3C9CE59b6c164c7b4c81f686C876EB85198cFC3;
    address public constant ATTEST_FACET = 0x10532164ca3BdaCf1dAd13Fb534262DEc5aAA9AA;
    address public constant MESSAGE_FACET = 0x0F67cd8c1D8A2F71d2bb8091B2eb166E6b6bB564;
    // The deployed factory is immutable and still points at the pre-heartbeat NetworkFacet.
    address public constant FACTORY_NETWORK_FACET = 0x6AB2b7D506c85C7A9eA22f2ECcE0cc9191006159;
    address public constant FACTORY_DSTACK_FACET = 0xe9d463974c6E833DC38794d7f2DC5AB692352968;
    address public constant FACTORY_MEMBER_IMPLEMENTATION =
        0xd05223da04B4E73AC02ECA9638D490f45f765843;
    bytes32 public constant DIAMOND_INIT_CODEHASH =
        0x0f11d9b0fa554b9b78051e49d7f92c440808e0eea23a4d30c7f925c4f44f842c;
    bytes32 public constant ATTEST_FACET_CODEHASH =
        0x94ec9771e40ecb2b817cd33b56ea1a442037911a4a04ed6a92cbef7b5abd6223;
    bytes32 public constant MESSAGE_FACET_CODEHASH =
        0x4328ba0202b88caceca4ed02eff3052c775f89f3620bba3fc90654f3c51256c2;
    bytes32 public constant FACTORY_NETWORK_FACET_CODEHASH =
        0x0e63b71315c355eb05547aafc898ca82618abf1fb5614da782b7db16690cfc55;
    bytes32 public constant FACTORY_DSTACK_FACET_CODEHASH =
        0xa8d3883794943b307f491bc6b8ff2cd7f3817f17b76457b6dc48eceba36d973e;
    bytes32 public constant FACTORY_MEMBER_IMPLEMENTATION_CODEHASH =
        0xf7228096d52f508780b4a03e0632340a1ba139b104d392ddf47bb264c3be81f5;

    address public constant CREATE2_DEPLOYER = 0x4e59b44847b379578588920cA78FbF26c0B4956C;
    bytes32 public constant CREATE2_DEPLOYER_CODEHASH =
        0x2fa86add0aed31f33a762c9d88e807c475bd51d0f52bd0955754b2608f7e4989;
    bytes32 public constant DSTACK_FACET_SALT =
        0xcef5a875b56ff1fd96fd03ab048c551f1e40e478d13fc040fcd374a671de86f3;
    bytes32 public constant MEMBER_IMPLEMENTATION_SALT =
        0xac3d0f1737eb55ea6a051b5a5d36d8a37dd42e0408cd9c8d415905623c7f9074;
    bytes32 public constant DSTACK_FACET_INIT_CODEHASH =
        0x61e193b29e486425995a684eaa54c35c9f8d6daddc162fed76ae6788719f3f21;
    bytes32 public constant MEMBER_IMPLEMENTATION_INIT_CODEHASH =
        0x3908e3b1dada03a44e17e2e5b21ce3ea721f7cdb99ee1fdb38ebe1e3ace2c2a9;
    bytes32 public constant DSTACK_FACET_RUNTIME_CODEHASH =
        0x91c3c31fabe7d7c55924bd46873bcb46960c1e5db5fb1e322fc8fb2f1ad76563;
    // ClusterMember inherits UUPSUpgradeable's immutable __self. This is the runtime
    // hash at DETERMINISTIC_MEMBER_IMPLEMENTATION, not forge inspect's zero-address template.
    bytes32 public constant MEMBER_IMPLEMENTATION_RUNTIME_CODEHASH =
        0xff7bc4fce11b058048beea8374ef775a81fc48c1133e3d28197212a12b1fee5f;
    address public constant DETERMINISTIC_DSTACK_FACET = 0x15ABc7087941AfD4640A8974F1D56aD4A4860d28;
    address public constant DETERMINISTIC_MEMBER_IMPLEMENTATION =
        0x6f66d930923d18cc6469954E2b90452e60266d64;

    error UnexpectedChain(uint256 expected, uint256 actual);
    error UnexpectedApprovedSafe(address expected, address actual);
    error UnexpectedCodeHash(address account, bytes32 expected, bytes32 actual);
    error UnexpectedSafeSingleton(address expected, address actual);
    error UnexpectedSafeVersion(string expected, string actual);
    error UnexpectedSafeOwners();
    error UnexpectedSafeModules(uint256 count, address next);
    error UnexpectedSafeGuard(address actual);
    error UnexpectedSafeFallbackHandler(address expected, address actual);
    error InvalidSafeStorageRead(bytes32 slot);
    error UnexpectedFactoryValue(bytes4 selector, address expected, address actual);
    error ClusterNotFactoryDeployed(address cluster);
    error ClusterFacetsReadFailed(address cluster);
    error UnexpectedClusterFacetCount(uint256 expected, uint256 actual);
    error UnexpectedClusterFacetSet(address target);
    error ClusterIsNotFresh(uint256 memberCount, bytes32 cskCommitment);
    error ReviewedBuildHashMismatch(bytes32 expected, bytes32 actual);
    error UnexpectedDeterministicAddress(address expected, address actual);
    error DeterministicDeploymentFailed(address expected);
    error UnexpectedDeploymentReturn(address expected, bytes returned);
    error ClusterHasNoCode(address cluster);
    error ExpectedSafeHasNoCode(address expectedSafe);
    error InvalidSafeBoundary(address expectedSafe);
    error InvalidSafeConfiguration(address expectedSafe, uint256 threshold, uint256 ownerCount);
    error ClusterOwnerReadFailed(address cluster);
    error UnexpectedClusterOwner(address expectedSafe, address actualOwner);
    error ClusterPolicyOwnerReadFailed(address cluster);
    error UnexpectedClusterPolicyOwner(address expectedSafe, address actualOwner);
    error FacetReadFailed(address cluster, bytes4 selector);
    error MissingDstackSelector(bytes4 selector);
    error SplitDstackSelector(bytes4 selector, address expectedFacet, address actualFacet);
    error CurrentDstackFacetHasNoCode(address facet);
    error NewDstackFacetIsZero();
    error NewImplementationHasNoCode(address implementation);

    uint256 internal constant BUNDLE_SCHEMA_VERSION = 1;

    /// @notice Returns the exact one-entry REPLACE cut the Safe will execute.
    function buildDiamondCut(address newDstackFacet)
        public
        pure
        returns (IERC2535DiamondCutInternal.FacetCut[] memory cut)
    {
        if (newDstackFacet == address(0)) revert NewDstackFacetIsZero();

        IERC2535DiamondCutInternal.FacetCut[] memory canonical =
            ClusterCut.buildFacetCuts(address(1), address(1), address(1), newDstackFacet);
        cut = new IERC2535DiamondCutInternal.FacetCut[](1);
        cut[0] = IERC2535DiamondCutInternal.FacetCut({
            target: newDstackFacet,
            action: IERC2535DiamondCutInternal.FacetCutAction.REPLACE,
            selectors: canonical[3].selectors
        });
    }

    /// @notice Returns the exact calldata for Safe -> ClusterDiamond.
    function buildSafeCalldata(address newDstackFacet) public pure returns (bytes memory) {
        return abi.encodeCall(
            IERC2535DiamondCut.diamondCut, (buildDiamondCut(newDstackFacet), address(0), bytes(""))
        );
    }

    /// @notice Validates the Safe ownership boundary and current dstack selector topology.
    /// @return currentDstackFacet The single facet currently serving all dstack selectors.
    function validateCluster(address cluster, address expectedSafe)
        public
        view
        returns (address currentDstackFacet)
    {
        _validateChain();
        if (cluster.code.length == 0) revert ClusterHasNoCode(cluster);
        _validateApprovedSafe(expectedSafe);
        _validateCanonicalCluster(cluster);

        (bool ownerOk, bytes memory ownerData) =
            cluster.staticcall(abi.encodeCall(IPathAClusterOwner.owner, ()));
        if (!ownerOk || ownerData.length != 32) revert ClusterOwnerReadFailed(cluster);
        address actualOwner = abi.decode(ownerData, (address));
        if (actualOwner != expectedSafe) {
            revert UnexpectedClusterOwner(expectedSafe, actualOwner);
        }

        (bool policyOwnerOk, bytes memory policyOwnerData) =
            cluster.staticcall(abi.encodeCall(IPathAClusterOwner.clusterOwner, ()));
        if (!policyOwnerOk || policyOwnerData.length != 32) {
            revert ClusterPolicyOwnerReadFailed(cluster);
        }
        address actualPolicyOwner = abi.decode(policyOwnerData, (address));
        if (actualPolicyOwner != expectedSafe) {
            revert UnexpectedClusterPolicyOwner(expectedSafe, actualPolicyOwner);
        }

        bytes4[] memory selectors = _dstackSelectors();
        for (uint256 i; i < selectors.length; ++i) {
            address resolved = _readFacet(cluster, selectors[i]);
            if (resolved == address(0)) revert MissingDstackSelector(selectors[i]);
            if (i == 0) {
                currentDstackFacet = resolved;
            } else if (resolved != currentDstackFacet) {
                revert SplitDstackSelector(selectors[i], currentDstackFacet, resolved);
            }
        }
        if (currentDstackFacet.code.length == 0) {
            revert CurrentDstackFacetHasNoCode(currentDstackFacet);
        }
    }

    function run()
        external
        returns (
            address newDstackFacet,
            address newClusterMemberImplementation,
            bytes memory safeCalldata
        )
    {
        uint256 broadcasterKey = vm.envUint("PRIVATE_KEY");
        address broadcaster = vm.addr(broadcasterKey);
        address cluster = vm.envAddress("CLUSTER");
        address expectedSafe = vm.envAddress("EXPECTED_SAFE_OWNER");
        string memory bundleFile = vm.envString("PATHA_BUNDLE_FILE");

        address currentDstackFacet = validateCluster(cluster, expectedSafe);
        _validateReviewedBuild();
        _validateCreate2Deployer();

        newDstackFacet = _deployOrVerify(
            broadcasterKey,
            DSTACK_FACET_SALT,
            type(DstackFacet).creationCode,
            DSTACK_FACET_INIT_CODEHASH,
            DSTACK_FACET_RUNTIME_CODEHASH,
            DETERMINISTIC_DSTACK_FACET
        );
        newClusterMemberImplementation = _deployOrVerify(
            broadcasterKey,
            MEMBER_IMPLEMENTATION_SALT,
            type(ClusterMember).creationCode,
            MEMBER_IMPLEMENTATION_INIT_CODEHASH,
            MEMBER_IMPLEMENTATION_RUNTIME_CODEHASH,
            DETERMINISTIC_MEMBER_IMPLEMENTATION
        );

        safeCalldata = buildSafeCalldata(newDstackFacet);
        _writeBundle(
            bundleFile,
            broadcaster,
            cluster,
            expectedSafe,
            currentDstackFacet,
            newDstackFacet,
            newClusterMemberImplementation,
            safeCalldata
        );

        console2.log("Path-A preparation only; the cluster has NOT been mutated.");
        console2.log("broadcaster:                       ", broadcaster);
        console2.log("accepted Safe owner:               ", expectedSafe);
        console2.log("cluster / Safe transaction target: ", cluster);
        console2.log("Safe transaction value:            ", uint256(0));
        console2.log("current DstackFacet:                ", currentDstackFacet);
        console2.log("new DstackFacet:                    ", newDstackFacet);
        console2.log("new ClusterMember implementation:   ", newClusterMemberImplementation);
        console2.log("Safe transaction calldata:");
        console2.logBytes(safeCalldata);
        console2.log("bundle file:                        ", bundleFile);
    }

    function _validateChain() internal view {
        if (block.chainid != REVIEWED_CHAIN_ID) {
            revert UnexpectedChain(REVIEWED_CHAIN_ID, block.chainid);
        }
    }

    function _validateApprovedSafe(address expectedSafe) internal view virtual {
        if (expectedSafe != APPROVED_SAFE) {
            revert UnexpectedApprovedSafe(APPROVED_SAFE, expectedSafe);
        }
        if (expectedSafe.code.length == 0) revert ExpectedSafeHasNoCode(expectedSafe);
        if (expectedSafe.codehash != APPROVED_SAFE_PROXY_CODEHASH) {
            revert UnexpectedCodeHash(
                expectedSafe, APPROVED_SAFE_PROXY_CODEHASH, expectedSafe.codehash
            );
        }
        if (APPROVED_SAFE_SINGLETON.codehash != APPROVED_SAFE_SINGLETON_CODEHASH) {
            revert UnexpectedCodeHash(
                APPROVED_SAFE_SINGLETON,
                APPROVED_SAFE_SINGLETON_CODEHASH,
                APPROVED_SAFE_SINGLETON.codehash
            );
        }

        address singleton = IPathASafeBoundary(expectedSafe).masterCopy();
        if (singleton != APPROVED_SAFE_SINGLETON) {
            revert UnexpectedSafeSingleton(APPROVED_SAFE_SINGLETON, singleton);
        }
        string memory version = IPathASafeBoundary(expectedSafe).VERSION();
        if (keccak256(bytes(version)) != keccak256(bytes("1.4.1"))) {
            revert UnexpectedSafeVersion("1.4.1", version);
        }
        uint256 threshold = IPathASafeBoundary(expectedSafe).getThreshold();
        address[] memory owners = IPathASafeBoundary(expectedSafe).getOwners();
        if (threshold != APPROVED_SAFE_THRESHOLD || !_hasExactApprovedOwners(owners)) {
            revert UnexpectedSafeOwners();
        }
        _validateSafeExecutionSurface(
            expectedSafe, APPROVED_SAFE_FALLBACK_HANDLER, APPROVED_SAFE_FALLBACK_HANDLER_CODEHASH
        );
    }

    function _validateSafeBoundary(address expectedSafe) internal view {
        if (expectedSafe.code.length == 0) revert ExpectedSafeHasNoCode(expectedSafe);
        try IPathASafeBoundary(expectedSafe).getThreshold() returns (uint256 threshold) {
            try IPathASafeBoundary(expectedSafe).getOwners() returns (address[] memory owners) {
                if (threshold == 0 || owners.length == 0 || threshold > owners.length) {
                    revert InvalidSafeConfiguration(expectedSafe, threshold, owners.length);
                }
                for (uint256 i; i < owners.length; ++i) {
                    if (owners[i] == address(0)) {
                        revert InvalidSafeConfiguration(expectedSafe, threshold, owners.length);
                    }
                    for (uint256 j = i + 1; j < owners.length; ++j) {
                        if (owners[i] == owners[j]) {
                            revert InvalidSafeConfiguration(expectedSafe, threshold, owners.length);
                        }
                    }
                }
            } catch {
                revert InvalidSafeBoundary(expectedSafe);
            }
        } catch {
            revert InvalidSafeBoundary(expectedSafe);
        }
    }

    function _validateCanonicalCluster(address cluster) internal view virtual {
        if (CANONICAL_CLUSTER_FACTORY.codehash != CANONICAL_CLUSTER_FACTORY_CODEHASH) {
            revert UnexpectedCodeHash(
                CANONICAL_CLUSTER_FACTORY,
                CANONICAL_CLUSTER_FACTORY_CODEHASH,
                CANONICAL_CLUSTER_FACTORY.codehash
            );
        }
        if (CANONICAL_MEMBER_FACTORY.codehash != CANONICAL_MEMBER_FACTORY_CODEHASH) {
            revert UnexpectedCodeHash(
                CANONICAL_MEMBER_FACTORY,
                CANONICAL_MEMBER_FACTORY_CODEHASH,
                CANONICAL_MEMBER_FACTORY.codehash
            );
        }
        _expectCodeHash(DIAMOND_INIT, DIAMOND_INIT_CODEHASH);
        _expectCodeHash(ATTEST_FACET, ATTEST_FACET_CODEHASH);
        _expectCodeHash(MESSAGE_FACET, MESSAGE_FACET_CODEHASH);
        _expectCodeHash(FACTORY_NETWORK_FACET, FACTORY_NETWORK_FACET_CODEHASH);
        _expectCodeHash(FACTORY_DSTACK_FACET, FACTORY_DSTACK_FACET_CODEHASH);
        _expectCodeHash(FACTORY_MEMBER_IMPLEMENTATION, FACTORY_MEMBER_IMPLEMENTATION_CODEHASH);

        IPathAClusterFactory factory = IPathAClusterFactory(CANONICAL_CLUSTER_FACTORY);
        _expectFactoryValue(
            IPathAClusterFactory.factoryOwner.selector, FACTORY_OWNER, factory.factoryOwner()
        );
        _expectFactoryValue(
            IPathAClusterFactory.diamondInitImpl.selector, DIAMOND_INIT, factory.diamondInitImpl()
        );
        _expectFactoryValue(
            IPathAClusterFactory.attestFacet.selector, ATTEST_FACET, factory.attestFacet()
        );
        _expectFactoryValue(
            IPathAClusterFactory.messageFacet.selector, MESSAGE_FACET, factory.messageFacet()
        );
        _expectFactoryValue(
            IPathAClusterFactory.networkFacet.selector,
            FACTORY_NETWORK_FACET,
            factory.networkFacet()
        );
        _expectFactoryValue(
            IPathAClusterFactory.dstackFacet.selector, FACTORY_DSTACK_FACET, factory.dstackFacet()
        );

        IPathAMemberFactory memberFactory = IPathAMemberFactory(CANONICAL_MEMBER_FACTORY);
        _expectFactoryValue(
            IPathAMemberFactory.factoryOwner.selector, FACTORY_OWNER, memberFactory.factoryOwner()
        );
        _expectFactoryValue(
            IPathAMemberFactory.implementation.selector,
            FACTORY_MEMBER_IMPLEMENTATION,
            memberFactory.implementation()
        );

        if (!factory.deployedClusters(cluster)) revert ClusterNotFactoryDeployed(cluster);
        _validateExactClusterTopology(cluster, FACTORY_DSTACK_FACET);
        uint256 members = IPathAClusterOwner(cluster).memberCount();
        bytes32 commitment = IPathAClusterOwner(cluster).cskCommitment();
        if (members != 0 || commitment != bytes32(0)) {
            revert ClusterIsNotFresh(members, commitment);
        }
    }

    function _expectCodeHash(address account, bytes32 expected) internal view {
        if (account.codehash != expected) {
            revert UnexpectedCodeHash(account, expected, account.codehash);
        }
    }

    function _validateSafeExecutionSurface(
        address safe,
        address expectedFallbackHandler,
        bytes32 expectedFallbackHandlerCodehash
    ) internal view {
        (address[] memory modules, address next) =
            IPathASafeBoundary(safe).getModulesPaginated(address(1), 100);
        if (modules.length != 0 || next != address(1)) {
            revert UnexpectedSafeModules(modules.length, next);
        }

        address guard = _readSafeStorageAddress(safe, SAFE_GUARD_STORAGE_SLOT);
        if (guard != address(0)) revert UnexpectedSafeGuard(guard);
        address handler = _readSafeStorageAddress(safe, SAFE_FALLBACK_HANDLER_STORAGE_SLOT);
        if (handler != expectedFallbackHandler) {
            revert UnexpectedSafeFallbackHandler(expectedFallbackHandler, handler);
        }
        _expectCodeHash(handler, expectedFallbackHandlerCodehash);
    }

    function _readSafeStorageAddress(address safe, bytes32 slot)
        internal
        view
        returns (address value)
    {
        bytes memory stored = IPathASafeBoundary(safe).getStorageAt(uint256(slot), 1);
        if (stored.length != 32) revert InvalidSafeStorageRead(slot);
        bytes32 word;
        assembly ("memory-safe") {
            word := mload(add(stored, 0x20))
        }
        value = address(uint160(uint256(word)));
    }

    function _validateExactClusterTopology(address cluster, address expectedDstackFacet)
        internal
        view
    {
        IERC2535DiamondLoupe.Facet[] memory actual;
        try IERC2535DiamondLoupe(cluster).facets() returns (
            IERC2535DiamondLoupe.Facet[] memory facets_
        ) {
            actual = facets_;
        } catch {
            revert ClusterFacetsReadFailed(cluster);
        }
        if (actual.length != 5) revert UnexpectedClusterFacetCount(5, actual.length);

        address[5] memory targets =
            [cluster, ATTEST_FACET, MESSAGE_FACET, FACTORY_NETWORK_FACET, expectedDstackFacet];
        bytes4[][] memory selectors = new bytes4[][](5);
        selectors[0] = _solidstateSelectors();
        selectors[1] = _attestSelectors();
        selectors[2] = _messageSelectors();
        selectors[3] = _factoryNetworkSelectors();
        selectors[4] = _dstackSelectors();
        bool[5] memory seen;

        for (uint256 i; i < actual.length; ++i) {
            bool matched;
            for (uint256 j; j < targets.length; ++j) {
                if (actual[i].target == targets[j]) {
                    if (seen[j] || !_sameSelectorSet(actual[i].selectors, selectors[j])) {
                        revert UnexpectedClusterFacetSet(actual[i].target);
                    }
                    seen[j] = true;
                    matched = true;
                    break;
                }
            }
            if (!matched) revert UnexpectedClusterFacetSet(actual[i].target);
        }
        for (uint256 i; i < seen.length; ++i) {
            if (!seen[i]) revert UnexpectedClusterFacetSet(targets[i]);
        }
    }

    function _sameSelectorSet(bytes4[] memory actual, bytes4[] memory expected)
        internal
        pure
        returns (bool)
    {
        if (actual.length != expected.length) return false;
        for (uint256 i; i < expected.length; ++i) {
            bool found;
            for (uint256 j; j < actual.length; ++j) {
                if (actual[j] == expected[i]) {
                    found = true;
                    break;
                }
            }
            if (!found) return false;
        }
        return true;
    }

    function _solidstateSelectors() internal pure returns (bytes4[] memory s) {
        s = new bytes4[](12);
        s[0] = 0x2c408059;
        s[1] = 0x91423765;
        s[2] = 0x1f931c1c;
        s[3] = 0x7a0ed627;
        s[4] = 0xadfca15e;
        s[5] = 0x52ef6b2c;
        s[6] = 0xcdffacc6;
        s[7] = 0x01ffc9a7;
        s[8] = 0x8da5cb5b;
        s[9] = 0x8ab5150a;
        s[10] = 0xf2fde38b;
        s[11] = 0x79ba5097;
    }

    function _attestSelectors() internal pure returns (bytes4[] memory s) {
        s = new bytes4[](19);
        s[0] = 0x7f989b8d;
        s[1] = 0x63a30b72;
        s[2] = 0x3b4c9891;
        s[3] = 0x87dc7ae5;
        s[4] = 0x7918228d;
        s[5] = 0xb6afd2ca;
        s[6] = 0x11aee380;
        s[7] = 0x08c75c4f;
        s[8] = 0x0441484e;
        s[9] = 0x9068639e;
        s[10] = 0xbb671732;
        s[11] = 0x3eeb8ee8;
        s[12] = 0x38c640e9;
        s[13] = 0xd604bed7;
        s[14] = 0x35901459;
        s[15] = 0x8af487aa;
        s[16] = 0x319b215c;
        s[17] = 0xd9c8dbfe;
        s[18] = 0x29ca97eb;
    }

    function _messageSelectors() internal pure returns (bytes4[] memory s) {
        s = new bytes4[](1);
        s[0] = 0x84076765;
    }

    function _factoryNetworkSelectors() internal pure returns (bytes4[] memory s) {
        s = new bytes4[](3);
        s[0] = 0x4979ff72;
        s[1] = 0x06815bc9;
        s[2] = 0x4fda654e;
    }

    function _expectFactoryValue(bytes4 selector, address expected, address actual) internal pure {
        if (actual != expected) revert UnexpectedFactoryValue(selector, expected, actual);
    }

    function _hasExactApprovedOwners(address[] memory owners) internal pure returns (bool) {
        if (owners.length != 2) return false;
        return (owners[0] == APPROVED_SAFE_OWNER_0 && owners[1] == APPROVED_SAFE_OWNER_1)
            || (owners[0] == APPROVED_SAFE_OWNER_1 && owners[1] == APPROVED_SAFE_OWNER_0);
    }

    function _validateReviewedBuild() internal pure {
        bytes32 dstackInitHash = keccak256(type(DstackFacet).creationCode);
        if (dstackInitHash != DSTACK_FACET_INIT_CODEHASH) {
            revert ReviewedBuildHashMismatch(DSTACK_FACET_INIT_CODEHASH, dstackInitHash);
        }
        bytes32 memberInitHash = keccak256(type(ClusterMember).creationCode);
        if (memberInitHash != MEMBER_IMPLEMENTATION_INIT_CODEHASH) {
            revert ReviewedBuildHashMismatch(MEMBER_IMPLEMENTATION_INIT_CODEHASH, memberInitHash);
        }
        bytes32 dstackRuntimeHash = keccak256(type(DstackFacet).runtimeCode);
        if (dstackRuntimeHash != DSTACK_FACET_RUNTIME_CODEHASH) {
            revert ReviewedBuildHashMismatch(DSTACK_FACET_RUNTIME_CODEHASH, dstackRuntimeHash);
        }
        address predictedDstack = Create2.computeAddress(
            DSTACK_FACET_SALT, DSTACK_FACET_INIT_CODEHASH, CREATE2_DEPLOYER
        );
        if (predictedDstack != DETERMINISTIC_DSTACK_FACET) {
            revert UnexpectedDeterministicAddress(DETERMINISTIC_DSTACK_FACET, predictedDstack);
        }
        address predictedMember = Create2.computeAddress(
            MEMBER_IMPLEMENTATION_SALT, MEMBER_IMPLEMENTATION_INIT_CODEHASH, CREATE2_DEPLOYER
        );
        if (predictedMember != DETERMINISTIC_MEMBER_IMPLEMENTATION) {
            revert UnexpectedDeterministicAddress(
                DETERMINISTIC_MEMBER_IMPLEMENTATION, predictedMember
            );
        }
    }

    function _validateCreate2Deployer() internal view {
        if (CREATE2_DEPLOYER.codehash != CREATE2_DEPLOYER_CODEHASH) {
            revert UnexpectedCodeHash(
                CREATE2_DEPLOYER, CREATE2_DEPLOYER_CODEHASH, CREATE2_DEPLOYER.codehash
            );
        }
    }

    function _deployOrVerify(
        uint256 broadcasterKey,
        bytes32 salt,
        bytes memory initCode,
        bytes32 expectedInitCodeHash,
        bytes32 expectedRuntimeCodeHash,
        address expectedAddress
    ) internal returns (address deployed) {
        bytes32 actualInitCodeHash = keccak256(initCode);
        if (actualInitCodeHash != expectedInitCodeHash) {
            revert ReviewedBuildHashMismatch(expectedInitCodeHash, actualInitCodeHash);
        }
        deployed = Create2.computeAddress(salt, actualInitCodeHash, CREATE2_DEPLOYER);
        if (deployed != expectedAddress) {
            revert UnexpectedDeterministicAddress(expectedAddress, deployed);
        }

        if (deployed.code.length == 0) {
            vm.broadcast(broadcasterKey);
            (bool ok, bytes memory returned) =
                CREATE2_DEPLOYER.call(abi.encodePacked(salt, initCode));
            if (!ok) revert DeterministicDeploymentFailed(deployed);
            address returnedAddress;
            if (returned.length == 20) {
                assembly ("memory-safe") {
                    returnedAddress := shr(96, mload(add(returned, 0x20)))
                }
            }
            if (returned.length != 20 || returnedAddress != deployed) {
                revert UnexpectedDeploymentReturn(deployed, returned);
            }
        }
        if (deployed.code.length == 0) revert NewImplementationHasNoCode(deployed);
        if (deployed.codehash != expectedRuntimeCodeHash) {
            revert UnexpectedCodeHash(deployed, expectedRuntimeCodeHash, deployed.codehash);
        }
    }

    function _readFacet(address cluster, bytes4 selector) internal view returns (address facet) {
        (bool ok, bytes memory data) =
            cluster.staticcall(abi.encodeCall(IPathADiamondLoupe.facetAddress, (selector)));
        if (!ok || data.length != 32) revert FacetReadFailed(cluster, selector);
        facet = abi.decode(data, (address));
    }

    function _dstackSelectors() internal pure returns (bytes4[] memory selectors) {
        IERC2535DiamondCutInternal.FacetCut[] memory canonical =
            ClusterCut.buildFacetCuts(address(1), address(1), address(1), address(1));
        selectors = canonical[3].selectors;
    }

    function _writeBundle(
        string memory bundleFile,
        address broadcaster,
        address cluster,
        address expectedSafe,
        address currentDstackFacet,
        address newDstackFacet,
        address newClusterMemberImplementation,
        bytes memory safeCalldata
    ) internal {
        string memory object = "pathaSafeBundle";
        vm.serializeUint(object, "schemaVersion", BUNDLE_SCHEMA_VERSION);
        vm.serializeUint(object, "chainId", block.chainid);
        vm.serializeAddress(object, "broadcaster", broadcaster);
        vm.serializeAddress(object, "safeOwner", expectedSafe);
        vm.serializeAddress(object, "safeSingleton", APPROVED_SAFE_SINGLETON);
        vm.serializeBytes32(object, "safeProxyCodeHash", APPROVED_SAFE_PROXY_CODEHASH);
        vm.serializeBytes32(object, "safeSingletonCodeHash", APPROVED_SAFE_SINGLETON_CODEHASH);
        vm.serializeUint(object, "safeModuleCount", 0);
        vm.serializeAddress(object, "safeModulesNext", address(1));
        vm.serializeAddress(object, "safeGuard", address(0));
        vm.serializeAddress(object, "safeFallbackHandler", APPROVED_SAFE_FALLBACK_HANDLER);
        vm.serializeBytes32(
            object, "safeFallbackHandlerCodeHash", APPROVED_SAFE_FALLBACK_HANDLER_CODEHASH
        );
        vm.serializeAddress(object, "clusterFactory", CANONICAL_CLUSTER_FACTORY);
        vm.serializeAddress(object, "memberFactory", CANONICAL_MEMBER_FACTORY);
        vm.serializeBytes32(object, "diamondInitCodeHash", DIAMOND_INIT_CODEHASH);
        vm.serializeBytes32(object, "attestFacetCodeHash", ATTEST_FACET_CODEHASH);
        vm.serializeBytes32(object, "messageFacetCodeHash", MESSAGE_FACET_CODEHASH);
        vm.serializeBytes32(object, "factoryNetworkFacetCodeHash", FACTORY_NETWORK_FACET_CODEHASH);
        vm.serializeBytes32(object, "factoryDstackFacetCodeHash", FACTORY_DSTACK_FACET_CODEHASH);
        vm.serializeBytes32(
            object, "factoryMemberImplementationCodeHash", FACTORY_MEMBER_IMPLEMENTATION_CODEHASH
        );
        vm.serializeAddress(object, "create2Deployer", CREATE2_DEPLOYER);
        vm.serializeAddress(object, "cluster", cluster);
        vm.serializeUint(object, "clusterFacetCount", 5);
        vm.serializeUint(object, "clusterSelectorCount", 54);
        vm.serializeUint(object, "memberCount", 0);
        vm.serializeBytes32(object, "cskCommitment", bytes32(0));
        vm.serializeAddress(object, "currentDstackFacet", currentDstackFacet);
        vm.serializeAddress(object, "dstackFacet", newDstackFacet);
        vm.serializeBytes32(object, "dstackFacetSalt", DSTACK_FACET_SALT);
        vm.serializeBytes32(object, "dstackFacetInitCodeHash", DSTACK_FACET_INIT_CODEHASH);
        vm.serializeBytes32(object, "dstackFacetRuntimeCodeHash", DSTACK_FACET_RUNTIME_CODEHASH);
        vm.serializeAddress(object, "clusterMemberImplementation", newClusterMemberImplementation);
        vm.serializeBytes32(object, "clusterMemberImplementationSalt", MEMBER_IMPLEMENTATION_SALT);
        vm.serializeBytes32(
            object, "clusterMemberImplementationInitCodeHash", MEMBER_IMPLEMENTATION_INIT_CODEHASH
        );
        vm.serializeBytes32(
            object,
            "clusterMemberImplementationRuntimeCodeHash",
            MEMBER_IMPLEMENTATION_RUNTIME_CODEHASH
        );
        vm.serializeAddress(object, "target", cluster);
        vm.serializeUint(object, "value", 0);
        vm.serializeBytes32(object, "calldataHash", keccak256(safeCalldata));
        string memory json = vm.serializeBytes(object, "data", safeCalldata);
        vm.writeJson(json, bundleFile);
    }
}
