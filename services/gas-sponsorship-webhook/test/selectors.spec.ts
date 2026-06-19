import { describe, it, expect } from "vitest";
import { toFunctionSelector } from "viem";

import {
  ALLOWED_SELECTORS,
  ALLOWED_SIGNATURES,
  CORE_SELECTORS,
  CORE_SIGNATURES,
  DSTACK_SELECTORS,
  DSTACK_SIGNATURES,
  EXECUTE_SELECTOR,
  OPERATOR_SELECTORS,
  OPERATOR_SIGNATURES,
  isAllowedInnerSelector,
} from "../src/selectors.js";

describe("selectors", () => {
  it("execute selector equals the EIP-4337 canonical 0xb61d27f6", () => {
    expect(EXECUTE_SELECTOR).toBe("0xb61d27f6");
  });

  it("dstack_register selector is computed over the EXPANDED struct tuple", () => {
    // Sanity: the allowlist must contain the tuple-expanded selector (0x537d491c),
    // NOT a selector computed against the struct *name* (which viem cannot hash).
    const expanded = toFunctionSelector(
      "dstack_register((bytes32,bytes32,bytes,bytes,bytes,bytes,bytes,string),address,bytes32,bytes32)",
    );
    expect(expanded).toBe("0x537d491c");
    expect(DSTACK_SELECTORS.has(expanded)).toBe(true);
  });

  it("operator_register selector is computed over the EXPANDED struct tuple", () => {
    // Cross-language pin (contracts Selectors.t.sol + sidecar abi.rs use the
    // same literal): operator_register((address,address,uint64,bytes),...).
    const expanded = toFunctionSelector(
      "operator_register((address,address,uint64,bytes),address,bytes32,bytes32)",
    );
    expect(expanded).toBe("0x57977c40");
    expect(OPERATOR_SELECTORS.has(expanded)).toBe(true);
  });

  it("is partitioned per attestation method with no collisions", () => {
    expect(CORE_SIGNATURES).toHaveLength(8);
    expect(DSTACK_SIGNATURES).toHaveLength(9);
    expect(OPERATOR_SIGNATURES).toHaveLength(3);
    expect(ALLOWED_SIGNATURES).toHaveLength(20);
    // Distinct selectors across the union — no hash collisions and no overlap
    // between the method sets (a collision would mis-route the operator gate).
    expect(ALLOWED_SELECTORS.size).toBe(20);
    expect(CORE_SELECTORS.size + DSTACK_SELECTORS.size + OPERATOR_SELECTORS.size).toBe(20);
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

  it("gates the operator set on sponsorOperatorMethod (default off)", () => {
    const operatorRegister = toFunctionSelector(
      "operator_register((address,address,uint64,bytes),address,bytes32,bytes32)",
    );
    const addSigner = toFunctionSelector("addOperatorSigner(address)");

    // Default and explicit-off: operator-method traffic is NOT sponsored.
    expect(isAllowedInnerSelector(operatorRegister)).toBe(false);
    expect(isAllowedInnerSelector(addSigner, { sponsorOperatorMethod: false })).toBe(false);

    // Opt-in: sponsored.
    expect(isAllowedInnerSelector(operatorRegister, { sponsorOperatorMethod: true })).toBe(true);
    expect(isAllowedInnerSelector(addSigner, { sponsorOperatorMethod: true })).toBe(true);

    // The flag must not affect core/dstack traffic.
    expect(isAllowedInnerSelector("0x4979ff72", { sponsorOperatorMethod: false })).toBe(true);
    expect(isAllowedInnerSelector("0x537d491c", { sponsorOperatorMethod: false })).toBe(true);
  });
});
