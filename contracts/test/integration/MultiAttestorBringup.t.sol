// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";
import { MessageHashUtils } from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import { IERC2535DiamondCut } from "@solidstate/contracts/interfaces/IERC2535DiamondCut.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";
import { ISafeOwnable } from "@solidstate/contracts/access/ownable/ISafeOwnable.sol";

import { AttestFacet } from "../../src/facets/core/AttestFacet.sol";
import { MessageFacet } from "../../src/facets/core/MessageFacet.sol";
import { NetworkFacet } from "../../src/facets/core/NetworkFacet.sol";
import { DstackFacet } from "../../src/facets/attestor/DstackFacet.sol";
import { OperatorFacet } from "../../src/facets/attestor/OperatorFacet.sol";
import { DiamondInitV2 } from "../../src/DiamondInitV2.sol";
import { ClusterMember } from "../../src/members/ClusterMember.sol";
import { ClusterMemberFactory } from "../../src/members/ClusterMemberFactory.sol";
import { ClusterDiamondFactoryV2 } from "../../src/factory/ClusterDiamondFactoryV2.sol";

import { IAttest } from "../../src/interfaces/IAttest.sol";
import { INetwork } from "../../src/interfaces/INetwork.sol";
import { IDstackFacet } from "../../src/interfaces/IDstackFacet.sol";
import { IOperatorFacet } from "../../src/interfaces/IOperatorFacet.sol";
import { IClusterMember } from "../../src/interfaces/IClusterMember.sol";
import { AttestorConfig } from "../../src/interfaces/IAttestorFacet.sol";

import { MockKmsChain } from "../helpers/MockKmsChain.sol";
import {
    NotOurMember,
    AlreadyRegistered,
    BindingMismatch,
    OperatorSignerNotAllowed,
    VoucherExpired,
    AttestorNotApproved,
    NotDiamondContext,
    NotFactoryOwner,
    NotClusterOwner
} from "../../src/errors/Errors.sol";

