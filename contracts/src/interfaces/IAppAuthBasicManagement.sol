// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IAppAuthBasicManagement — dstack allowlist management (mirrored from dstack).
/// @notice Mirrors dstack's interface exactly so phala-cli and the dstack KMS see
///         the standard ABI (contracts spec §6.1). DstackFacet implements these;
///         ClusterMember forwards them.
interface IAppAuthBasicManagement {
    function addComposeHash(bytes32 composeHash) external;
    function removeComposeHash(bytes32 composeHash) external;
    function addDevice(bytes32 deviceId) external;
    function removeDevice(bytes32 deviceId) external;
    function setAllowAnyDevice(bool allowAny) external;
    function setRequireTcbUpToDate(bool require_) external;

    function allowedComposeHashes(bytes32 composeHash) external view returns (bool);
    function allowedDeviceIds(bytes32 deviceId) external view returns (bool);
    function allowAnyDevice() external view returns (bool);
    function requireTcbUpToDate() external view returns (bool);
    function owner() external view returns (address);
    function version() external view returns (uint256);

    event ComposeHashAdded(bytes32 indexed composeHash);
    event ComposeHashRemoved(bytes32 composeHash);
    event DeviceAdded(bytes32 deviceId);
    event DeviceRemoved(bytes32 deviceId);
    event AllowAnyDeviceSet(bool allowAny);
    event RequireTcbUpToDateSet(bool requireUpToDate);
}
