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
  BaseError,
  ContractFunctionExecutionError,
  createPublicClient,
  getAddress,
  http,
  HttpRequestError,
  RpcRequestError,
  TimeoutError,
  zeroAddress,
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

// Path A (dstack base KMS): the member contract is a dstack DstackApp upgraded to
// ClusterMember, so it is NOT minted by our member factory. It exposes cluster() and the
// cluster exposes allowedAppIds(appId); together with isDeployedCluster they prove the
// sender is an owner-allowlisted app_id of one of our clusters.
const CLUSTER_OF_ABI = [
  {
    type: "function",
    name: "cluster",
    stateMutability: "view",
    inputs: [],
    outputs: [{ name: "", type: "address" }],
  },
] as const;

const ALLOWED_APP_ID_ABI = [
  {
    type: "function",
    name: "allowedAppIds",
    stateMutability: "view",
    inputs: [{ name: "appId", type: "address" }],
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

/**
 * `isDeployedCluster(target)` against the v1 factory OR the v2 factory when one
 * is configured (multi-attestor spec: the factories coexist; v1 clusters keep
 * working untouched, new deploys come from v2), with caching (spec §6 step 7).
 */
export async function isDeployedCluster(deps: ProvenanceDeps, target: Address): Promise<boolean> {
  const ask = (client: PublicClient, factory: Address) =>
    client.readContract({
      address: factory,
      abi: IS_DEPLOYED_CLUSTER_ABI,
      functionName: "isDeployedCluster",
      args: [getAddress(target)],
    }) as Promise<boolean>;

  return cachedBoolCall({
    deps,
    kv: deps.factoryCache,
    address: target,
    label: "isDeployedCluster",
    call: async (client) => {
      if (await ask(client, deps.config.canonicalClusterFactory)) return true;
      const v2 = deps.config.canonicalClusterFactoryV2;
      return v2 !== undefined && (await ask(client, v2));
    },
  });
}

/** Sentinel for a `view` call that reverted or hit an absent function (vs. a real value). */
const REVERTED = Symbol("reverted");

/**
 * True when the error chain shows a transport/RPC failure rather than a contract-level
 * rejection. viem wraps BOTH a real revert AND a dead-RPC/timeout in
 * `ContractFunctionExecutionError`, so the outer class is not decisive — we must walk the
 * cause chain for a transport error. Non-viem errors are treated as transport (fail safe:
 * retryable, never cached as a denial).
 */
function isTransportFailure(err: unknown): boolean {
  if (!(err instanceof BaseError)) return true;
  return (
    err.walk(
      (e) =>
        e instanceof HttpRequestError ||
        e instanceof TimeoutError ||
        e instanceof RpcRequestError,
    ) !== null
  );
}

/**
 * One `view` read returning its value, {@link REVERTED} (the function reverted or doesn't
 * exist — e.g. `cluster()` on a not-yet-upgraded stock proxy), or throwing
 * {@link RpcFailureError} for a transport failure. The split matters: a revert is cacheable
 * as a definitive "no", a transport flake must NOT be cached (it would wrongly deny
 * sponsorship for the whole negative-cache TTL — the sidecar retry would keep hitting it).
 */
async function readViewOrRevert<T>(args: {
  client: PublicClient;
  address: Address;
  abi: readonly unknown[];
  functionName: string;
  functionArgs?: readonly unknown[];
  label: string;
  logger: Logger;
}): Promise<T | typeof REVERTED> {
  try {
    return (await args.client.readContract({
      address: getAddress(args.address),
      abi: args.abi as never,
      functionName: args.functionName as never,
      args: (args.functionArgs ?? []) as never,
    })) as T;
  } catch (cause) {
    if (cause instanceof ContractFunctionExecutionError && !isTransportFailure(cause)) {
      return REVERTED; // definitive contract-level "no" (revert / absent selector)
    }
    args.logger.error("rpc-call-failed", {
      label: args.label,
      address: args.address,
      error: String(cause),
    });
    throw new RpcFailureError(`${args.label}(${args.address}) eth_call failed`, { cause });
  }
}

/**
 * Path A membership: `sender` is a dstack app upgraded to ClusterMember. It is not a
 * factory member (so {@link isOurMember} is false), but its cluster — which MUST be one of
 * our deployed ClusterDiamonds — has owner-allowlisted its app_id. We read `sender.cluster()`,
 * confirm it via {@link isDeployedCluster} (so a hostile sender can't name a contract it
 * controls), then read `cluster.allowedAppIds(sender)`. Cached under a `pathA:` key so it
 * never collides with the `isOurMember(sender)` entry in the same KV namespace.
 */
export async function isAllowlistedAppId(deps: ProvenanceDeps, sender: Address): Promise<boolean> {
  const { config, logger } = deps;
  const key = `pathA:${cacheKey(config.expectedChainId, sender)}`;

  const cached = await readCache(deps.memberCache, key, logger);
  if (cached !== undefined) return cached;

  const client = deps.client ?? makeClient(config);

  let allowed = false;
  const cluster = await readViewOrRevert<Address>({
    client,
    address: sender,
    abi: CLUSTER_OF_ABI,
    functionName: "cluster",
    label: "cluster",
    logger,
  });
  if (cluster !== REVERTED && cluster !== zeroAddress && (await isDeployedCluster(deps, cluster))) {
    const read = await readViewOrRevert<boolean>({
      client,
      address: cluster,
      abi: ALLOWED_APP_ID_ABI,
      functionName: "allowedAppIds",
      functionArgs: [getAddress(sender)],
      label: "allowedAppIds",
      logger,
    });
    allowed = read === true;
  }

  await writeCache(deps.memberCache, key, allowed, config, logger);
  return allowed;
}
