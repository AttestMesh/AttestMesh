// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title NetworkStorage — canonical wireguard pubkey registry (contracts spec §4.3).
/// @notice Owned by NetworkFacet. The mirror in MemberStorage.MemberRecord.wgPubKey
///         is denormalized for one-shot reads; this is the single source of truth.
library NetworkStorage {
    /// keccak256(abi.encode(uint256(keccak256("attestmesh.storage.Network")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 internal constant SLOT =
        0x474b09740077ffacfbf2d439dfedf08d163ee733bb577268273bf24437752a00;

    struct Layout {
        mapping(bytes32 memberId => bytes32 wgPubKey) wgPubKeys;
    }

    function layout() internal pure returns (Layout storage l) {
        bytes32 slot = SLOT;
        assembly {
            l.slot := slot
        }
    }
}
