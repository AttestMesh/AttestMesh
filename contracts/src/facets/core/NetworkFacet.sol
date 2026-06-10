// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { INetwork } from "../../interfaces/INetwork.sol";
import { IAttest } from "../../interfaces/IAttest.sol";
import { NetworkStorage } from "../../storage/NetworkStorage.sol";
import { ClusterAccess } from "../../access/ClusterAccess.sol";

/// @title NetworkFacet — wireguard pubkey signalling (contracts spec §5.3).
/// @notice Owns the canonical NetworkStorage wg-pubkey registry and keeps the
///         denormalized MemberStorage mirror in sync via AttestFacet._setWgMirror.
///         Endpoint (IP:port) info is exchanged off-chain via MessageFacet, never here.
contract NetworkFacet is INetwork, ClusterAccess {
    /// @notice Member publishes / rotates its wireguard public key.
    function publishWgKey(bytes32 wgPubKey) external onlyClusterMember {
        _writeWg(_senderMemberId(), wgPubKey);
    }

    /// @notice Internal canonical writer used by DstackFacet at registration
    ///         (contracts spec §6.3 step 9). Gated to sibling facets.
    function _setWgPubKey(bytes32 memberId, bytes32 wgPubKey) external onlyInternal {
        _writeWg(memberId, wgPubKey);
    }

    function wgPubKeyOf(bytes32 memberId) external view returns (bytes32) {
        return NetworkStorage.layout().wgPubKeys[memberId];
    }

    function _writeWg(bytes32 memberId, bytes32 wgPubKey) private {
        NetworkStorage.layout().wgPubKeys[memberId] = wgPubKey;
        // Mirror lives in MemberStorage (AttestFacet's namespace) — update via its selector.
        IAttest(address(this))._setWgMirror(memberId, wgPubKey);
        emit WgKeyPublished(memberId, wgPubKey);
    }
}
