// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";
import { stdJson } from "forge-std/StdJson.sol";

import { ClusterDiamondFactoryV2 } from "../src/factory/ClusterDiamondFactoryV2.sol";
import { DiamondInitV2 } from "../src/DiamondInitV2.sol";
import { DstackFacet } from "../src/facets/attestor/DstackFacet.sol";
import { AttestorConfig } from "../src/interfaces/IAttestorFacet.sol";

/// @notice Per-cluster deploy (contracts spec §12.2, multi-attestor generation).
///         Reads a JSON config and calls the v2 factory to atomically deploy a
///         ClusterDiamond + core cuts + per-attestor cuts + DiamondInitV2 (which
///         delegatecalls each facet's initAttestor blob). The v1 factory path is
///         deprecated for new deploys.
///
/// Config JSON: top-level core fields plus an optional section per attestor method —
/// a cluster picks its attestation policy by picking its facets:
///   {
///     "clusterOwner": "0x..", "meshCidrIp": 168624128, "meshCidrPrefix": 16,
///     "salt": "0x..",
///     "dstack":   { "kmsRootSigner": "0x..", "initialComposeHashes": ["0x.."],
///                   "initialDeviceIds": ["0x.."], "allowAnyDevice": false,
///                   "requireTcbUpToDate": true },
///     "operator": { "signers": ["0x.."] }
///   }
///
/// TRUST NOTE: including the "operator" section installs OperatorFacet — members it
/// admits are vouched for by an allowlisted operator key, NOT hardware attestation.
///
/// Env:
///   PRIVATE_KEY      — broadcaster
///   CLUSTER_FACTORY  — ClusterDiamondFactoryV2 address (from the infra receipt)
///   MEMBER_FACTORY   — ClusterMemberFactory address (from the infra receipt)
///   DSTACK_FACET     — DstackFacet impl (required iff config has "dstack")
///   OPERATOR_FACET   — OperatorFacet impl (required iff config has "operator")
///   CLUSTER_CONFIG   — path to the cluster JSON config
contract DeployCluster is Script {
    using stdJson for string;

    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        ClusterDiamondFactoryV2 factory = ClusterDiamondFactoryV2(vm.envAddress("CLUSTER_FACTORY"));
        address memberFactory = vm.envAddress("MEMBER_FACTORY");

        string memory cfg = vm.readFile(vm.envString("CLUSTER_CONFIG"));

        DiamondInitV2.CoreInitArgs memory core = DiamondInitV2.CoreInitArgs({
            clusterOwner: cfg.readAddress(".clusterOwner"),
            meshCidrIp: uint32(cfg.readUint(".meshCidrIp")),
            meshCidrPrefix: uint8(cfg.readUint(".meshCidrPrefix")),
            memberFactory: memberFactory
        });
        bytes32 salt = cfg.readBytes32(".salt");

        AttestorConfig[] memory attestors = _attestorConfigs(cfg);
        require(attestors.length > 0, "config selects no attestor method");

        vm.startBroadcast(pk);
        address cluster = factory.deployCluster(core, attestors, salt);
        vm.stopBroadcast();

        console2.log("Cluster deployed:", cluster);
        console2.log("  clusterOwner (must acceptOwnership):", core.clusterOwner);
    }

    function _attestorConfigs(string memory cfg)
        internal
        returns (AttestorConfig[] memory attestors)
    {
        bool hasDstack = vm.keyExistsJson(cfg, ".dstack");
        bool hasOperator = vm.keyExistsJson(cfg, ".operator");

        attestors = new AttestorConfig[]((hasDstack ? 1 : 0) + (hasOperator ? 1 : 0));
        uint256 n;
        if (hasDstack) {
            attestors[n++] = AttestorConfig({
                facet: vm.envAddress("DSTACK_FACET"),
                initData: abi.encode(
                    DstackFacet.DstackInitArgs({
                        kmsRootSigner: cfg.readAddress(".dstack.kmsRootSigner"),
                        initialComposeHashes: cfg.readBytes32Array(".dstack.initialComposeHashes"),
                        initialDeviceIds: cfg.readBytes32Array(".dstack.initialDeviceIds"),
                        allowAnyDevice: cfg.readBool(".dstack.allowAnyDevice"),
                        requireTcbUpToDate: cfg.readBool(".dstack.requireTcbUpToDate")
                    })
                )
            });
        }
        if (hasOperator) {
            console2.log(
                "TRUST NOTE: operator method enabled - members admitted by it are"
                " vouched for by an operator key, not hardware attestation."
            );
            attestors[n++] = AttestorConfig({
                facet: vm.envAddress("OPERATOR_FACET"),
                initData: abi.encode(cfg.readAddressArray(".operator.signers"))
            });
        }
    }
}
