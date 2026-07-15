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
error AlreadyBound();
error OwnerAlreadySet();
error InvalidBootstrapCall();
error NotInternalCall();
