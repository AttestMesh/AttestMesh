/**
 * Decoding of the Alchemy webhook body and the inner `execute(...)` calldata
 * (spec §5.1, §6 steps 4-6).
 *
 * The outer call a ClusterMember makes is always
 * `execute(address target, uint256 value, bytes data)`. We decode that, then peel
 * the first 4 bytes off `data` to get the inner selector the policy allowlists.
 *
 * All ABI work goes through viem so encoding/decoding round-trips exactly and we
 * never hand-parse hex offsets.
 */

import {
  decodeFunctionData,
  encodeFunctionData,
  getAddress,
  isHex,
  size,
  slice,
  type Address,
  type Hex,
} from "viem";

import { EXECUTE_SELECTOR, type Selector } from "./selectors.js";

/** Minimal ABI for the outer ClusterMember call. */
export const EXECUTE_ABI = [
  {
    type: "function",
    name: "execute",
    stateMutability: "nonpayable",
    inputs: [
      { name: "target", type: "address" },
      { name: "value", type: "uint256" },
      { name: "data", type: "bytes" },
    ],
    outputs: [],
  },
] as const;

/** The Alchemy `userOperation` object (EntryPoint v0.7 unpacked shape, spec §5.1). */
export interface UserOperation {
  sender: Hex;
  callData: Hex;
  // Remaining fields are present in Alchemy's body but irrelevant to policy.
  nonce?: Hex;
  factory?: Hex | null;
  factoryData?: Hex | null;
  callGasLimit?: Hex;
  verificationGasLimit?: Hex;
  preVerificationGas?: Hex;
  maxFeePerGas?: Hex;
  maxPriorityFeePerGas?: Hex;
  paymaster?: Hex | null;
  paymasterVerificationGasLimit?: Hex | null;
  paymasterPostOpGasLimit?: Hex | null;
  paymasterData?: Hex | null;
  signature?: Hex;
}

/** Top-level Alchemy custom-rules webhook body (spec §5.1). */
export interface AlchemyWebhookBody {
  userOperation: UserOperation;
  entryPoint?: Hex;
  /** Hex-encoded chain id, e.g. `0x14a34`. */
  chainId?: Hex;
}

/** Result of decoding the outer `execute(...)` call. */
export interface DecodedExecute {
  target: Address;
  value: bigint;
  /** The inner calldata `execute` will forward to `target`. */
  data: Hex;
  /** First 4 bytes of {@link data}, lower-cased. `0x` if `data` is shorter than 4 bytes. */
  innerSelector: Selector;
}

export class DecodeError extends Error {
  override readonly name = "DecodeError";
}

function assertHex(value: unknown, label: string): asserts value is Hex {
  if (typeof value !== "string" || !isHex(value)) {
    throw new DecodeError(`${label} must be 0x-hex`);
  }
}

/**
 * Extract the leading 4-byte selector of an arbitrary calldata blob.
 * Returns `0x` when there are fewer than 4 bytes (e.g. a bare ETH transfer).
 */
export function selectorOf(data: Hex): Selector {
  if (size(data) < 4) return "0x" as Selector;
  return slice(data, 0, 4).toLowerCase() as Selector;
}

/**
 * Decode an outer `execute(address,uint256,bytes)` calldata into its arguments
 * plus the inner selector. Throws {@link DecodeError} if `callData` is not a
 * well-formed `execute` call.
 */
export function decodeExecute(callData: Hex): DecodedExecute {
  assertHex(callData, "callData");

  if (size(callData) < 4) {
    throw new DecodeError("callData shorter than 4-byte selector");
  }
  const outerSelector = selectorOf(callData);
  if (outerSelector !== EXECUTE_SELECTOR) {
    throw new DecodeError(`outer selector ${outerSelector} is not execute (${EXECUTE_SELECTOR})`);
  }

  let decoded;
  try {
    decoded = decodeFunctionData({ abi: EXECUTE_ABI, data: callData });
  } catch (cause) {
    throw new DecodeError(`callData is not a valid execute(...) call: ${(cause as Error).message}`);
  }

  const [target, value, data] = decoded.args as readonly [Address, bigint, Hex];
  return {
    target: getAddress(target),
    value,
    data,
    innerSelector: selectorOf(data),
  };
}

/** Parse and shallow-validate the Alchemy webhook JSON body. */
export function parseWebhookBody(raw: unknown): AlchemyWebhookBody {
  if (typeof raw !== "object" || raw === null) {
    throw new DecodeError("body must be a JSON object");
  }
  const body = raw as Record<string, unknown>;
  const userOperation = body["userOperation"];
  if (typeof userOperation !== "object" || userOperation === null) {
    throw new DecodeError("body.userOperation missing");
  }
  const uo = userOperation as Record<string, unknown>;
  assertHex(uo["sender"], "userOperation.sender");
  assertHex(uo["callData"], "userOperation.callData");

  const chainId = body["chainId"];
  if (chainId !== undefined) assertHex(chainId, "chainId");

  return body as unknown as AlchemyWebhookBody;
}

/**
 * Build `execute(target, value, innerCallData)` calldata. The round-trip inverse
 * of {@link decodeExecute}; used by tests and by callers constructing fixtures.
 */
export function encodeExecute(target: Address, value: bigint, innerCallData: Hex): Hex {
  return encodeFunctionData({
    abi: EXECUTE_ABI,
    functionName: "execute",
    args: [target, value, innerCallData],
  });
}
