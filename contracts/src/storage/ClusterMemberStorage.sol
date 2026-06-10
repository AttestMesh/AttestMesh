// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title ClusterMemberStorage — per-CVM member contract layout (contracts spec §9.1.1).
/// @notice ERC-7201 namespace for the ClusterMember (dstack proxy + EIP-4337 wallet).
///         EIP-4337 v0.7 nonces are tracked in the EntryPoint, not here.
library ClusterMemberStorage {
    /// keccak256(abi.encode(uint256(keccak256("attestmesh.storage.ClusterMember")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 internal constant SLOT =
        0xeaff3df3ee5169d005a03d5a55e3db27753713bb429f09c0a0f5b446a482f300;

    struct Layout {
        address cluster; // the ClusterDiamond this member belongs to
        address owner; // dstack-derived secp256k1 address; address(0) until first dstack_register
    }

    function layout() internal pure returns (Layout storage l) {
        bytes32 slot = SLOT;
        assembly {
            l.slot := slot
        }
    }
}
