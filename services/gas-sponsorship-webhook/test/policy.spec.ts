import { describe, it, expect, beforeEach, vi } from "vitest";
import { encodeFunctionData, type Address, type Hex } from "viem";

// Mock the provenance module so no real RPC/KV is touched. The mock factory must
// not close over outer mutable bindings; we drive behavior via module-level vars
// declared with `var` so they are hoisted alongside the vi.mock call.
vi.mock("../src/provenance.js", () => {
  class RpcFailureError extends Error {
    override readonly name = "RpcFailureError";
  }
  return {
    RpcFailureError,
    isOurMember: (...args: unknown[]) => memberImpl(...args),
    isDeployedCluster: (...args: unknown[]) => clusterImpl(...args),
    isAllowlistedAppId: (...args: unknown[]) => appIdImpl(...args),
  };
});

// eslint-disable-next-line no-var
var memberImpl: (...args: unknown[]) => Promise<boolean>;
// eslint-disable-next-line no-var
var clusterImpl: (...args: unknown[]) => Promise<boolean>;
// eslint-disable-next-line no-var
var appIdImpl: (...args: unknown[]) => Promise<boolean>;

import { evaluatePolicy, constantTimeEqual, type PolicyInput } from "../src/policy.js";
import { RpcFailureError } from "../src/provenance.js";
import type { Config } from "../src/env.js";
import type { ProvenanceDeps } from "../src/provenance.js";
import { createLogger } from "../src/log.js";
import { encodeExecute } from "../src/decode.js";

const TOKEN = "super-secret-token";
const SENDER: Address = "0x1111111111111111111111111111111111111111";
const CLUSTER: Address = "0x00000000000000000000000000000000000000C1";
const WG_KEY: Hex = `0x${"cd".repeat(32)}`;

const config: Config = {
  expectedChainId: 84532,
  canonicalClusterFactory: "0x000000000000000000000000000000000000FacE",
  canonicalMemberFactory: "0x000000000000000000000000000000000000BeeF",
  rpcUrl: "https://rpc.invalid",
  alchemyWebhookToken: TOKEN,
  cacheTtlSeconds: 86_400,
  negativeCacheTtlSeconds: 600,
  logLevel: "error",
};

// Provenance deps are unused by the mock but the function signature wants them.
const provenance: ProvenanceDeps = {
  config,
  factoryCache: undefined,
  memberCache: undefined,
  logger: createLogger("error"),
};

function publishWgKeyInner(): Hex {
  return encodeFunctionData({
    abi: [
      {
        type: "function",
        name: "publishWgKey",
        stateMutability: "nonpayable",
        inputs: [{ name: "wgPubKey", type: "bytes32" }],
        outputs: [],
      },
    ] as const,
    functionName: "publishWgKey",
    args: [WG_KEY],
  });
}

/** A fully-valid input that should pass every check when provenance says yes. */
function validInput(overrides: Partial<PolicyInput> = {}): PolicyInput {
  return {
    token: TOKEN,
    chainId: "0x14a34", // 84532
    userOperation: {
      sender: SENDER,
      callData: encodeExecute(CLUSTER, 0n, publishWgKeyInner()),
    },
    ...overrides,
  };
}

beforeEach(() => {
  // Default: factory membership + cluster provenance pass; Path A app_id check defaults
  // off (it is only consulted when isOurMember is false).
  memberImpl = vi.fn(async () => true);
  clusterImpl = vi.fn(async () => true);
  appIdImpl = vi.fn(async () => false);
});

describe("constantTimeEqual", () => {
  it("true for identical strings", () => {
    expect(constantTimeEqual("abc", "abc")).toBe(true);
    expect(constantTimeEqual("", "")).toBe(true);
  });
  it("false for differing content of equal length", () => {
    expect(constantTimeEqual("abc", "abd")).toBe(false);
  });
  it("false for differing length", () => {
    expect(constantTimeEqual("abc", "abcd")).toBe(false);
    expect(constantTimeEqual("abcd", "abc")).toBe(false);
  });
  it("handles multibyte utf-8", () => {
    expect(constantTimeEqual("café", "café")).toBe(true);
    expect(constantTimeEqual("café", "cafe")).toBe(false);
  });
});

