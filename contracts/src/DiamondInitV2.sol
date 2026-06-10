// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { MemberStorage } from "./storage/MemberStorage.sol";
import { IAttestorFacet, AttestorConfig } from "./interfaces/IAttestorFacet.sol";

/// @title DiamondInitV2 — one-shot atomic cluster initializer, multi-attestor
///        generation (contracts spec §8 "per-attestor-facet init blobs" extension).
/// @notice delegatecall'd from ClusterDiamond's constructor; runs in the diamond's
///         storage context. Seeds the attestation-method-agnostic core namespace
///         first, then delegatecalls each attestor facet's `initAttestor(initData)`
///         so every facet seeds its own ERC-7201 namespace — the core init knows
///         nothing about any method's fields (the dstack fields that lived in
///         DiamondInit v1's InitArgs moved into DstackFacet's blob). A NEW
///         deployment alongside v1, not an upgrade of the live init.
contract DiamondInitV2 {
    /// @notice Method-agnostic cluster config: DiamondInit v1's InitArgs minus the
    ///         dstack-shaped fields.
    struct CoreInitArgs {
        address clusterOwner; // Safe address — written into the cluster-owner slot
        uint32 meshCidrIp; // network address of the cluster's wireguard CIDR
        uint8 meshCidrPrefix; // prefix length (e.g. 16 for /16)
        address memberFactory; // canonical per-chain ClusterMemberFactory (provenance check)
    }

    function init(CoreInitArgs calldata core, AttestorConfig[] calldata attestors) external {
        // Core first: attestor initAttestor guards on clusterOwner being seeded
        // (the NotDiamondContext sentinel), so ordering here is load-bearing.
        MemberStorage.Layout storage m = MemberStorage.layout();
        m.clusterOwner = core.clusterOwner;
        m.meshCidrIp = core.meshCidrIp;
        m.meshCidrPrefix = core.meshCidrPrefix;
        m.memberFactory = core.memberFactory;

        for (uint256 i; i < attestors.length; ++i) {
            (bool ok, bytes memory ret) = attestors[i].facet
                .delegatecall(abi.encodeCall(IAttestorFacet.initAttestor, (attestors[i].initData)));
            if (!ok) {
                assembly {
                    revert(add(ret, 0x20), mload(ret))
                }
            }
        }
    }
}
