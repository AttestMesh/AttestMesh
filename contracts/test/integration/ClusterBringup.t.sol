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
import { IAppAuth } from "../../src/interfaces/IAppAuth.sol";
import { IDstackFacet } from "../../src/interfaces/IDstackFacet.sol";
import { IClusterMember } from "../../src/interfaces/IClusterMember.sol";

import { MockKmsChain } from "../helpers/MockKmsChain.sol";
import { MockStockApp } from "../helpers/MockStockApp.sol";
import { DstackSigChain } from "../../src/libraries/DstackSigChain.sol";
import { ERC1967Proxy } from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {
    UUPSUpgradeable
} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import { Initializable } from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {
    NotClusterMember,
    AlreadyRegistered,
    DuplicateEnvelope,
    CodeIdMismatch,
    BindingMismatch,
    NotOurMember
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

        cluster = clusterFactory.deployCluster(_initArgs(), keccak256("cluster-1"));
    }

    // ── Scenario 1: three members register, exchange messages ─────────────────

    function test_threeMembersRegisterAndMessage() public {
        (address mA, bytes32 idA) = _register(COMP3, 3, "xpub-A", "wg-A", 0);
        (address mB, bytes32 idB) = _register(COMP4, 4, "xpub-B", "wg-B", 1);
        (, bytes32 idC) = _register(COMP5, 5, "xpub-C", "wg-C", 2);

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

    // ── Scenario 1b: on-chain Ed25519 heartbeat key (ed25519-onchain-key spec) ──
    // Members publish their heartbeat key on chain so peers read it instead of
    // exchanging a sponsored PeerEndpoint envelope. Mirrors publishWgKey.

    function test_publishEd25519Key() public {
        (address mA, bytes32 idA) = _register(COMP3, 3, "xpub-A", "wg-A", 0);

        // Unset until published.
        assertEq(INetwork(cluster).ed25519KeyOf(idA), bytes32(0));

        // Member publishes; event carries (memberId, key); read returns it.
        vm.prank(mA);
        vm.expectEmit(true, true, true, true, cluster);
        emit INetwork.Ed25519KeyPublished(idA, bytes32("ed-A"));
        INetwork(cluster).publishEd25519Key(bytes32("ed-A"));
        assertEq(INetwork(cluster).ed25519KeyOf(idA), bytes32("ed-A"));

        // Rotation: unconditional overwrite.
        vm.prank(mA);
        INetwork(cluster).publishEd25519Key(bytes32("ed-A2"));
        assertEq(INetwork(cluster).ed25519KeyOf(idA), bytes32("ed-A2"));
    }

    function test_publishEd25519KeyNonMemberReverts() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert(NotClusterMember.selector);
        INetwork(cluster).publishEd25519Key(bytes32("ed-x"));
    }

    // ── Scenario 2: the KMS boot gate rejects a non-allowed compose hash ───────
    // Compose hash is no longer self-asserted at registration (it is not part of the
    // signed KMS chain); it is the boot-gate policy the dstack KMS enforces before the
    // CVM boots. So we assert the gate (isAppAllowed), not the registration call.

    function test_bootGateRejectsBadComposeHash() public {
        IAppAuth.AppBootInfo memory info = _bootInfo(address(0xBEEF), keccak256("not-allowed"));
        (bool ok, string memory reason) = IAppAuth(cluster).isAppAllowed(info);
        assertFalse(ok);
        assertEq(reason, "compose hash not allowed");
    }

    // ── Scenario 3: non-member send reverts ───────────────────────────────────

    function test_nonMemberSendReverts() public {
        (, bytes32 idA) = _register(COMP3, 3, "xpub-A", "wg-A", 0);
        vm.prank(address(0xDEAD));
        vm.expectRevert(NotClusterMember.selector);
        IMessage(cluster).send(idA, keccak256("env"), bytes("x"));
    }

    // ── Scenario 4: re-register reverts ───────────────────────────────────────

    function test_reRegisterReverts() public {
        (address mA,) = _register(COMP3, 3, "xpub-A", "wg-A", 0);
        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: 3, compressed: COMP3 }),
            cluster,
            mA,
            bytes32("xpub-A"),
            bytes32("wg-A")
        );
        vm.expectRevert(AlreadyRegistered.selector);
        IDstackFacet(cluster).dstack_register(proof, mA, bytes32("xpub-A"), bytes32("wg-A"));
    }

    // ── Scenario 5: owner removes compose hash; boot gate then rejects it ──────

    function test_removeComposeHashBlocksBootGate() public {
        IAppAuth.AppBootInfo memory info = _bootInfo(address(0xBEEF), COMPOSE);

        // Before removal the compose branch passes (the gate fails later, on device).
        (, string memory reasonBefore) = IAppAuth(cluster).isAppAllowed(info);
        assertTrue(
            keccak256(bytes(reasonBefore)) != keccak256(bytes("compose hash not allowed")),
            "compose should pass before removal"
        );

        // Cluster owner removes the compose hash.
        DstackFacet(cluster).removeComposeHash(COMPOSE);

        // Now the gate rejects specifically on the compose branch.
        (bool okAfter, string memory reasonAfter) = IAppAuth(cluster).isAppAllowed(info);
        assertFalse(okAfter);
        assertEq(reasonAfter, "compose hash not allowed");
    }

    // ── Boot gate: owner-allowlisted app_id boots before it has registered ─────
    // Regression for the cold-start deadlock (premortem 1780517097 / audit finding 3):
    // the KMS calls isAppAllowed at boot, before registration, so the gate must admit
    // an owner-pre-approved app_id that is not yet a member.

    function test_bootGateAcceptsAllowlistedUnregisteredAppId() public {
        address m = memberFactory.deployMember(cluster, keccak256("boot-1"));
        IAppAuth.AppBootInfo memory info = _bootInfo(m, COMPOSE);

        // Not yet allowlisted nor registered → the gate rejects on the appId branch.
        (bool ok0, string memory r0) = IAppAuth(cluster).isAppAllowed(info);
        assertFalse(ok0);
        assertEq(r0, "appId not allowlisted");

        // Owner pre-approves the (not-yet-registered) member address → cold-start boot passes.
        DstackFacet(cluster).addAllowedAppId(m);
        (bool ok1, string memory r1) = IAppAuth(cluster).isAppAllowed(info);
        assertTrue(ok1, "cold-start boot must be allowed once the app_id is allowlisted");
        assertEq(r1, "");

        // Removing it closes the gate again.
        DstackFacet(cluster).removeAllowedAppId(m);
        (bool ok2,) = IAppAuth(cluster).isAppAllowed(info);
        assertFalse(ok2);
    }

    // ── Scenario 6: a proof signed by a non-allowlisted KMS root is rejected ───

    function test_kmsRootNotAllowedReverts() public {
        address m = memberFactory.deployMember(cluster, keccak256("salt-kms"));
        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: 6, compressed: COMP6 }),
            cluster,
            m,
            bytes32("xpub-k"),
            bytes32("wg-k")
        );
        // Owner removes the only allowlisted KMS root; the sig chain no longer roots
        // in a trusted KMS, so verify() reverts.
        DstackFacet(cluster).removeAllowedKmsRoot(kms.rootAddress());
        vm.expectRevert(DstackSigChain.InvalidSigChain.selector);
        IDstackFacet(cluster).dstack_register(proof, m, bytes32("xpub-k"), bytes32("wg-k"));
    }

    // ── Scenario 7: the attested app_id (codeId) must equal the member contract ─

    function test_codeIdMismatchReverts() public {
        address mA = memberFactory.deployMember(cluster, keccak256("salt-a"));
        address mB = memberFactory.deployMember(cluster, keccak256("salt-b"));
        // Proof attests app_id = mA, but we try to register mB.
        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: 6, compressed: COMP6 }),
            cluster,
            mA,
            bytes32("xpub"),
            bytes32("wg")
        );
        vm.expectRevert(CodeIdMismatch.selector);
        IDstackFacet(cluster).dstack_register(proof, mB, bytes32("xpub"), bytes32("wg"));
    }

    // ── Scenario 8: the binding message must commit to the exact keys ──────────

    function test_bindingMismatchReverts() public {
        address m = memberFactory.deployMember(cluster, keccak256("salt-bind"));
        // Proof binds (xpub-1, wg-1); call register with different keys.
        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: 6, compressed: COMP6 }),
            cluster,
            m,
            bytes32("xpub-1"),
            bytes32("wg-1")
        );
        vm.expectRevert(BindingMismatch.selector);
        IDstackFacet(cluster).dstack_register(proof, m, bytes32("xpub-2"), bytes32("wg-2"));
    }

    // ── Drift guard: the KMS preimage literal is exactly "dstack-kms-issued:" ──
    // If MockKmsChain or DstackSigChain ever drift from dstack's real preimage, this
    // independent recompute of the literal would stop recovering the KMS root.

    function test_dstackKmsIssuedPreimageLiteral() public {
        address m = memberFactory.deployMember(cluster, keccak256("salt-lit"));
        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: 6, compressed: COMP6 }),
            cluster,
            m,
            bytes32("xpub"),
            bytes32("wg")
        );
        bytes32 kmsMsgHash = keccak256(
            abi.encodePacked("dstack-kms-issued:", bytes20(proof.codeId), proof.appCompressedPubkey)
        );
        address recovered = DstackSigChain.recover(kmsMsgHash, proof.kmsSignature);
        assertEq(recovered, kms.rootAddress(), "KMS sig must be over the dstack-kms-issued literal");
    }

    // ── CSK commitment: originator-only, set-once ─────────────────────────────

    function test_cskCommitmentOriginatorOnly() public {
        (address mA,) = _register(COMP3, 3, "xpub-A", "wg-A", 0);
        (address mB,) = _register(COMP4, 4, "xpub-B", "wg-B", 1);

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
        (, bytes32 idA) = _register(COMP3, 3, "xpub-A", "wg-A", 0);
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
            meshCidrIp: 0x0a0d0000, // 10.13.0.0
            meshCidrPrefix: 16,
            memberFactory: address(memberFactory)
        });
    }

    // ── Path A: register a member the factory did NOT deploy, via app_id allowlist ─
    // When the KMS only mints an app_id it provisioned (dstack base KMS), the member
    // contract IS that app_id (a stock DstackApp upgraded to ClusterMember), so it is not
    // in the factory's deployedMembers. dstack_register must accept it on the strength of
    // the owner's app_id allowlist instead — codeId (step 2) and the binding sig (step 3)
    // still pin identity, so the looser membership anchor can't be abused.

    function test_pathA_allowlistedNonFactoryMemberRegisters() public {
        // A ClusterMember proxy deployed OUTSIDE the factory (stands in for the upgraded
        // DstackApp proxy); cluster() is seeded so __setOwnerFromCluster accepts the diamond.
        address impl = address(new ClusterMember());
        address m =
            address(new ERC1967Proxy(impl, abi.encodeCall(ClusterMember.initialize, (cluster))));
        assertFalse(memberFactory.isOurMember(m), "must not be a factory member");

        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: 4, compressed: COMP4 }),
            cluster,
            m,
            bytes32("xpub-pa"),
            bytes32("wg-pa")
        );

        // Neither factory-deployed nor allowlisted → NotOurMember.
        vm.expectRevert(NotOurMember.selector);
        IDstackFacet(cluster).dstack_register(proof, m, bytes32("xpub-pa"), bytes32("wg-pa"));

        // Owner allowlists the app_id (== member address) → registration succeeds (Path A).
        DstackFacet(cluster).addAllowedAppId(m);
        bytes32 memberId =
            IDstackFacet(cluster).dstack_register(proof, m, bytes32("xpub-pa"), bytes32("wg-pa"));

        assertEq(IClusterMember(m).owner(), vm.addr(4), "derived key installed as owner");
        assertEq(IAttest(cluster).memberCount(), 1);
        assertEq(IAttest(cluster).xPubKeyOf(memberId), bytes32("xpub-pa"));
    }

    // ── Path A: reinitializeFromDstackApp re-seats a stock proxy and burns initialize ──
    // Models `phala deploy` (a stock DstackApp proxy: _initialized==1, owner==deployer)
    // then our `upgradeToAndCall` to the ClusterMember impl that binds the cluster in the
    // same call. reinitializer(2) must run once and then lock both re-entry points.

    function test_pathA_reinitFromStockAppProxy() public {
        address stockImpl = address(new MockStockApp());
        MockStockApp proxy = MockStockApp(
            address(
                new ERC1967Proxy(
                    stockImpl, abi.encodeCall(MockStockApp.initialize, (address(this)))
                )
            )
        );
        assertEq(proxy.owner(), address(this), "stock proxy owned by deployer");

        // Upgrade the stock proxy to ClusterMember + seat the cluster atomically.
        address memberImpl = address(new ClusterMember());
        UUPSUpgradeable(address(proxy))
            .upgradeToAndCall(
                memberImpl, abi.encodeCall(ClusterMember.reinitializeFromDstackApp, (cluster))
            );

        // It is now a ClusterMember bound to our cluster; owner lands later at registration.
        assertEq(IClusterMember(address(proxy)).cluster(), cluster, "cluster seated");
        assertEq(IClusterMember(address(proxy)).owner(), address(0), "owner not yet seated");

        // The v1 initialize slot is burned: neither reinit nor initialize can run again.
        vm.expectRevert(Initializable.InvalidInitialization.selector);
        ClusterMember(payable(address(proxy))).reinitializeFromDstackApp(address(0xBEEF));
        vm.expectRevert(Initializable.InvalidInitialization.selector);
        ClusterMember(payable(address(proxy))).initialize(address(0xBEEF));
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    function _register(
        bytes memory comp,
        uint256 priv,
        bytes32 xPub,
        bytes32 wgPub,
        uint256 saltSeq
    ) internal returns (address member, bytes32 memberId) {
        member = memberFactory.deployMember(cluster, keccak256(abi.encode("member", saltSeq)));
        IDstackFacet.DstackProof memory proof = kms.buildProof(
            MockKmsChain.DerivedKey({ priv: priv, compressed: comp }), cluster, member, xPub, wgPub
        );
        memberId = IDstackFacet(cluster).dstack_register(proof, member, xPub, wgPub);
    }

    function _bootInfo(address appId, bytes32 composeHash)
        internal
        pure
        returns (IAppAuth.AppBootInfo memory)
    {
        return IAppAuth.AppBootInfo({
            appId: appId,
            composeHash: composeHash,
            instanceId: address(0),
            deviceId: DEVICE, // allowlisted, so probes reach branches past the device check
            mrAggregated: bytes32(0),
            mrSystem: bytes32(0),
            osImageHash: bytes32(0),
            tcbStatus: "UpToDate",
            advisories: new string[](0)
        });
    }
}
