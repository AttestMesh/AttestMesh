// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IIndexerRegistry — per-chain Indexer discovery (contracts spec §11).
interface IIndexerRegistry {
    struct IndexerRecord {
        string endpoint; // "https://indexer.teesql.io:443" or similar
        bytes32 codeId; // attested Indexer code identifier
        bytes32 pubKey; // ed25519 pubkey the Indexer signs envelopes with
        uint64 updatedAt;
    }

    function setIndexer(IndexerRecord calldata rec) external;
    function current()
        external
        view
        returns (string memory endpoint, bytes32 codeId, bytes32 pubKey, uint64 updatedAt);

    event IndexerUpdated(IndexerRecord rec);
}
