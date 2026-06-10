/**
 * The inner-selector allowlist (spec §6 step 6, §7) and the outer `execute`
 * selector (spec §6 step 4).
 *
 * Partitioned per attestation method (multi-attestor spec): `CORE_SELECTORS` are
 * method-agnostic cluster operations, `DSTACK_SELECTORS` belong to DstackFacet,
 * and `OPERATOR_SELECTORS` belong to OperatorFacet. The operator set is gated by
 * `SPONSOR_OPERATOR_METHOD` so an operator can stop paying for operator-method
 * traffic independently. TRUST NOTE: operator-method members are vouched for by
 * an allowlisted operator key, not hardware attestation.
 *
 * Every selector is derived at module load from its canonical signature via
 * viem's `toFunctionSelector` (keccak under the hood) — nothing is hand-rolled.
 *
 * NOTE on the register selectors: the on-chain functions take proof structs. A
 * 4-byte selector is computed over the *expanded tuple*, so the canonical
 * signature must list the struct's field types in order, not the struct name.
 */

import { toFunctionSelector, type Hex } from "viem";

/** A 4-byte function selector, lower-cased `0x`-prefixed hex (10 chars). */
export type Selector = Hex;

/**
 * `DstackProof` expanded to its tuple of field types. Mirrors the struct used by
 * `DstackFacet.dstack_register(...)` in contracts. If the struct layout changes,
 * this tuple (and therefore the selector) changes with it.
 */
const DSTACK_PROOF_TUPLE = "(bytes32,bytes32,bytes,bytes,bytes,bytes,bytes,string)";

/** `OperatorProof` expanded: (signer, ownerKey, expiry, signature). */
const OPERATOR_PROOF_TUPLE = "(address,address,uint64,bytes)";

/** Method-agnostic cluster operations (core facets + ownership transitions). */
export const CORE_SIGNATURES = [
  // Member-driven operations:
  "publishWgKey(bytes32)",
  "send(bytes32,bytes32,bytes)",
  "setCskCommitment(bytes32)", // originator publishes keccak256(CSK) once (master §8.1)

  // Cluster ownership transitions (deploy + Safe rotation):
  "transferClusterOwnership(address)",
  "acceptClusterOwnership()",
  "transferBothOwners(address)",
  "acceptBothOwners()",
  "acceptOwnership()", // solidstate SafeOwnable accept side
] as const;

/** DstackFacet surface (registration + dstack allowlist admin). */
export const DSTACK_SIGNATURES = [
  `dstack_register(${DSTACK_PROOF_TUPLE},address,bytes32,bytes32)`,
  "addComposeHash(bytes32)",
  "removeComposeHash(bytes32)",
  "addDevice(bytes32)",
  "removeDevice(bytes32)",
  "setAllowAnyDevice(bool)",
  "setRequireTcbUpToDate(bool)",
  "addAllowedKmsRoot(address)",
  "removeAllowedKmsRoot(address)",
] as const;

/** OperatorFacet surface (registration + signer-allowlist admin). */
export const OPERATOR_SIGNATURES = [
  `operator_register(${OPERATOR_PROOF_TUPLE},address,bytes32,bytes32)`,
  "addOperatorSigner(address)",
  "removeOperatorSigner(address)",
] as const;

function toSet(signatures: readonly string[]): ReadonlySet<Selector> {
  return new Set(signatures.map((sig) => toFunctionSelector(sig)));
}

export const CORE_SELECTORS: ReadonlySet<Selector> = toSet(CORE_SIGNATURES);
export const DSTACK_SELECTORS: ReadonlySet<Selector> = toSet(DSTACK_SIGNATURES);
export const OPERATOR_SELECTORS: ReadonlySet<Selector> = toSet(OPERATOR_SIGNATURES);

/**
 * Canonical signatures of every operation the webhook can ever sponsor, all
 * methods included (documentation + collision checks; policy decisions go
 * through {@link isAllowedInnerSelector} which applies the operator gate).
 */
export const ALLOWED_SIGNATURES = [
  ...CORE_SIGNATURES,
  ...DSTACK_SIGNATURES,
  ...OPERATOR_SIGNATURES,
] as const;

/** Union selector set of {@link ALLOWED_SIGNATURES} (ungated). */
export const ALLOWED_SELECTORS: ReadonlySet<Selector> = toSet(ALLOWED_SIGNATURES);

/**
 * Outer-call selector: `ClusterMember.execute(address,uint256,bytes)`.
 * Asserted at load time to equal the EIP-4337 canonical `0xb61d27f6` (spec §6 step 4).
 */
export const EXECUTE_SELECTOR: Selector = toFunctionSelector("execute(address,uint256,bytes)");

if (EXECUTE_SELECTOR !== "0xb61d27f6") {
  throw new Error(`execute selector mismatch: expected 0xb61d27f6, computed ${EXECUTE_SELECTOR}`);
}

/** Options for {@link isAllowedInnerSelector}. */
export interface SelectorPolicyOptions {
  /** Sponsor OperatorFacet traffic (`SPONSOR_OPERATOR_METHOD`, default false). */
  sponsorOperatorMethod: boolean;
}

/**
 * True iff `selector` (lower-cased `0x`-hex) is sponsorable: always for the core
 * and dstack sets, and for the operator set only when the flag enables it.
 */
export function isAllowedInnerSelector(
  selector: Selector,
  opts: SelectorPolicyOptions = { sponsorOperatorMethod: false },
): boolean {
  const s = selector.toLowerCase() as Selector;
  if (CORE_SELECTORS.has(s) || DSTACK_SELECTORS.has(s)) return true;
  return opts.sponsorOperatorMethod && OPERATOR_SELECTORS.has(s);
}
