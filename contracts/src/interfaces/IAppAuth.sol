// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IAppAuth — dstack KMS boot-gate interface (mirrored from dstack).
/// @notice The dstack KMS calls `isAppAllowed` at CVM boot. AttestMesh keeps the
///         ABI identical so the dstack KMS and phala-cli interact with a cluster
///         the same way they interact with a stock dstack app contract.
interface IAppAuth {
    struct AppBootInfo {
        address appId;
        bytes32 composeHash;
        address instanceId;
        bytes32 deviceId;
        bytes32 mrAggregated;
        bytes32 mrSystem;
        bytes32 osImageHash;
        string tcbStatus;
        string[] advisories;
    }

    function isAppAllowed(AppBootInfo calldata bootInfo)
        external
        view
        returns (bool isAllowed, string memory reason);
}
