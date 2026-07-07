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

    /// @notice Member publishes / rotates its Ed25519 heartbeat key. Unconditional
    ///         overwrite (rotation allowed). No MemberStorage mirror — peers read it
    ///         directly, so mesh bring-up needs no PeerEndpoint envelope
    ///         (ed25519-onchain-key spec).
    function publishEd25519Key(bytes32 ed25519Key) external onlyClusterMember {
        bytes32 memberId = _senderMemberId();
        NetworkStorage.layout().ed25519Keys[memberId] = ed25519Key;
        emit Ed25519KeyPublished(memberId, ed25519Key);
    }

    function ed25519KeyOf(bytes32 memberId) external view returns (bytes32) {
        return NetworkStorage.layout().ed25519Keys[memberId];
    }
}
