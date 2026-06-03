// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IDstackFacet — dstack attestor facet registration surface (contracts spec §6.3).
interface IDstackFacet {
    /// @notice The dstack KMS signature-chain proof a CVM sidecar presents at
    ///         registration. Mirrors dstack's real KMS issuance chain (ported from
    ///         the verification primitive in TeeSQL/dstackgres `DstackSigChain`):
    ///         KMS root -> app key -> derived key -> registration message.
    /// @dev `codeId` is `bytes20(app_id)` left-aligned in a bytes32; in AttestMesh the
    ///      dstack `app_id` is the ClusterMember contract address. The compose hash,
    ///      device id, and TCB status are NOT part of the signed chain — they are the
    ///      KMS *boot-gate* policy (see `isAppAllowed`), enforced before the CVM boots.
    struct DstackProof {
        bytes32 codeId; // bytes20(app_id) left-aligned; app_id == ClusterMember address
        bytes32 messageHash; // the registration binding hash the derived key signed (pre-EIP-191)
        bytes messageSignature; // derived-key signature over the EIP-191 message of messageHash
        bytes appSignature; // app-key signature over "purpose:hex(derivedCompressedPubkey)"
        bytes kmsSignature; // KMS-root sig over "dstack-kms-issued:" || bytes20(codeId) || appCompressedPubkey
        bytes derivedCompressedPubkey; // 33-byte compressed SEC1 (the registration signer / member owner)
        bytes appCompressedPubkey; // 33-byte compressed SEC1 (the dstack app key)
        string purpose; // dstack key-derivation purpose label for the app->derived signature
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

    // Owner-seeded app_id allowlist (boot-gate cold-start; contracts spec §6.2).
    function addAllowedAppId(address appId) external;
    function removeAllowedAppId(address appId) external;
    function allowedAppIds(address appId) external view returns (bool);

    event DstackMemberRegistered(bytes32 indexed memberId, bytes32 codeId, address derivedKey);
    event KmsRootAdded(address indexed kmsRoot);
    event KmsRootRemoved(address indexed kmsRoot);
    event AppIdAllowed(address indexed appId);
    event AppIdDisallowed(address indexed appId);
}