/// @notice End-to-end coverage of the multi-attestor framework: ClusterDiamondFactoryV2
///         (approved-attestor set), DiamondInitV2 (core seed + per-facet init blobs),
///         dynamic attestor cuts, dstack-on-v2 parity, and the OperatorFacet method.
///         TRUST NOTE exercised throughout: operator-admitted members are vouched for
///         by a key, not hardware attestation, and the registry treats them identically.
contract MultiAttestorBringupTest is Test {
    using MessageHashUtils for bytes32;

    ClusterMemberFactory internal memberFactory;
    ClusterDiamondFactoryV2 internal clusterFactory;
    MockKmsChain internal kms;

    address internal attestFacet;
    address internal messageFacet;
    address internal networkFacet;
    address internal dstackFacetImpl;
    address internal operatorFacetImpl;
    address internal diamondInit;

    address internal cluster; // dstack + operator, deployed in setUp
    address internal orgSafe = address(0xA11CE);

    bytes32 internal constant COMPOSE = keccak256("attestmesh.compose.v1");
    bytes32 internal constant DEVICE = keccak256("attestmesh.device.1");
    bytes32 internal constant DSTACK_ATTESTOR_ID = keccak256("attestmesh.attestor.dstack");
    bytes32 internal constant OPERATOR_ATTESTOR_ID = keccak256("attestmesh.attestor.operator");
    string internal constant OPERATOR_BIND_DOMAIN = "attestmesh.operator.bind.v1";

    uint256 internal opSignerPriv = 0xC0DE;
    address internal opSigner;

    // Canonical derived-key vector (priv 3) for the dstack parity path.
    bytes internal constant COMP3 =
        hex"02f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9";

    function setUp() public {
        kms = new MockKmsChain();
        opSigner = vm.addr(opSignerPriv);

        attestFacet = address(new AttestFacet());
        messageFacet = address(new MessageFacet());
        networkFacet = address(new NetworkFacet());
        dstackFacetImpl = address(new DstackFacet());
        operatorFacetImpl = address(new OperatorFacet());
        diamondInit = address(new DiamondInitV2());

        memberFactory = new ClusterMemberFactory(address(new ClusterMember()), orgSafe);
        clusterFactory = new ClusterDiamondFactoryV2(
            address(this), diamondInit, attestFacet, messageFacet, networkFacet
        );
        clusterFactory.addApprovedAttestor(dstackFacetImpl);
        clusterFactory.addApprovedAttestor(operatorFacetImpl);

        cluster = clusterFactory.deployCluster(_coreArgs(), _bothAttestors(), keccak256("c-v2"));
    }

    // ── factory v2: approved-attestor registry ────────────────────────────────

    function test_approvedAttestorSetIsOwnerManaged() public {
        address probe = address(0xFACE7);
        vm.prank(address(0xDEAD));
        vm.expectRevert(NotFactoryOwner.selector);
        clusterFactory.addApprovedAttestor(probe);

        clusterFactory.addApprovedAttestor(probe);
        assertTrue(clusterFactory.isApprovedAttestor(probe));
        assertEq(clusterFactory.approvedAttestors().length, 3);

        vm.prank(address(0xDEAD));
        vm.expectRevert(NotFactoryOwner.selector);
        clusterFactory.removeApprovedAttestor(probe);

        clusterFactory.removeApprovedAttestor(probe);
        assertFalse(clusterFactory.isApprovedAttestor(probe));
    }

    function test_deployRevertsOnUnapprovedAttestor() public {
        clusterFactory.removeApprovedAttestor(operatorFacetImpl);
        // Materialize args first: the helpers make external calls (kms.rootAddress)
        // that would otherwise consume the expectRevert.
        DiamondInitV2.CoreInitArgs memory core = _coreArgs();
        AttestorConfig[] memory attestors = _bothAttestors();
        vm.expectRevert(AttestorNotApproved.selector);
        clusterFactory.deployCluster(core, attestors, keccak256("c-bad"));
    }

    function test_predictClusterAddressMatchesDeploy() public {
        DiamondInitV2.CoreInitArgs memory core = _coreArgs();
        AttestorConfig[] memory attestors = _bothAttestors();
        bytes32 salt = keccak256("c-predict");
        address predicted = clusterFactory.predictClusterAddress(core, attestors, salt);
        address deployed = clusterFactory.deployCluster(core, attestors, salt);
        assertEq(predicted, deployed, "prediction must match the deployed CREATE2 address");
        assertTrue(clusterFactory.isDeployedCluster(deployed));
    }

    // ── DiamondInitV2: per-facet init blobs landed ────────────────────────────

    function test_initSeededCoreAndBothAttestorNamespaces() public view {
        // Core namespace.
        assertEq(IAttest(cluster).clusterOwner(), address(this));
        (uint32 ip, uint8 prefix) = IAttest(cluster).meshCidr();
        assertEq(ip, 0x0a0d0000);
        assertEq(prefix, 16);

        // Dstack blob.
        assertTrue(DstackFacet(cluster).allowedKmsRoots(kms.rootAddress()));
        assertTrue(DstackFacet(cluster).allowedComposeHashes(COMPOSE));
        assertTrue(DstackFacet(cluster).allowedDeviceIds(DEVICE));

        // Operator blob.
        assertTrue(IOperatorFacet(cluster).isOperatorSigner(opSigner));
        assertEq(IOperatorFacet(cluster).operatorSigners().length, 1);
    }

    function test_initAttestorRevertsOutsideDiamondContext() public {
        // Direct calls on the implementation contracts (no seeded core storage)
        // must hit the NotDiamondContext sentinel.
        vm.expectRevert(NotDiamondContext.selector);
        OperatorFacet(operatorFacetImpl).initAttestor(abi.encode(new address[](0)));

        DstackFacet.DstackInitArgs memory args;
        vm.expectRevert(NotDiamondContext.selector);
        DstackFacet(dstackFacetImpl).initAttestor(abi.encode(args));
    }

    // ── dstack parity on a v2-deployed cluster ────────────────────────────────

    function test_dstackRegisterWorksOnV2Cluster() public {
        (address m, bytes32 memberId) = _registerDstack(COMP3, 3, "xpub-A", "wg-A", 0);
        assertTrue(IAttest(cluster).isClusterMember(m));
        assertEq(IAttest(cluster).memberById(memberId).attestorId, DSTACK_ATTESTOR_ID);
        assertEq(IClusterMember(m).owner(), vm.addr(3), "derived key installed as owner");
        assertEq(INetwork(cluster).wgPubKeyOf(memberId), bytes32("wg-A"));
    }

    // ── operator registration ─────────────────────────────────────────────────

    function test_operatorRegisterHappyPath() public {
        address ownerKey = vm.addr(0xBEEF01);
        (address m, IOperatorFacet.OperatorProof memory proof) =
            _operatorMemberAndProof("salt-op-1", ownerKey, uint64(block.timestamp + 1 days));

        vm.expectEmit(false, true, true, true, cluster);
        emit IOperatorFacet.OperatorMemberRegistered(bytes32(0), opSigner, ownerKey);
        bytes32 memberId = IOperatorFacet(cluster)
            .operator_register(proof, m, bytes32("xpub-op"), bytes32("wg-op"));

        assertTrue(IAttest(cluster).isClusterMember(m));
        assertEq(IAttest(cluster).memberById(memberId).attestorId, OPERATOR_ATTESTOR_ID);
        assertEq(IAttest(cluster).xPubKeyOf(memberId), bytes32("xpub-op"));
        assertEq(INetwork(cluster).wgPubKeyOf(memberId), bytes32("wg-op"));
        assertEq(IClusterMember(m).owner(), ownerKey, "voucher ownerKey installed as 4337 owner");
        // memberId commits to the attestor method.
        assertEq(memberId, keccak256(abi.encode(cluster, m, OPERATOR_ATTESTOR_ID)));
    }

    function test_operatorRegisterRejectsUnlistedSigner() public {
        address ownerKey = vm.addr(0xBEEF02);
        (address m, IOperatorFacet.OperatorProof memory proof) = _operatorMemberAndProofSigned(
            "salt-op-2", ownerKey, uint64(block.timestamp + 1 days), 0xBAD51
        );
        proof.signer = vm.addr(0xBAD51); // consistent sig, but signer not allowlisted
        vm.expectRevert(OperatorSignerNotAllowed.selector);
        IOperatorFacet(cluster).operator_register(proof, m, bytes32("xpub-op"), bytes32("wg-op"));
    }

    function test_operatorRegisterRejectsExpiredVoucher() public {
        address ownerKey = vm.addr(0xBEEF03);
        uint64 expiry = uint64(block.timestamp + 1 hours);
        (address m, IOperatorFacet.OperatorProof memory proof) =
            _operatorMemberAndProof("salt-op-3", ownerKey, expiry);

        vm.warp(expiry + 1);
        vm.expectRevert(VoucherExpired.selector);
        IOperatorFacet(cluster).operator_register(proof, m, bytes32("xpub-op"), bytes32("wg-op"));

        // At exactly the deadline it still registers (block.timestamp <= expiry).
        vm.warp(expiry);
        IOperatorFacet(cluster).operator_register(proof, m, bytes32("xpub-op"), bytes32("wg-op"));
    }

    function test_operatorRegisterRejectsTamperedBinding() public {
        address ownerKey = vm.addr(0xBEEF04);
        (address m, IOperatorFacet.OperatorProof memory proof) =
            _operatorMemberAndProof("salt-op-4", ownerKey, uint64(block.timestamp + 1 days));

        // Different wg key than the voucher binds.
        vm.expectRevert(BindingMismatch.selector);
        IOperatorFacet(cluster).operator_register(proof, m, bytes32("xpub-op"), bytes32("wg-EVIL"));

        // Different ownerKey than the voucher binds.
        IOperatorFacet.OperatorProof memory tampered = proof;
        tampered.ownerKey = vm.addr(0xEEEE);
        vm.expectRevert(BindingMismatch.selector);
        IOperatorFacet(cluster).operator_register(tampered, m, bytes32("xpub-op"), bytes32("wg-op"));
    }

    function test_operatorRegisterRejectsNonFactoryMember() public {
        // An address the member factory never deployed (operator method has no
        // Path A escape hatch — that allowlist is dstack-KMS-specific).
        address stranger = address(0x5717A11);
        uint64 expiry = uint64(block.timestamp + 1 days);
        IOperatorFacet.OperatorProof memory proof =
            _signProof(stranger, vm.addr(0xBEEF05), expiry, opSignerPriv);
        vm.expectRevert(NotOurMember.selector);
        IOperatorFacet(cluster)
            .operator_register(proof, stranger, bytes32("xpub-op"), bytes32("wg-op"));
    }

    function test_operatorReplayReverts() public {
        address ownerKey = vm.addr(0xBEEF06);
        (address m, IOperatorFacet.OperatorProof memory proof) =
            _operatorMemberAndProof("salt-op-6", ownerKey, uint64(block.timestamp + 1 days));
        IOperatorFacet(cluster).operator_register(proof, m, bytes32("xpub-op"), bytes32("wg-op"));

        // (memberContract, attestorId) registers at most once.
        vm.expectRevert(AlreadyRegistered.selector);
        IOperatorFacet(cluster).operator_register(proof, m, bytes32("xpub-op"), bytes32("wg-op"));
    }

    function test_operatorSignerAdminIsClusterOwnerGated() public {
        address newSigner = address(0x51493);
        vm.prank(address(0xDEAD));
        vm.expectRevert(NotClusterOwner.selector);
        IOperatorFacet(cluster).addOperatorSigner(newSigner);

        IOperatorFacet(cluster).addOperatorSigner(newSigner);
        assertTrue(IOperatorFacet(cluster).isOperatorSigner(newSigner));

        vm.prank(address(0xDEAD));
        vm.expectRevert(NotClusterOwner.selector);
        IOperatorFacet(cluster).removeOperatorSigner(newSigner);

        IOperatorFacet(cluster).removeOperatorSigner(newSigner);
        assertFalse(IOperatorFacet(cluster).isOperatorSigner(newSigner));

        // A removed signer's voucher no longer admits members.
        IOperatorFacet(cluster).removeOperatorSigner(opSigner);
        address ownerKey = vm.addr(0xBEEF07);
        (address m, IOperatorFacet.OperatorProof memory proof) =
            _operatorMemberAndProof("salt-op-7", ownerKey, uint64(block.timestamp + 1 days));
        vm.expectRevert(OperatorSignerNotAllowed.selector);
        IOperatorFacet(cluster).operator_register(proof, m, bytes32("xpub-op"), bytes32("wg-op"));
    }

    // ── mixed cluster: methods coexist, members are equal once admitted ───────

    function test_dstackAndOperatorMembersCoexist() public {
        (address mDstack,) = _registerDstack(COMP3, 3, "xpub-A", "wg-A", 0);

        address ownerKey = vm.addr(0xBEEF08);
        (address mOp, IOperatorFacet.OperatorProof memory proof) =
            _operatorMemberAndProof("salt-op-8", ownerKey, uint64(block.timestamp + 1 days));
        IOperatorFacet(cluster).operator_register(proof, mOp, bytes32("xpub-op"), bytes32("wg-op"));

        assertEq(IAttest(cluster).memberCount(), 2);
        assertTrue(IAttest(cluster).isClusterMember(mDstack));
        assertTrue(IAttest(cluster).isClusterMember(mOp));
        // Admission method is observable per member (off-chain consumers filter on it).
        assertEq(IAttest(cluster).memberOf(mDstack).attestorId, DSTACK_ATTESTOR_ID);
        assertEq(IAttest(cluster).memberOf(mOp).attestorId, OPERATOR_ATTESTOR_ID);
    }

    // ── runbook: owner adds OperatorFacet to a live dstack-only cluster ────────

    function test_runbookAddOperatorFacetToDstackOnlyCluster() public {
        AttestorConfig[] memory dstackOnly = new AttestorConfig[](1);
        dstackOnly[0] = AttestorConfig({ facet: dstackFacetImpl, initData: _dstackBlob() });
        address c = clusterFactory.deployCluster(_coreArgs(), dstackOnly, keccak256("c-d-only"));

        // Operator selectors are absent before the cut.
        vm.expectRevert();
        IOperatorFacet(c).operatorSigners();

        // The owner accepts the solidstate nomination, then cuts the operator facet
        // in with its manifest + initAttestor as the cut's init step — the explicit
        // runbook action that changes this cluster's trust model (operator-admitted
        // members are key-vouched, not hardware-attested).
        ISafeOwnable(c).acceptOwnership();
        IERC2535DiamondCutInternal.FacetCut[] memory cuts =
            new IERC2535DiamondCutInternal.FacetCut[](1);
        cuts[0] = IERC2535DiamondCutInternal.FacetCut({
            target: operatorFacetImpl,
            action: IERC2535DiamondCutInternal.FacetCutAction.ADD,
            selectors: OperatorFacet(operatorFacetImpl).selectorManifest()
        });
        address[] memory signers = new address[](1);
        signers[0] = opSigner;
        IERC2535DiamondCut(c)
            .diamondCut(
                cuts,
                operatorFacetImpl,
                abi.encodeWithSelector(OperatorFacet.initAttestor.selector, abi.encode(signers))
            );

        assertTrue(IOperatorFacet(c).isOperatorSigner(opSigner));

        // And the method works end-to-end on the upgraded cluster.
        address m = memberFactory.deployMember(c, keccak256("salt-runbook"));
        uint64 expiry = uint64(block.timestamp + 1 days);
        address ownerKey = vm.addr(0xBEEF09);
        IOperatorFacet.OperatorProof memory proof =
            _signProofFor(c, m, ownerKey, expiry, opSignerPriv);
        bytes32 memberId =
            IOperatorFacet(c).operator_register(proof, m, bytes32("xpub-op"), bytes32("wg-op"));
        assertEq(IAttest(c).memberById(memberId).attestorId, OPERATOR_ATTESTOR_ID);
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    function _coreArgs() internal view returns (DiamondInitV2.CoreInitArgs memory) {
        return DiamondInitV2.CoreInitArgs({
            clusterOwner: address(this),
            meshCidrIp: 0x0a0d0000, // 10.13.0.0
            meshCidrPrefix: 16,
            memberFactory: address(memberFactory)
        });
    }

    function _dstackBlob() internal view returns (bytes memory) {
        bytes32[] memory composes = new bytes32[](1);
        composes[0] = COMPOSE;
        bytes32[] memory devices = new bytes32[](1);
        devices[0] = DEVICE;
        return abi.encode(
            DstackFacet.DstackInitArgs({
                kmsRootSigner: kms.rootAddress(),
                initialComposeHashes: composes,
                initialDeviceIds: devices,
                allowAnyDevice: false,
                requireTcbUpToDate: false
            })
        );
    }

    function _operatorBlob() internal view returns (bytes memory) {
        address[] memory signers = new address[](1);
        signers[0] = opSigner;
        return abi.encode(signers);
    }

    function _bothAttestors() internal view returns (AttestorConfig[] memory attestors) {
        attestors = new AttestorConfig[](2);
        attestors[0] = AttestorConfig({ facet: dstackFacetImpl, initData: _dstackBlob() });
        attestors[1] = AttestorConfig({ facet: operatorFacetImpl, initData: _operatorBlob() });
    }

    function _registerDstack(
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

    function _operatorMemberAndProof(string memory salt, address ownerKey, uint64 expiry)
        internal
        returns (address m, IOperatorFacet.OperatorProof memory proof)
    {
        return _operatorMemberAndProofSigned(salt, ownerKey, expiry, opSignerPriv);
    }

    function _operatorMemberAndProofSigned(
        string memory salt,
        address ownerKey,
        uint64 expiry,
        uint256 signerPriv
    ) internal returns (address m, IOperatorFacet.OperatorProof memory proof) {
        m = memberFactory.deployMember(cluster, keccak256(bytes(salt)));
        proof = _signProof(m, ownerKey, expiry, signerPriv);
    }

    function _signProof(address m, address ownerKey, uint64 expiry, uint256 signerPriv)
        internal
        view
        returns (IOperatorFacet.OperatorProof memory)
    {
        return _signProofFor(cluster, m, ownerKey, expiry, signerPriv);
    }

    /// The voucher: EIP-191 signature over the operator bind preimage (xpub-op/wg-op
    /// are the keys every operator test registers with).
    function _signProofFor(
        address cluster_,
        address m,
        address ownerKey,
        uint64 expiry,
        uint256 signerPriv
    ) internal pure returns (IOperatorFacet.OperatorProof memory) {
        bytes32 bindHash = keccak256(
            abi.encode(
                OPERATOR_BIND_DOMAIN,
                cluster_,
                m,
                bytes32("xpub-op"),
                bytes32("wg-op"),
                ownerKey,
                expiry
            )
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerPriv, bindHash.toEthSignedMessageHash());
        return IOperatorFacet.OperatorProof({
            signer: vm.addr(signerPriv),
            ownerKey: ownerKey,
            expiry: expiry,
            signature: abi.encodePacked(r, s, v)
        });
    }
}
