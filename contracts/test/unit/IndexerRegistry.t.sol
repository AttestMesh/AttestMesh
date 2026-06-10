// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";

import { IndexerRegistry } from "../../src/registry/IndexerRegistry.sol";
import { IIndexerRegistry } from "../../src/interfaces/IIndexerRegistry.sol";

/// @notice IndexerRegistry is tiny but live-load-bearing: every sidecar discovers
///         the Indexer (endpoint + signing pubkey) from `current()` and verifies
///         every push against the registered key.
contract IndexerRegistryTest is Test {
    IndexerRegistry internal registry;
    address internal owner = address(0xA11CE);

    function setUp() public {
        registry = new IndexerRegistry(owner);
    }

    function _rec(string memory endpoint)
        internal
        pure
        returns (IIndexerRegistry.IndexerRecord memory)
    {
        return IIndexerRegistry.IndexerRecord({
            endpoint: endpoint,
            codeId: keccak256("code"),
            pubKey: keccak256("pubkey"),
            updatedAt: 0 // caller-supplied value is ignored; contract stamps block.timestamp
        });
    }

    function test_startsEmpty() public view {
        (string memory endpoint, bytes32 codeId, bytes32 pubKey, uint64 updatedAt) =
            registry.current();
        assertEq(bytes(endpoint).length, 0);
        assertEq(codeId, bytes32(0));
        assertEq(pubKey, bytes32(0));
        assertEq(updatedAt, 0);
    }

    function test_ownerSetsRecordAndTimestampIsStamped() public {
        vm.warp(1_781_000_000);
        vm.prank(owner);
        registry.setIndexer(_rec("https://indexer.example:443"));

        (string memory endpoint, bytes32 codeId, bytes32 pubKey, uint64 updatedAt) =
            registry.current();
        assertEq(endpoint, "https://indexer.example:443");
        assertEq(codeId, keccak256("code"));
        assertEq(pubKey, keccak256("pubkey"));
        assertEq(updatedAt, 1_781_000_000, "updatedAt is block.timestamp, not caller input");
    }

    function test_setEmitsEvent() public {
        vm.warp(42);
        IIndexerRegistry.IndexerRecord memory expected = _rec("ep");
        expected.updatedAt = 42;
        vm.prank(owner);
        vm.expectEmit(address(registry));
        emit IIndexerRegistry.IndexerUpdated(expected);
        registry.setIndexer(_rec("ep"));
    }

    function test_rotationOverwrites() public {
        vm.prank(owner);
        registry.setIndexer(_rec("old"));
        vm.prank(owner);
        registry.setIndexer(_rec("new"));
        (string memory endpoint,,,) = registry.current();
        assertEq(endpoint, "new");
    }

    function test_nonOwnerReverts() public {
        vm.prank(address(0xBEEF));
        vm.expectRevert(IndexerRegistry.NotOwner.selector);
        registry.setIndexer(_rec("ep"));
    }
}
