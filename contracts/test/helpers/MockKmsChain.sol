// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Vm } from "forge-std/Vm.sol";
import { MessageHashUtils } from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import { IDstackFacet } from "../../src/interfaces/IDstackFacet.sol";

/// @notice In-test dstack KMS sig-chain producer (contracts spec §15 helpers).
///         Root = private key 1, app key = private key 2 (canonical secp256k1
///         vectors). Each member supplies its own derived key.
contract MockKmsChain {
    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    uint256 internal constant ROOT_PRIV = 1;
    bytes internal constant ROOT_COMP =
        hex"0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798";
    uint256 internal constant APP_PRIV = 2;
    bytes internal constant APP_COMP =
        hex"02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5";

    string internal constant BIND_DOMAIN = "attestmesh.bind.v1";

    function rootAddress() external pure returns (address) {
        return 0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf; // vm.addr(1)
    }

    struct DerivedKey {
        uint256 priv;
        bytes compressed;
    }

    /// @notice Build a valid DstackProof for `memberContract` joining `cluster`.
    function buildProof(
        DerivedKey memory derived,
        bytes32 composeHash,
        bytes32 instanceId,
        bytes32 deviceId,
        string memory tcbStatus,
        address cluster,
        address memberContract,
        bytes32 xPubKey,
        bytes32 wgPubKey
    ) public returns (IDstackFacet.DstackProof memory proof) {
        proof.kmsRootPubKey = ROOT_COMP;
        proof.appKey = APP_COMP;
        proof.appComposeHash = composeHash;
        proof.derivedPubKey = derived.compressed;
        proof.derivedInstanceId = instanceId;
        proof.derivedDeviceId = deviceId;
        proof.tcbStatus = tcbStatus;
        proof.advisoryIds = new string[](0);

        // KMS root -> app key (raw keccak hash).
        bytes32 hApp = keccak256(abi.encode("dstack.app", APP_COMP, composeHash));
        proof.appKeySig = _sign(ROOT_PRIV, hApp);

        // App key -> derived key (raw keccak hash).
        bytes32 hDerived =
            keccak256(abi.encode("dstack.instance", derived.compressed, instanceId, deviceId));
        proof.derivedKeySig = _sign(APP_PRIV, hDerived);

        // Derived key signs the binding (EIP-191 prefixed).
        bytes32 bindHash = MessageHashUtils.toEthSignedMessageHash(
            keccak256(abi.encode(BIND_DOMAIN, cluster, memberContract, xPubKey, wgPubKey))
        );
        proof.bindingSig = _sign(derived.priv, bindHash);
    }

    function _sign(uint256 priv, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(priv, digest);
        return abi.encodePacked(r, s, v);
    }
}
