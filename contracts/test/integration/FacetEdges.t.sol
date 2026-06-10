// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";

import { AttestFacet } from "../../src/facets/core/AttestFacet.sol";
import { MessageFacet } from "../../src/facets/core/MessageFacet.sol";
import { NetworkFacet } from "../../src/facets/core/NetworkFacet.sol";
import { DstackFacet } from "../../src/facets/attestor/DstackFacet.sol";
import { DiamondInit } from "../../src/DiamondInit.sol";
import { ClusterMember } from "../../src/members/ClusterMember.sol";
import { ClusterMemberFactory } from "../../src/members/ClusterMemberFactory.sol";
import { ClusterDiamondFactory } from "../../src/factory/ClusterDiamondFactory.sol";

import { IAttest } from "../../src/interfaces/IAttest.sol";
import { IMessage } from "../../src/interfaces/IMessage.sol";
import { IAppAuth } from "../../src/interfaces/IAppAuth.sol";
import { IAppAuthBasicManagement } from "../../src/interfaces/IAppAuthBasicManagement.sol";
import { IDstackFacet } from "../../src/interfaces/IDstackFacet.sol";
import { MemberStorage } from "../../src/storage/MemberStorage.sol";

import { MockKmsChain } from "../helpers/MockKmsChain.sol";
import {
    NotOriginator,
    CskCommitmentAlreadySet,
    NotClusterOwner,
    RecipientNotMember,
    NotInternalCall
} from "../../src/errors/Errors.sol";

