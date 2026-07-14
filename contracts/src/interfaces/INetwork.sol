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

    /// @notice Member publishes / rotates its Ed25519 heartbeat verification key.
    ///         Public key — on-chain publication leaks nothing heartbeat signatures
    ///         don't already. Lets peers learn it via a chain read instead of a
    ///         sponsored PeerEndpoint envelope (ed25519-onchain-key spec).
    function publishEd25519Key(bytes32 ed25519Key) external;
    function ed25519KeyOf(bytes32 memberId) external view returns (bytes32);

    event WgKeyPublished(bytes32 indexed memberId, bytes32 wgPubKey);
    event Ed25519KeyPublished(bytes32 indexed memberId, bytes32 ed25519Key);
}
