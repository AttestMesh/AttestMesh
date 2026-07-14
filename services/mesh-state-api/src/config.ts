// Configuration (spec §3). All via env; no config files. Only secret is RPC_URL.

export interface Config {
  rpcUrl: string;
  clusterFactoryAddr: `0x${string}`;
  clusterFactoryStartBlock: bigint;
  chainId: number;
  gatewayDomain: string | null;
  listenHost: string;
  listenPort: number;
  cacheTtlMs: number;
  discoveryCacheTtlMs: number;
  timelineCacheTtlMs: number;
  logChunkBlocks: bigint;
  discoveryStatePath: string;
  indexStatePath: string;
  indexedReads: boolean;
  indexRefreshMs: number;
  timelineEnabled: boolean;
  rpcRetryCount: number;
  rpcRetryBaseMs: number;
  rpcRetryMaxMs: number;
  rpcTimeoutMs: number;
  rpcMinIntervalMs: number;
}

function req(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`missing required env ${name}`);
  return v;
}
function opt(name: string, dflt: string): string {
  const v = process.env[name];
  return v === undefined || v === "" ? dflt : v;
}
function optBool(name: string, dflt: boolean): boolean {
  const v = process.env[name];
  if (v === undefined || v === "") return dflt;
  return !["0", "false", "no", "off"].includes(v.toLowerCase());
}
function optInt(name: string, dflt: number): number {
  const raw = opt(name, String(dflt));
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`${name} must be a non-negative integer, got ${raw}`);
  }
  return value;
}

export function loadConfig(): Config {
  const listen = opt("LISTEN_ADDR", "127.0.0.1:8787");
  const lastColon = listen.lastIndexOf(":");
  if (lastColon <= 0) throw new Error(`LISTEN_ADDR must be host:port, got ${listen}`);
  const listenHost = listen.slice(0, lastColon);
  const listenPort = Number(listen.slice(lastColon + 1));
  if (!Number.isInteger(listenPort) || listenPort <= 0 || listenPort > 65535) {
    throw new Error(`LISTEN_ADDR port invalid: ${listen}`);
  }

  return {
    rpcUrl: req("RPC_URL"),
    clusterFactoryAddr: req("CLUSTER_FACTORY_ADDR") as `0x${string}`,
    clusterFactoryStartBlock: BigInt(req("CLUSTER_FACTORY_START_BLOCK")),
    chainId: Number(opt("CHAIN_ID", "8453")),
    gatewayDomain: process.env.GATEWAY_DOMAIN?.trim() || null,
    listenHost,
    listenPort,
    cacheTtlMs: Number(opt("CACHE_TTL_MS", "300000")),
    discoveryCacheTtlMs: Number(opt("DISCOVERY_CACHE_TTL_MS", "3600000")),
    timelineCacheTtlMs: Number(opt("TIMELINE_CACHE_TTL_MS", "3600000")),
    // 9000-block windows stay under the ~10k public-RPC getLogs cap (spec §6).
    logChunkBlocks: BigInt(opt("LOG_CHUNK_BLOCKS", "9000")),
    discoveryStatePath: opt("DISCOVERY_STATE_PATH", `${process.env.HOME ?? "/tmp"}/.cache/mesh-state-api/discovery.json`),
    indexStatePath: opt("INDEX_STATE_PATH", `${process.env.HOME ?? "/tmp"}/.cache/mesh-state-api/index.json`),
    indexedReads: optBool("INDEXED_READS", true),
    indexRefreshMs: Number(opt("INDEX_REFRESH_MS", "300000")),
    timelineEnabled: optBool("TIMELINE_ENABLED", false),
    rpcRetryCount: optInt("RPC_RETRY_COUNT", 5),
    rpcRetryBaseMs: optInt("RPC_RETRY_BASE_MS", 1000),
    rpcRetryMaxMs: optInt("RPC_RETRY_MAX_MS", 30000),
    rpcTimeoutMs: optInt("RPC_TIMEOUT_MS", 30000),
    rpcMinIntervalMs: optInt("RPC_MIN_INTERVAL_MS", 250),
  };
}
