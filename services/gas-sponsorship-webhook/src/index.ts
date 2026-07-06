/**
 * Worker entry + router (spec §5).
 *
 *   POST /                       — Alchemy custom-rules webhook. Token in `?token=`.
 *                                  Runs the policy, returns `{ approved }` (+ `reason`).
 *   GET  /check?cluster=<addr>   — sidecar startup probe → `{ deployed, sponsored }`.
 *   GET  /healthz                — uptime probe → `{ ok: true }` (200) or 503 if RPC down.
 *
 * A malformed POST body yields 400 and never crashes the worker (spec §9).
 */

import {
  createPublicClient,
  http,
  isAddress,
  getAddress,
  type Address,
  type PublicClient,
} from "viem";

import { EnvError, parseEnv, type Config, type Env } from "./env.js";
import { createLogger, type Logger } from "./log.js";
import { DecodeError, parseWebhookBody } from "./decode.js";
import { evaluatePolicy } from "./policy.js";
import { parsePimlicoBody, verifyPimlicoSignature } from "./pimlico.js";
import { isDeployedCluster, RpcFailureError, type ProvenanceDeps } from "./provenance.js";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function buildProvenanceDeps(config: Config, env: Env, logger: Logger): ProvenanceDeps {
  return {
    config,
    factoryCache: env.FACTORY_PROVENANCE_CACHE,
    memberCache: env.MEMBER_PROVENANCE_CACHE,
    logger,
  };
}

/** POST / — the paymaster decision endpoint (spec §5.1, §6). */
async function handleWebhook(
  request: Request,
  url: URL,
  config: Config,
  env: Env,
  logger: Logger,
): Promise<Response> {
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    logger.warn("bad-body", { reason: "json-parse" });
    return json({ error: "malformed-json" }, 400);
  }

  let body;
  try {
    body = parseWebhookBody(raw);
  } catch (err) {
    if (err instanceof DecodeError) {
      logger.warn("bad-body", { reason: err.message });
      return json({ error: "malformed-userop" }, 400);
    }
    throw err;
  }

  const decision = await evaluatePolicy(
    {
      token: url.searchParams.get("token"),
      chainId: body.chainId,
      userOperation: body.userOperation,
    },
    config,
    buildProvenanceDeps(config, env, logger),
  );

  if (decision.approved) {
    logger.info("decision", { approved: true, sender: body.userOperation.sender });
    return json({ approved: true });
  }
  logger.info("decision", {
    approved: false,
    reason: decision.reason,
    sender: body.userOperation.sender,
  });
  return json({ approved: false, reason: decision.reason });
}

/**
 * POST /pimlico — Pimlico sponsorship-policy webhook (Standard-Webhooks signed).
 * Same policy engine as the Alchemy route; reply contract is `{"sponsor": bool}`.
 * Authentication is the HMAC signature (no `?token=`), so the policy's step-1
 * token check is satisfied internally after verification.
 */
async function handlePimlico(
  request: Request,
  config: Config,
  env: Env,
  logger: Logger,
): Promise<Response> {
  if (!config.pimlicoWebhookSecret) {
    return json({ error: "pimlico-route-disabled" }, 503);
  }
  const rawBody = await request.text();
  const verify = await verifyPimlicoSignature({
    secret: config.pimlicoWebhookSecret,
    headers: request.headers,
    rawBody,
    logger,
  });

  // Persist a decision log to KV (readable via GET /pimlico-status); Cloudflare
  // tail misses these subrequests, so this is our observability into the flow.
  const record = async (outcome: Record<string, unknown>) => {
    try {
      const ptr = { at: new Date().toISOString(), ...outcome };
      await env.MEMBER_PROVENANCE_CACHE.put("log:pimlico:last", JSON.stringify(ptr), {
        expirationTtl: 86400,
      });
    } catch {
      /* best-effort */
    }
  };

  if (!verify.ok) {
    await record({
      stage: "verify",
      ok: false,
      reason: verify.reason,
      computedHead: verify.computedHead,
      receivedHead: verify.receivedHead,
    });
    return json({ error: "bad-signature", reason: verify.reason }, 401);
  }

  let parsed;
  try {
    parsed = parsePimlicoBody(JSON.parse(rawBody));
  } catch (err) {
    if (err instanceof DecodeError || err instanceof SyntaxError) {
      logger.warn("pimlico-bad-body", { reason: String(err) });
      await record({ stage: "parse", ok: false, reason: String(err).slice(0, 80) });
      // Unknown/malformed events are refused sponsorship, not 4xx'd — Pimlico
      // treats non-200s as transport errors and may retry.
      return json({ sponsor: false });
    }
    throw err;
  }

  const decision = await evaluatePolicy(
    {
      token: config.alchemyWebhookToken, // signature already authenticated the caller
      chainId: parsed.chainId,
      userOperation: parsed.userOperation,
    },
    config,
    buildProvenanceDeps(config, env, logger),
  );
  logger.info("pimlico-decision", {
    sponsor: decision.approved,
    ...(decision.approved ? {} : { reason: decision.reason }),
    sender: parsed.userOperation.sender,
  });
  await record({
    stage: "decision",
    ok: true,
    sponsor: decision.approved,
    denyReason: decision.approved ? undefined : decision.reason,
    sender: parsed.userOperation.sender,
  });
  return json({ sponsor: decision.approved });
}

