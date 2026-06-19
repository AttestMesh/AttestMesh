// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

// Canonical AttestMesh error declarations (contracts spec §13).
// All custom errors live here and are imported where used so revert sigids stay
// stable across compilations.

// ── Membership ──────────────────────────────────────────────────────────────
error NotOurMember();
error AlreadyRegistered();
error NotClusterMember();

// ── Dstack registration ───────────────────────────────────────────────────────
// The KMS sig-chain itself reverts with DstackSigChain.InvalidSigChain; these guard
// the facet-level binding of the proof to this member + registration message. Compose
// hash / device / TCB are enforced at the KMS boot gate (isAppAllowed), not here.
error CodeIdMismatch(); // proof.codeId != bytes20(memberContract)
error BindingMismatch(); // proof.messageHash != keccak256(bind domain, cluster, member, xPub, wgPub)

// ── Operator registration ───────────────────────────────────────────────────
// OperatorFacet admits members on an allowlisted operator's signature, not hardware
// attestation; these guard the voucher. BindingMismatch is shared with dstack for
// the signature-vs-preimage check.
error OperatorSignerNotAllowed(); // proof.signer not in OperatorStorage.signers
error VoucherExpired(); // block.timestamp > proof.expiry

// ── Multi-attestor framework ────────────────────────────────────────────────
error AttestorNotApproved(); // factory v2: facet not in the approved-attestor set
error NotDiamondContext(); // initAttestor called outside an initialized diamond
error NotFactoryOwner();

// ── Messaging ───────────────────────────────────────────────────────────────
error DuplicateEnvelope();
error RecipientNotMember();

// ── CSK commitment ──────────────────────────────────────────────────────────
error NotOriginator();
error CskCommitmentAlreadySet();

// ── Admin ───────────────────────────────────────────────────────────────────
error NotClusterOwner();
error ClusterDestroyed(); // reserved for milestone B; not used in v1

// ── Member contract / EIP-4337 ──────────────────────────────────────────────
error OnlyEntryPoint();
error OnlyCluster();
error OwnerAlreadySet(); // unused since the multi-attestor skip-if-set change; kept for ABI tooling
error InvalidBootstrapCall();
error NotInternalCall();
