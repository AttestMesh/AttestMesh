// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";
import { DstackSigChain } from "../../src/libraries/DstackSigChain.sol";

/// Harness so we can call the internal `view` library function externally.
contract DstackSigChainHarness {
    function addrOf(bytes memory pk) external view returns (address) {
        return DstackSigChain.compressedToAddress(pk);
    }

    function recover(bytes32 digest, bytes memory sig) external pure returns (address) {
        return DstackSigChain.recover(digest, sig);
    }
}

/// @notice Vectors generated off-chain with @noble/curves for private keys 1..6.
///         They are the canonical Ethereum addresses for those keys, so they also
///         cross-check the on-chain secp256k1 point decompression.
contract DstackSigChainTest is Test {
    DstackSigChainHarness internal h;

    function setUp() public {
        h = new DstackSigChainHarness();
    }

    function test_compressedToAddress_matchesKnownVectors() public view {
        _check(
            hex"0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
            0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf
        );
        _check(
            hex"02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5",
            0x2B5AD5c4795c026514f8317c7a215E218DcCD6cF
        );
        _check(
            hex"02f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9",
            0x6813Eb9362372EEF6200f3b1dbC3f819671cBA69
        );
        _check(
            hex"02e493dbf1c10d80f3581e4904930b1404cc6c13900ee0758474fa94abe8c4cd13",
            0x1efF47bc3a10a45D4B230B5d10E37751FE6AA718
        );
        _check(
            hex"022f8bde4d1a07209355b4a7250a5c5128e88b84bddc619ab7cba8d569b240efe4",
            0xe1AB8145F7E55DC933d51a18c793F901A3A0b276
        );
        // priv 6 has an odd y (0x03 prefix) — exercises the parity flip.
        _check(
            hex"03fff97bd5755eeea420453a14355235d382f6472f8568a18b2f057a1460297556",
            0xE57bFE9F44b819898F47BF37E5AF72a0783e1141
        );
    }

    function test_uncompressedAlsoWorks() public view {
        // 65-byte 0x04-prefixed encoding for priv 1.
        bytes memory uncompressed =
            hex"0479be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8";
        assertEq(h.addrOf(uncompressed), 0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf);
    }

    function test_recover_roundTrip() public {
        uint256 pk = 0xA11CE;
        bytes32 digest = keccak256("hello attestmesh");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        bytes memory sig = abi.encodePacked(r, s, v);
        assertEq(h.recover(digest, sig), vm.addr(pk));
    }

    function _check(bytes memory compressed, address expected) internal view {
        assertEq(h.addrOf(compressed), expected, "compressed pubkey -> address mismatch");
    }
}
