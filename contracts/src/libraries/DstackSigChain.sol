// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { ECDSA } from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

import { IDstackFacet } from "../interfaces/IDstackFacet.sol";

/// @title DstackSigChain — on-chain verification of the dstack KMS signature chain.
/// @notice Ports the verification primitive from TeeSQL/dstackgres (`DstackSigChain`),
///         rebranded for AttestMesh but preserving dstack's *exact* preimages and
///         encodings, so a proof produced by a real dstack CVM verifies unchanged.
///         The chain is: KMS root -> app key -> derived key -> registration message.
/// @dev    Pure/view. Storage for the trusted KMS-root set lives in the caller; the
///         caller passes itself as the registry so this library stays storage-free.
///         secp256k1 point decompression uses the 0x05 modexp precompile.
library DstackSigChain {
    /// secp256k1 field prime p.
    uint256 internal constant P =
        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F;
    /// (p + 1) / 4 — the exponent for the modular square root, since p ≡ 3 (mod 4).
    /// p + 1 does not overflow uint256 (p < 2^256 - 1), so this constant expr is exact.
    uint256 internal constant SQRT_EXP = (P + 1) / 4;

    error InvalidPubKeyLength();
    error PointNotOnCurve();
    error InvalidSigChain();

    /// @notice Verify a dstack KMS sig-chain proof. Reverts on any failure.
    /// @param p the proof presented by a CVM sidecar.
    /// @param registry contract answering `allowedKmsRoots(address)` for the trusted
    ///        KMS-root set (the DstackFacet passes itself).
    /// @return codeId the verified codeId (`bytes20(app_id)` left-aligned) — the caller
    ///         binds this against the member contract.
    /// @return derivedKey the derived key's EOA — the registration signer / member owner.
    function verify(IDstackFacet.DstackProof memory p, IDstackFacet registry)
        internal
        view
        returns (bytes32 codeId, address derivedKey)
    {
        // codeId = bytes32(bytes20(app_id)): the address occupies the top 20 bytes and
        // the bottom 12 must be zero. bytes20(p.codeId) below takes the leftmost 20
        // bytes, and the KMS signature was computed over the raw 20-byte app_id.
        if ((uint256(p.codeId) << 160) != 0) revert InvalidSigChain();

        // Step 1: the app key signs "purpose:hex(derivedCompressedPubkey)" -> app EOA.
        address recoveredApp;
        {
            string memory derivedHex = bytesToHex(p.derivedCompressedPubkey);
            bytes32 appMsgHash = keccak256(abi.encodePacked(p.purpose, ":", derivedHex));
            recoveredApp = recover(appMsgHash, p.appSignature);
        }

        // Step 2: the KMS root signs "dstack-kms-issued:" || bytes20(app_id) || appPubkey.
        {
            bytes32 kmsMsgHash = keccak256(
                abi.encodePacked("dstack-kms-issued:", bytes20(p.codeId), p.appCompressedPubkey)
            );
            address kmsSigner = recover(kmsMsgHash, p.kmsSignature);
            if (!registry.allowedKmsRoots(kmsSigner)) revert InvalidSigChain();
        }

        derivedKey = compressedToAddress(p.derivedCompressedPubkey);

        // Step 3: the derived key signs the registration messageHash (EIP-191 wrapped).
        {
            bytes32 ethHash =
                keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", p.messageHash));
            if (recover(ethHash, p.messageSignature) != derivedKey) revert InvalidSigChain();
        }

        // Step 4: the app pubkey must match the recovered app signer.
        if (recoveredApp != compressedToAddress(p.appCompressedPubkey)) revert InvalidSigChain();

        return (p.codeId, derivedKey);
    }

    /// @notice Recover the signer address from a 65-byte secp256k1 signature over `digest`.
    function recover(bytes32 digest, bytes memory signature) internal pure returns (address) {
        return ECDSA.recover(digest, signature);
    }

    /// @notice Derive the Ethereum address for a secp256k1 public key.
    /// @param pubKey 33-byte compressed (0x02/0x03 prefix), 64-byte raw (x||y),
    ///        or 65-byte uncompressed (0x04 prefix) encoding.
    function compressedToAddress(bytes memory pubKey) internal view returns (address) {
        uint256 x;
        uint256 y;

        if (pubKey.length == 33) {
            uint8 prefix = uint8(pubKey[0]);
            assembly {
                x := mload(add(pubKey, 0x21)) // skip 32-byte length + 1 prefix byte
            }
            y = _decompressY(x, prefix);
        } else if (pubKey.length == 64) {
            assembly {
                x := mload(add(pubKey, 0x20))
                y := mload(add(pubKey, 0x40))
            }
        } else if (pubKey.length == 65) {
            // 0x04 || x || y
            assembly {
                x := mload(add(pubKey, 0x21))
                y := mload(add(pubKey, 0x41))
            }
        } else {
            revert InvalidPubKeyLength();
        }

        return address(uint160(uint256(keccak256(abi.encodePacked(x, y)))));
    }

    /// @notice Lowercase hex of `data` with no `0x` prefix. Used to reconstruct the
    ///         dstack app->derived preimage `"purpose:" || hex(derivedCompressedPubkey)`.
    function bytesToHex(bytes memory data) internal pure returns (string memory) {
        bytes memory alphabet = "0123456789abcdef";
        bytes memory str = new bytes(data.length * 2);
        for (uint256 i = 0; i < data.length; i++) {
            str[i * 2] = alphabet[uint8(data[i] >> 4)];
            str[i * 2 + 1] = alphabet[uint8(data[i] & 0x0f)];
        }
        return string(str);
    }

    /// @notice Recover y for a compressed point given x and the parity prefix.
    /// @param prefix 0x02 (y even) or 0x03 (y odd).
    function _decompressY(uint256 x, uint8 prefix) private view returns (uint256 y) {
        // y^2 = x^3 + 7 (mod p)
        uint256 rhs = addmod(mulmod(mulmod(x, x, P), x, P), 7, P);
        y = _modexp(rhs, SQRT_EXP, P);
        // On-curve sanity: y^2 must equal rhs.
        if (mulmod(y, y, P) != rhs) revert PointNotOnCurve();
        // Fix parity: prefix 0x02 => even, 0x03 => odd.
        uint256 wantOdd = uint256(prefix) & 1;
        if ((y & 1) != wantOdd) {
            y = P - y;
        }
    }

    /// @notice base^e mod modulus via the 0x05 modexp precompile (all 32-byte words).
    function _modexp(uint256 base, uint256 e, uint256 modulus)
        private
        view
        returns (uint256 result)
    {
        assembly {
            let p := mload(0x40)
            mstore(p, 0x20) // baseLen
            mstore(add(p, 0x20), 0x20) // expLen
            mstore(add(p, 0x40), 0x20) // modLen
            mstore(add(p, 0x60), base)
            mstore(add(p, 0x80), e)
            mstore(add(p, 0xa0), modulus)
            if iszero(staticcall(gas(), 0x05, p, 0xc0, p, 0x20)) { revert(0, 0) }
            result := mload(p)
            mstore(0x40, add(p, 0xc0))
        }
    }
}
