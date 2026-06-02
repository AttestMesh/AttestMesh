import { describe, it, expect, afterEach, beforeEach } from "vitest";
import type { Address } from "viem";

import worker, { __resetHealthProbe } from "../src/index.js";
import {
  makeEnv,
  installRpcMock,
  executeCallData,
  publishWgKeyInner,
  fakeKV,
  brokenKV,
  TEST_TOKEN,
} from "./helpers.js";

const SENDER: Address = "0x1111111111111111111111111111111111111111";
const CLUSTER: Address = "0x00000000000000000000000000000000000000c1";
const UNKNOWN_CLUSTER: Address = "0x00000000000000000000000000000000000000dd";

const ctx = {} as ExecutionContext;

function postRequest(token: string | null, body: unknown): Request {
  const url = token === null ? "https://wh.test/" : `https://wh.test/?token=${token}`;
  return new Request(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

function validBody() {
  return {
    userOperation: {
      sender: SENDER,
      callData: executeCallData(CLUSTER, 0n, publishWgKeyInner()),
    },
    entryPoint: "0x0000000071727De22E5E9d8BAf0edAc6f37da032",
    chainId: "0x14a34", // 84532
  };
}

let restore: () => void;

afterEach(() => {
  restore?.();
  __resetHealthProbe();
});

describe("router POST /", () => {
  beforeEach(() => {
    __resetHealthProbe();
  });

  it("approves a valid UserOp end-to-end", async () => {
    restore = installRpcMock({
      isOurMember: (a) => a === SENDER.toLowerCase(),
      isDeployedCluster: (a) => a === CLUSTER.toLowerCase(),
    });
    const env = makeEnv();
    const res = await worker.fetch(postRequest(TEST_TOKEN, validBody()), env, ctx);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ approved: true });
  });

  it("denies bad-token (and never hits RPC)", async () => {
    let rpcHit = false;
    restore = installRpcMock({
      isOurMember: () => {
        rpcHit = true;
        return true;
      },
    });
    const env = makeEnv();
    const res = await worker.fetch(postRequest("nope", validBody()), env, ctx);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ approved: false, reason: "bad-token" });
    expect(rpcHit).toBe(false);
  });

  it("denies target-not-our-cluster for an undeployed target", async () => {
    restore = installRpcMock({
      isOurMember: () => true,
      isDeployedCluster: (a) => a === CLUSTER.toLowerCase(), // UNKNOWN_CLUSTER → false
    });
    const env = makeEnv();
    const body = {
      ...validBody(),
      userOperation: {
        sender: SENDER,
        callData: executeCallData(UNKNOWN_CLUSTER, 0n, publishWgKeyInner()),
      },
    };
    const res = await worker.fetch(postRequest(TEST_TOKEN, body), env, ctx);
    expect(await res.json()).toEqual({ approved: false, reason: "target-not-our-cluster" });
  });

  it("denies not-cluster-member for an unknown sender", async () => {
    restore = installRpcMock({ isOurMember: () => false, isDeployedCluster: () => true });
    const env = makeEnv();
    const res = await worker.fetch(postRequest(TEST_TOKEN, validBody()), env, ctx);
    expect(await res.json()).toEqual({ approved: false, reason: "not-cluster-member" });
  });

  it("returns rpc-failure reason when RPC is down", async () => {
    restore = installRpcMock({ down: true });
    const env = makeEnv();
    const res = await worker.fetch(postRequest(TEST_TOKEN, validBody()), env, ctx);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ approved: false, reason: "rpc-failure" });
  });

  it("returns 400 on malformed JSON", async () => {
    restore = installRpcMock();
    const env = makeEnv();
    const res = await worker.fetch(postRequest(TEST_TOKEN, "{not json"), env, ctx);
    expect(res.status).toBe(400);
  });

  it("returns 400 on a body missing userOperation", async () => {
    restore = installRpcMock();
    const env = makeEnv();
    const res = await worker.fetch(postRequest(TEST_TOKEN, { chainId: "0x14a34" }), env, ctx);
    expect(res.status).toBe(400);
  });

  it("caches the positive member/cluster answers in KV", async () => {
    const factoryCache = fakeKV();
    const memberCache = fakeKV();
    restore = installRpcMock({
      isOurMember: () => true,
      isDeployedCluster: () => true,
    });
    const env = makeEnv({ factoryCache, memberCache });
    await worker.fetch(postRequest(TEST_TOKEN, validBody()), env, ctx);
    expect(memberCache.store.get(`84532:${SENDER}`)).toBe("1");
    // Cluster key uses checksum address; just assert one positive entry exists.
    expect([...factoryCache.store.values()]).toContain("1");
  });

  it("falls back to RPC when KV is broken (still approves)", async () => {
    restore = installRpcMock({ isOurMember: () => true, isDeployedCluster: () => true });
    const env = makeEnv({ factoryCache: brokenKV(), memberCache: brokenKV() });
    const res = await worker.fetch(postRequest(TEST_TOKEN, validBody()), env, ctx);
    expect(await res.json()).toEqual({ approved: true });
  });
});

