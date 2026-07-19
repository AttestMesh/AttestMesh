// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";

import { IERC2535DiamondCut } from "@solidstate/contracts/interfaces/IERC2535DiamondCut.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";

import { ClusterAccess } from "../src/access/ClusterAccess.sol";
import { NetworkStorage } from "../src/storage/NetworkStorage.sol";
import { MemberStorage } from "../src/storage/MemberStorage.sol";

/// @dev Ephemeral test-cluster facet used when the external EIP-4337 bundler is
/// unavailable. The script removes its selector immediately after seeding the
/// two public heartbeat verification keys; no privileged test surface remains.
contract TestHeartbeatKeySeeder is ClusterAccess {
    function seedHeartbeatKey(bytes32 memberId, bytes32 ed25519Key) external onlyClusterOwner {
        NetworkStorage.layout().ed25519Keys[memberId] = ed25519Key;
    }

    function seedCskCommitment(bytes32 commitment) external onlyClusterOwner {
        MemberStorage.layout().cskCommitment = commitment;
    }
}

/// Env: PRIVATE_KEY, CLUSTER, MEMBER_ID_A, ED25519_KEY_A, MEMBER_ID_B,
/// ED25519_KEY_B, CSK_COMMITMENT. Intended only for isolated test clusters.
contract SeedTestHeartbeatKeys is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address cluster = vm.envAddress("CLUSTER");
        bytes32 memberA = vm.envBytes32("MEMBER_ID_A");
        bytes32 keyA = vm.envBytes32("ED25519_KEY_A");
        bytes32 memberB = vm.envBytes32("MEMBER_ID_B");
        bytes32 keyB = vm.envBytes32("ED25519_KEY_B");
        bytes32 cskCommitment = vm.envBytes32("CSK_COMMITMENT");

        vm.startBroadcast(pk);
        TestHeartbeatKeySeeder seeder = new TestHeartbeatKeySeeder();

        bytes4[] memory selectors = new bytes4[](2);
        selectors[0] = TestHeartbeatKeySeeder.seedHeartbeatKey.selector;
        selectors[1] = TestHeartbeatKeySeeder.seedCskCommitment.selector;
        IERC2535DiamondCutInternal.FacetCut[] memory cut =
            new IERC2535DiamondCutInternal.FacetCut[](1);
        cut[0] = IERC2535DiamondCutInternal.FacetCut({
            target: address(seeder),
            action: IERC2535DiamondCutInternal.FacetCutAction.ADD,
            selectors: selectors
        });
        IERC2535DiamondCut(cluster).diamondCut(cut, address(0), "");

        TestHeartbeatKeySeeder(cluster).seedHeartbeatKey(memberA, keyA);
        TestHeartbeatKeySeeder(cluster).seedHeartbeatKey(memberB, keyB);
        TestHeartbeatKeySeeder(cluster).seedCskCommitment(cskCommitment);

        cut[0] = IERC2535DiamondCutInternal.FacetCut({
            target: address(0),
            action: IERC2535DiamondCutInternal.FacetCutAction.REMOVE,
            selectors: selectors
        });
        IERC2535DiamondCut(cluster).diamondCut(cut, address(0), "");
        vm.stopBroadcast();

        console2.log("Seeded test heartbeat keys and CSK commitment; ephemeral selectors removed");
    }
}
