/**
 * The inner-selector allowlist (spec §6 step 6, §7) and the outer `execute`
 * selector (spec §6 step 4).
 *
 * Every selector is derived at module load from its canonical signature via
 * viem's `toFunctionSelector` (keccak under the hood) — nothing is hand-rolled.
 *
 * NOTE on `dstack_register`: the on-chain function takes a `DstackProof` struct.
 * A 4-byte selector is computed over the *expanded tuple*, so the canonical
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

/**
 * Canonical signatures of every cluster operation the operator will sponsor (spec §7).
 * Order is documentation only; lookups go through {@link ALLOWED_SELECTORS}.
 */
export const ALLOWED_SIGNATURES = [
  // Member-driven operations:
  `dstack_register(${DSTACK_PROOF_TUPLE},address,bytes32,bytes32)`,
  "publishWgKey(bytes32)",
  "publishEd25519Key(bytes32)", // member publishes its heartbeat key on chain (ed25519-onchain-key)
  "send(bytes32,bytes32,bytes)",
  "setCskCommitment(bytes32)", // originator publishes keccak256(CSK) once (master §8.1)

  // Cluster ownership transitions (deploy + Safe rotation):
  "transferClusterOwnership(address)",
  "acceptClusterOwnership()",
  "transferBothOwners(address)",
  "acceptBothOwners()",
  "acceptOwnership()", // solidstate SafeOwnable accept side

  // dstack allowlist mutations (ops Safe operated via 4337):
  "addComposeHash(bytes32)",
  "removeComposeHash(bytes32)",
  "addDevice(bytes32)",
  "removeDevice(bytes32)",
  "setAllowAnyDevice(bool)",
  "setRequireTcbUpToDate(bool)",
  "addAllowedKmsRoot(address)",
  "removeAllowedKmsRoot(address)",
] as const;

/** The 4-byte selectors of {@link ALLOWED_SIGNATURES}, for O(1) membership tests. */
export const ALLOWED_SELECTORS: ReadonlySet<Selector> = new Set(
  ALLOWED_SIGNATURES.map((sig) => toFunctionSelector(sig)),
);

/**
 * Outer-call selector: `ClusterMember.execute(address,uint256,bytes)`.
 * Asserted at load time to equal the EIP-4337 canonical `0xb61d27f6` (spec §6 step 4).
 */
export const EXECUTE_SELECTOR: Selector = toFunctionSelector("execute(address,uint256,bytes)");

if (EXECUTE_SELECTOR !== "0xb61d27f6") {
  throw new Error(`execute selector mismatch: expected 0xb61d27f6, computed ${EXECUTE_SELECTOR}`);
}

/** True iff `selector` (lower-cased `0x`-hex) is in the sponsorship allowlist. */
export function isAllowedInnerSelector(selector: Selector): boolean {
  return ALLOWED_SELECTORS.has(selector.toLowerCase() as Selector);
}
