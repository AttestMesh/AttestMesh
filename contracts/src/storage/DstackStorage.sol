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
        // Owner-seeded app_id (ClusterMember address) allowlist. Lets the KMS boot gate
        // (isAppAllowed) admit a freshly-deployed member that has NOT yet registered —
        // the operator pre-approves the predicted member address before the CVM boots,
        // breaking the cold-start deadlock (registration can't happen until the node
        // boots, but the node can't boot until the gate passes). Appended last to keep
        // the ERC-7201 layout of the existing fields stable.
        mapping(address appId => bool allowed) allowedAppIds;
    }

    function layout() internal pure returns (Layout storage l) {
        bytes32 slot = SLOT;
        assembly {
            l.slot := slot
        }
    }
}
