// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { DstackStorage } from "./storage/DstackStorage.sol";
import { MemberStorage } from "./storage/MemberStorage.sol";

/// @title DiamondInit — one-shot atomic cluster initializer (contracts spec §8).
/// @notice delegatecall'd from ClusterDiamond's constructor; runs in the diamond's
///         storage context and seeds every facet's namespace in one transaction.
contract DiamondInit {
    struct InitArgs {
        address clusterOwner; // Safe address — written into the cluster-owner slot
        address kmsRootSigner; // initial allowed KMS root (added to DstackStorage)
        bytes32[] initialComposeHashes; // seeded into DstackStorage
        bytes32[] initialDeviceIds; // seeded into DstackStorage
        bool allowAnyDevice;
        bool requireTcbUpToDate;
        uint32 meshCidrIp; // network address of the cluster's wireguard CIDR
        uint8 meshCidrPrefix; // prefix length (e.g. 16 for /16)
        address memberFactory; // canonical per-chain ClusterMemberFactory (provenance check)
    }

    function init(InitArgs calldata args) external {
        DstackStorage.Layout storage d = DstackStorage.layout();
        d.allowedKmsRoots[args.kmsRootSigner] = true;
        for (uint256 i; i < args.initialComposeHashes.length; ++i) {
            d.allowedComposeHashes[args.initialComposeHashes[i]] = true;
        }
        for (uint256 i; i < args.initialDeviceIds.length; ++i) {
            d.allowedDeviceIds[args.initialDeviceIds[i]] = true;
        }
        d.allowAnyDevice = args.allowAnyDevice;
        d.requireTcbUpToDate = args.requireTcbUpToDate;

        MemberStorage.Layout storage m = MemberStorage.layout();
        m.clusterOwner = args.clusterOwner;
        m.meshCidrIp = args.meshCidrIp;
        m.meshCidrPrefix = args.meshCidrPrefix;
        m.memberFactory = args.memberFactory;
    }
}
