import { describe, it, expect } from "vitest";

import { parsePimlicoBody, pimlicoHmacKey, verifyPimlicoSignature } from "../src/pimlico.js";
import { DecodeError } from "../src/decode.js";
import { createLogger } from "../src/log.js";

const logger = createLogger("error");

// A throwaway Pimlico-format secret (base58-custom of bytes 0x00..0x1f — carries no
// real credential). Its correct HMAC key is derived via the same non-standard path
// production uses (base58-custom → hex → base64), so signing here also exercises it.
const SECRET = "pim_whsec_12oeWzGziAeWhm1Hpi4zvXDz54bzSMCBj3CeCb6rAPV";

async function sign(id: string, timestamp: string, body: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    pimlicoHmacKey(SECRET),
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
    expect(ok.ok).toBe(true);
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
    expect(ok.ok).toBe(true);
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
    expect(ok.ok).toBe(false);
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
    expect(ok.ok).toBe(false);
  });

  it("rejects missing headers", async () => {
    const ok = await verifyPimlicoSignature({
      secret: SECRET,
      headers: new Headers(),
      rawBody: body,
      nowSeconds: now,
      logger,
    });
    expect(ok.ok).toBe(false);
  });

  // Golden vector: pins Pimlico's non-standard key derivation (base58-custom → hex →
  // base64). This exact (secret, id, ts, body, sig) tuple was produced by the
  // reference @pimlico/webhook lib; if the derivation regresses to plain base64 this
  // fails. Regression guard for the live 2026-07-06 all-401 incident.
  it("verifies a reference @pimlico/webhook signature (golden vector)", async () => {
    const res = await verifyPimlicoSignature({
      secret: "pim_whsec_12oeWzGziAeWhm1Hpi4zvXDz54bzSMCBj3CeCb6rAPV",
      headers: headers(
        "msg_test",
        "1783379000",
        "v1,FegSBwAL3Dulyu7SFiGvw1GCj7N+us7vJohmBsP9cxY=",
      ),
      rawBody: '{"type":"user_operation.sponsorship.requested"}',
      nowSeconds: 1783379000,
      logger,
    });
    expect(res.ok).toBe(true);
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
