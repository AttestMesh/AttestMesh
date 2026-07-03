// HTTP server + router (spec §5). Stateless read-through; no framework.

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { loadConfig, type Config } from "./config.ts";
import { TtlCache } from "./cache.ts";
import {
  deploymentsFromIndex,
  fetchClusterDeployments,
  fetchSnapshot,
  fetchTimeline,
  makeClient,
  snapshotsFromIndex,
  timelinesFromIndex,
  updateMeshIndex,
  type ClusterDeployment,
  type Snapshot,
  type Timeline,
} from "./chain.ts";

function json(res: ServerResponse, status: number, body: unknown, extraHeaders: Record<string, string> = {}): void {
  const payload = JSON.stringify(body, null, 2);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    ...extraHeaders,
  });
  res.end(payload);
}

export function buildTopology(s: Snapshot) {
  const nodes = s.members.map((m) => ({
    memberId: m.memberId,
    appId: m.appId,
    label: m.memberContract, // this tier has no VM name; Box Admin may relabel
    meshIp: m.meshIp,
    isOriginator: m.isOriginator,
  }));
  const edges: { a: string; b: string; state: "unknown" }[] = [];
  for (let i = 0; i < s.members.length; i++) {
    for (let j = i + 1; j < s.members.length; j++) {
      edges.push({ a: s.members[i]!.memberId, b: s.members[j]!.memberId, state: "unknown" });
    }
  }
  return { cluster: s.cluster, atBlock: s.atBlock, nodes, edges };
}

export function buildHealth(s: Snapshot) {
  return {
    cluster: s.cluster,
    atBlock: s.atBlock,
    memberCount: s.memberCount,
    cskCommitted: s.cskCommitment !== null,
    originatorMemberId: s.originatorMemberId,
    note: "liveness (phase/live_peers) is operator-tier; null here",
  };
}

function clusterKey(cluster: string): string {
  return cluster.toLowerCase();
}

function withDeployment<T extends { cluster: `0x${string}` }>(
  value: T,
  deployment: ClusterDeployment | undefined,
): T & Partial<ClusterDeployment> {
  return deployment ? { ...deployment, ...value } : value;
}

function aggregateSnapshots(snapshots: Snapshot[], deployments: ClusterDeployment[], stale: boolean) {
  const deploymentsByCluster = new Map(deployments.map((d) => [clusterKey(d.cluster), d]));
  const atBlock = snapshots.reduce((max, s) => Math.max(max, s.atBlock), 0);
  return {
    chainId: snapshots[0]?.chainId ?? null,
    atBlock: atBlock || null,
    clusterCount: snapshots.length,
    memberCount: snapshots.reduce((sum, s) => sum + s.memberCount, 0),
    clusters: snapshots.map((s) => withDeployment(s, deploymentsByCluster.get(clusterKey(s.cluster)))),
    stale: stale || undefined,
  };
}

function aggregateTopologies(snapshots: Snapshot[], deployments: ClusterDeployment[], stale: boolean) {
  const deploymentsByCluster = new Map(deployments.map((d) => [clusterKey(d.cluster), d]));
  const topologies = snapshots.map((s) => withDeployment(buildTopology(s), deploymentsByCluster.get(clusterKey(s.cluster))));
  return {
    chainId: snapshots[0]?.chainId ?? null,
    atBlock: snapshots.reduce((max, s) => Math.max(max, s.atBlock), 0) || null,
    clusterCount: topologies.length,
    clusters: topologies,
    stale: stale || undefined,
  };
}

function aggregateHealth(snapshots: Snapshot[], deployments: ClusterDeployment[], stale: boolean) {
  const deploymentsByCluster = new Map(deployments.map((d) => [clusterKey(d.cluster), d]));
  const health = snapshots.map((s) => withDeployment(buildHealth(s), deploymentsByCluster.get(clusterKey(s.cluster))));
  return {
    chainId: snapshots[0]?.chainId ?? null,
    atBlock: snapshots.reduce((max, s) => Math.max(max, s.atBlock), 0) || null,
    clusterCount: health.length,
    memberCount: snapshots.reduce((sum, s) => sum + s.memberCount, 0),
    cskCommitted: health.every((h) => h.cskCommitted),
    clusters: health,
    stale: stale || undefined,
  };
}

function aggregateTimelines(timelines: Timeline[], deployments: ClusterDeployment[], stale: boolean) {
  const deploymentsByCluster = new Map(deployments.map((d) => [clusterKey(d.cluster), d]));
  const events = timelines
    .flatMap((t) => t.events.map((e) => ({ ...e, cluster: t.cluster })))
    .sort((a, b) => (a.block === b.block ? a.logIndex - b.logIndex : a.block - b.block));
  return {
    clusterCount: timelines.length,
    fromBlock: timelines.reduce((min, t) => Math.min(min, t.fromBlock), Number.MAX_SAFE_INTEGER),
    toBlock: timelines.reduce((max, t) => Math.max(max, t.toBlock), 0),
    eventCount: events.length,
    events,
    clusters: timelines.map((t) => withDeployment(t, deploymentsByCluster.get(clusterKey(t.cluster)))),
    stale: stale || undefined,
  };
}

