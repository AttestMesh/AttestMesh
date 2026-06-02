import { describe, it, expect } from "vitest";
import { toFunctionSelector } from "viem";

import {
  ALLOWED_SELECTORS,
  ALLOWED_SIGNATURES,
  EXECUTE_SELECTOR,
  isAllowedInnerSelector,
} from "../src/selectors.js";

describe("selectors", () => {
  it("execute selector equals the EIP-4337 canonical 0xb61d27f6", () => {
    expect(EXECUTE_SELECTOR).toBe("0xb61d27f6");
  });

  it("dstack_register selector is computed over the EXPANDED struct tuple", () => {
    // Sanity: the allowlist must contain the tuple-expanded selector (0x3f330b63),
    // NOT a selector computed against the struct *name* (which viem cannot hash).
    const expanded = toFunctionSelector(
      "dstack_register((bytes,bytes,bytes,bytes32,bytes,bytes,bytes32,bytes32,string,string[],bytes),address,bytes32,bytes32)",
    );
    expect(expanded).toBe("0x3f330b63");
    expect(ALLOWED_SELECTORS.has(expanded)).toBe(true);
  });

  it("contains exactly the 17 allowlisted operations with no collisions", () => {
    expect(ALLOWED_SIGNATURES).toHaveLength(17);
    expect(ALLOWED_SELECTORS.size).toBe(17); // distinct selectors, no hash collisions
  });

  it("known selectors are present", () => {
    const expect4 = (sig: string, sel: string) => {
      expect(toFunctionSelector(sig)).toBe(sel);
      expect(isAllowedInnerSelector(sel as `0x${string}`)).toBe(true);
    };
    expect4("publishWgKey(bytes32)", "0x4979ff72");
    expect4("send(bytes32,bytes32,bytes)", "0x84076765");
    expect4("setCskCommitment(bytes32)", "0x3eeb8ee8");
    expect4("acceptOwnership()", "0x79ba5097");
  });

  it("isAllowedInnerSelector is case-insensitive", () => {
    expect(isAllowedInnerSelector("0x4979FF72")).toBe(true);
  });

  it("rejects selectors outside the allowlist", () => {
    expect(isAllowedInnerSelector("0xdeadbeef")).toBe(false);
    expect(isAllowedInnerSelector("0x00000000")).toBe(false);
    // execute itself is NOT an allowed *inner* selector.
    expect(isAllowedInnerSelector(EXECUTE_SELECTOR)).toBe(false);
  });
});
