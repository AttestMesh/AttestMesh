// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Create2 } from "@openzeppelin/contracts/utils/Create2.sol";
import { EnumerableSet } from "@openzeppelin/contracts/utils/structs/EnumerableSet.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";

import { ClusterDiamond } from "../ClusterDiamond.sol";
import { DiamondInitV2 } from "../DiamondInitV2.sol";
import { ClusterCut } from "../libraries/ClusterCut.sol";
import { AttestorConfig } from "../interfaces/IAttestorFacet.sol";
import { AttestorNotApproved, NotFactoryOwner } from "../errors/Errors.sol";

/// @notice Minimal solidstate ownership-transfer surface the factory needs.
interface IOwnableTransferV2 {
    function transferOwnership(address account) external;
}

/// @title ClusterDiamondFactoryV2 — multi-attestor ClusterDiamond deployer
///        (multi-attestor spec; supersedes the v1 factory for new deploys).
/// @notice Core facet addresses stay immutable; attestor facets are chosen per
///         deploy from an owner-managed approved-attestor registry of vetted
///         implementations, and each facet's cut is built from its own
///         `selectorManifest()`. Coexists with the deployed v1 factory (the gas
///         webhook trusts `isDeployedCluster` on either); v1 is deprecated for new
///         deploys. TRUST NOTE: approving an attestor facet only vets its code —
///         a cluster that installs OperatorFacet admits members vouched by a key,
///         not hardware attestation, by that cluster's own choice.
contract ClusterDiamondFactoryV2 {
    using EnumerableSet for EnumerableSet.AddressSet;

    address public immutable factoryOwner; // AttestMesh org Safe — gates the approved set
    address public immutable diamondInitImpl; // DiamondInitV2
    address public immutable attestFacet;
    address public immutable messageFacet;
    address public immutable networkFacet;

    EnumerableSet.AddressSet private _approvedAttestors;
    mapping(address account => bool) public deployedClusters;

    event ClusterDeployed(address indexed cluster, address indexed clusterOwner, bytes32 salt);
    event AttestorApproved(address indexed facet);
    event AttestorRevoked(address indexed facet);

    modifier onlyFactoryOwner() {
        if (msg.sender != factoryOwner) revert NotFactoryOwner();
        _;
    }

    constructor(
        address factoryOwner_,
        address diamondInitImpl_,
        address attestFacet_,
        address messageFacet_,
        address networkFacet_
    ) {
        factoryOwner = factoryOwner_;
        diamondInitImpl = diamondInitImpl_;
        attestFacet = attestFacet_;
        messageFacet = messageFacet_;
        networkFacet = networkFacet_;
    }

    // ── Approved-attestor registry (owner-managed) ────────────────────────────

    function addApprovedAttestor(address facet) external onlyFactoryOwner {
        if (_approvedAttestors.add(facet)) {
            emit AttestorApproved(facet);
        }
    }

    function removeApprovedAttestor(address facet) external onlyFactoryOwner {
        if (_approvedAttestors.remove(facet)) {
            emit AttestorRevoked(facet);
        }
    }

    function approvedAttestors() external view returns (address[] memory) {
        return _approvedAttestors.values();
    }

    function isApprovedAttestor(address facet) external view returns (bool) {
        return _approvedAttestors.contains(facet);
    }

    // ── Deploy ────────────────────────────────────────────────────────────────

    function deployCluster(
        DiamondInitV2.CoreInitArgs calldata core,
        AttestorConfig[] calldata attestors,
        bytes32 salt
    ) external returns (address cluster) {
        for (uint256 i; i < attestors.length; ++i) {
            if (!_approvedAttestors.contains(attestors[i].facet)) {
                revert AttestorNotApproved();
            }
        }

        (IERC2535DiamondCutInternal.FacetCut[] memory cuts, bytes memory initCalldata) =
            _buildDeploy(core, attestors);

        cluster = address(new ClusterDiamond{ salt: salt }(cuts, diamondInitImpl, initCalldata));
        deployedClusters[cluster] = true;

        // Factory is the transient solidstate owner; nominate the cluster Safe.
        IOwnableTransferV2(cluster).transferOwnership(core.clusterOwner);

        emit ClusterDeployed(cluster, core.clusterOwner, salt);
    }

    /// @notice Exact CREATE2 prediction. The init calldata (and thus the address)
    ///         depends on `core` + `attestors`, so the same values must be passed
    ///         to deployCluster.
    function predictClusterAddress(
        DiamondInitV2.CoreInitArgs calldata core,
        AttestorConfig[] calldata attestors,
        bytes32 salt
    ) external view returns (address) {
        (IERC2535DiamondCutInternal.FacetCut[] memory cuts, bytes memory initCalldata) =
            _buildDeploy(core, attestors);
        bytes32 initCodeHash = keccak256(
            abi.encodePacked(
                type(ClusterDiamond).creationCode, abi.encode(cuts, diamondInitImpl, initCalldata)
            )
        );
        return Create2.computeAddress(salt, initCodeHash);
    }

    function isDeployedCluster(address account) external view returns (bool) {
        return deployedClusters[account];
    }

    function _buildDeploy(
        DiamondInitV2.CoreInitArgs calldata core,
        AttestorConfig[] calldata attestors
    )
        private
        view
        returns (IERC2535DiamondCutInternal.FacetCut[] memory cuts, bytes memory initCalldata)
    {
        IERC2535DiamondCutInternal.FacetCut[] memory
            coreCuts = ClusterCut.buildCoreCuts(attestFacet, messageFacet, networkFacet);
        IERC2535DiamondCutInternal.FacetCut[] memory attestorCuts =
            ClusterCut.buildAttestorCuts(attestors);

        cuts = new IERC2535DiamondCutInternal.FacetCut[](coreCuts.length + attestorCuts.length);
        for (uint256 i; i < coreCuts.length; ++i) {
            cuts[i] = coreCuts[i];
        }
        for (uint256 i; i < attestorCuts.length; ++i) {
            cuts[coreCuts.length + i] = attestorCuts[i];
        }

        initCalldata = abi.encodeCall(DiamondInitV2.init, (core, attestors));
    }
}
