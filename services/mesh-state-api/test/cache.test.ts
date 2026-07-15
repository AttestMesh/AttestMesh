import { test } from "node:test";
import assert from "node:assert/strict";
import { TtlCache } from "../src/cache.ts";

test("forced refresh updates the same cache used by request reads", async () => {
  let now = 1_000;
  let next = 1;
  let loads = 0;
  const cache = new TtlCache(
    100,
    async () => {
      loads += 1;
      return next;
    },
    () => now,
  );

  assert.equal((await cache.get()).value, 1);
  next = 2;
  now += 10;
  assert.equal((await cache.get()).value, 1);
  assert.equal(loads, 1);

  assert.equal((await cache.refresh()).value, 2);
  assert.equal(cache.peek()?.value, 2);
  assert.equal(loads, 2);
});

test("failed refresh marks last-good state stale and the next read retries", async () => {
  let now = 2_000;
  let value = 1;
  let fail = false;
  const cache = new TtlCache(
    100,
    async () => {
      if (fail) throw new Error("rpc unavailable");
      return value;
    },
    () => now,
  );

  await cache.get();
  fail = true;
  now += 10;
  const stale = await cache.refresh();
  assert.equal(stale.value, 1);
  assert.equal(stale.stale, true);
  assert.equal(cache.peek()?.stale, true);

  fail = false;
  value = 2;
  now += 10;
  const recovered = await cache.get();
  assert.equal(recovered.value, 2);
  assert.equal(recovered.stale, false);
});
