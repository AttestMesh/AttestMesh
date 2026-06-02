// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

// Canonical AttestMesh error declarations (contracts spec §13).
// All custom errors live here and are imported where used so revert sigids stay
// stable across compilations.

// ── Membership ──────────────────────────────────────────────────────────────
error NotOurMember();
error AlreadyRegistered();
error NotClusterMember();

// ── Dstack KMS chain ────────────────────────────────────────────────────────
error KmsRootNotAllowed();
error KmsAppKeySigInvalid();
error AppKeyDerivedSigInvalid();

// ── Dstack allowlist ────────────────────────────────────────────────────────
error ComposeHashNotAllowed();
error DeviceNotAllowed();
error TcbStale();

// ── Binding ─────────────────────────────────────────────────────────────────
error BindingSigInvalid();

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
error OwnerAlreadySet();
error InvalidBootstrapCall();
error NotInternalCall();
