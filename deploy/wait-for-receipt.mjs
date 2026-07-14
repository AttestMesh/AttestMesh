#!/usr/bin/env node

import { pathToFileURL } from "node:url";

const TX_HASH_RE = /^0x[0-9a-fA-F]{64}$/;

class TerminalReceiptError extends Error {}

function asPositiveInteger(value, name) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

async function rpc(fetchImpl, url, method, params, signal) {
  const response = await fetchImpl(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    signal,
  });
  if (!response.ok) throw new Error(`${method} HTTP ${response.status}`);
  const body = await response.json();
  if (body.error) throw new Error(`${method}: ${JSON.stringify(body.error)}`);
  return body.result;
}

function parseQuantity(value, name) {
  if (typeof value !== "string" || !/^0x[0-9a-fA-F]+$/.test(value)) {
    throw new TerminalReceiptError(`receipt has invalid ${name}`);
  }
  return BigInt(value);
}

async function confirmedReceipt({ fetchImpl, httpUrl, txHash, confirmations, signal }) {
  const receipt = await rpc(fetchImpl, httpUrl, "eth_getTransactionReceipt", [txHash], signal);
  if (receipt == null) return null;
  if (String(receipt.transactionHash).toLowerCase() !== txHash.toLowerCase()) {
    throw new TerminalReceiptError(`receipt transactionHash does not match ${txHash}`);
  }
  const status = parseQuantity(receipt.status, "status");
  if (status === 0n) throw new TerminalReceiptError(`transaction reverted: ${txHash}`);
  if (status !== 1n) throw new TerminalReceiptError(`receipt has unsupported status ${receipt.status}`);
  const receiptBlock = parseQuantity(receipt.blockNumber, "blockNumber");
  if (confirmations > 1) {
    const head = parseQuantity(
      await rpc(fetchImpl, httpUrl, "eth_blockNumber", [], signal),
      "head block number",
    );
    if (head < receiptBlock + BigInt(confirmations - 1)) return null;
  }
  return receipt;
}

function waitForHead({ WebSocketImpl, wsUrl, stallMs, signal }) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let subscribed = false;
    const ws = new WebSocketImpl(wsUrl);
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      try { ws.close(); } catch {}
      fn(value);
    };
    const timer = setTimeout(() => finish(reject, new Error("WebSocket head subscription stalled")), stallMs);
    const onAbort = () => finish(reject, signal.reason ?? new Error("aborted"));
    signal?.addEventListener("abort", onAbort, { once: true });
    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_subscribe", params: ["newHeads"] }));
    });
    ws.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(String(event.data));
      } catch {
        finish(reject, new Error("malformed WebSocket JSON"));
        return;
      }
      if (message.id === 1) {
        if (message.error || typeof message.result !== "string") {
          finish(reject, new Error(`newHeads subscription rejected: ${JSON.stringify(message.error ?? message)}`));
        } else {
          subscribed = true;
        }
        return;
      }
      if (subscribed && message.method === "eth_subscription" && message.params?.result) {
        finish(resolve, message.params.result);
      }
    });
    ws.addEventListener("error", () => finish(reject, new Error("WebSocket connection error")));
    ws.addEventListener("close", () => finish(reject, new Error("WebSocket closed before a new head")));
  });
}

export async function waitForReceipt(options) {
  const {
    txHash,
    httpUrl,
    wsUrl = "",
    timeoutMs = 300_000,
    pollMs = 2_000,
    confirmations = 1,
    wsStallMs = 30_000,
    fetchImpl = globalThis.fetch,
    WebSocketImpl = globalThis.WebSocket,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    log = () => {},
  } = options;
  if (!TX_HASH_RE.test(txHash ?? "")) throw new Error("invalid transaction hash");
  if (!httpUrl) throw new Error("HTTP RPC URL is required");
  if (typeof fetchImpl !== "function") throw new Error("Fetch API is unavailable");

  const deadline = Date.now() + asPositiveInteger(timeoutMs, "timeoutMs");
  const controller = new AbortController();
  const check = async () => {
    try {
      return await confirmedReceipt({
        fetchImpl,
        httpUrl,
        txHash,
        confirmations: asPositiveInteger(confirmations, "confirmations"),
        signal: controller.signal,
      });
    } catch (error) {
      if (error instanceof TerminalReceiptError) throw error;
      log(`receipt RPC unavailable (${error.message}); retrying`);
      return null;
    }
  };

  let receipt = await check();
  if (receipt) {
    log("receipt was already available");
    return { receipt, mode: "immediate" };
  }

  if (wsUrl && typeof WebSocketImpl === "function") {
    try {
      log(`subscribing to newHeads via ${new URL(wsUrl).host}`);
      while (Date.now() < deadline) {
        const remaining = deadline - Date.now();
        await waitForHead({
          WebSocketImpl,
          wsUrl,
          stallMs: Math.min(wsStallMs, remaining),
          signal: controller.signal,
        });
        receipt = await check();
        if (receipt) return { receipt, mode: "websocket" };
      }
    } catch (error) {
      log(`WebSocket unavailable (${error.message}); falling back to HTTP receipt polling`);
    }
  } else {
    log("WebSocket RPC unavailable; using HTTP receipt polling");
  }

  while (Date.now() < deadline) {
    receipt = await check();
    if (receipt) return { receipt, mode: "http-poll" };
    await sleep(Math.min(asPositiveInteger(pollMs, "pollMs"), Math.max(1, deadline - Date.now())));
  }
  controller.abort();
  throw new Error(`transaction receipt not confirmed within ${timeoutMs}ms: ${txHash}`);
}

async function main() {
  const [txHash, httpUrl = process.env.RPC_URL] = process.argv.slice(2);
  const result = await waitForReceipt({
    txHash,
    httpUrl,
    wsUrl: process.env.WS_RPC_URL ?? "",
    timeoutMs: asPositiveInteger(process.env.TX_RECEIPT_TIMEOUT_SECONDS ?? "300", "TX_RECEIPT_TIMEOUT_SECONDS") * 1000,
    pollMs: asPositiveInteger(process.env.TX_RECEIPT_POLL_SECONDS ?? "2", "TX_RECEIPT_POLL_SECONDS") * 1000,
    confirmations: asPositiveInteger(process.env.TX_CONFIRMATIONS ?? "1", "TX_CONFIRMATIONS"),
    wsStallMs: asPositiveInteger(process.env.TX_WS_STALL_SECONDS ?? "30", "TX_WS_STALL_SECONDS") * 1000,
    log: (message) => console.error(`[receipt] ${message}`),
  });
  console.log(JSON.stringify({
    transactionHash: result.receipt.transactionHash,
    blockNumber: result.receipt.blockNumber,
    status: result.receipt.status,
    confirmationMode: result.mode,
  }));
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main().catch((error) => {
    console.error(`[receipt] ${error.message}`);
    process.exitCode = 1;
  });
}
