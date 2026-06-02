// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { IIndexerRegistry } from "../interfaces/IIndexerRegistry.sol";

/// @title IndexerRegistry — per-chain Indexer discovery (contracts spec §11).
/// @notice The node sidecar reads `current()` at startup to find the Indexer
///         endpoint + signing pubkey. One instance per chain, owned by the org Safe.
contract IndexerRegistry is IIndexerRegistry {
    address public immutable owner; // AttestMesh org Safe
    IndexerRecord public current;

    error NotOwner();

    constructor(address owner_) {
        owner = owner_;
    }

    function setIndexer(IndexerRecord calldata rec) external {
        if (msg.sender != owner) revert NotOwner();
        current = IndexerRecord({
            endpoint: rec.endpoint,
            codeId: rec.codeId,
            pubKey: rec.pubKey,
            updatedAt: uint64(block.timestamp)
        });
        emit IndexerUpdated(current);
    }
}
