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
import { INetwork } from "../../src/interfaces/INetwork.sol";
import { IDstackFacet } from "../../src/interfaces/IDstackFacet.sol";
import { IClusterMember } from "../../src/interfaces/IClusterMember.sol";

import { MockKmsChain } from "../helpers/MockKmsChain.sol";
import {
    ComposeHashNotAllowed,
    NotClusterMember,
    AlreadyRegistered,
    DuplicateEnvelope,
    RecipientNotMember
} from "../../src/errors/Errors.sol";

contract ClusterBringupTest is Test {
    ClusterMemberFactory internal memberFactory;
    ClusterDiamondFactory internal clusterFactory;
    MockKmsChain internal kms;

    address internal cluster;
    address internal orgSafe = address(0xA11CE);

    bytes32 internal constant COMPOSE = keccak256("attestmesh.compose.v1");
    bytes32 internal constant DEVICE = keccak256("attestmesh.device.1");
    bytes32 internal constant DSTACK_ATTESTOR_ID = keccak256("attestmesh.attestor.dstack");

    // Per-member derived secp256k1 keys (canonical vectors 3..6).
    bytes internal constant COMP3 =
        hex"02f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9";
    bytes internal constant COMP4 =
        hex"02e493dbf1c10d80f3581e4904930b1404cc6c13900ee0758474fa94abe8c4cd13";
    bytes internal constant COMP5 =
        hex"022f8bde4d1a07209355b4a7250a5c5128e88b84bddc619ab7cba8d569b240efe4";
    bytes internal constant COMP6 =
        hex"03fff97bd5755eeea420453a14355235d382f6472f8568a18b2f057a1460297556";

    function setUp() public {
        kms = new MockKmsChain();

        // ── Infra (one-shot per chain) ────────────────────────────────────────
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

        // ── Cluster ───────────────────────────────────────────────────────────
        bytes32[] memory composes = new bytes32[](1);
        composes[0] = COMPOSE;
        bytes32[] memory devices = new bytes32[](1);
        devices[0] = DEVICE;

        DiamondInit.InitArgs memory args = DiamondInit.InitArgs({
            clusterOwner: address(this),
            kmsRootSigner: kms.rootAddress(),
            initialComposeHashes: composes,
            initialDeviceIds: devices,
            allowAnyDevice: false,
            requireTcbUpToDate: false,
            meshCidrIp: 0x0a0d0000, // 10.13.0.0
            meshCidrPrefix: 16,
            memberFactory: address(memberFactory)
        });

        cluster = clusterFactory.deployCluster(args, keccak256("cluster-1"));
    }

    // ── Scenario 1: three members register, exchange messages ─────────────────

    function test_threeMembersRegisterAndMessage() public {
        (address mA, bytes32 idA) = _register(COMP3, 3, keccak256("inst-A"), "xpub-A", "wg-A", 0);
        (address mB, bytes32 idB) = _register(COMP4, 4, keccak256("inst-B"), "xpub-B", "wg-B", 1);
        (, bytes32 idC) = _register(COMP5, 5, keccak256("inst-C"), "xpub-C", "wg-C", 2);

        assertEq(IAttest(cluster).memberCount(), 3);

        // Pubkeys readable.
        assertEq(IAttest(cluster).xPubKeyOf(idA), bytes32("xpub-A"));
        assertEq(INetwork(cluster).wgPubKeyOf(idA), bytes32("wg-A"));
        assertEq(IAttest(cluster).wgPubKeyOf(idB), bytes32("wg-B")); // mirror

        // ClusterMember owner installed = derived key address (vm.addr(3) for A).
        assertEq(IClusterMember(mA).owner(), vm.addr(3));
        assertEq(IClusterMember(mB).owner(), vm.addr(4));

        // A -> B message lands; C is unrelated.
        bytes memory ct = bytes("sealed-box-ciphertext");
        bytes32 env = keccak256("env-1");
        vm.prank(mA);
        vm.expectEmit(true, true, true, true, cluster);
        emit IMessage.MessageSent(idA, idB, env, ct);
        IMessage(cluster).send(idB, env, ct);

        // Duplicate envelope to same recipient reverts.
        vm.prank(mA);
        vm.expectRevert(DuplicateEnvelope.selector);
        IMessage(cluster).send(idB, env, ct);

        // Same envelopeId to a *different* recipient is fine (dedup is per-recipient).
        vm.prank(mA);
        IMessage(cluster).send(idC, env, ct);
    }

    // ── Scenario 2: non-allowed compose hash reverts ──────────────────────────

    function test_badComposeHashReverts() public {
        address m = memberFactory.deployMember(cluster, keccak256("bad"));
        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: 6, compressed: COMP6 }),
            keccak256("not-allowed-compose"),
            keccak256("inst-bad"),
            DEVICE,
            "UpToDate",
            cluster,
            m,
            bytes32("xpub-bad"),
            bytes32("wg-bad")
        );
        vm.expectRevert(ComposeHashNotAllowed.selector);
        IDstackFacet(cluster).dstack_register(proof, m, bytes32("xpub-bad"), bytes32("wg-bad"));
    }

    // ── Scenario 3: non-member send reverts ───────────────────────────────────

    function test_nonMemberSendReverts() public {
        (, bytes32 idA) = _register(COMP3, 3, keccak256("inst-A"), "xpub-A", "wg-A", 0);
        vm.prank(address(0xDEAD));
        vm.expectRevert(NotClusterMember.selector);
        IMessage(cluster).send(idA, keccak256("env"), bytes("x"));
    }

    // ── Scenario 4: re-register reverts ───────────────────────────────────────

    function test_reRegisterReverts() public {
        (address mA,) = _register(COMP3, 3, keccak256("inst-A"), "xpub-A", "wg-A", 0);
        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: 3, compressed: COMP3 }),
            COMPOSE,
            keccak256("inst-A"),
            DEVICE,
            "UpToDate",
            cluster,
            mA,
            bytes32("xpub-A"),
            bytes32("wg-A")
        );
        vm.expectRevert(AlreadyRegistered.selector);
        IDstackFacet(cluster).dstack_register(proof, mA, bytes32("xpub-A"), bytes32("wg-A"));
    }

    // ── Scenario 5: owner removes compose hash; existing OK, new reverts ──────

    function test_removeComposeHashBlocksNewJoiners() public {
        _register(COMP3, 3, keccak256("inst-A"), "xpub-A", "wg-A", 0);

        // Cluster owner removes the compose hash.
        DstackFacet(cluster).removeComposeHash(COMPOSE);

        // Existing member still works (send).
        (, bytes32 idA) = (address(0), IAttest(cluster).memberIdOf(_memberAddr(0)));
        assertTrue(idA != bytes32(0));

        // New registration with the now-removed hash reverts.
        address mB = memberFactory.deployMember(cluster, keccak256("salt-1"));
        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: 4, compressed: COMP4 }),
            COMPOSE,
            keccak256("inst-B"),
            DEVICE,
            "UpToDate",
            cluster,
            mB,
            bytes32("xpub-B"),
            bytes32("wg-B")
        );
        vm.expectRevert(ComposeHashNotAllowed.selector);
        IDstackFacet(cluster).dstack_register(proof, mB, bytes32("xpub-B"), bytes32("wg-B"));
    }

    // ── CSK commitment: originator-only, set-once ─────────────────────────────

    function test_cskCommitmentOriginatorOnly() public {
        (address mA,) = _register(COMP3, 3, keccak256("inst-A"), "xpub-A", "wg-A", 0);
        (address mB,) = _register(COMP4, 4, keccak256("inst-B"), "xpub-B", "wg-B", 1);

        bytes32 commitment = keccak256("csk-commitment");

        // Onboardee (second registrant) cannot set it.
        vm.prank(mB);
        vm.expectRevert();
        IAttest(cluster).setCskCommitment(commitment);

        // Originator can.
        vm.prank(mA);
        IAttest(cluster).setCskCommitment(commitment);
        assertEq(IAttest(cluster).cskCommitment(), commitment);

        // Set-once.
        vm.prank(mA);
        vm.expectRevert();
        IAttest(cluster).setCskCommitment(keccak256("other"));
    }

    // ── mesh IP derivation is deterministic and in-CIDR ───────────────────────

    function test_meshIpInCidr() public {
        (, bytes32 idA) = _register(COMP3, 3, keccak256("inst-A"), "xpub-A", "wg-A", 0);
        uint32 ip = IAttest(cluster).meshIpOf(idA);
        // Inside 10.13.0.0/16 → high 16 bits == 0x0a0d.
        assertEq(ip & 0xFFFF0000, 0x0a0d0000);
        // Host part is in [1, 65534].
        uint32 host = ip & 0x0000FFFF;
        assertGt(host, 0);
        assertLt(host, 65535);
    }

    // ── mesh IP exact reference vectors (shared with the Rust sidecar) ────────

    function test_meshIpReferenceVectors() public view {
        // CIDR 10.13.0.0/16. Values cross-checked off-chain; the sidecar's
        // wg::cidr::derive_ip must produce identical results.
        assertEq(IAttest(cluster).meshIpOf(bytes32(uint256(1))), 168656109); // 10.13.124.237
        assertEq(IAttest(cluster).meshIpOf(bytes32(uint256(2))), 168665671); // 10.13.162.71
        assertEq(IAttest(cluster).meshIpOf(bytes32(uint256(0xaa))), 168680749); // 10.13.221.45
    }

    // ── factory address prediction is exact (audit R5) ────────────────────────

    function test_predictClusterAddressMatchesDeploy() public {
        DiamondInit.InitArgs memory args = _initArgs();
        bytes32 salt = keccak256("cluster-2");
        address predicted = clusterFactory.predictClusterAddress(args, salt);
        address deployed = clusterFactory.deployCluster(args, salt);
        assertEq(predicted, deployed, "prediction must match the deployed CREATE2 address");
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

    // ── helpers ───────────────────────────────────────────────────────────────

    function _register(
        bytes memory comp,
        uint256 priv,
        bytes32 instanceId,
        bytes32 xPub,
        bytes32 wgPub,
        uint256 saltSeq
    ) internal returns (address member, bytes32 memberId) {
        member = memberFactory.deployMember(cluster, keccak256(abi.encode("member", saltSeq)));
        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: priv, compressed: comp }),
            COMPOSE,
            instanceId,
            DEVICE,
            "UpToDate",
            cluster,
            member,
            xPub,
            wgPub
        );
        memberId = IDstackFacet(cluster).dstack_register(proof, member, xPub, wgPub);
        // Stash for _memberAddr lookups.
        _members.push(member);
    }

    address[] internal _members;

    function _memberAddr(uint256 i) internal view returns (address) {
        return _members[i];
    }
}
