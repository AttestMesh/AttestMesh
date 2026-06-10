import { describe, it, expect } from "vitest";
import { encodeFunctionData, type Address, type Hex } from "viem";

import {
  decodeExecute,
  encodeExecute,
  selectorOf,
  parseWebhookBody,
  DecodeError,
} from "../src/decode.js";
import { EXECUTE_SELECTOR } from "../src/selectors.js";

const CLUSTER: Address = "0x00000000000000000000000000000000000000C1";
const WG_KEY: Hex = `0x${"ab".repeat(32)}`;

// Inner call: publishWgKey(bytes32) on the cluster diamond.
const PUBLISH_WG_ABI = [
  {
    type: "function",
    name: "publishWgKey",
    stateMutability: "nonpayable",
    inputs: [{ name: "wgPubKey", type: "bytes32" }],
    outputs: [],
  },
] as const;

function publishWgKeyCalldata(): Hex {
  return encodeFunctionData({
    abi: PUBLISH_WG_ABI,
    functionName: "publishWgKey",
    args: [WG_KEY],
  });
}

describe("decode", () => {
  it("round-trips execute(cluster, 0, publishWgKey(...))", () => {
    const inner = publishWgKeyCalldata();
    const callData = encodeExecute(CLUSTER, 0n, inner);

    const decoded = decodeExecute(callData);
    expect(decoded.target.toLowerCase()).toBe(CLUSTER.toLowerCase());
    expect(decoded.value).toBe(0n);
    expect(decoded.data).toBe(inner);
    // publishWgKey selector is 0x4979ff72.
    expect(decoded.innerSelector).toBe("0x4979ff72");
  });

  it("encodeExecute is the inverse of decodeExecute (random-ish inputs)", () => {
    const inner: Hex = "0xdeadbeef";
    const value = 12345n;
    const callData = encodeExecute(CLUSTER, value, inner);
    const decoded = decodeExecute(callData);
    expect(decoded.target.toLowerCase()).toBe(CLUSTER.toLowerCase());
    expect(decoded.value).toBe(value);
    expect(decoded.data).toBe(inner);
    expect(decoded.innerSelector).toBe("0xdeadbeef");
  });

  it("outer selector of encoded execute is 0xb61d27f6", () => {
    const callData = encodeExecute(CLUSTER, 0n, "0x");
    expect(selectorOf(callData)).toBe(EXECUTE_SELECTOR);
    expect(EXECUTE_SELECTOR).toBe("0xb61d27f6");
  });

  it("selectorOf returns 0x for sub-4-byte data", () => {
    expect(selectorOf("0x")).toBe("0x");
    expect(selectorOf("0xaabb")).toBe("0x");
  });

  it("selectorOf lower-cases the selector", () => {
    expect(selectorOf("0xAABBCCDDEE")).toBe("0xaabbccdd");
  });

  it("rejects calldata whose outer selector is not execute", () => {
    const notExecute = publishWgKeyCalldata(); // starts with 0x4979ff72
    expect(() => decodeExecute(notExecute)).toThrow(DecodeError);
  });

  it("rejects calldata shorter than 4 bytes", () => {
    expect(() => decodeExecute("0x")).toThrow(DecodeError);
  });

  it("rejects truncated execute args", () => {
    // execute selector but no ABI body.
    expect(() => decodeExecute(EXECUTE_SELECTOR)).toThrow(DecodeError);
  });

  describe("parseWebhookBody", () => {
    const goodUo = {
      sender: "0x1111111111111111111111111111111111111111",
      callData: encodeExecute(CLUSTER, 0n, publishWgKeyCalldata()),
    };

    it("accepts a well-formed body", () => {
      const body = parseWebhookBody({ userOperation: goodUo, chainId: "0x14a34" });
      expect(body.userOperation.sender).toBe(goodUo.sender);
      expect(body.chainId).toBe("0x14a34");
    });

    it("rejects non-object body", () => {
      expect(() => parseWebhookBody(null)).toThrow(DecodeError);
      expect(() => parseWebhookBody("nope")).toThrow(DecodeError);
    });

    it("rejects body missing userOperation", () => {
      expect(() => parseWebhookBody({})).toThrow(DecodeError);
    });

    it("rejects userOperation missing sender/callData", () => {
      expect(() => parseWebhookBody({ userOperation: { callData: "0x" } })).toThrow(DecodeError);
      expect(() =>
        parseWebhookBody({ userOperation: { sender: goodUo.sender } }),
      ).toThrow(DecodeError);
    });

    it("rejects non-hex sender", () => {
      expect(() =>
        parseWebhookBody({ userOperation: { sender: "not-hex", callData: "0x" } }),
      ).toThrow(DecodeError);
    });

    it("rejects non-hex chainId when present", () => {
      expect(() =>
        parseWebhookBody({ userOperation: goodUo, chainId: "84532" }),
      ).toThrow(DecodeError);
    });
  });
});
