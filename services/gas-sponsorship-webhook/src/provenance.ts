/**
 * On-chain provenance lookups with KV caching (spec §6 steps 3 & 7, §8, §9).
 *
 *   isOurMember(sender)       → ClusterMemberFactory  → MEMBER_PROVENANCE_CACHE
 *   isDeployedCluster(target) → ClusterDiamondFactory → FACTORY_PROVENANCE_CACHE
 *
 * Both are `view` functions returning `bool`, read via viem `readContract`.
 *
 * Caching (spec §8):
 *   - key            `${chainId}:${address}` (address checksum-normalized).
 *   - positive (true) answers  → TTL = config.cacheTtlSeconds (default 24h);
 *     deployed contracts can't be un-deployed.
 *   - negative (false) answers → TTL = config.negativeCacheTtlSeconds (10 min);
 *     a freshly deployed member/cluster surfaces within that window.
 *
 * Failure handling (spec §9):
 *   - RPC unreachable → throw {@link RpcFailureError} so policy maps it to "rpc-failure".
 *   - KV unavailable  → swallow the KV error, fall back to a direct RPC read.
 */

import {
  createPublicClient,
  getAddress,
  http,
  type Address,
  type PublicClient,
} from "viem";

import type { Config } from "./env.js";
import type { Logger } from "./log.js";

const IS_OUR_MEMBER_ABI = [
  {
    type: "function",
    name: "isOurMember",
    stateMutability: "view",
    inputs: [{ name: "account", type: "address" }],
    outputs: [{ name: "", type: "bool" }],
  },
] as const;

const IS_DEPLOYED_CLUSTER_ABI = [
  {
    type: "function",
    name: "isDeployedCluster",
    stateMutability: "view",
    inputs: [{ name: "account", type: "address" }],
    outputs: [{ name: "", type: "bool" }],
  },
] as const;

/** Thrown when the RPC endpoint cannot be reached / the eth_call fails (spec §9). */
export class RpcFailureError extends Error {
  override readonly name = "RpcFailureError";
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
  }
}

/** Dependencies for the provenance helpers. KV namespaces may be absent (spec §9). */
export interface ProvenanceDeps {
  config: Config;
  factoryCache: KVNamespace | undefined;
  memberCache: KVNamespace | undefined;
  logger: Logger;
  /** Override for tests; defaults to a viem http() client against `config.rpcUrl`. */
  client?: PublicClient;
}

function cacheKey(chainId: number, address: Address): string {
  return `${chainId}:${getAddress(address)}`;
}

function makeClient(config: Config): PublicClient {
  // No retries: the webhook is on Alchemy's critical sponsorship path. A dead RPC
  // should fail fast → "rpc-failure" → sidecar retries later (spec §9), rather
  // than holding the paymaster request open through viem's default backoff.
  return createPublicClient({ transport: http(config.rpcUrl, { retryCount: 0 }) });
}

/** Read a cached boolean from KV. Returns undefined on miss or any KV error. */
async function readCache(
  kv: KVNamespace | undefined,
  key: string,
  logger: Logger,
): Promise<boolean | undefined> {
  if (!kv) return undefined;
  try {
    const raw = await kv.get(key);
    if (raw === "1") return true;
    if (raw === "0") return false;
    return undefined;
  } catch (cause) {
    logger.warn("kv-read-failed", { key, error: String(cause) });
    return undefined;
  }
}

/** Write a boolean to KV with the TTL appropriate to its sign. Best-effort. */
async function writeCache(
  kv: KVNamespace | undefined,
  key: string,
  value: boolean,
  config: Config,
  logger: Logger,
): Promise<void> {
  if (!kv) return;
  const ttl = value ? config.cacheTtlSeconds : config.negativeCacheTtlSeconds;
  try {
    await kv.put(key, value ? "1" : "0", { expirationTtl: ttl });
  } catch (cause) {
    logger.warn("kv-write-failed", { key, error: String(cause) });
  }
}

/**
 * Shared cache-through read: check KV, else eth_call, else persist.
 * Throws {@link RpcFailureError} when the underlying read fails on a cache miss.
 */
async function cachedBoolCall(args: {
  deps: ProvenanceDeps;
  kv: KVNamespace | undefined;
  address: Address;
  call: (client: PublicClient) => Promise<boolean>;
  label: string;
}): Promise<boolean> {
  const { deps, kv, address, call, label } = args;
  const { config, logger } = deps;
  const key = cacheKey(config.expectedChainId, address);

  const cached = await readCache(kv, key, logger);
  if (cached !== undefined) return cached;

  const client = deps.client ?? makeClient(config);
  let result: boolean;
  try {
    result = await call(client);
  } catch (cause) {
    logger.error("rpc-call-failed", { label, address, error: String(cause) });
    throw new RpcFailureError(`${label}(${address}) eth_call failed`, { cause });
  }

  await writeCache(kv, key, result, config, logger);
  return result;
}

/** `ClusterMemberFactory.isOurMember(sender)` with caching (spec §6 step 3). */
export async function isOurMember(deps: ProvenanceDeps, sender: Address): Promise<boolean> {
  return cachedBoolCall({
    deps,
    kv: deps.memberCache,
    address: sender,
    label: "isOurMember",
    call: (client) =>
      client.readContract({
        address: deps.config.canonicalMemberFactory,
        abi: IS_OUR_MEMBER_ABI,
        functionName: "isOurMember",
        args: [getAddress(sender)],
      }) as Promise<boolean>,
  });
}

/** `ClusterDiamondFactory.isDeployedCluster(target)` with caching (spec §6 step 7). */
export async function isDeployedCluster(deps: ProvenanceDeps, target: Address): Promise<boolean> {
  return cachedBoolCall({
    deps,
    kv: deps.factoryCache,
    address: target,
    label: "isDeployedCluster",
    call: (client) =>
      client.readContract({
        address: deps.config.canonicalClusterFactory,
        abi: IS_DEPLOYED_CLUSTER_ABI,
        functionName: "isDeployedCluster",
        args: [getAddress(target)],
      }) as Promise<boolean>,
  });
}
