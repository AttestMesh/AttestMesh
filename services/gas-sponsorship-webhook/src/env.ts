/**
 * Worker environment: KV bindings + plaintext/secret vars (spec §4).
 *
 * `Env` is the raw shape Cloudflare hands to `fetch(request, env, ctx)`. Values
 * arrive as strings (or KVNamespace objects); {@link parseEnv} validates them
 * and produces a normalized {@link Config} the rest of the worker consumes.
 */

import { getAddress, isAddress, type Address } from "viem";

export type LogLevel = "info" | "warn" | "error";

/** Raw environment as provided by the Workers runtime. */
export interface Env {
  // KV bindings (spec §4, §8).
  FACTORY_PROVENANCE_CACHE: KVNamespace;
  MEMBER_PROVENANCE_CACHE: KVNamespace;

  // Plaintext vars.
  EXPECTED_CHAIN_ID: string;
  CANONICAL_CLUSTER_FACTORY: string;
  CANONICAL_MEMBER_FACTORY: string;
  CACHE_TTL_SECONDS?: string;
  LOG_LEVEL?: string;

  // Secrets.
  RPC_URL: string;
  ALCHEMY_WEBHOOK_TOKEN: string;
}

/** Validated, normalized configuration derived from {@link Env}. */
export interface Config {
  expectedChainId: number;
  canonicalClusterFactory: Address;
  canonicalMemberFactory: Address;
  rpcUrl: string;
  alchemyWebhookToken: string;
  cacheTtlSeconds: number;
  /** TTL for negative (`false`) answers — short so new members/clusters surface quickly (spec §8). */
  negativeCacheTtlSeconds: number;
  logLevel: LogLevel;
}

/** Full default TTL for positive answers when `CACHE_TTL_SECONDS` is unset (spec §4, §8). */
export const DEFAULT_CACHE_TTL_SECONDS = 86_400;
/** Fixed TTL for negative answers (spec §8). */
export const NEGATIVE_CACHE_TTL_SECONDS = 600;
/** Cloudflare KV enforces a 60s floor on expirationTtl. */
const KV_MIN_TTL_SECONDS = 60;

export class EnvError extends Error {
  override readonly name = "EnvError";
}

function parseLogLevel(raw: string | undefined): LogLevel {
  switch (raw) {
    case undefined:
    case "":
    case "info":
      return "info";
    case "warn":
      return "warn";
    case "error":
      return "error";
    default:
      throw new EnvError(`LOG_LEVEL must be one of info|warn|error, got "${raw}"`);
  }
}

function parseAddressVar(name: string, raw: string | undefined): Address {
  // Accept any casing — operators commonly paste lowercase addresses into env
  // vars. We checksum-normalize below, so a strict EIP-55 check here would only
  // create avoidable misconfiguration failures.
  if (!raw || !isAddress(raw, { strict: false })) {
    throw new EnvError(`${name} must be a valid hex address, got "${raw ?? ""}"`);
  }
  // Checksum-normalize so cache keys are stable regardless of input casing.
  return getAddress(raw);
}

/**
 * Validate and normalize {@link Env}. Throws {@link EnvError} on any missing or
 * malformed required value so the caller can surface a 500 rather than crash mid-policy.
 */
export function parseEnv(env: Env): Config {
  const chainId = Number(env.EXPECTED_CHAIN_ID);
  if (!Number.isInteger(chainId) || chainId <= 0) {
    throw new EnvError(`EXPECTED_CHAIN_ID must be a positive integer, got "${env.EXPECTED_CHAIN_ID}"`);
  }

  if (typeof env.RPC_URL !== "string" || env.RPC_URL.length === 0) {
    throw new EnvError("RPC_URL is required");
  }
  if (typeof env.ALCHEMY_WEBHOOK_TOKEN !== "string" || env.ALCHEMY_WEBHOOK_TOKEN.length === 0) {
    throw new EnvError("ALCHEMY_WEBHOOK_TOKEN is required");
  }

  let cacheTtl = DEFAULT_CACHE_TTL_SECONDS;
  if (env.CACHE_TTL_SECONDS !== undefined && env.CACHE_TTL_SECONDS !== "") {
    const parsed = Number(env.CACHE_TTL_SECONDS);
    if (!Number.isInteger(parsed) || parsed < KV_MIN_TTL_SECONDS) {
      throw new EnvError(
        `CACHE_TTL_SECONDS must be an integer >= ${KV_MIN_TTL_SECONDS}, got "${env.CACHE_TTL_SECONDS}"`,
      );
    }
    cacheTtl = parsed;
  }

  return {
    expectedChainId: chainId,
    canonicalClusterFactory: parseAddressVar("CANONICAL_CLUSTER_FACTORY", env.CANONICAL_CLUSTER_FACTORY),
    canonicalMemberFactory: parseAddressVar("CANONICAL_MEMBER_FACTORY", env.CANONICAL_MEMBER_FACTORY),
    rpcUrl: env.RPC_URL,
    alchemyWebhookToken: env.ALCHEMY_WEBHOOK_TOKEN,
    cacheTtlSeconds: cacheTtl,
    negativeCacheTtlSeconds: NEGATIVE_CACHE_TTL_SECONDS,
    logLevel: parseLogLevel(env.LOG_LEVEL),
  };
}
