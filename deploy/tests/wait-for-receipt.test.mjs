import assert from "node:assert/strict";
import test from "node:test";

import { waitForReceipt } from "../wait-for-receipt.mjs";

const HASH = `0x${"ab".repeat(32)}`;
const receipt = (overrides = {}) => ({
  transactionHash: HASH,
  blockNumber: "0x10",
  status: "0x1",
  ...overrides,
});

function fetchSequence(results) {
  let index = 0;
  return async (_url, request) => {
    const call = JSON.parse(request.body);
    const next = results[Math.min(index++, results.length - 1)];
    const result = typeof next === "function" ? next(call) : next;
    return { ok: true, status: 200, json: async () => ({ jsonrpc: "2.0", id: 1, result }) };
  };
}

class HeadWebSocket {
  listeners = new Map();
  constructor() {
    setTimeout(() => this.emit("open", {}), 0);
  }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  send() {
    setTimeout(() => this.emit("message", { data: JSON.stringify({ id: 1, result: "sub-id" }) }), 0);
    setTimeout(() => this.emit("message", { data: JSON.stringify({
      method: "eth_subscription",
      params: { subscription: "sub-id", result: { number: "0x10" } },
    }) }), 1);
  }
  close() {}
  emit(name, event) { this.listeners.get(name)?.(event); }
}

class FailingWebSocket extends HeadWebSocket {
  constructor() {
    super();
    setTimeout(() => this.emit("error", {}), 0);
  }
}

class MalformedWebSocket extends HeadWebSocket {
  send() {
    setTimeout(() => this.emit("message", { data: "not-json" }), 0);
  }
}

class StallingWebSocket extends HeadWebSocket {
  send() {
    setTimeout(() => this.emit("message", { data: JSON.stringify({ id: 1, result: "sub-id" }) }), 0);
  }
}

test("returns a receipt that already exists without opening WebSocket", async () => {
  class MustNotOpen { constructor() { throw new Error("unexpected WebSocket"); } }
  const result = await waitForReceipt({
    txHash: HASH,
    httpUrl: "https://rpc.invalid",
    wsUrl: "wss://rpc.invalid",
    fetchImpl: fetchSequence([receipt()]),
    WebSocketImpl: MustNotOpen,
  });
  assert.equal(result.mode, "immediate");
});

test("uses newHeads to wake receipt fetching", async () => {
  const result = await waitForReceipt({
    txHash: HASH,
    httpUrl: "https://rpc.invalid",
    wsUrl: "wss://rpc.invalid",
    fetchImpl: fetchSequence([null, receipt()]),
    WebSocketImpl: HeadWebSocket,
    timeoutMs: 100,
  });
  assert.equal(result.mode, "websocket");
});

test("falls back to HTTP polling after WebSocket failure", async () => {
  const logs = [];
  const result = await waitForReceipt({
    txHash: HASH,
    httpUrl: "https://rpc.invalid",
    wsUrl: "wss://rpc.invalid",
    fetchImpl: fetchSequence([null, null, receipt()]),
    WebSocketImpl: FailingWebSocket,
    sleep: async () => {},
    timeoutMs: 100,
    log: (line) => logs.push(line),
  });
  assert.equal(result.mode, "http-poll");
  assert.ok(logs.some((line) => line.includes("falling back")));
});

for (const [name, WebSocketImpl] of [
  ["malformed WebSocket messages", MalformedWebSocket],
  ["a stalled WebSocket subscription", StallingWebSocket],
]) {
  test(`falls back after ${name}`, async () => {
    const result = await waitForReceipt({
      txHash: HASH,
      httpUrl: "https://rpc.invalid",
      wsUrl: "wss://rpc.invalid",
      fetchImpl: fetchSequence([null, receipt()]),
      WebSocketImpl,
      sleep: async () => {},
      timeoutMs: 100,
      wsStallMs: 5,
    });
    assert.equal(result.mode, "http-poll");
  });
}

test("rejects reverted and mismatched receipts", async () => {
  await assert.rejects(() => waitForReceipt({
    txHash: HASH,
    httpUrl: "https://rpc.invalid",
    fetchImpl: fetchSequence([receipt({ status: "0x0" })]),
  }), /reverted/);
  await assert.rejects(() => waitForReceipt({
    txHash: HASH,
    httpUrl: "https://rpc.invalid",
    fetchImpl: fetchSequence([receipt({ transactionHash: `0x${"cd".repeat(32)}` })]),
  }), /does not match/);
});

test("waits for the configured confirmation count", async () => {
  const result = await waitForReceipt({
    txHash: HASH,
    httpUrl: "https://rpc.invalid",
    fetchImpl: fetchSequence([receipt(), "0x10", receipt(), "0x11"]),
    confirmations: 2,
    sleep: async () => {},
    timeoutMs: 100,
  });
  assert.equal(result.mode, "http-poll");
});

test("times out when no receipt appears", async () => {
  await assert.rejects(() => waitForReceipt({
    txHash: HASH,
    httpUrl: "https://rpc.invalid",
    fetchImpl: fetchSequence([null]),
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    timeoutMs: 5,
    pollMs: 1,
  }), /not confirmed/);
});
