// Pure formatting/derivation helpers (spec §6). No I/O — unit-tested in isolation.

import { keccak256, stringToBytes, getAddress } from "viem";

const ZERO_BYTES32 =
  "0x0000000000000000000000000000000000000000000000000000000000000000";

/** Decode the packed big-endian uint32 returned by meshIpOf() / meshCidr() to a dotted quad. */
export function ipToDotted(packed: number | bigint): string {
  const n = Number(packed) >>> 0; // force uint32
  return `${(n >>> 24) & 0xff}.${(n >>> 16) & 0xff}.${(n >>> 8) & 0xff}.${n & 0xff}`;
}

/** Render meshCidr() (network ip, prefix) as CIDR string, e.g. "10.13.0.0/16". */
export function cidrToString(ip: number | bigint, prefix: number): string {
  return `${ipToDotted(ip)}/${prefix}`;
}

/** appId = the ClusterMember contract address, lowercased, no 0x (the operator join key). */
export function appIdOf(memberContract: string): string {
  return memberContract.toLowerCase().replace(/^0x/, "");
}

/** Checksum an address (EIP-55) for public address fields. */
export function checksum(addr: string): `0x${string}` {
  return getAddress(addr as `0x${string}`);
}

/** null out the zero bytes32 (unset cskCommitment etc.). */
export function nonZeroBytes32(v: string): string | null {
  return v.toLowerCase() === ZERO_BYTES32 ? null : v.toLowerCase();
}

// Known attestor labels → their attestorId. Built once; keys are lowercase hex.
// The set is intentionally tiny and generic — this service encodes NO
// attestation-method specifics beyond a display label (project directive).
const KNOWN_ATTESTOR_LABELS = ["dstack"] as const;
const ATTESTOR_LABEL_BY_ID: Record<string, string> = Object.fromEntries(
  KNOWN_ATTESTOR_LABELS.map((label) => [
    keccak256(stringToBytes(`attestmesh.attestor.${label}`)).toLowerCase(),
    label,
  ]),
);

/** Map an attestorId to a display label; unknown ids get a stable "unknown:0x…" tag. */
export function attestorLabel(attestorId: string): string {
  const id = attestorId.toLowerCase();
  return ATTESTOR_LABEL_BY_ID[id] ?? `unknown:${id.slice(0, 10)}`;
}

/** Derive the wg-over-gateway-TCP endpoint, or null if no gateway domain configured. */
export function deriveEndpoint(appId: string, gatewayDomain: string | null): string | null {
  if (!gatewayDomain) return null;
  return `https://${appId}-51900s.${gatewayDomain}`;
}
