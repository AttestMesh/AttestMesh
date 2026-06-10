// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";
import { MessageHashUtils } from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {
    PackedUserOperation
} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";

import { ClusterMember } from "../../src/members/ClusterMember.sol";
import { ClusterMemberFactory } from "../../src/members/ClusterMemberFactory.sol";
import { IDstackFacet } from "../../src/interfaces/IDstackFacet.sol";
import { IOperatorFacet } from "../../src/interfaces/IOperatorFacet.sol";

/// @notice Exercises ClusterMember.validateUserOp bootstrap + standard modes
///         without a live EntryPoint/bundler (contracts spec §9.1.2).
contract ClusterMemberUserOpTest is Test {
    ClusterMemberFactory internal factory;
    ClusterMember internal member;
    address internal cluster = address(0xC0FFEE);
    address internal entryPoint;

    uint256 internal constant BIND_PRIV = 0xB17D;
    bytes32 internal constant XPUB = bytes32("xpub");
    bytes32 internal constant WGPUB = bytes32("wgpub");

    function setUp() public {
        address impl = address(new ClusterMember());
        factory = new ClusterMemberFactory(impl, address(0xA11CE));
        member = ClusterMember(payable(factory.deployMember(cluster, keccak256("m1"))));
        entryPoint = member.ENTRY_POINT();
    }

    function test_bootstrapAcceptsMatchingSigner() public {
        bytes32 userOpHash = keccak256("uoh-1");
        PackedUserOperation memory op = _bootstrapOp(BIND_PRIV, BIND_PRIV, userOpHash);

        vm.prank(entryPoint);
        uint256 validationData = member.validateUserOp(op, userOpHash, 0);
        assertEq(validationData, 0, "should accept when userOp signer == binding signer");
    }

    function test_bootstrapRejectsMismatchedSigner() public {
        bytes32 userOpHash = keccak256("uoh-2");
        // UserOp signed by a different key than the binding.
        PackedUserOperation memory op = _bootstrapOp(BIND_PRIV, 0xBEEF, userOpHash);

        vm.prank(entryPoint);
        uint256 validationData = member.validateUserOp(op, userOpHash, 0);
        assertEq(validationData, 1, "should reject mismatched signer");
    }

    // ── operator-method bootstrap (multi-attestor spec): the bootstrap signer is
    // the ownerKey named inside the signed OperatorProof, not a recovered binding key.

    function test_operatorBootstrapAcceptsOwnerKeySigner() public {
        bytes32 userOpHash = keccak256("uoh-op-1");
        PackedUserOperation memory op = _operatorBootstrapOp(BIND_PRIV, BIND_PRIV, userOpHash);

        vm.prank(entryPoint);
        uint256 validationData = member.validateUserOp(op, userOpHash, 0);
        assertEq(validationData, 0, "should accept when userOp signer == proof.ownerKey");
    }

    function test_operatorBootstrapRejectsMismatchedSigner() public {
        bytes32 userOpHash = keccak256("uoh-op-2");
        // UserOp signed by a key other than the voucher's ownerKey.
        PackedUserOperation memory op = _operatorBootstrapOp(BIND_PRIV, 0xBEEF, userOpHash);

        vm.prank(entryPoint);
        uint256 validationData = member.validateUserOp(op, userOpHash, 0);
        assertEq(validationData, 1, "should reject signer != proof.ownerKey");
    }

    function test_operatorBootstrapRejectsForeignMemberContract() public {
        bytes32 userOpHash = keccak256("uoh-op-3");
        PackedUserOperation memory op = _operatorBootstrapOp(BIND_PRIV, BIND_PRIV, userOpHash);
        // Same shape, but the inner call registers a DIFFERENT member contract.
        IOperatorFacet.OperatorProof memory proof;
        proof.ownerKey = vm.addr(BIND_PRIV);
        bytes memory inner = abi.encodeWithSelector(
            IOperatorFacet.operator_register.selector, proof, address(0xD00D), XPUB, WGPUB
        );
        op.callData =
            abi.encodeWithSelector(ClusterMember.execute.selector, cluster, uint256(0), inner);

        vm.prank(entryPoint);
        vm.expectRevert(); // InvalidBootstrapCall
        member.validateUserOp(op, userOpHash, 0);
    }

    function test_standardModeAfterOwnerSet() public {
        // Cluster installs the owner (simulating the dstack_register callback).
        vm.prank(cluster);
        member.__setOwnerFromCluster(vm.addr(BIND_PRIV));

        bytes32 userOpHash = keccak256("uoh-3");
        PackedUserOperation memory op;
        op.sender = address(member);
        op.signature = _sign(BIND_PRIV, MessageHashUtils.toEthSignedMessageHash(userOpHash));

        vm.prank(entryPoint);
        assertEq(member.validateUserOp(op, userOpHash, 0), 0);

        // Wrong signer fails.
        op.signature = _sign(0xBEEF, MessageHashUtils.toEthSignedMessageHash(userOpHash));
        vm.prank(entryPoint);
        assertEq(member.validateUserOp(op, userOpHash, 0), 1);
    }

    function test_onlyEntryPointCanValidate() public {
        PackedUserOperation memory op;
        vm.expectRevert();
        member.validateUserOp(op, keccak256("x"), 0);
    }

    function test_executeOnlyEntryPoint() public {
        vm.expectRevert();
        member.execute(cluster, 0, "");
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    /// Build a bootstrap UserOp whose callData is execute(cluster, 0, dstack_register(...))
    /// with the binding sig from `bindPriv`, and the outer userOp signed by `opPriv`.
    function _bootstrapOp(uint256 bindPriv, uint256 opPriv, bytes32 userOpHash)
        internal
        view
        returns (PackedUserOperation memory op)
    {
        bytes32 messageHash =
            keccak256(abi.encode("attestmesh.bind.v1", cluster, address(member), XPUB, WGPUB));

        IDstackFacet.DstackProof memory proof;
        proof.messageHash = messageHash;
        proof.messageSignature =
            _sign(bindPriv, MessageHashUtils.toEthSignedMessageHash(messageHash));

        bytes memory inner = abi.encodeWithSelector(
            IDstackFacet.dstack_register.selector, proof, address(member), XPUB, WGPUB
        );
        bytes memory callData =
            abi.encodeWithSelector(ClusterMember.execute.selector, cluster, uint256(0), inner);

        op.sender = address(member);
        op.callData = callData;
        op.signature = _sign(opPriv, MessageHashUtils.toEthSignedMessageHash(userOpHash));
    }

    /// Build an operator bootstrap UserOp: callData = execute(cluster, 0,
    /// operator_register(proof, member, ...)) where proof.ownerKey = addr(ownerPriv),
    /// and the outer userOp signed by `opPriv`.
    function _operatorBootstrapOp(uint256 ownerPriv, uint256 opPriv, bytes32 userOpHash)
        internal
        view
        returns (PackedUserOperation memory op)
    {
        IOperatorFacet.OperatorProof memory proof;
        proof.signer = address(0x51493); // voucher contents are facet-verified on execution
        proof.ownerKey = vm.addr(ownerPriv);
        proof.expiry = type(uint64).max;

        bytes memory inner = abi.encodeWithSelector(
            IOperatorFacet.operator_register.selector, proof, address(member), XPUB, WGPUB
        );
        op.sender = address(member);
        op.callData =
            abi.encodeWithSelector(ClusterMember.execute.selector, cluster, uint256(0), inner);
        op.signature = _sign(opPriv, MessageHashUtils.toEthSignedMessageHash(userOpHash));
    }

    function _sign(uint256 priv, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(priv, digest);
        return abi.encodePacked(r, s, v);
    }
}
