import { test } from "node:test";
import assert from "node:assert/strict";
import {
  appIdOf,
  attestorLabel,
  checksum,
  cidrToString,
  deriveEndpoint,
  ipToDotted,
  nonZeroBytes32,
} from "../src/format.ts";
import { buildTopology, buildHealth } from "../src/server.ts";
import type { Snapshot } from "../src/chain.ts";

test("ipToDotted decodes packed big-endian uint32", () => {
  assert.equal(ipToDotted(0x0a120000), "10.18.0.0");
  assert.equal(ipToDotted(0x0a0d2ef1), "10.13.46.241"); // arbitrary decode fixture
  assert.equal(ipToDotted(168951808), "10.18.0.0"); // C3 meshCidr() live value
});

test("cidrToString renders network/prefix", () => {
  assert.equal(cidrToString(168951808, 16), "10.18.0.0/16"); // C3 live
});

test("appIdOf lowercases and strips 0x (the operator join key)", () => {
  assert.equal(appIdOf("0xF847Dd18F55aBB9C5d62BD60A3c2bD4a7Ee6555A"), "f847dd18f55abb9c5d62bd60a3c2bd4a7ee6555a");
});

test("checksum yields EIP-55 mixed case", () => {
  assert.equal(checksum("0xf847dd18f55abb9c5d62bd60a3c2bd4a7ee6555a"), "0xF847Dd18F55aBB9C5d62BD60A3c2bD4a7Ee6555A");
});

test("attestorLabel maps dstack and tags unknowns", () => {
  assert.equal(attestorLabel(attestorIdForDstack()), "dstack");
  assert.match(attestorLabel("0xdeadbeef" + "00".repeat(28)), /^unknown:0xdeadbeef$/);
});

test("nonZeroBytes32 nulls the zero word", () => {
  assert.equal(nonZeroBytes32("0x" + "00".repeat(32)), null);
  assert.equal(nonZeroBytes32("0x3ac453f5" + "00".repeat(28)), "0x3ac453f5" + "00".repeat(28));
});

test("deriveEndpoint honours gateway domain", () => {
  assert.equal(deriveEndpoint("f847dd18", null), null);
  assert.equal(deriveEndpoint("f847dd18", "example.net"), "https://f847dd18-51900s.example.net");
});

test("buildTopology emits a complete graph and preserves originator", () => {
  const s = fakeSnapshot(3);
  const topo = buildTopology(s);
  assert.equal(topo.nodes.length, 3);
  assert.equal(topo.edges.length, 3); // 3 choose 2
  assert.ok(topo.edges.every((e) => e.state === "unknown"));
  assert.equal(topo.nodes[0]!.isOriginator, true);
});

test("buildHealth reports csk + count without liveness", () => {
  const h = buildHealth(fakeSnapshot(2));
  assert.equal(h.memberCount, 2);
  assert.equal(h.cskCommitted, true);
  assert.ok("note" in h);
});

// helpers -------------------------------------------------------------

import { keccak256, stringToBytes } from "viem";
function attestorIdForDstack(): string {
  return keccak256(stringToBytes("attestmesh.attestor.dstack"));
}

function fakeSnapshot(n: number): Snapshot {
  return {
    cluster: "0x5ab4706fCa998A0792E5c06432b13c73c54E4557",
    chainId: 8453,
    atBlock: 48079256,
    meshCidr: "10.18.0.0/16",
    cskCommitment: "0x3ac453f5" + "00".repeat(28),
    originatorMemberId: "0x" + "11".repeat(32),
    memberCount: n,
    members: Array.from({ length: n }, (_, i) => ({
      memberId: "0x" + String(i).padStart(2, "0").repeat(32).slice(0, 64),
      memberContract: "0xF847Dd18F55aBB9C5d62BD60A3c2bD4a7Ee6555A" as `0x${string}`,
      appId: "f847dd18f55abb9c5d62bd60a3c2bd4a7ee6555a",
      attestorId: "0x" + "22".repeat(32),
      attestor: "dstack",
      xPubKey: "0x" + "33".repeat(32),
      wgPubKey: "0x" + "44".repeat(32),
      meshIp: "10.13.1.1",
      registeredAt: 1781843777,
      isOriginator: i === 0,
      endpoint: null,
      vm: null,
      health: null,
    })),
  };
}
