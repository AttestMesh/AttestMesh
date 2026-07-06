import { describe, it, expect } from "vitest";

import { parsePimlicoBody, verifyPimlicoSignature } from "../src/pimlico.js";
import { DecodeError } from "../src/decode.js";
import { createLogger } from "../src/log.js";

const logger = createLogger("error");

const SECRET_BYTES = new TextEncoder().encode("test-secret-key-32-bytes-long!!!");
const SECRET = "pim_whsec_" + btoa(String.fromCharCode(...SECRET_BYTES));

async function sign(id: string, timestamp: string, body: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    SECRET_BYTES,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${id}.${timestamp}.${body}`)),
  );
  return "v1," + btoa(String.fromCharCode(...mac));
}

function headers(id: string, ts: string, sig: string): Headers {
  return new Headers({ "webhook-id": id, "webhook-timestamp": ts, "webhook-signature": sig });
}

describe("verifyPimlicoSignature", () => {
  const body = '{"type":"user_operation.sponsorship.requested"}';
  const now = 1_800_000_000;

  it("accepts a valid signature", async () => {
    const ts = String(now);
    const ok = await verifyPimlicoSignature({
      secret: SECRET,
      headers: headers("msg_1", ts, await sign("msg_1", ts, body)),
      rawBody: body,
      nowSeconds: now,
      logger,
    });
    expect(ok).toBe(true);
  });

  it("accepts when a valid sig appears among rotated ones", async () => {
    const ts = String(now);
    const good = await sign("msg_1", ts, body);
    const ok = await verifyPimlicoSignature({
      secret: SECRET,
      headers: headers("msg_1", ts, `v1,${btoa("garbage!")} ${good}`),
      rawBody: body,
      nowSeconds: now,
      logger,
    });
    expect(ok).toBe(true);
  });

  it("rejects a tampered body", async () => {
    const ts = String(now);
    const ok = await verifyPimlicoSignature({
      secret: SECRET,
      headers: headers("msg_1", ts, await sign("msg_1", ts, body)),
      rawBody: body + " ",
      nowSeconds: now,
      logger,
    });
    expect(ok).toBe(false);
  });

  it("rejects a stale timestamp (replay guard)", async () => {
    const ts = String(now - 3600);
    const ok = await verifyPimlicoSignature({
      secret: SECRET,
      headers: headers("msg_1", ts, await sign("msg_1", ts, body)),
      rawBody: body,
      nowSeconds: now,
      logger,
    });
    expect(ok).toBe(false);
  });

  it("rejects missing headers", async () => {
    const ok = await verifyPimlicoSignature({
      secret: SECRET,
      headers: new Headers(),
      rawBody: body,
      nowSeconds: now,
      logger,
    });
    expect(ok).toBe(false);
  });
});

describe("parsePimlicoBody", () => {
  const op = { sender: "0x1111111111111111111111111111111111111111", callData: "0xb61d27f6" };

  it("parses a sponsorship-requested event with numeric chainId", () => {
    const out = parsePimlicoBody({
      type: "user_operation.sponsorship.requested",
      data: { object: { userOperation: op, entryPoint: "0x", chainId: 8453, sponsorshipPolicyId: "sp_x" } },
    });
    expect(out.chainId).toBe("0x2105");
    expect(out.userOperation.sender).toBe(op.sender);
    expect(out.sponsorshipPolicyId).toBe("sp_x");
  });

  it("accepts hex and decimal-string chainIds", () => {
    const mk = (chainId: unknown) =>
      parsePimlicoBody({
        type: "user_operation.sponsorship.requested",
        data: { object: { userOperation: op, chainId } },
      }).chainId;
    expect(mk("0x2105")).toBe("0x2105");
    expect(mk("8453")).toBe("0x2105");
  });

  it("rejects other event types", () => {
    expect(() =>
      parsePimlicoBody({ type: "user_operation.sponsorship.finalized", data: { object: {} } }),
    ).toThrow(DecodeError);
  });

  it("rejects missing userOperation fields", () => {
    expect(() =>
      parsePimlicoBody({
        type: "user_operation.sponsorship.requested",
        data: { object: { userOperation: { sender: op.sender }, chainId: 8453 } },
      }),
    ).toThrow(DecodeError);
  });
});
