// Operator-tier image-pinning join. The spec allows operator-tier fields to be
// sourced from the host; this on-chain reader cannot reach the host VMM, so
// box-admin computes per-app digest-pinning from the VMM and publishes it at
// PINNING_SOURCE_URL. We merge that map onto members by appId at the response
// boundary (see server.ts) — chain.ts stays purely on-chain/index shaped.
//
// Disabled (total no-op) when PINNING_SOURCE_URL is unset, so the public
// instance — which has no box-admin to reach — is unaffected. Fail-open: any
// fetch error serves the last-known map (or empty → imagePinning: null), so a
// box-admin blip degrades to an honest "unknown" rather than a hard failure.

import type { Snapshot } from "./chain.ts";

export interface ImagePinning {
  digestPinned: boolean;
  images: { service: string | null; image: string; digestPinned: boolean }[];
}

type PinningMap = Record<string, ImagePinning | null>;

// box-admin recomputes cheaply from a local VMM call; a short TTL bounds how
// often we call it under load without letting the join go meaningfully stale.
const PINNING_CACHE_TTL_MS = 15_000;
const PINNING_FETCH_TIMEOUT_MS = 4_000;

let cache: { at: number; map: PinningMap } | null = null;

// Match box-admin's key format (app_id lowercase, no 0x). mesh-state-api's own
// appId is already normalized, but normalize defensively so a 0x/mixed-case
// member.appId never silently misses the lookup.
function normAppId(value: unknown): string {
  return String(value ?? "").toLowerCase().replace(/^0x/, "");
}

export function pinningSourceUrl(): string | null {
  return process.env.PINNING_SOURCE_URL?.trim() || null;
}

async function fetchPinningMap(url: string): Promise<PinningMap> {
  const now = Date.now();
  if (cache && now - cache.at < PINNING_CACHE_TTL_MS) return cache.map;
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(PINNING_FETCH_TIMEOUT_MS) });
    if (!res.ok) throw new Error(`pinning source HTTP ${res.status}`);
    const body = (await res.json()) as { pinning?: unknown };
    const map =
      body && typeof body.pinning === "object" && body.pinning !== null
        ? (body.pinning as PinningMap)
        : {};
    cache = { at: now, map };
    return map;
  } catch (err) {
    // fail-open: last-known map if we have one, else empty (→ null per member)
    console.warn(JSON.stringify({ msg: "mesh-state-api pinning fetch failed", error: String(err) }));
    return cache?.map ?? {};
  }
}

// Returns NEW snapshot objects with NEW member objects carrying imagePinning;
// the cached Snapshot objects from chain.ts are never mutated.
export async function withImagePinning(snapshots: Snapshot[]): Promise<Snapshot[]> {
  const url = pinningSourceUrl();
  if (!url) return snapshots; // gate off → public instance no-op
  const map = await fetchPinningMap(url);
  return snapshots.map((s) => ({
    ...s,
    members: s.members.map((m) => ({ ...m, imagePinning: map[normAppId(m.appId)] ?? null })),
  }));
}