describe("router GET /check", () => {
  it("returns deployed+sponsored true for a known cluster", async () => {
    restore = installRpcMock({ isDeployedCluster: (a) => a === CLUSTER.toLowerCase() });
    const env = makeEnv();
    const res = await worker.fetch(
      new Request(`https://wh.test/check?cluster=${CLUSTER}`),
      env,
      ctx,
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ deployed: true, sponsored: true });
  });

  it("returns deployed false for an unknown cluster", async () => {
    restore = installRpcMock({ isDeployedCluster: () => false });
    const env = makeEnv();
    const res = await worker.fetch(
      new Request(`https://wh.test/check?cluster=${UNKNOWN_CLUSTER}`),
      env,
      ctx,
    );
    expect(await res.json()).toEqual({ deployed: false, sponsored: false });
  });

  it("returns 400 for missing/invalid cluster param", async () => {
    restore = installRpcMock();
    const env = makeEnv();
    const res = await worker.fetch(new Request("https://wh.test/check"), env, ctx);
    expect(res.status).toBe(400);
    const res2 = await worker.fetch(new Request("https://wh.test/check?cluster=nope"), env, ctx);
    expect(res2.status).toBe(400);
  });

  it("returns 503 when RPC is down", async () => {
    restore = installRpcMock({ down: true });
    const env = makeEnv();
    const res = await worker.fetch(
      new Request(`https://wh.test/check?cluster=${CLUSTER}`),
      env,
      ctx,
    );
    expect(res.status).toBe(503);
  });
});

describe("router GET /healthz", () => {
  it("returns 200 {ok:true} when RPC reachable", async () => {
    restore = installRpcMock({ chainId: 84532 });
    const env = makeEnv();
    const res = await worker.fetch(new Request("https://wh.test/healthz"), env, ctx);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });
  });

  it("returns 503 when RPC unreachable", async () => {
    restore = installRpcMock({ down: true });
    const env = makeEnv();
    const res = await worker.fetch(new Request("https://wh.test/healthz"), env, ctx);
    expect(res.status).toBe(503);
  });
});

describe("router misc", () => {
  it("returns 404 for unknown routes", async () => {
    restore = installRpcMock();
    const env = makeEnv();
    const res = await worker.fetch(new Request("https://wh.test/nope"), env, ctx);
    expect(res.status).toBe(404);
  });

  it("returns 500 when env is misconfigured", async () => {
    restore = installRpcMock();
    const badEnv = { ...makeEnv(), EXPECTED_CHAIN_ID: "not-a-number" };
    const res = await worker.fetch(postRequest(TEST_TOKEN, validBody()), badEnv, ctx);
    expect(res.status).toBe(500);
  });
});
