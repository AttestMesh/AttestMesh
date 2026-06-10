// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title INetwork — wireguard pubkey signalling (contracts spec §5.3).
interface INetwork {
    function publishWgKey(bytes32 wgPubKey) external;
    function wgPubKeyOf(bytes32 memberId) external view returns (bytes32);

    /// @notice Internal canonical writer, gated `msg.sender == address(this)`.
    ///         Called by DstackFacet during registration (contracts spec §6.3 step 9)
    ///         and by NetworkFacet.publishWgKey. Writes NetworkStorage and updates
    ///         the MemberStorage mirror via IAttest._setWgMirror.
    function _setWgPubKey(bytes32 memberId, bytes32 wgPubKey) external;

    event WgKeyPublished(bytes32 indexed memberId, bytes32 wgPubKey);
}