export function createApp(cfg: Config) {
  const client = makeClient(cfg);
  const indexCache = new TtlCache(cfg.cacheTtlMs, () => updateMeshIndex(client, cfg));
  const discoveryCache = new TtlCache<ClusterDeployment[]>(cfg.discoveryCacheTtlMs, () =>
    fetchClusterDeployments(client, cfg),
  );
  const snapshotCaches = new Map<string, TtlCache<Snapshot>>();
  const timelineCaches = new Map<string, TtlCache<Timeline>>();

  const findDeployment = async (requested: string | null): Promise<ClusterDeployment> => {
    const deployments = await discoveryCache.get();
    if (deployments.value.length === 0) throw new Error("no clusters discovered from factory");
    if (!requested) {
      if (deployments.value.length === 1) return deployments.value[0]!;
      throw new Error("multiple clusters discovered; pass ?cluster=0x...");
    }
    const deployment = deployments.value.find((d) => clusterKey(d.cluster) === clusterKey(requested));
    if (!deployment) throw new Error(`unknown cluster ${requested}`);
    return deployment;
  };

  const snapshotCacheFor = (cluster: ClusterDeployment): TtlCache<Snapshot> => {
    const key = clusterKey(cluster.cluster);
    let cache = snapshotCaches.get(key);
    if (!cache) {
      cache = new TtlCache<Snapshot>(cfg.cacheTtlMs, () => fetchSnapshot(client, cfg, cluster));
      snapshotCaches.set(key, cache);
    }
    return cache;
  };

  const timelineCacheFor = (cluster: ClusterDeployment): TtlCache<Timeline> => {
    const key = clusterKey(cluster.cluster);
    let cache = timelineCaches.get(key);
    if (!cache) {
      cache = new TtlCache<Timeline>(cfg.timelineCacheTtlMs, () => fetchTimeline(client, cfg, cluster));
      timelineCaches.set(key, cache);
    }
    return cache;
  };

  const getSnapshots = async (): Promise<{
    deployments: ClusterDeployment[];
    snapshots: Snapshot[];
    stale: boolean;
  }> => {
    if (cfg.indexedReads) {
      const index = await indexCache.get();
      return {
        deployments: deploymentsFromIndex(index.value),
        snapshots: snapshotsFromIndex(index.value, cfg),
        stale: index.stale,
      };
    }
    const deployments = await discoveryCache.get();
    const results = await Promise.all(deployments.value.map((d) => snapshotCacheFor(d).get()));
    return {
      deployments: deployments.value,
      snapshots: results.map((r) => r.value),
      stale: deployments.stale || results.some((r) => r.stale),
    };
  };

  const getTimelines = async (): Promise<{
    deployments: ClusterDeployment[];
    timelines: Timeline[];
    stale: boolean;
  }> => {
    if (cfg.indexedReads) {
      const index = await indexCache.get();
      return {
        deployments: deploymentsFromIndex(index.value),
        timelines: timelinesFromIndex(index.value),
        stale: index.stale,
      };
    }
    const deployments = await discoveryCache.get();
    const results = await Promise.all(deployments.value.map((d) => timelineCacheFor(d).get()));
    return {
      deployments: deployments.value,
      timelines: results.map((r) => r.value),
      stale: deployments.stale || results.some((r) => r.stale),
    };
  };

  const staleHeaders = (stale: boolean): Record<string, string> => (stale ? { "x-mesh-stale": "true" } : {});

  return async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const url = new URL(req.url ?? "/", "http://localhost");
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (req.method !== "GET") {
      json(res, 405, { error: "method_not_allowed", message: "only GET is supported" });
      return;
    }

    try {
      switch (path) {
        case "/mesh/clusters": {
          const c = await getSnapshots();
          json(res, 200, aggregateSnapshots(c.snapshots, c.deployments, c.stale), staleHeaders(c.stale));
          return;
        }
        case "/mesh/members": {
          const clusterParam = url.searchParams.get("cluster");
          if (clusterParam) {
            const c = await getSnapshots();
            const deployment = c.deployments.find((d) => clusterKey(d.cluster) === clusterKey(clusterParam));
            const snapshot = c.snapshots.find((s) => clusterKey(s.cluster) === clusterKey(clusterParam));
            if (!deployment || !snapshot) throw new Error(`unknown cluster ${clusterParam}`);
            json(res, 200, withDeployment({ ...snapshot, stale: c.stale || undefined }, deployment), staleHeaders(c.stale));
            return;
          }
          const c = await getSnapshots();
          json(res, 200, aggregateSnapshots(c.snapshots, c.deployments, c.stale), staleHeaders(c.stale));
          return;
        }
        case "/mesh/topology": {
          const clusterParam = url.searchParams.get("cluster");
          if (clusterParam) {
            const c = await getSnapshots();
            const deployment = c.deployments.find((d) => clusterKey(d.cluster) === clusterKey(clusterParam));
            const snapshot = c.snapshots.find((s) => clusterKey(s.cluster) === clusterKey(clusterParam));
            if (!deployment || !snapshot) throw new Error(`unknown cluster ${clusterParam}`);
            json(
              res,
              200,
              withDeployment({ ...buildTopology(snapshot), stale: c.stale || undefined }, deployment),
              staleHeaders(c.stale),
            );
            return;
          }
          const c = await getSnapshots();
          json(res, 200, aggregateTopologies(c.snapshots, c.deployments, c.stale), staleHeaders(c.stale));
          return;
        }
        case "/mesh/health": {
          const clusterParam = url.searchParams.get("cluster");
          if (clusterParam) {
            const c = await getSnapshots();
            const deployment = c.deployments.find((d) => clusterKey(d.cluster) === clusterKey(clusterParam));
            const snapshot = c.snapshots.find((s) => clusterKey(s.cluster) === clusterKey(clusterParam));
            if (!deployment || !snapshot) throw new Error(`unknown cluster ${clusterParam}`);
            json(
              res,
              200,
              withDeployment({ ...buildHealth(snapshot), stale: c.stale || undefined }, deployment),
              staleHeaders(c.stale),
            );
            return;
          }
          const c = await getSnapshots();
          json(res, 200, aggregateHealth(c.snapshots, c.deployments, c.stale), staleHeaders(c.stale));
          return;
        }
        case "/mesh/timeline": {
          if (!cfg.indexedReads && !cfg.timelineEnabled) {
            json(res, 403, {
              error: "timeline_disabled",
              message: "live timeline scans are disabled; set TIMELINE_ENABLED=1 to allow full-history getLogs reads",
            });
            return;
          }
          const clusterParam = url.searchParams.get("cluster");
          if (clusterParam) {
            const c = await getTimelines();
            const deployment = c.deployments.find((d) => clusterKey(d.cluster) === clusterKey(clusterParam));
            const timeline = c.timelines.find((t) => clusterKey(t.cluster) === clusterKey(clusterParam));
            if (!deployment || !timeline) throw new Error(`unknown cluster ${clusterParam}`);
            json(res, 200, withDeployment({ ...timeline, stale: c.stale || undefined }, deployment), staleHeaders(c.stale));
            return;
          }
          const c = await getTimelines();
          json(res, 200, aggregateTimelines(c.timelines, c.deployments, c.stale), staleHeaders(c.stale));
          return;
        }
        case "/healthz": {
          const peek = discoveryCache.peek();
          try {
            const c = await discoveryCache.get();
            json(res, 200, {
              ok: true,
              rpcReachable: !c.stale,
              clusterCount: c.value.length,
              clusters: c.value,
              cacheAgeMs: Date.now() - c.fetchedAt,
              stale: c.stale || undefined,
            });
          } catch {
            json(res, 503, {
              ok: false,
              rpcReachable: false,
              clusterCount: peek?.value.length ?? null,
              clusters: peek?.value ?? [],
            });
          }
          return;
        }
        default:
          json(res, 404, { error: "not_found", message: `no route ${path}` });
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      json(res, 502, { error: "upstream_rpc_error", message });
    }
  };
}