/** GET /check?cluster=<addr> — sidecar startup probe (spec §5.2). */
async function handleCheck(
  url: URL,
  config: Config,
  env: Env,
  logger: Logger,
): Promise<Response> {
  const cluster = url.searchParams.get("cluster");
  if (!cluster || !isAddress(cluster)) {
    return json({ error: "missing-or-invalid-cluster" }, 400);
  }
  const target: Address = getAddress(cluster);

  try {
    const deployed = await isDeployedCluster(buildProvenanceDeps(config, env, logger), target);
    // `sponsored` mirrors `deployed` today; reserved for v1.5+ quota logic (spec §5.2).
    return json({ deployed, sponsored: deployed });
  } catch (err) {
    if (err instanceof RpcFailureError) {
      logger.error("check-rpc-failure", { cluster: target });
      return json({ error: "rpc-failure" }, 503);
    }
    throw err;
  }
}

// 60s in-memory cache for the healthz chain-id probe (spec §5.3). Per-isolate;
// good enough to keep a steady uptime poll off the RPC on every hit.
const HEALTH_PROBE_TTL_MS = 60_000;
let healthProbe: { ok: boolean; at: number } | undefined;

/** GET /healthz — uptime probe; 200 if RPC reachable, else 503 (spec §5.3). */
async function handleHealthz(
  config: Config,
  logger: Logger,
  clientFactory: (cfg: Config) => PublicClient,
): Promise<Response> {
  const now = Date.now();
  if (healthProbe && now - healthProbe.at < HEALTH_PROBE_TTL_MS) {
    return healthProbe.ok ? json({ ok: true }) : json({ ok: false }, 503);
  }

  let ok = false;
  try {
    await clientFactory(config).getChainId();
    ok = true;
  } catch (cause) {
    logger.error("healthz-rpc-unreachable", { error: String(cause) });
  }
  healthProbe = { ok, at: now };
  return ok ? json({ ok: true }) : json({ ok: false }, 503);
}

/** Reset the healthz cache. Test-only seam. */
export function __resetHealthProbe(): void {
  healthProbe = undefined;
}

const defaultClientFactory = (config: Config): PublicClient =>
  createPublicClient({ transport: http(config.rpcUrl, { retryCount: 0 }) });

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    // Validate config once per request; misconfiguration is a 500, not a crash.
    let config: Config;
    let logger: Logger;
    try {
      config = parseEnv(env);
      logger = createLogger(config.logLevel);
    } catch (err) {
      if (err instanceof EnvError) {
        console.error(JSON.stringify({ level: "error", event: "bad-env", error: err.message }));
        return json({ error: "server-misconfigured" }, 500);
      }
      throw err;
    }

    const url = new URL(request.url);
    const { pathname } = url;

    try {
      if (request.method === "POST" && pathname === "/") {
        return await handleWebhook(request, url, config, env, logger);
      }
      if (request.method === "POST" && pathname === "/pimlico") {
        return await handlePimlico(request, config, env, logger);
      }
      if (request.method === "GET" && pathname === "/pimlico-status") {
        // Token-gated observability into the last webhook decision (TEE blocks the
        // sidecar's container logs, so this is our window during the fleet roll).
        if (url.searchParams.get("token") !== config.alchemyWebhookToken) {
          return json({ error: "unauthorized" }, 401);
        }
        const v = await env.MEMBER_PROVENANCE_CACHE.get("log:pimlico:last");
        return json(v ? JSON.parse(v) : { empty: true });
      }
      if (request.method === "GET" && pathname === "/check") {
        return await handleCheck(url, config, env, logger);
      }
      if (request.method === "GET" && pathname === "/healthz") {
        return await handleHealthz(config, logger, defaultClientFactory);
      }
      return json({ error: "not-found" }, 404);
    } catch (err) {
      // Last-resort guard: never let an unexpected throw crash the isolate.
      logger.error("unhandled", { error: String(err), path: pathname });
      return json({ error: "internal-error" }, 500);
    }
  },
} satisfies ExportedHandler<Env>;
