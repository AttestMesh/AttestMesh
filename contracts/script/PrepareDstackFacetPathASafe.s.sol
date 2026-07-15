// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";

import { IERC2535DiamondCut } from "@solidstate/contracts/interfaces/IERC2535DiamondCut.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";

import { DstackFacet } from "../src/facets/attestor/DstackFacet.sol";
import { ClusterMember } from "../src/members/ClusterMember.sol";
import { ClusterCut } from "../src/libraries/ClusterCut.sol";

interface IPathAClusterOwner {
    function owner() external view returns (address);

    function clusterOwner() external view returns (address);
}

interface IPathADiamondLoupe {
    function facetAddress(bytes4 selector) external view returns (address);
}

interface IPathASafeBoundary {
    function getThreshold() external view returns (uint256);

    function getOwners() external view returns (address[] memory);
}

/// @notice Prepares, but deliberately does not execute, the Path-A diamond upgrade for a
///         Safe-owned cluster. An arbitrary funded EOA may broadcast the two implementation
///         deployments. The cluster's Safe must separately review and execute the emitted
///         target/value/calldata tuple.
///
/// The script fails closed unless:
/// - CLUSTER and EXPECTED_SAFE_OWNER are deployed contracts;
/// - EXPECTED_SAFE_OWNER exposes an initialized Safe-compatible owner/threshold surface;
/// - both the accepted solidstate owner and cluster-policy owner are EXPECTED_SAFE_OWNER; and
/// - all 19 canonical dstack selectors currently resolve to one deployed facet.
///
/// Env:
/// - PRIVATE_KEY: arbitrary broadcaster used only to deploy the new implementations;
/// - CLUSTER: ClusterDiamond to prepare the cut for;
/// - EXPECTED_SAFE_OWNER: accepted Safe owner of CLUSTER;
/// - PATHA_BUNDLE_FILE: output JSON path under contracts/script/deployments/.
contract PrepareDstackFacetPathASafe is Script {
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
        if (cluster.code.length == 0) revert ClusterHasNoCode(cluster);
        if (expectedSafe.code.length == 0) revert ExpectedSafeHasNoCode(expectedSafe);
        _validateSafeBoundary(expectedSafe);

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

        vm.startBroadcast(broadcasterKey);
        newDstackFacet = address(new DstackFacet());
        newClusterMemberImplementation = address(new ClusterMember());
        vm.stopBroadcast();

        if (newDstackFacet.code.length == 0) {
            revert NewImplementationHasNoCode(newDstackFacet);
        }
        if (newClusterMemberImplementation.code.length == 0) {
            revert NewImplementationHasNoCode(newClusterMemberImplementation);
        }

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

    function _validateSafeBoundary(address expectedSafe) internal view {
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
        vm.serializeAddress(object, "cluster", cluster);
        vm.serializeAddress(object, "currentDstackFacet", currentDstackFacet);
        vm.serializeAddress(object, "dstackFacet", newDstackFacet);
        vm.serializeAddress(object, "clusterMemberImplementation", newClusterMemberImplementation);
        vm.serializeAddress(object, "target", cluster);
        vm.serializeUint(object, "value", 0);
        vm.serializeBytes32(object, "calldataHash", keccak256(safeCalldata));
        string memory json = vm.serializeBytes(object, "data", safeCalldata);
        vm.writeJson(json, bundleFile);
    }
}
