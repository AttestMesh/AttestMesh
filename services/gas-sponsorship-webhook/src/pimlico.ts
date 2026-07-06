/**
 * Pimlico sponsorship-webhook adapter (docs/research/paymaster-provider-selection.md).
 *
 * Pimlico POSTs `user_operation.sponsorship.requested` events, signed in the
 * Standard Webhooks (svix) convention:
 *   signed content   = `${webhook-id}.${webhook-timestamp}.${rawBody}`
 *   signature header = `webhook-signature: v1,<base64 hmac> [v1,<...> ...]`
 *   key              = base64-decode(secret minus its `pim_whsec_`/`whsec_` prefix)
 * The reply contract is `{"sponsor": true|false}` — policy semantics identical to
 * the Alchemy route: same seven checks + daily cap, different envelope.
 */

import { toHex, type Hex } from "viem";

import type { Logger } from "./log.js";
import { DecodeError, type UserOperation } from "./decode.js";

/** Reject events whose timestamp is further than this from now (replay guard). */
const TIMESTAMP_TOLERANCE_SECONDS = 300;

function stripSecretPrefix(secret: string): string {
  for (const p of ["pim_whsec_", "whsec_"]) {
    if (secret.startsWith(p)) return secret.slice(p.length);
  }
  return secret;
}

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Constant-time byte comparison (both sides are fixed-length HMAC outputs). */
function bytesEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i]! ^ b[i]!;
  return diff === 0;
}

/**
 * Verify the Standard-Webhooks signature. Returns true only for a well-formed,
 * fresh, correctly-signed request. Never throws.
 */
export async function verifyPimlicoSignature(args: {
  secret: string;
  headers: Headers;
  rawBody: string;
  nowSeconds?: number;
  logger: Logger;
}): Promise<boolean> {
  const { secret, headers, rawBody, logger } = args;
  const id = headers.get("webhook-id");
  const timestamp = headers.get("webhook-timestamp");
  const sigHeader = headers.get("webhook-signature");
  if (!id || !timestamp || !sigHeader) {
    logger.warn("pimlico-missing-signature-headers", {});
    return false;
  }

  const ts = Number(timestamp);
  const now = args.nowSeconds ?? Math.floor(Date.now() / 1000);
  if (!Number.isFinite(ts) || Math.abs(now - ts) > TIMESTAMP_TOLERANCE_SECONDS) {
    logger.warn("pimlico-stale-timestamp", { timestamp });
    return false;
  }

  let key: CryptoKey;
  try {
    key = await crypto.subtle.importKey(
      "raw",
      b64ToBytes(stripSecretPrefix(secret)),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
  } catch {
    logger.error("pimlico-secret-undecodable", {});
    return false;
  }
  const signed = new TextEncoder().encode(`${id}.${timestamp}.${rawBody}`);
  const expected = new Uint8Array(await crypto.subtle.sign("HMAC", key, signed));

  // Header may carry several space-separated `v1,<b64>` entries (key rotation).
  for (const part of sigHeader.split(" ")) {
    const [version, sig] = part.split(",", 2);
    if (version !== "v1" || !sig) continue;
    try {
      if (bytesEqual(b64ToBytes(sig), expected)) return true;
    } catch {
      /* malformed base64 entry — try the next one */
    }
  }
  logger.warn("pimlico-bad-signature", {});
  return false;
}

/** The subset of the Pimlico event we act on. */
export interface PimlicoSponsorshipRequest {
  chainId: Hex;
  userOperation: UserOperation;
  sponsorshipPolicyId?: string;
}

/**
 * Parse a `user_operation.sponsorship.requested` event body into the shape the
 * shared policy engine consumes. Throws {@link DecodeError} on anything malformed
 * and on non-sponsorship event types (callers reply `{sponsor:false}` to those).
 */
export function parsePimlicoBody(raw: unknown): PimlicoSponsorshipRequest {
  if (typeof raw !== "object" || raw === null) throw new DecodeError("body not an object");
  const evt = raw as Record<string, unknown>;
  if (evt.type !== "user_operation.sponsorship.requested") {
    throw new DecodeError(`unexpected event type: ${String(evt.type)}`);
  }
  const data = evt.data as Record<string, unknown> | undefined;
  const obj = (data?.object ?? data) as Record<string, unknown> | undefined;
  if (!obj) throw new DecodeError("missing data.object");

  const op = obj.userOperation as Record<string, unknown> | undefined;
  if (!op || typeof op.sender !== "string" || typeof op.callData !== "string") {
    throw new DecodeError("missing userOperation.sender/callData");
  }

  const rawChain = obj.chainId;
  let chainId: Hex;
  if (typeof rawChain === "number") chainId = toHex(rawChain);
  else if (typeof rawChain === "string" && rawChain.startsWith("0x")) chainId = rawChain as Hex;
  else if (typeof rawChain === "string" && /^\d+$/.test(rawChain)) chainId = toHex(Number(rawChain));
  else throw new DecodeError(`bad chainId: ${String(rawChain)}`);

  return {
    chainId,
    userOperation: { sender: op.sender as Hex, callData: op.callData as Hex },
    sponsorshipPolicyId:
      typeof obj.sponsorshipPolicyId === "string" ? obj.sponsorshipPolicyId : undefined,
  };
}
