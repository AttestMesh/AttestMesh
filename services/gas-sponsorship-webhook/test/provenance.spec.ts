import { describe, it, expect, vi } from "vitest";
import type { Address, PublicClient } from "viem";

import {
  isOurMember,
  isDeployedCluster,
  RpcFailureError,
  type ProvenanceDeps,
} from "../src/provenance.js";
import type { Config } from "../src/env.js";
import { createLogger } from "../src/log.js";

const config: Config = {
  expectedChainId: 84532,
  canonicalClusterFactory: "0x000000000000000000000000000000000000Face",
  canonicalMemberFactory: "0x000000000000000000000000000000000000bEEF",
  rpcUrl: "https://rpc.test.invalid",
  alchemyWebhookToken: "tok",
  cacheTtlSeconds: 86_400,
  negativeCacheTtlSeconds: 600,
  logLevel: "error",
};

const SENDER: Address = "0x2222222222222222222222222222222222222222";
const TARGET: Address = "0x00000000000000000000000000000000000000c1";

interface PutCall {
  key: string;
  value: string;
  ttl: number | undefined;
}

/** Fake KV that records put() TTLs so we can assert positive/negative caching. */
function recordingKV() {
  const store = new Map<string, string>();
  const puts: PutCall[] = [];
  const kv = {
    async get(key: string): Promise<string | null> {
      return store.has(key) ? store.get(key)! : null;
    },
    async put(key: string, value: string, opts?: { expirationTtl?: number }): Promise<void> {
      store.set(key, value);
      puts.push({ key, value, ttl: opts?.expirationTtl });
    },
    async delete(key: string): Promise<void> {
      store.delete(key);
    },
  };
  return { kv: kv as unknown as KVNamespace, store, puts };
}

/** A fake PublicClient whose readContract returns `result` (or throws). */
function fakeClient(readContract: (args: unknown) => Promise<boolean>): PublicClient {
  return { readContract } as unknown as PublicClient;
}

function deps(over: Partial<ProvenanceDeps>): ProvenanceDeps {
  return {
    config,
    factoryCache: undefined,
    memberCache: undefined,
    logger: createLogger("error"),
    ...over,
  };
}

describe("provenance caching", () => {
  it("isOurMember: cache miss → RPC, then writes positive answer with full TTL", async () => {
    const { kv, store, puts } = recordingKV();
    const readContract = vi.fn(async () => true);
    const d = deps({ memberCache: kv, client: fakeClient(readContract) });

    const result = await isOurMember(d, SENDER);
    expect(result).toBe(true);
    expect(readContract).toHaveBeenCalledTimes(1);
    expect(store.get(`84532:${SENDER}`)).toBe("1");
    expect(puts).toHaveLength(1);
    expect(puts[0]!.ttl).toBe(86_400); // positive → full TTL
  });

  it("isOurMember: negative answer cached with the short TTL", async () => {
    const { kv, puts } = recordingKV();
    const d = deps({ memberCache: kv, client: fakeClient(async () => false) });
    const result = await isOurMember(d, SENDER);
    expect(result).toBe(false);
    expect(puts[0]!.value).toBe("0");
    expect(puts[0]!.ttl).toBe(600); // negative → short TTL
  });

  it("isOurMember: cache hit short-circuits the RPC", async () => {
    const { kv, store } = recordingKV();
    store.set(`84532:${SENDER}`, "1");
    const readContract = vi.fn(async () => false); // would disagree if called
    const d = deps({ memberCache: kv, client: fakeClient(readContract) });

    const result = await isOurMember(d, SENDER);
    expect(result).toBe(true);
    expect(readContract).not.toHaveBeenCalled();
  });

  it("isDeployedCluster: cache hit for a negative answer short-circuits RPC", async () => {
    const { kv, store } = recordingKV();
    store.set(`84532:0x00000000000000000000000000000000000000C1`, "0");
    const readContract = vi.fn(async () => true);
    const d = deps({ factoryCache: kv, client: fakeClient(readContract) });

    const result = await isDeployedCluster(d, TARGET);
    expect(result).toBe(false);
    expect(readContract).not.toHaveBeenCalled();
  });

  it("throws RpcFailureError when the eth_call rejects", async () => {
    const d = deps({
      client: fakeClient(async () => {
        throw new Error("ECONNREFUSED");
      }),
    });
    await expect(isOurMember(d, SENDER)).rejects.toBeInstanceOf(RpcFailureError);
    await expect(isDeployedCluster(d, TARGET)).rejects.toBeInstanceOf(RpcFailureError);
  });

  it("falls back to RPC when KV.get throws (still returns the chain answer)", async () => {
    const brokenKV = {
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
    const readContract = vi.fn(async () => true);
    const d = deps({ memberCache: brokenKV, client: fakeClient(readContract) });

    const result = await isOurMember(d, SENDER);
    expect(result).toBe(true);
    expect(readContract).toHaveBeenCalledTimes(1);
  });

  it("works with no KV namespace bound at all (direct RPC)", async () => {
    const readContract = vi.fn(async () => true);
    const d = deps({ memberCache: undefined, client: fakeClient(readContract) });
    const result = await isOurMember(d, SENDER);
    expect(result).toBe(true);
    expect(readContract).toHaveBeenCalledTimes(1);
  });

  it("cache key uses checksummed address regardless of input casing", async () => {
    const { kv, store } = recordingKV();
    const d = deps({ factoryCache: kv, client: fakeClient(async () => true) });
    // Pass an all-lowercase target; key should be the checksummed form.
    await isDeployedCluster(d, "0x00000000000000000000000000000000000000c1");
    expect(store.has("84532:0x00000000000000000000000000000000000000C1")).toBe(true);
  });
});
