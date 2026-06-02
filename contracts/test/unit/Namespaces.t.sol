// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";
import { MemberStorage } from "../../src/storage/MemberStorage.sol";
import { MessageStorage } from "../../src/storage/MessageStorage.sol";
import { NetworkStorage } from "../../src/storage/NetworkStorage.sol";
import { DstackStorage } from "../../src/storage/DstackStorage.sol";
import { ClusterMemberStorage } from "../../src/storage/ClusterMemberStorage.sol";

/// @notice Asserts each ERC-7201 slot constant matches the canonical formula so a
///         drift between the source string and the hard-coded constant fails CI.
contract NamespacesTest is Test {
    function test_erc7201Slots() public pure {
        assertEq(MemberStorage.SLOT, _erc7201("attestmesh.storage.Member"));
        assertEq(MessageStorage.SLOT, _erc7201("attestmesh.storage.Message"));
        assertEq(NetworkStorage.SLOT, _erc7201("attestmesh.storage.Network"));
        assertEq(DstackStorage.SLOT, _erc7201("attestmesh.storage.Dstack"));
        assertEq(ClusterMemberStorage.SLOT, _erc7201("attestmesh.storage.ClusterMember"));
    }

    function _erc7201(string memory id) internal pure returns (bytes32) {
        return keccak256(abi.encode(uint256(keccak256(bytes(id))) - 1)) & ~bytes32(uint256(0xff));
    }
}
