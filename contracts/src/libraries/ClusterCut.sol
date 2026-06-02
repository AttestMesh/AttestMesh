// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";

import { IAttest } from "../interfaces/IAttest.sol";
import { IMessage } from "../interfaces/IMessage.sol";
import { INetwork } from "../interfaces/INetwork.sol";
import { IDstackFacet } from "../interfaces/IDstackFacet.sol";
import { IAppAuth } from "../interfaces/IAppAuth.sol";
import { IAppAuthBasicManagement } from "../interfaces/IAppAuthBasicManagement.sol";

/// @title ClusterCut — builds the canonical v1 FacetCut array (contracts spec §10).
/// @notice Centralizes the diamond's selector set so the factory and the deploy
///         script can never drift. Two selectors are resolved against clashes:
///         - `wgPubKeyOf(bytes32)` is registered on NetworkFacet only (AttestFacet
///           also implements it as a mirror, but the diamond serves the canonical
///           NetworkStorage value).
///         - `owner()` is NOT registered from DstackFacet; the diamond's SafeOwnable
///           `owner()` (the solidstate owner) serves it — equal to the cluster owner
///           in the common single-Safe case.
library ClusterCut {
    function buildFacetCuts(
        address attestFacet,
        address messageFacet,
        address networkFacet,
        address dstackFacet
    ) internal pure returns (IERC2535DiamondCutInternal.FacetCut[] memory cuts) {
        cuts = new IERC2535DiamondCutInternal.FacetCut[](4);

        cuts[0] = IERC2535DiamondCutInternal.FacetCut({
            target: attestFacet,
            action: IERC2535DiamondCutInternal.FacetCutAction.ADD,
            selectors: _attestSelectors()
        });
        cuts[1] = IERC2535DiamondCutInternal.FacetCut({
            target: messageFacet,
            action: IERC2535DiamondCutInternal.FacetCutAction.ADD,
            selectors: _messageSelectors()
        });
        cuts[2] = IERC2535DiamondCutInternal.FacetCut({
            target: networkFacet,
            action: IERC2535DiamondCutInternal.FacetCutAction.ADD,
            selectors: _networkSelectors()
        });
        cuts[3] = IERC2535DiamondCutInternal.FacetCut({
            target: dstackFacet,
            action: IERC2535DiamondCutInternal.FacetCutAction.ADD,
            selectors: _dstackSelectors()
        });
    }

    function _attestSelectors() private pure returns (bytes4[] memory s) {
        s = new bytes4[](19);
        s[0] = IAttest.isClusterMember.selector;
        s[1] = IAttest.memberIdOf.selector;
        s[2] = IAttest.memberOf.selector;
        s[3] = IAttest.memberById.selector;
        s[4] = IAttest.xPubKeyOf.selector;
        s[5] = IAttest.listMembers.selector;
        s[6] = IAttest.memberCount.selector;
        s[7] = IAttest.clusterOwner.selector;
        s[8] = IAttest.pendingClusterOwner.selector;
        s[9] = IAttest.meshCidr.selector;
        s[10] = IAttest.meshIpOf.selector;
        s[11] = IAttest.setCskCommitment.selector;
        s[12] = IAttest.cskCommitment.selector;
        s[13] = IAttest._addMember.selector;
        s[14] = IAttest._setWgMirror.selector;
        s[15] = IAttest.transferClusterOwnership.selector;
        s[16] = IAttest.acceptClusterOwnership.selector;
        s[17] = IAttest.transferBothOwners.selector;
        s[18] = IAttest.acceptBothOwners.selector;
    }

    function _messageSelectors() private pure returns (bytes4[] memory s) {
        s = new bytes4[](1);
        s[0] = IMessage.send.selector;
    }

    function _networkSelectors() private pure returns (bytes4[] memory s) {
        s = new bytes4[](3);
        s[0] = INetwork.publishWgKey.selector;
        s[1] = INetwork._setWgPubKey.selector;
        s[2] = INetwork.wgPubKeyOf.selector;
    }

    function _dstackSelectors() private pure returns (bytes4[] memory s) {
        s = new bytes4[](16);
        s[0] = IAppAuthBasicManagement.addComposeHash.selector;
        s[1] = IAppAuthBasicManagement.removeComposeHash.selector;
        s[2] = IAppAuthBasicManagement.addDevice.selector;
        s[3] = IAppAuthBasicManagement.removeDevice.selector;
        s[4] = IAppAuthBasicManagement.setAllowAnyDevice.selector;
        s[5] = IAppAuthBasicManagement.setRequireTcbUpToDate.selector;
        s[6] = IAppAuthBasicManagement.allowedComposeHashes.selector;
        s[7] = IAppAuthBasicManagement.allowedDeviceIds.selector;
        s[8] = IAppAuthBasicManagement.allowAnyDevice.selector;
        s[9] = IAppAuthBasicManagement.requireTcbUpToDate.selector;
        s[10] = IAppAuthBasicManagement.version.selector;
        s[11] = IDstackFacet.addAllowedKmsRoot.selector;
        s[12] = IDstackFacet.removeAllowedKmsRoot.selector;
        s[13] = IDstackFacet.allowedKmsRoots.selector;
        s[14] = IDstackFacet.dstack_register.selector;
        s[15] = IAppAuth.isAppAllowed.selector;
        // DSTACK_ATTESTOR_ID() getter is intentionally not registered — the
        // constant is derivable off chain (keccak256("attestmesh.attestor.dstack")).
    }
}
