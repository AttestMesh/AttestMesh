import { vi } from "vitest";
import {
  encodeAbiParameters,
  encodeFunctionData,
  pad,
  toHex,
  type Address,
  type Hex,
} from "viem";

import type { Env } from "../src/env.js";

/** Minimal in-memory KVNamespace good enough for the worker's get/put usage. */
export function fakeKV(): KVNamespace & { store: Map<string, string> } {
  const store = new Map<string, string>();
  const kv = {
    store,
    async get(key: string): Promise<string | null> {
      return store.has(key) ? store.get(key)! : null;
    },
    async put(key: string, value: string): Promise<void> {
      store.set(key, value);
    },
    async delete(key: string): Promise<void> {
      store.delete(key);
    },
  };
  return kv as unknown as KVNamespace & { store: Map<string, string> };
}

/** A KVNamespace whose every operation rejects — exercises the RPC fallback path. */
export function brokenKV(): KVNamespace {
  return {
    async get() {
      throw new Error("kv down");
    },
    async put() {
      throw new Error("kv down");
    },
    async delete() {
      throw new Error("kv down");
    },
  } as unknown as KVNamespace;
}

export const TEST_TOKEN = "test-token";

export interface TestEnvOptions {
  factoryCache?: KVNamespace;
  memberCache?: KVNamespace;
  token?: string;
  chainId?: number;
  /** SPONSOR_OPERATOR_METHOD value; omitted (default-off) when undefined. */
  sponsorOperatorMethod?: "true" | "false";
  /** CANONICAL_CLUSTER_FACTORY_V2; omitted (v1-only) when undefined. */
  clusterFactoryV2?: string;
}

export function makeEnv(opts: TestEnvOptions = {}): Env {
  return {
    FACTORY_PROVENANCE_CACHE: opts.factoryCache ?? fakeKV(),
    MEMBER_PROVENANCE_CACHE: opts.memberCache ?? fakeKV(),
    EXPECTED_CHAIN_ID: String(opts.chainId ?? 84532),
    CANONICAL_CLUSTER_FACTORY: "0x000000000000000000000000000000000000FacE",
    ...(opts.clusterFactoryV2 !== undefined && {
      CANONICAL_CLUSTER_FACTORY_V2: opts.clusterFactoryV2,
    }),
    CANONICAL_MEMBER_FACTORY: "0x000000000000000000000000000000000000BeeF",
    ...(opts.sponsorOperatorMethod !== undefined && {
      SPONSOR_OPERATOR_METHOD: opts.sponsorOperatorMethod,
    }),
    CACHE_TTL_SECONDS: "86400",
    LOG_LEVEL: "error",
    RPC_URL: "https://rpc.test.invalid",
    ALCHEMY_WEBHOOK_TOKEN: opts.token ?? TEST_TOKEN,
  };
}

/** ABI-encode a bool return value as an eth_call result. */
function boolResult(value: boolean): Hex {
  return encodeAbiParameters([{ type: "bool" }], [value]);
}

/**
 * Decode the `to` address and the called selector from a JSON-RPC eth_call params
 * object so the mock can route to member-vs-cluster factory.
 */
function callTarget(params: unknown): { to: string; selector: string } {
  const p = (params as unknown[])[0] as { to: string; data: Hex };
  return { to: p.to.toLowerCase(), selector: p.data.slice(0, 10).toLowerCase() };
}

export interface RpcMockConfig {
  /** Member factory address (lower-case-compared). */
  memberFactory?: string;
  clusterFactory?: string;
  /** Map of `${selector-target}` decisions; simpler: callbacks below. */
  isOurMember?: (sender: string) => boolean;
  isDeployedCluster?: (target: string) => boolean;
  chainId?: number;
  /** When true, every RPC call rejects (simulates RPC down). */
  down?: boolean;
}

/**
 * Install a `globalThis.fetch` mock that answers viem's JSON-RPC POSTs for
 * `eth_chainId` and `eth_call` (isOurMember / isDeployedCluster). Returns a
 * restore function. Hermetic — no real network.
 */
export function installRpcMock(cfg: RpcMockConfig = {}): () => void {
  const memberFactory = (cfg.memberFactory ?? "0x000000000000000000000000000000000000BeeF").toLowerCase();
  const clusterFactory = (cfg.clusterFactory ?? "0x000000000000000000000000000000000000FacE").toLowerCase();
  const chainId = cfg.chainId ?? 84532;
  const isOurMember = cfg.isOurMember ?? (() => true);
  const isDeployedCluster = cfg.isDeployedCluster ?? (() => true);

  const original = globalThis.fetch;

  const mock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    if (cfg.down) throw new Error("ECONNREFUSED");
    const payload = JSON.parse(String(init?.body ?? "{}")) as {
      id: number;
      method: string;
      params: unknown;
    };

    const reply = (result: unknown) =>
      new Response(JSON.stringify({ jsonrpc: "2.0", id: payload.id, result }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });

    if (payload.method === "eth_chainId") {
      return reply(toHex(chainId));
    }
    if (payload.method === "eth_call") {
      const { to, selector } = callTarget(payload.params);
      // isOurMember(address) selector
      const isOurMemberSel = encodeFunctionData({
        abi: [
          {
            type: "function",
            name: "isOurMember",
            stateMutability: "view",
            inputs: [{ type: "address" }],
            outputs: [{ type: "bool" }],
          },
        ] as const,
        functionName: "isOurMember",
        args: ["0x0000000000000000000000000000000000000000"],
      }).slice(0, 10);
      const isDeployedSel = encodeFunctionData({
        abi: [
          {
            type: "function",
            name: "isDeployedCluster",
            stateMutability: "view",
            inputs: [{ type: "address" }],
            outputs: [{ type: "bool" }],
          },
        ] as const,
        functionName: "isDeployedCluster",
        args: ["0x0000000000000000000000000000000000000000"],
      }).slice(0, 10);

      // Extract the address argument (last 20 bytes of the 32-byte word).
      const argWord = `0x${(payload.params as Array<{ data: string }>)[0].data.slice(10)}`;
      const addr = `0x${argWord.slice(-40)}`;

      if (to === memberFactory && selector === isOurMemberSel) {
        return reply(boolResult(isOurMember(addr.toLowerCase())));
      }
      if (to === clusterFactory && selector === isDeployedSel) {
        return reply(boolResult(isDeployedCluster(addr.toLowerCase())));
      }
      // Unknown call — return false.
      return reply(boolResult(false));
    }
    // Unhandled method.
    return new Response(
      JSON.stringify({ jsonrpc: "2.0", id: payload.id, error: { code: -32601, message: "unknown" } }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  });

  globalThis.fetch = mock as unknown as typeof fetch;
  return () => {
    globalThis.fetch = original;
  };
}

/** Build an `execute(target, value, inner)` UserOp callData for fixtures. */
export function executeCallData(target: Address, value: bigint, inner: Hex): Hex {
  // Re-implemented here to avoid coupling tests to src internals beyond decode.
  return encodeFunctionData({
    abi: [
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
    ] as const,
    functionName: "execute",
    args: [target, value, inner],
  });
}

/** publishWgKey(bytes32) inner calldata for a given key. */
export function publishWgKeyInner(key: Hex = pad("0x01", { size: 32 })): Hex {
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
    args: [key],
  });
}
