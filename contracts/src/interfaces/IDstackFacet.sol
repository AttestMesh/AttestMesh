// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IDstackFacet — dstack attestor facet registration surface (contracts spec §6.3).
interface IDstackFacet {
    /// @notice dstack KMS sig-chain proof + binding signature (contracts spec §6.3).
    struct DstackProof {
        // KMS sig chain
        bytes kmsRootPubKey; // compressed (33B) or uncompressed (64/65B) secp256k1
        bytes appKey; // compressed/uncompressed secp256k1 derived for compose-hash X
        bytes appKeySig; // KMS root signature over keccak256("dstack.app", appKey, appComposeHash)
        bytes32 appComposeHash; // compose hash the KMS bound to
        bytes derivedPubKey; // the one-shot binding signer pubkey
        bytes derivedKeySig; // app-key signature over keccak256("dstack.instance", derivedPubKey, instanceId, deviceId)
        bytes32 derivedInstanceId; // instance id the app key bound to
        bytes32 derivedDeviceId; // device id the app key bound to
        string tcbStatus;
        string[] advisoryIds;
        // Binding signature from the derived key over the EIP-191-prefixed registration message
        bytes bindingSig;
    }

    function dstack_register(
        DstackProof calldata proof,
        address memberContract,
        bytes32 xPubKey,
        bytes32 wgPubKey
    ) external returns (bytes32 memberId);

    // AttestMesh-specific KMS root allowlist admin (contracts spec §6.1).
    function addAllowedKmsRoot(address kmsRoot) external;
    function removeAllowedKmsRoot(address kmsRoot) external;
    function allowedKmsRoots(address kmsRoot) external view returns (bool);

    event DstackMemberRegistered(bytes32 indexed memberId, bytes32 composeHash, bytes32 deviceId);
    event KmsRootAdded(address indexed kmsRoot);
    event KmsRootRemoved(address indexed kmsRoot);
}
