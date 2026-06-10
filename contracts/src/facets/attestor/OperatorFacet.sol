// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { ECDSA } from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import { MessageHashUtils } from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import { EnumerableSet } from "@openzeppelin/contracts/utils/structs/EnumerableSet.sol";

import { IOperatorFacet } from "../../interfaces/IOperatorFacet.sol";
import { IAttestorFacet } from "../../interfaces/IAttestorFacet.sol";
import { IAttest } from "../../interfaces/IAttest.sol";
import { INetwork } from "../../interfaces/INetwork.sol";
import { IClusterMember } from "../../interfaces/IClusterMember.sol";
import { IClusterMemberFactory } from "../../interfaces/IClusterMemberFactory.sol";

import { MemberStorage } from "../../storage/MemberStorage.sol";
import { OperatorStorage } from "../../storage/OperatorStorage.sol";
import { ClusterAccess } from "../../access/ClusterAccess.sol";

import {
    NotOurMember,
    BindingMismatch,
    OperatorSignerNotAllowed,
    VoucherExpired,
    NotDiamondContext
} from "../../errors/Errors.sol";

/// @title OperatorFacet — the operator-signature attestor facet (multi-attestor spec).
/// @notice Admits a member on an allowlisted operator's ECDSA voucher over the
///         standard bind preimage. TRUST DISCLOSURE (load-bearing): members admitted
///         here are vouched for by a key, NOT by hardware attestation — installing
///         this facet changes the cluster's trust model, and `isClusterMember`
///         treats all members identically once admitted. Intended for dev/test
///         clusters and non-TEE nodes admitted by operator fiat (master spec §5.1:
///         a cluster picks its attestation policy by picking its facets).
contract OperatorFacet is IOperatorFacet, IAttestorFacet, ClusterAccess {
    using MessageHashUtils for bytes32;
    using EnumerableSet for EnumerableSet.AddressSet;

    bytes32 public constant OPERATOR_ATTESTOR_ID = keccak256("attestmesh.attestor.operator");
    string internal constant OPERATOR_BIND_DOMAIN = "attestmesh.operator.bind.v1";

    // ── IAttestorFacet (multi-attestor spec) ──────────────────────────────────

    function attestorId() external pure returns (bytes32) {
        return OPERATOR_ATTESTOR_ID;
    }

    /// @notice The selectors cut into a diamond for this facet. The IAttestorFacet
    ///         selectors are impl-level only (same precedent as DstackFacet).
    function selectorManifest() external pure returns (bytes4[] memory s) {
        s = new bytes4[](5);
        s[0] = IOperatorFacet.operator_register.selector;
        s[1] = IOperatorFacet.addOperatorSigner.selector;
        s[2] = IOperatorFacet.removeOperatorSigner.selector;
        s[3] = IOperatorFacet.operatorSigners.selector;
        s[4] = IOperatorFacet.isOperatorSigner.selector;
    }

    /// @notice Seed the initial operator signer set. Delegatecall-only (same storage
    ///         sentinel as DstackFacet.initAttestor: core storage seeds clusterOwner
    ///         before any initAttestor runs). Blob: `abi.encode(address[] signers)`.
    function initAttestor(bytes calldata initData) external {
        if (MemberStorage.layout().clusterOwner == address(0)) revert NotDiamondContext();
        address[] memory signers = abi.decode(initData, (address[]));
        OperatorStorage.Layout storage o = OperatorStorage.layout();
        for (uint256 i; i < signers.length; ++i) {
            if (o.signers.add(signers[i])) {
                emit OperatorSignerAdded(signers[i]);
            }
        }
    }

    // ── Operator signer allowlist admin ───────────────────────────────────────

    function addOperatorSigner(address signer) external onlyClusterOwner {
        if (OperatorStorage.layout().signers.add(signer)) {
            emit OperatorSignerAdded(signer);
        }
    }

    function removeOperatorSigner(address signer) external onlyClusterOwner {
        if (OperatorStorage.layout().signers.remove(signer)) {
            emit OperatorSignerRemoved(signer);
        }
    }

    function operatorSigners() external view returns (address[] memory) {
        return OperatorStorage.layout().signers.values();
    }

    function isOperatorSigner(address signer) external view returns (bool) {
        return OperatorStorage.layout().signers.contains(signer);
    }

    // ── Registration (mirrors DstackFacet.dstack_register step-for-step) ──────

    function operator_register(
        OperatorProof calldata proof,
        address memberContract,
        bytes32 xPubKey,
        bytes32 wgPubKey
    ) external returns (bytes32 memberId) {
        // 1. memberContract is one of ours: factory-deployed. (DstackFacet's second
        //    anchor — the owner app_id allowlist — is a dstack-KMS Path A workaround
        //    and stays in DstackStorage; operator members have no KMS constraint
        //    forcing a foreign address, so they are factory-deployed by construction.)
        address factory = MemberStorage.layout().memberFactory;
        if (!IClusterMemberFactory(factory).isOurMember(memberContract)) revert NotOurMember();

        // 2. The voucher signer must be in the owner-managed allowlist, and the
        //    voucher must not have expired (expiry bounds its lifetime; replay for
        //    the same (memberContract, attestorId) is already impossible — memberId
        //    uniqueness in _addMember).
        if (!OperatorStorage.layout().signers.contains(proof.signer)) {
            revert OperatorSignerNotAllowed();
        }
        if (block.timestamp > proof.expiry) revert VoucherExpired();

        // 3. The signed voucher must bind exactly this (cluster, member, xPubKey,
        //    wgPubKey, ownerKey, expiry) — what dstack binds, plus the 4337 owner and
        //    the deadline. EIP-191 personal-sign, like dstack's binding signature.
        bytes32 bindHash = keccak256(
            abi.encode(
                OPERATOR_BIND_DOMAIN,
                address(this),
                memberContract,
                xPubKey,
                wgPubKey,
                proof.ownerKey,
                proof.expiry
            )
        );
        if (ECDSA.recover(bindHash.toEthSignedMessageHash(), proof.signature) != proof.signer) {
            revert BindingMismatch();
        }

        // 4. Write the member + canonical wg key (folded for atomicity).
        memberId = IAttest(address(this))
            ._addMember(
                MemberStorage.MemberRecord({
                attestorId: OPERATOR_ATTESTOR_ID,
                memberContract: memberContract,
                xPubKey: xPubKey,
                wgPubKey: wgPubKey,
                registeredAt: uint64(block.timestamp)
            })
            );
        INetwork(address(this))._setWgPubKey(memberId, wgPubKey);

        // 5. Install the voucher's ownerKey as the ClusterMember's EIP-4337 owner —
        //    same atomicity as dstack. Skip-if-set semantics live in the member
        //    (it emits OwnerSetSkipped when an owner already exists).
        IClusterMember(memberContract).__setOwnerFromCluster(proof.ownerKey);

        // 6. Emit operator-specific event (MemberRegistered already emitted by _addMember).
        emit OperatorMemberRegistered(memberId, proof.signer, proof.ownerKey);
    }
}
