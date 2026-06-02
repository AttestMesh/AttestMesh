// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { ECDSA } from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

/// @title DstackSigChain — secp256k1 primitives for the dstack KMS sig chain.
/// @notice Provides ecrecover-based signature recovery plus secp256k1 public-key
///         -> address derivation (incl. point decompression) so the dstack KMS
///         chain can be verified entirely on chain (contracts spec §6.3).
library DstackSigChain {
    /// secp256k1 field prime p.
    uint256 internal constant P =
        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F;
    /// (p + 1) / 4 — the exponent for the modular square root, since p ≡ 3 (mod 4).
    /// p + 1 does not overflow uint256 (p < 2^256 - 1), so this constant expr is exact.
    uint256 internal constant SQRT_EXP = (P + 1) / 4;

    error InvalidPubKeyLength();
    error PointNotOnCurve();

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
