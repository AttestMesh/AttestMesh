// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Vm } from "forge-std/Vm.sol";
import { MessageHashUtils } from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import { IDstackFacet } from "../../src/interfaces/IDstackFacet.sol";
import { DstackSigChain } from "../../src/libraries/DstackSigChain.sol";

/// @notice In-test dstack KMS sig-chain producer (contracts spec §15 helpers).
///         Emits proofs in dstack's *real* on-chain format (the preimages ported
///         from TeeSQL/dstackgres), so DstackFacet.dstack_register verifies them the
///         same way it would a proof captured from a live CVM. Root = private key 1,
///         app key = private key 2 (canonical secp256k1 vectors); each member supplies
///         its own derived key.
contract MockKmsChain {
    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    uint256 internal constant ROOT_PRIV = 1;
    bytes internal constant ROOT_COMP =
        hex"0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798";
    uint256 internal constant APP_PRIV = 2;
    bytes internal constant APP_COMP =
        hex"02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5";

    string internal constant BIND_DOMAIN = "attestmesh.bind.v1";

    /// dstack key-derivation purpose label for the app->derived signature. The real
    /// label is supplied per-proof by the dstack runtime (dstackgres treats it as a
    /// proof field, not a constant); this placeholder must be confirmed against a
    /// captured proof once a live dstack node exists. The facet does not constrain it.
    string internal constant PURPOSE = "app-key";

    function rootAddress() external pure returns (address) {
        return 0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf; // vm.addr(1)
    }

    struct DerivedKey {
        uint256 priv;
        bytes compressed;
    }

    /// @notice Build a valid real-format DstackProof for `memberContract` joining
    ///         `cluster`. codeId = bytes20(memberContract): the dstack app_id is the
    ///         member contract address.
    function buildProof(
        DerivedKey memory derived,
        address cluster,
        address memberContract,
        bytes32 xPubKey,
        bytes32 wgPubKey
    ) public returns (IDstackFacet.DstackProof memory proof) {
        proof.codeId = bytes32(bytes20(memberContract));
        proof.derivedCompressedPubkey = derived.compressed;
        proof.appCompressedPubkey = APP_COMP;
        proof.purpose = PURPOSE;

        // App key signs "purpose:hex(derivedCompressedPubkey)" (raw keccak).
        bytes32 appMsgHash = keccak256(
            abi.encodePacked(PURPOSE, ":", DstackSigChain.bytesToHex(derived.compressed))
        );
        proof.appSignature = _sign(APP_PRIV, appMsgHash);

        // KMS root signs "dstack-kms-issued:" || bytes20(codeId) || appCompressedPubkey.
        bytes32 kmsMsgHash =
            keccak256(abi.encodePacked("dstack-kms-issued:", bytes20(proof.codeId), APP_COMP));
        proof.kmsSignature = _sign(ROOT_PRIV, kmsMsgHash);

        // Derived key signs the EIP-191 message of the registration binding hash.
        proof.messageHash =
            keccak256(abi.encode(BIND_DOMAIN, cluster, memberContract, xPubKey, wgPubKey));
        proof.messageSignature =
            _sign(derived.priv, MessageHashUtils.toEthSignedMessageHash(proof.messageHash));
    }

    function _sign(uint256 priv, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(priv, digest);
        return abi.encodePacked(r, s, v);
    }
}
