/**
 * The sponsorship policy (spec §6). Seven checks, in order, first failure
 * short-circuits. Steps 1-6 are free (string/number/selector compares); only
 * steps 3 and 7 touch the chain, and those go through the cached provenance
 * helpers (one cached eth_call each, rare miss → one RPC roundtrip).
 *
 * The reason strings are exactly the §5.1 enumeration:
 *   bad-token, chain-mismatch, not-cluster-member, outer-selector-not-execute,
 *   value-nonzero, inner-selector-not-allowed, target-not-our-cluster, rpc-failure.
 */

import { fromHex, type Hex } from "viem";

import type { Config } from "./env.js";
import { DecodeError, decodeExecute, type UserOperation } from "./decode.js";
import { isAllowedInnerSelector } from "./selectors.js";
import {
  isAllowlistedAppId,
  isDeployedCluster,
  isOurMember,
  RpcFailureError,
  type ProvenanceDeps,
} from "./provenance.js";

export type DenyReason =
  | "bad-token"
  | "chain-mismatch"
  | "not-cluster-member"
  | "outer-selector-not-execute"
  | "value-nonzero"
  | "inner-selector-not-allowed"
  | "target-not-our-cluster"
  | "rpc-failure";

export type PolicyDecision = { approved: true } | { approved: false; reason: DenyReason };

const APPROVE: PolicyDecision = { approved: true };
function deny(reason: DenyReason): PolicyDecision {
  return { approved: false, reason };
}

/**
 * Constant-time string equality (spec §5.1, §6 step 1).
 *
 * Compares every byte with no data-dependent early return. Length is mixed into
 * the accumulator (rather than short-circuiting) so a length mismatch can't be
 * distinguished by timing; we iterate over the longer of the two and treat any
 * out-of-range byte as a guaranteed-nonzero diff.
 */
export function constantTimeEqual(a: string, b: string): boolean {
  const ba = new TextEncoder().encode(a);
  const bb = new TextEncoder().encode(b);
  const len = Math.max(ba.length, bb.length);
  // Seed with the length delta so differing lengths always fail, in constant time.
  let diff = ba.length ^ bb.length;
  for (let i = 0; i < len; i++) {
    const xa = i < ba.length ? ba[i]! : 0;
    const xb = i < bb.length ? bb[i]! : 0;
    diff |= xa ^ xb;
  }
  return diff === 0;
}

/** Parse Alchemy's hex chainId. Returns NaN on anything non-hex so step 2 denies. */
function parseChainId(raw: Hex | undefined): number {
  if (raw === undefined) return Number.NaN;
  try {
    return fromHex(raw, "number");
  } catch {
    return Number.NaN;
  }
}

/** Inputs to {@link evaluatePolicy}. */
export interface PolicyInput {
  /** Token supplied by Alchemy in `?token=...`; `null` when the param is absent. */
  token: string | null;
  /** Hex-encoded chain id from the webhook body. */
  chainId: Hex | undefined;
  userOperation: UserOperation;
}

/**
 * Run the full policy. `provenance` carries config + KV + logger and is used for
 * the two on-chain checks. Any {@link RpcFailureError} surfaced from those checks
 * is mapped to `rpc-failure` (spec §9).
 */
export async function evaluatePolicy(
  input: PolicyInput,
  config: Config,
  provenance: ProvenanceDeps,
): Promise<PolicyDecision> {
  // 1. Token — constant-time compare.
  if (input.token === null || !constantTimeEqual(input.token, config.alchemyWebhookToken)) {
    return deny("bad-token");
  }

  // 2. Chain id.
  if (parseChainId(input.chainId) !== config.expectedChainId) {
    return deny("chain-mismatch");
  }

  // 3. Sender provenance (cached eth_call). A sponsorable sender is either a factory-minted
  //    ClusterMember, or a Path A member — a dstack app upgraded to ClusterMember whose
  //    cluster has owner-allowlisted its app_id (isOurMember is false for those, since the
  //    member contract is the dstack-provisioned app_id, not a factory deployment).
  try {
    const sender = input.userOperation.sender;
    const sponsorable =
      (await isOurMember(provenance, sender)) || (await isAllowlistedAppId(provenance, sender));
    if (!sponsorable) {
      return deny("not-cluster-member");
    }
  } catch (err) {
    if (err instanceof RpcFailureError) return deny("rpc-failure");
    throw err;
  }

  // 4. Outer selector must be execute(address,uint256,bytes), AND the call must
  //    decode cleanly. decodeExecute enforces the 0xb61d27f6 prefix and that the
  //    ABI body is well-formed; either failure is a DecodeError → step-4 deny.
  let decoded;
  try {
    decoded = decodeExecute(input.userOperation.callData);
  } catch (err) {
    if (err instanceof DecodeError) {
      return deny("outer-selector-not-execute");
    }
    throw err;
  }

  // 5. Value must be zero.
  if (decoded.value !== 0n) {
    return deny("value-nonzero");
  }

  // 6. Inner selector in allowlist (the operator-method set is gated by
  //    SPONSOR_OPERATOR_METHOD — multi-attestor spec).
  if (
    !isAllowedInnerSelector(decoded.innerSelector, {
      sponsorOperatorMethod: config.sponsorOperatorMethod,
    })
  ) {
    return deny("inner-selector-not-allowed");
  }

  // 7. Target provenance (cached eth_call).
  try {
    if (!(await isDeployedCluster(provenance, decoded.target))) {
      return deny("target-not-our-cluster");
    }
  } catch (err) {
    if (err instanceof RpcFailureError) return deny("rpc-failure");
    throw err;
  }

  return APPROVE;
}