function startIndexer(cfg: Config): void {
  if (!cfg.indexedReads || cfg.indexRefreshMs <= 0) return;
  const client = makeClient(cfg);
  let inflight: Promise<unknown> | null = null;
  const refresh = () => {
    inflight ??= updateMeshIndex(client, cfg)
      .catch((err) => {
        // eslint-disable-next-line no-console
        console.error(JSON.stringify({ msg: "mesh-state-api index refresh failed", error: String(err) }));
      })
      .finally(() => {
        inflight = null;
      });
  };
  refresh();
  setInterval(refresh, cfg.indexRefreshMs).unref();
}

// Entry point.
const isMain = import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  const cfg = loadConfig();
  const handle = createApp(cfg);
  const server = createServer((req, res) => {
    handle(req, res).catch((err) => {
      json(res, 500, { error: "internal", message: err instanceof Error ? err.message : String(err) });
    });
  });
  server.listen(cfg.listenPort, cfg.listenHost, () => {
    // eslint-disable-next-line no-console
    console.log(
      JSON.stringify({
        msg: "mesh-state-api listening",
        addr: `${cfg.listenHost}:${cfg.listenPort}`,
        clusterFactory: cfg.clusterFactoryAddr,
        chainId: cfg.chainId,
        clusterFactoryStartBlock: String(cfg.clusterFactoryStartBlock),
        gatewayDomain: cfg.gatewayDomain,
      }),
    );
    startIndexer(cfg);
  });
}
