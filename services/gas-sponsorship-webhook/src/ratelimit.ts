/**
 * Per-sender daily sponsorship cap (spec §6 step 8; security audit finding M2).
 *
 * A KV counter under `cap:{chainId}:{sender}:{YYYY-MM-DD}` (UTC day) is incremented
 * once per otherwise-approved UserOp; once it reaches `config.maxDailyOpsPerSender`,
 * further ops from that sender are denied until the day rolls over. Denials never
 * write, so a capped runaway sender costs one KV read per request.
 *
 * Two deliberate softnesses:
 *   - KV is eventually consistent across edge locations, so a concurrent burst can
 *     overshoot the cap by a few ops. The goal is bounding runaway loops (a sidecar
 *     resend bug once burned ~1.3k sponsored ops/day), not exact metering.
 *   - On any KV error (or an absent binding) we fail OPEN: sponsorship availability
 *     beats strict limiting, matching the provenance-cache philosophy (spec §9).
 */

import { getAddress, type Address } from "viem";

import type { ProvenanceDeps } from "./provenance.js";

/** Counter entries outlive their UTC day by one more so a day-boundary read never
 * resurrects a stale count; KV expiry does the cleanup. */
const CAP_TTL_SECONDS = 2 * 86_400;

function capKey(chainId: number, sender: Address, day: string): string {
  return `cap:${chainId}:${getAddress(sender)}:${day}`;
}

/**
 * True when `sender` is under its daily cap (and counts this op); false when the
 * cap is exhausted. A cap of 0 disables the check entirely.
 */
export async function underDailyCap(deps: ProvenanceDeps, sender: Address): Promise<boolean> {
  const { config, logger } = deps;
  const cap = config.maxDailyOpsPerSender;
  if (cap <= 0) return true;
  const kv = deps.memberCache;
  if (!kv) return true;

  const day = new Date().toISOString().slice(0, 10);
  const key = capKey(config.expectedChainId, sender, day);
  try {
    const raw = await kv.get(key);
    const parsed = raw === null ? 0 : Number(raw);
    const used = Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0;
    if (used >= cap) {
      logger.warn("sender-daily-cap", { sender, used, cap });
      return false;
    }
    await kv.put(key, String(used + 1), { expirationTtl: CAP_TTL_SECONDS });
    return true;
  } catch (cause) {
    logger.warn("cap-kv-failed", { sender, error: String(cause) });
    return true;
  }
}
