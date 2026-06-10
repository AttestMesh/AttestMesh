// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title MessageStorage — per-channel envelope dedup (contracts spec §4.2).
/// @notice No message bytes are stored on chain; only a per-(recipient channel,
///         envelopeId) "seen" flag for idempotency. Messages live in events.
library MessageStorage {
    /// keccak256(abi.encode(uint256(keccak256("attestmesh.storage.Message")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 internal constant SLOT =
        0x01ed23594c696028727ead8f00e63e0673d543f2d3613a2c2fc01abf2ca2d100;

    struct Layout {
        // channelId (== recipient memberId) => envelopeId => seen
        mapping(bytes32 channelId => mapping(bytes32 envelopeId => bool seen)) envelopeNonces;
    }

    function layout() internal pure returns (Layout storage l) {
        bytes32 slot = SLOT;
        assembly {
            l.slot := slot
        }
    }
}
