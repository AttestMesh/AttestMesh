import { describe, it, expect, vi } from "vitest";
import {
  ContractFunctionExecutionError,
  HttpRequestError,
  type Address,
  type PublicClient,
} from "viem";

import {
  isOurMember,
  isDeployedCluster,
  isAllowlistedAppId,
  RpcFailureError,
  type ProvenanceDeps,
} from "../src/provenance.js";
import type { Config } from "../src/env.js";
import { createLogger } from "../src/log.js";

const config: Config = {
  expectedChainId: 84532,
  canonicalClusterFactory: "0x000000000000000000000000000000000000Face",
  canonicalClusterFactoryV2: undefined,
  canonicalMemberFactory: "0x000000000000000000000000000000000000bEEF",
  sponsorOperatorMethod: false,
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

  // Multi-attestor spec: v1 and v2 factories coexist; the webhook trusts either.
  describe("isDeployedCluster across both factories", () => {
    const V2_FACTORY: Address = "0x000000000000000000000000000000000000Fad2";
    const v2Config: Config = { ...config, canonicalClusterFactoryV2: V2_FACTORY };

    /** Route the read by factory address: v1 says `v1`, v2 says `v2`. */
    function dualFactoryClient(v1: boolean, v2: boolean) {
      return fakeClient(async (args: unknown) => {
        const { address } = args as { address: Address };
        return address.toLowerCase() === V2_FACTORY.toLowerCase() ? v2 : v1;
      });
    }

    it("true when only the v1 factory knows the cluster", async () => {
      const d = deps({ config: v2Config, client: dualFactoryClient(true, false) });
      expect(await isDeployedCluster(d, TARGET)).toBe(true);
    });

    it("true when only the v2 factory knows the cluster", async () => {
      const d = deps({ config: v2Config, client: dualFactoryClient(false, true) });
      expect(await isDeployedCluster(d, TARGET)).toBe(true);
    });

    it("false when neither factory knows the cluster", async () => {
      const d = deps({ config: v2Config, client: dualFactoryClient(false, false) });
      expect(await isDeployedCluster(d, TARGET)).toBe(false);
    });

    it("does not consult v2 when unset (v1-only deployments unchanged)", async () => {
      const readContract = vi.fn(async (args: unknown) => {
        const { address } = args as { address: Address };
        expect(address.toLowerCase()).toBe(config.canonicalClusterFactory.toLowerCase());
        return false;
      });
      const d = deps({ client: fakeClient(readContract) });
      expect(await isDeployedCluster(d, TARGET)).toBe(false);
      expect(readContract).toHaveBeenCalledTimes(1);
    });
  });
});

const CLUSTER: Address = "0x00000000000000000000000000000000C1C1c1C1";

/** Dispatching fake client: routes readContract by functionName (cluster / isDeployedCluster
 *  / allowedAppIds). Missing routes throw, so a test asserts exactly which calls happen. */
function routingClient(routes: {
  cluster?: () => Promise<unknown>;
  isDeployedCluster?: () => Promise<boolean>;
  allowedAppIds?: () => Promise<boolean>;
}): PublicClient {
  return {
    async readContract(args: { functionName: string }) {
      const fn = routes[args.functionName as keyof typeof routes];
      if (!fn) throw new Error(`unexpected readContract(${args.functionName})`);
      return fn();
    },
  } as unknown as PublicClient;
}

/** An error that passes `instanceof ContractFunctionExecutionError` (a view revert). */
function revert(): Error {
  const e = new Error("execution reverted");
  Object.setPrototypeOf(e, ContractFunctionExecutionError.prototype);
  return e;
}

/** What viem actually throws when an eth_call fails at the transport layer: a
 *  ContractFunctionExecutionError wrapping an HttpRequestError. This MUST be treated as a
 *  transport failure (RpcFailureError), not a contract-level "no". */
function transportWrapped(): Error {
  const http = new Error("HTTP request failed");
  Object.setPrototypeOf(http, HttpRequestError.prototype);
  const outer = new Error("contract call failed");
  Object.setPrototypeOf(outer, ContractFunctionExecutionError.prototype);
  (outer as { cause?: unknown }).cause = http;
  return outer;
}

describe("isAllowlistedAppId (Path A membership)", () => {
  it("true when sender.cluster() is a deployed cluster that allowlisted the app_id", async () => {
    const d = deps({
      client: routingClient({
        cluster: async () => CLUSTER,
        isDeployedCluster: async () => true,
        allowedAppIds: async () => true,
      }),
    });
    expect(await isAllowlistedAppId(d, SENDER)).toBe(true);
  });

  it("fails closed when sender names a cluster we did NOT deploy (hostile cluster())", async () => {
    // The sender controls cluster(); it points at a contract that would self-report allowed.
    // isDeployedCluster=false must short-circuit before allowedAppIds is ever consulted.
    const allowed = vi.fn(async () => true);
    const d = deps({
      client: routingClient({
        cluster: async () => "0x00000000000000000000000000000000DeaDDEAd",
        isDeployedCluster: async () => false,
        allowedAppIds: allowed,
      }),
    });
    expect(await isAllowlistedAppId(d, SENDER)).toBe(false);
    expect(allowed).not.toHaveBeenCalled();
  });

  it("false when the deployed cluster has not allowlisted the app_id", async () => {
    const d = deps({
      client: routingClient({
        cluster: async () => CLUSTER,
        isDeployedCluster: async () => true,
        allowedAppIds: async () => false,
      }),
    });
    expect(await isAllowlistedAppId(d, SENDER)).toBe(false);
  });

  it("false (not rpc-failure) when sender.cluster() reverts — a not-yet-upgraded stock proxy", async () => {
    const d = deps({
      client: routingClient({
        cluster: async () => {
          throw revert();
        },
      }),
    });
    expect(await isAllowlistedAppId(d, SENDER)).toBe(false);
  });

  it("throws RpcFailureError on a transport failure (so it is not cached as a denial)", async () => {
    const d = deps({
      client: routingClient({
        cluster: async () => {
          throw new Error("ECONNREFUSED");
        },
      }),
    });
    await expect(isAllowlistedAppId(d, SENDER)).rejects.toBeInstanceOf(RpcFailureError);
  });

  it("treats a transport error wrapped in ContractFunctionExecutionError as rpc-failure", async () => {
    // viem wraps a dead-RPC eth_call in ContractFunctionExecutionError; it must NOT be
    // mistaken for a contract revert and cached as a denial.
    const { kv, puts } = recordingKV();
    const d = deps({
      memberCache: kv,
      client: routingClient({
        cluster: async () => {
          throw transportWrapped();
        },
      }),
    });
    await expect(isAllowlistedAppId(d, SENDER)).rejects.toBeInstanceOf(RpcFailureError);
    expect(puts.find((p) => p.key.startsWith("pathA:"))).toBeUndefined();
  });

  it("caches a positive answer under a pathA: key with the full TTL", async () => {
    const { kv, puts } = recordingKV();
    const d = deps({
      memberCache: kv,
      client: routingClient({
        cluster: async () => CLUSTER,
        isDeployedCluster: async () => true,
        allowedAppIds: async () => true,
      }),
    });
    await isAllowlistedAppId(d, SENDER);
    const put = puts.find((p) => p.key.startsWith("pathA:"));
    expect(put?.value).toBe("1");
    expect(put?.ttl).toBe(config.cacheTtlSeconds);
  });
});