describe("evaluatePolicy", () => {
  it("approves a fully-valid UserOp (all checks pass)", async () => {
    const decision = await evaluatePolicy(validInput(), config, provenance);
    expect(decision).toEqual({ approved: true });
  });

  // 1. token
  it("denies bad-token when token absent", async () => {
    const decision = await evaluatePolicy(validInput({ token: null }), config, provenance);
    expect(decision).toEqual({ approved: false, reason: "bad-token" });
  });

  it("denies bad-token when token wrong", async () => {
    const decision = await evaluatePolicy(validInput({ token: "wrong" }), config, provenance);
    expect(decision).toEqual({ approved: false, reason: "bad-token" });
  });

  // 2. chain id
  it("denies chain-mismatch on wrong chain", async () => {
    const decision = await evaluatePolicy(validInput({ chainId: "0x1" }), config, provenance);
    expect(decision).toEqual({ approved: false, reason: "chain-mismatch" });
  });

  it("denies chain-mismatch when chainId missing", async () => {
    const decision = await evaluatePolicy(validInput({ chainId: undefined }), config, provenance);
    expect(decision).toEqual({ approved: false, reason: "chain-mismatch" });
  });

  // 3. sender provenance
  it("denies not-cluster-member when sender unknown", async () => {
    memberImpl = vi.fn(async () => false);
    const decision = await evaluatePolicy(validInput(), config, provenance);
    expect(decision).toEqual({ approved: false, reason: "not-cluster-member" });
  });

  it("approves a Path A member (not a factory member, but app_id allowlisted)", async () => {
    memberImpl = vi.fn(async () => false); // not factory-minted
    appIdImpl = vi.fn(async () => true); // but its cluster allowlisted the app_id
    const decision = await evaluatePolicy(validInput(), config, provenance);
    expect(decision).toEqual({ approved: true });
  });

  it("denies rpc-failure when sender lookup throws RpcFailureError", async () => {
    memberImpl = vi.fn(async () => {
      throw new RpcFailureError("boom");
    });
    const decision = await evaluatePolicy(validInput(), config, provenance);
    expect(decision).toEqual({ approved: false, reason: "rpc-failure" });
  });

  // 4. outer selector
  it("denies outer-selector-not-execute when callData is not execute", async () => {
    const decision = await evaluatePolicy(
      validInput({
        userOperation: { sender: SENDER, callData: publishWgKeyInner() },
      }),
      config,
      provenance,
    );
    expect(decision).toEqual({ approved: false, reason: "outer-selector-not-execute" });
  });

  it("denies outer-selector-not-execute when callData is truncated", async () => {
    const decision = await evaluatePolicy(
      validInput({ userOperation: { sender: SENDER, callData: "0xb61d27f6" } }),
      config,
      provenance,
    );
    expect(decision).toEqual({ approved: false, reason: "outer-selector-not-execute" });
  });

  // 5. value
  it("denies value-nonzero when execute value > 0", async () => {
    const decision = await evaluatePolicy(
      validInput({
        userOperation: {
          sender: SENDER,
          callData: encodeExecute(CLUSTER, 1n, publishWgKeyInner()),
        },
      }),
      config,
      provenance,
    );
    expect(decision).toEqual({ approved: false, reason: "value-nonzero" });
  });

  // 6. inner selector
  it("denies inner-selector-not-allowed for an unknown inner selector", async () => {
    const decision = await evaluatePolicy(
      validInput({
        userOperation: {
          sender: SENDER,
          callData: encodeExecute(CLUSTER, 0n, "0x12345678"),
        },
      }),
      config,
      provenance,
    );
    expect(decision).toEqual({ approved: false, reason: "inner-selector-not-allowed" });
  });

  it("denies inner-selector-not-allowed for empty inner data (selector 0x)", async () => {
    const decision = await evaluatePolicy(
      validInput({
        userOperation: { sender: SENDER, callData: encodeExecute(CLUSTER, 0n, "0x") },
      }),
      config,
      provenance,
    );
    expect(decision).toEqual({ approved: false, reason: "inner-selector-not-allowed" });
  });

  // 7. target provenance
  it("denies target-not-our-cluster when target is unknown", async () => {
    clusterImpl = vi.fn(async () => false);
    const decision = await evaluatePolicy(validInput(), config, provenance);
    expect(decision).toEqual({ approved: false, reason: "target-not-our-cluster" });
  });

  it("denies rpc-failure when target lookup throws RpcFailureError", async () => {
    clusterImpl = vi.fn(async () => {
      throw new RpcFailureError("boom");
    });
    const decision = await evaluatePolicy(validInput(), config, provenance);
    expect(decision).toEqual({ approved: false, reason: "rpc-failure" });
  });

  // Short-circuit ordering: a bad token must not even invoke provenance.
  it("does not call provenance when token is bad (short-circuit)", async () => {
    const memberSpy = vi.fn(async () => true);
    const clusterSpy = vi.fn(async () => true);
    memberImpl = memberSpy;
    clusterImpl = clusterSpy;
    await evaluatePolicy(validInput({ token: "wrong" }), config, provenance);
    expect(memberSpy).not.toHaveBeenCalled();
    expect(clusterSpy).not.toHaveBeenCalled();
  });

  // Accept a different allowlisted selector (setCskCommitment) end-to-end.
  it("approves setCskCommitment inner call", async () => {
    const inner = encodeFunctionData({
      abi: [
        {
          type: "function",
          name: "setCskCommitment",
          stateMutability: "nonpayable",
          inputs: [{ name: "commitment", type: "bytes32" }],
          outputs: [],
        },
      ] as const,
      functionName: "setCskCommitment",
      args: [`0x${"11".repeat(32)}`],
    });
    const decision = await evaluatePolicy(
      validInput({ userOperation: { sender: SENDER, callData: encodeExecute(CLUSTER, 0n, inner) } }),
      config,
      provenance,
    );
    expect(decision).toEqual({ approved: true });
  });
});
