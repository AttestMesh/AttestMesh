// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";
import { IDstackFacet } from "../../src/interfaces/IDstackFacet.sol";
import { IOperatorFacet } from "../../src/interfaces/IOperatorFacet.sol";

/// @notice Cross-language ABI guard (audit AUDIT_1780519998 finding 2).
/// @dev The register selectors are keccak256 of their canonical signatures, which
///      expand the full proof tuples. Pinning the same literals here (solc), in the
///      sidecar (`abi.rs`, alloy `sol!`), and in the gas-webhook
///      (`selectors.spec.ts`, viem) means any field-order/type drift in one
///      encoder fails that language's test before it can diverge on-chain.
contract SelectorsTest is Test {
    function test_dstackRegisterSelectorIsPinned() public pure {
        assertEq(IDstackFacet.dstack_register.selector, bytes4(0x537d491c));
    }

    function test_operatorRegisterSelectorIsPinned() public pure {
        // operator_register((address,address,uint64,bytes),address,bytes32,bytes32)
        assertEq(IOperatorFacet.operator_register.selector, bytes4(0x57977c40));
    }
}
