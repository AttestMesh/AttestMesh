import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { TtlCache, type Cached } from "../src/cache.ts";
import type { MeshIndexState } from "../src/chain.ts";
import type { Config } from "../src/config.ts";
import { buildIndexedHealth, createApp, startIndexer } from "../src/server.ts";

const CLUSTER = "0x5ab4706fCa998A0792E5c06432b13c73c54E4557" as const;
const OWNER = "0x1111111111111111111111111111111111111111" as const;
const FACTORY = "0x2222222222222222222222222222222222222222" as const;

function cachedIndex(now: number, clusterBlock: number, stale = false): Cached<MeshIndexState> {
  const value = {
    version: 1,
    chainId: 8453,
    clusterFactoryAddr: FACTORY,
    scannedToBlock: 105,
    updatedAt: new Date(now - 5_000).toISOString(),
    clusters: [
      {
        cluster: CLUSTER,
        clusterOwner: OWNER,
        salt: `0x${"00".repeat(32)}`,
        deployedAtBlock: 90,
        deploymentTxHash: `0x${"33".repeat(32)}`,
        deploymentLogIndex: 0,
        meshCidrIp: 0x0a120000,
        meshCidrPrefix: 16,
        cskCommitment: null,
        scannedToBlock: clusterBlock,
        members: [],
        events: [],
      },
    ],
  } as MeshIndexState;
  return { value, fetchedAt: now - 4_000, stale };
}

test("indexed health publishes persisted progress, age, and freshness", () => {
  const now = Date.parse("2026-07-15T21:00:00.000Z");
  const health = buildIndexedHealth(cachedIndex(now, 105), { cacheTtlMs: 300_000, indexRefreshMs: 300_000 }, now);

  assert.equal(health.rpcReachable, true);
  assert.equal(health.clusterCount, 1);
  assert.equal(health.atBlock, 105);
  assert.equal(health.headBlock, 105);
  assert.equal(health.blockLag, 0);
  assert.equal(health.indexedAt, "2026-07-15T20:59:55.000Z");
  assert.equal(health.ageSeconds, 5);
  assert.equal(health.fresh, true);
});

test("indexed health fails freshness on partial progress or a failed refresh", () => {
  const now = Date.parse("2026-07-15T21:00:00.000Z");
  const lagging = buildIndexedHealth(cachedIndex(now, 100), { cacheTtlMs: 300_000, indexRefreshMs: 300_000 }, now);
  assert.equal(lagging.atBlock, 100);
  assert.equal(lagging.headBlock, 105);
  assert.equal(lagging.blockLag, 5);
  assert.equal(lagging.fresh, false);

  const stale = buildIndexedHealth(cachedIndex(now, 105, true), { cacheTtlMs: 300_000, indexRefreshMs: 300_000 }, now);
  assert.equal(stale.rpcReachable, false);
  assert.equal(stale.stale, true);
  assert.equal(stale.fresh, false);
});

test("background indexer populates the exact cache read by health", async () => {
  const now = Date.parse("2026-07-15T21:00:00.000Z");
  const expected = cachedIndex(now, 105).value;
  const cache = new TtlCache<MeshIndexState>(300_000, async () => expected, () => now);

  startIndexer({ indexedReads: true, indexRefreshMs: 60_000 } as Config, cache);
  await new Promise<void>((resolve) => setImmediate(resolve));

  assert.equal(cache.peek()?.value, expected);
  assert.equal(cache.peek()?.stale, false);
});

test("health route reads the injected background cache", async (t) => {
  const now = Date.now();
  const cache = new TtlCache<MeshIndexState>(300_000, async () => cachedIndex(now, 105).value, () => now);
  await cache.refresh();
  const cfg: Config = {
    rpcUrl: "http://127.0.0.1:1",
    clusterFactoryAddr: FACTORY,
    clusterFactoryStartBlock: 1n,
    chainId: 8453,
    gatewayDomain: null,
    listenHost: "127.0.0.1",
    listenPort: 0,
    cacheTtlMs: 300_000,
    discoveryCacheTtlMs: 300_000,
    timelineCacheTtlMs: 300_000,
    logChunkBlocks: 9_000n,
    discoveryStatePath: "/tmp/unused-discovery.json",
    indexStatePath: "/tmp/unused-index.json",
    indexedReads: true,
    indexRefreshMs: 300_000,
    timelineEnabled: false,
    rpcRetryCount: 0,
    rpcRetryBaseMs: 0,
    rpcRetryMaxMs: 0,
    rpcTimeoutMs: 100,
    rpcMinIntervalMs: 0,
  };
  const handle = createApp(cfg, { indexCache: cache });
  const server = createServer((req, res) => void handle(req, res));
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => server.close());

  const address = server.address();
  assert.ok(address && typeof address === "object");
  const response = await fetch(`http://127.0.0.1:${address.port}/healthz`);
  const health = (await response.json()) as Record<string, unknown>;
  assert.equal(response.status, 200);
  assert.equal(health.atBlock, 105);
  assert.equal(health.headBlock, 105);
  assert.equal(health.fresh, true);
});
