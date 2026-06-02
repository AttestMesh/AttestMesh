// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title MemberStorage — canonical member registry layout (contracts spec §4.1).
/// @notice Owned by AttestFacet; attestor facets write into it via the internal
///         `_addMember` selector. No other facet touches this namespace.
library MemberStorage {
    /// keccak256(abi.encode(uint256(keccak256("attestmesh.storage.Member")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 internal constant SLOT =
        0xb467e8594a304c917a13600fb1a45cab0b4afdd28a6631433cce9a01f8451200;

    struct MemberRecord {
        bytes32 attestorId; // keccak256("attestmesh.attestor.dstack") etc.
        address memberContract; // ClusterMember address (also the EIP-4337 sender per §9)
        bytes32 xPubKey; // x25519 public key for sealed-box
        bytes32 wgPubKey; // wireguard public key (mirror; canonical source is NetworkStorage)
        uint64 registeredAt; // block.timestamp
    }

    struct Layout {
        mapping(bytes32 memberId => MemberRecord) members;
        mapping(address memberAddr => bytes32 memberId) memberIdOf;
        bytes32[] memberIds; // enumeration
        // Cluster-wide config (written once by DiamondInit, updated by AttestFacet transfer selectors):
        address clusterOwner;
        address pendingClusterOwner;
        // Per-cluster wireguard mesh CIDR (DiamondInit-seeded; immutable thereafter in v1):
        uint32 meshCidrIp; // packed network address, big-endian (0x0a0d0000 for 10.13.0.0)
        uint8 meshCidrPrefix; // e.g. 16 for /16
        bytes32 cskCommitment; // keccak256(CSK); set once by the originator (master §8.1)
        // Canonical per-chain ClusterMemberFactory — read by DstackFacet for the
        // on-chain `isOurMember` provenance check (master spec §9; contracts §6.3 step 1).
        address memberFactory;
    }

    function layout() internal pure returns (Layout storage l) {
        bytes32 slot = SLOT;
        assembly {
            l.slot := slot
        }
    }
}
