// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { SolidStateDiamond } from "@solidstate/contracts/proxy/diamond/SolidStateDiamond.sol";

/// @title ClusterDiamond — the ERC-2535 proxy for an AttestMesh cluster (contracts spec §7).
/// @notice The constructor seeds the facet set and atomically delegatecalls the
///         DiamondInit contract so the diamond is never reachable in a
///         "deployed but unconfigured" state (master spec §13 item 8).
contract ClusterDiamond is SolidStateDiamond {
    constructor(FacetCut[] memory facetCuts, address init, bytes memory initCalldata)
        SolidStateDiamond()
    {
        _diamondCut(facetCuts, init, initCalldata);
    }
}