/// @notice Edge branches of the live facets that the bring-up scenarios skip:
///         CSK-commitment gating, cluster-ownership transfer flows, the boot
///         gate's full decision ladder, allowlist management round-trips, and
///         the registry getters on registered + unknown members.
contract FacetEdgesTest is Test {
    ClusterMemberFactory internal memberFactory;
    ClusterDiamondFactory internal clusterFactory;
    MockKmsChain internal kms;

    address internal cluster;
    address internal orgSafe = address(0xA11CE);

    bytes32 internal constant COMPOSE = keccak256("attestmesh.compose.v1");
    bytes32 internal constant DEVICE = keccak256("attestmesh.device.1");

    bytes internal constant COMP3 =
        hex"02f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9";
    bytes internal constant COMP4 =
        hex"02e493dbf1c10d80f3581e4904930b1404cc6c13900ee0758474fa94abe8c4cd13";

    function setUp() public {
        kms = new MockKmsChain();
        address attestFacet = address(new AttestFacet());
        address messageFacet = address(new MessageFacet());
        address networkFacet = address(new NetworkFacet());
        address dstackFacet = address(new DstackFacet());
        address diamondInit = address(new DiamondInit());
        address memberImpl = address(new ClusterMember());

        memberFactory = new ClusterMemberFactory(memberImpl, orgSafe);
        clusterFactory = new ClusterDiamondFactory(
            orgSafe, diamondInit, attestFacet, messageFacet, networkFacet, dstackFacet
        );
        cluster = clusterFactory.deployCluster(_initArgs(), keccak256("edges-1"));
    }

    // ── CSK commitment gating ─────────────────────────────────────────────────

    function test_cskOnlyOriginatorAndOnlyOnce() public {
        (address m1, bytes32 id1) = _register(COMP3, 3, "x1", "w1", 0);
        (address m2,) = _register(COMP4, 4, "x2", "w2", 1);

        // Non-member: no memberId at all → NotOriginator.
        vm.prank(address(0xDEAD));
        vm.expectRevert(NotOriginator.selector);
        IAttest(cluster).setCskCommitment(keccak256("csk"));

        // Member 2 is not memberIds[0] → NotOriginator.
        vm.prank(m2);
        vm.expectRevert(NotOriginator.selector);
        IAttest(cluster).setCskCommitment(keccak256("csk"));

        // Originator sets it; reads back.
        vm.prank(m1);
        IAttest(cluster).setCskCommitment(keccak256("csk"));
        assertEq(IAttest(cluster).cskCommitment(), keccak256("csk"));
        assertEq(IAttest(cluster).memberIdOf(m1), id1);

        // Second set reverts even for the originator.
        vm.prank(m1);
        vm.expectRevert(CskCommitmentAlreadySet.selector);
        IAttest(cluster).setCskCommitment(keccak256("other"));
    }

    // ── Registry getters ──────────────────────────────────────────────────────

    function test_gettersOnRegisteredAndUnknown() public {
        (address m1, bytes32 id1) = _register(COMP3, 3, "x1", "w1", 0);

        assertTrue(IAttest(cluster).isClusterMember(m1));
        assertFalse(IAttest(cluster).isClusterMember(address(0xBEEF)));

        MemberStorage.MemberRecord memory byAddr = IAttest(cluster).memberOf(m1);
        MemberStorage.MemberRecord memory byId = IAttest(cluster).memberById(id1);
        assertEq(byAddr.memberContract, m1);
        assertEq(byId.memberContract, m1);
        assertEq(byId.xPubKey, bytes32("x1"));

        // Unknown member id → zeroed record + zeroed key getters.
        bytes32 ghost = keccak256("ghost");
        assertEq(IAttest(cluster).memberById(ghost).memberContract, address(0));
        assertEq(IAttest(cluster).xPubKeyOf(ghost), bytes32(0));
        assertEq(IAttest(cluster).wgPubKeyOf(ghost), bytes32(0));

        (uint32 ip, uint8 prefix) = IAttest(cluster).meshCidr();
        assertEq(ip, 0x0a0d0000);
        assertEq(prefix, 16);
        // Derived mesh IP sits inside the CIDR.
        uint32 mip = IAttest(cluster).meshIpOf(id1);
        assertEq(mip & 0xffff0000, 0x0a0d0000);

        bytes32[] memory members = IAttest(cluster).listMembers();
        assertEq(members.length, 1);
        assertEq(members[0], id1);
    }

    function test_wgMirrorIsInternalOnly() public {
        vm.expectRevert(NotInternalCall.selector);
        AttestFacet(cluster)._setWgMirror(keccak256("id"), bytes32("wg"));
    }

    function test_sendToUnknownRecipientReverts() public {
        (address m1,) = _register(COMP3, 3, "x1", "w1", 0);
        vm.prank(m1);
        vm.expectRevert(RecipientNotMember.selector);
        IMessage(cluster).send(keccak256("ghost"), keccak256("env"), bytes("ct"));
    }

    // ── Cluster-ownership transfers ───────────────────────────────────────────

    function test_clusterOwnershipTwoStepTransfer() public {
        address next = address(0x00000000000000000000000000000000000BD0De);
        assertEq(IAttest(cluster).clusterOwner(), address(this));

        // Only the cluster owner can propose.
        vm.prank(address(0xBEEF));
        vm.expectRevert(NotClusterOwner.selector);
        IAttest(cluster).transferClusterOwnership(next);

        IAttest(cluster).transferClusterOwnership(next);
        assertEq(IAttest(cluster).pendingClusterOwner(), next);

        // Only the nominee can accept.
        vm.prank(address(0xBEEF));
        vm.expectRevert(NotClusterOwner.selector);
        IAttest(cluster).acceptClusterOwnership();

        vm.prank(next);
        IAttest(cluster).acceptClusterOwnership();
        assertEq(IAttest(cluster).clusterOwner(), next);
        assertEq(IAttest(cluster).pendingClusterOwner(), address(0));
    }

    function test_bothOwnersFusedTransfer() public {
        address next = address(0xFADE);

        // The factory only NOMINATES the cluster owner on the solidstate side
        // (SafeOwnable two-step); accept it first — transferBothOwners requires
        // holding both the DiamondCut authority and the allowlist authority.
        (bool accepted,) = cluster.call(abi.encodeWithSignature("acceptOwnership()"));
        assertTrue(accepted, "solidstate acceptOwnership");

        // Non-holder cannot propose the fused transfer.
        vm.prank(address(0xBEEF));
        vm.expectRevert(NotClusterOwner.selector);
        IAttest(cluster).transferBothOwners(next);

        IAttest(cluster).transferBothOwners(next);

        // Non-nominee cannot accept.
        vm.prank(address(0xBEEF));
        vm.expectRevert(NotClusterOwner.selector);
        IAttest(cluster).acceptBothOwners();

        vm.prank(next);
        IAttest(cluster).acceptBothOwners();
        assertEq(IAttest(cluster).clusterOwner(), next);

        // The old owner lost the allowlist authority.
        vm.expectRevert(NotClusterOwner.selector);
        IAppAuthBasicManagement(cluster).addComposeHash(keccak256("nope"));
    }

    // ── Boot-gate decision ladder ─────────────────────────────────────────────

    function test_bootGateFullLadder() public {
        // 1. compose hash gate.
        (bool ok, string memory reason) = IAppAuth(cluster)
            .isAppAllowed(_bootInfo(address(1), keccak256("bad"), DEVICE, "UpToDate"));
        assertFalse(ok);
        assertEq(reason, "compose hash not allowed");

        // 2. device gate.
        (ok, reason) = IAppAuth(cluster)
            .isAppAllowed(_bootInfo(address(1), COMPOSE, keccak256("baddev"), "UpToDate"));
        assertFalse(ok);
        assertEq(reason, "device not allowed");

        // 3. app-id gate (not allowlisted, not registered).
        (ok, reason) =
            IAppAuth(cluster).isAppAllowed(_bootInfo(address(1), COMPOSE, DEVICE, "UpToDate"));
        assertFalse(ok);
        assertEq(reason, "appId not allowlisted");

        // Owner allowlists the app id → passes.
        DstackFacet(cluster).addAllowedAppId(address(1));
        (ok, reason) =
            IAppAuth(cluster).isAppAllowed(_bootInfo(address(1), COMPOSE, DEVICE, "OutOfDate"));
        assertTrue(ok, "requireTcbUpToDate=false admits stale tcb");

        // 4. tcb gate once required.
        IAppAuthBasicManagement(cluster).setRequireTcbUpToDate(true);
        (ok, reason) =
            IAppAuth(cluster).isAppAllowed(_bootInfo(address(1), COMPOSE, DEVICE, "OutOfDate"));
        assertFalse(ok);
        assertEq(reason, "tcb not up to date");

        // 5. allowAnyDevice bypasses the device gate.
        IAppAuthBasicManagement(cluster).setAllowAnyDevice(true);
        (ok,) = IAppAuth(cluster)
            .isAppAllowed(_bootInfo(address(1), COMPOSE, keccak256("baddev"), "UpToDate"));
        assertTrue(ok);
    }

    function test_allowlistRemovalRoundTrips() public {
        assertTrue(IAppAuthBasicManagement(cluster).allowedComposeHashes(COMPOSE));
        IAppAuthBasicManagement(cluster).removeComposeHash(COMPOSE);
        assertFalse(IAppAuthBasicManagement(cluster).allowedComposeHashes(COMPOSE));

        assertTrue(IAppAuthBasicManagement(cluster).allowedDeviceIds(DEVICE));
        IAppAuthBasicManagement(cluster).removeDevice(DEVICE);
        assertFalse(IAppAuthBasicManagement(cluster).allowedDeviceIds(DEVICE));

        IAppAuthBasicManagement(cluster).addDevice(keccak256("dev2"));
        assertTrue(IAppAuthBasicManagement(cluster).allowedDeviceIds(keccak256("dev2")));

        DstackFacet(cluster).addAllowedKmsRoot(address(0x00000000000000000000000000000000000ec0dE));
        assertTrue(
            DstackFacet(cluster)
                .allowedKmsRoots(address(0x00000000000000000000000000000000000ec0dE))
        );
        DstackFacet(cluster)
            .removeAllowedKmsRoot(address(0x00000000000000000000000000000000000ec0dE));
        assertFalse(
            DstackFacet(cluster)
                .allowedKmsRoots(address(0x00000000000000000000000000000000000ec0dE))
        );

        // All management writes are cluster-owner-gated.
        vm.prank(address(0xBEEF));
        vm.expectRevert(NotClusterOwner.selector);
        IAppAuthBasicManagement(cluster).addDevice(keccak256("nope"));
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    function _register(
        bytes memory comp,
        uint256 priv,
        bytes32 xPub,
        bytes32 wgPub,
        uint256 saltSeq
    ) internal returns (address member, bytes32 memberId) {
        member = memberFactory.deployMember(cluster, keccak256(abi.encode("edge", saltSeq)));
        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: priv, compressed: comp }), cluster, member, xPub, wgPub
        );
        memberId = IDstackFacet(cluster).dstack_register(proof, member, xPub, wgPub);
    }

    function _bootInfo(address appId, bytes32 composeHash, bytes32 deviceId, string memory tcb)
        internal
        pure
        returns (IAppAuth.AppBootInfo memory)
    {
        return IAppAuth.AppBootInfo({
            appId: appId,
            composeHash: composeHash,
            instanceId: address(0),
            deviceId: deviceId,
            mrAggregated: bytes32(0),
            mrSystem: bytes32(0),
            osImageHash: bytes32(0),
            tcbStatus: tcb,
            advisories: new string[](0)
        });
    }

    function _initArgs() internal view returns (DiamondInit.InitArgs memory) {
        bytes32[] memory composes = new bytes32[](1);
        composes[0] = COMPOSE;
        bytes32[] memory devices = new bytes32[](1);
        devices[0] = DEVICE;
        return DiamondInit.InitArgs({
            clusterOwner: address(this),
            kmsRootSigner: kms.rootAddress(),
            initialComposeHashes: composes,
            initialDeviceIds: devices,
            allowAnyDevice: false,
            requireTcbUpToDate: false,
            meshCidrIp: 0x0a0d0000,
            meshCidrPrefix: 16,
            memberFactory: address(memberFactory)
        });
    }
}
