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

import baseX from "base-x";
import { toHex, type Hex } from "viem";

import type { Logger } from "./log.js";
import { DecodeError, type UserOperation } from "./decode.js";

/** Reject events whose timestamp is further than this from now (replay guard). */
const TIMESTAMP_TOLERANCE_SECONDS = 300;

// Pimlico's secret encoding is NON-STANDARD (verified against @pimlico/webhook
// source, pimlicoWebhookVerifier). The HMAC key is derived as:
//   base64Decode( hex( base58Decode_customAlphabet( secret_without_pim_whsec_ ) ) )
// The base58 alphabet below is Pimlico's exact one — note it OMITS both `l` and
// `w` (57 chars, so base-x treats it as base-57). Getting this wrong makes every
// signature fail (live-found 2026-07-06). Do not "simplify" to plain base64.
const PIMLICO_B58 = baseX("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvxyz");

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToHex(b: Uint8Array): string {
  let s = "";
  for (const x of b) s += x.toString(16).padStart(2, "0");
  return s;
}

/** Derive the raw HMAC-SHA256 key from a `pim_whsec_…` secret, matching Pimlico exactly. */
export function pimlicoHmacKey(secret: string): Uint8Array {
  const after = secret.replace(/^pim_whsec_/, "").replace(/^whsec_/, "");
  const decoded = PIMLICO_B58.decode(after); // custom base58 → raw bytes
  const hexStr = bytesToHex(decoded); // hex string of those bytes
  return b64ToBytes(hexStr); // svix then base64-decodes that hex string
}

/** Constant-time byte comparison (both sides are fixed-length HMAC outputs). */
function bytesEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i]! ^ b[i]!;
  return diff === 0;
}

function bytesToB64(b: Uint8Array): string {
  let s = "";
  for (const x of b) s += String.fromCharCode(x);
  return btoa(s);
}

/** Structured verification result so the caller can log exactly why it failed. */
export interface PimlicoVerifyResult {
  ok: boolean;
  reason: "ok" | "missing-headers" | "stale-timestamp" | "secret-undecodable" | "bad-signature";
  /** First 10 chars of the computed vs received base64 sig (diagnostics only). */
  computedHead?: string;
  receivedHead?: string;
}

/**
 * Verify the Standard-Webhooks signature. Never throws; returns a structured result
 * so the route can persist a decision log (Cloudflare tail is unreliable for
 * subrequests).
 */
export async function verifyPimlicoSignature(args: {
  secret: string;
  headers: Headers;
  rawBody: string;
  nowSeconds?: number;
  logger: Logger;
}): Promise<PimlicoVerifyResult> {
  const { secret, headers, rawBody, logger } = args;
  const id = headers.get("webhook-id") ?? headers.get("svix-id");
  const timestamp = headers.get("webhook-timestamp") ?? headers.get("svix-timestamp");
  const sigHeader = headers.get("webhook-signature") ?? headers.get("svix-signature");
  if (!id || !timestamp || !sigHeader) {
    logger.warn("pimlico-missing-signature-headers", {});
    return { ok: false, reason: "missing-headers" };
  }

  const ts = Number(timestamp);
  const now = args.nowSeconds ?? Math.floor(Date.now() / 1000);
  if (!Number.isFinite(ts) || Math.abs(now - ts) > TIMESTAMP_TOLERANCE_SECONDS) {
    logger.warn("pimlico-stale-timestamp", { timestamp });
    return { ok: false, reason: "stale-timestamp" };
  }

  let key: CryptoKey;
  try {
    key = await crypto.subtle.importKey(
      "raw",
      pimlicoHmacKey(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
  } catch {
    logger.error("pimlico-secret-undecodable", {});
    return { ok: false, reason: "secret-undecodable" };
  }
  const signed = new TextEncoder().encode(`${id}.${timestamp}.${rawBody}`);
  const expected = new Uint8Array(await crypto.subtle.sign("HMAC", key, signed));
  const computedHead = bytesToB64(expected).slice(0, 10);

  let receivedHead = "";
  for (const part of sigHeader.split(" ")) {
    const [version, sig] = part.split(",", 2);
    if (version !== "v1" || !sig) continue;
    receivedHead = sig.slice(0, 10);
    try {
      if (bytesEqual(b64ToBytes(sig), expected)) {
        return { ok: true, reason: "ok" };
      }
    } catch {
      /* malformed base64 entry — try the next one */
    }
  }
  logger.warn("pimlico-bad-signature", { computedHead, receivedHead });
  return { ok: false, reason: "bad-signature", computedHead, receivedHead };
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
