// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Create2 } from "@openzeppelin/contracts/utils/Create2.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";

import { ClusterDiamond } from "../ClusterDiamond.sol";
import { DiamondInit } from "../DiamondInit.sol";
import { ClusterCut } from "../libraries/ClusterCut.sol";

/// @notice Minimal solidstate ownership-transfer surface the factory needs.
interface IOwnableTransfer {
    function transferOwnership(address account) external;
}

/// @title ClusterDiamondFactory — canonical per-chain ClusterDiamond deployer
///        (contracts spec §10).
/// @notice Atomically deploys a ClusterDiamond + DiamondInit with the v1 default
///         facet cut. `isDeployedCluster` is the read the gas webhook uses to
///         validate UserOp targets.
contract ClusterDiamondFactory {
    address public immutable factoryOwner; // AttestMesh org Safe — gates upgrades
    address public immutable diamondInitImpl;
    address public immutable attestFacet;
    address public immutable messageFacet;
    address public immutable networkFacet;
    address public immutable dstackFacet;

    mapping(address account => bool) public deployedClusters;

    event ClusterDeployed(address indexed cluster, address indexed clusterOwner, bytes32 salt);

    constructor(
        address factoryOwner_,
        address diamondInitImpl_,
        address attestFacet_,
        address messageFacet_,
        address networkFacet_,
        address dstackFacet_
    ) {
        factoryOwner = factoryOwner_;
        diamondInitImpl = diamondInitImpl_;
        attestFacet = attestFacet_;
        messageFacet = messageFacet_;
        networkFacet = networkFacet_;
        dstackFacet = dstackFacet_;
    }

    function deployCluster(DiamondInit.InitArgs calldata args, bytes32 salt)
        external
        returns (address cluster)
    {
        IERC2535DiamondCutInternal.FacetCut[] memory cuts =
            ClusterCut.buildFacetCuts(attestFacet, messageFacet, networkFacet, dstackFacet);
        bytes memory initCalldata = abi.encodeCall(DiamondInit.init, (args));

        cluster = address(new ClusterDiamond{ salt: salt }(cuts, diamondInitImpl, initCalldata));
        deployedClusters[cluster] = true;

        // Factory is the transient solidstate owner; nominate the cluster Safe.
        IOwnableTransfer(cluster).transferOwnership(args.clusterOwner);

        emit ClusterDeployed(cluster, args.clusterOwner, salt);
    }

    function predictClusterAddress(bytes32 salt) external view returns (address) {
        // NB: the constructor args (facet addresses, init calldata) are part of the
        // CREATE2 init code, but only the salt varies per call here. Callers that
        // need an exact prediction must pass the same InitArgs to deployCluster.
        IERC2535DiamondCutInternal.FacetCut[] memory cuts =
            ClusterCut.buildFacetCuts(attestFacet, messageFacet, networkFacet, dstackFacet);
        bytes32 initCodeHash = keccak256(
            abi.encodePacked(
                type(ClusterDiamond).creationCode, abi.encode(cuts, diamondInitImpl, bytes(""))
            )
        );
        return Create2.computeAddress(salt, initCodeHash);
    }

    function isDeployedCluster(address account) external view returns (bool) {
        return deployedClusters[account];
    }
}
