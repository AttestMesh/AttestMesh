// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title DstackStorage — dstack attestor allowlists (contracts spec §4.4).
/// @notice Owned by DstackFacet. Mirrors dstackgres's KmsDstackStorage + the
///         IAppAuthBasicManagement set.
library DstackStorage {
    /// keccak256(abi.encode(uint256(keccak256("attestmesh.storage.Dstack")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 internal constant SLOT =
        0x3a6ca0bce3f2eabeff789f3e634310c8ac9ff254a0d573f6389711dd0d3b9600;

    struct Layout {
        mapping(bytes32 composeHash => bool allowed) allowedComposeHashes;
        mapping(bytes32 deviceId => bool allowed) allowedDeviceIds;
        mapping(address kmsRoot => bool allowed) allowedKmsRoots;
        bool allowAnyDevice;
        bool requireTcbUpToDate;
    }

    function layout() internal pure returns (Layout storage l) {
        bytes32 slot = SLOT;
        assembly {
            l.slot := slot
        }
    }
}
