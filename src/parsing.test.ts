// Unit tests for temperature-bucket parsing and midpoint.
//
// A Polymarket US bucket labelled "between 62-63°F" resolves YES on a 62 or 63
// reading => true interval [62, 64) => midpoint 63 (NOT 62.5). The live TS bot is
// US-only: parseTempRange returns null for °C / "be X°" single-degree markets.
//
// Run: npm test   (ts-node src/parsing.test.ts)

import assert from "assert";
import { parseTempRange, bucketMidpoint } from "./parsing";

let passed = 0;
function test(name: string, fn: () => void) {
  return Promise.resolve()
    .then(fn)
    .then(() => { passed++; console.log(`  ok  ${name}`); })
    .catch((e) => { console.error(`FAIL  ${name}\n      ${e.message}`); process.exitCode = 1; });
}

async function main() {
  // ── parseTempRange: literal [lo, hi] contract with sentinels ───────────────
  await test("parseTempRange parses a US range to literal [lo, hi]", () => {
    assert.deepStrictEqual(
      parseTempRange("Will the highest temperature in Atlanta be between 62-63°F on May 11?"),
      [62, 63]
    );
  });

  await test("parseTempRange maps 'or below' to [-999, X]", () => {
    assert.deepStrictEqual(parseTempRange("...be 70°F or below on May 11?"), [-999, 70]);
  });

  await test("parseTempRange maps 'or higher' to [X, 999]", () => {
    assert.deepStrictEqual(parseTempRange("...be 70°F or higher on May 11?"), [70, 999]);
  });

  await test("parseTempRange returns null for single-degree Celsius (live TS does not trade it)", () => {
    assert.strictEqual(parseTempRange("Will the highest temperature in London be 13°C on May 11?"), null);
  });

  // ── bucketMidpoint: lo + width/2 for ranges, bound for tails ───────────────
  await test("bucketMidpoint of [62,63] is 63 (lo + width/2, width 2°F)", () => {
    assert.strictEqual(bucketMidpoint([62, 63]), 63);
  });

  await test("bucketMidpoint of an 'or below' tail uses the upper bound", () => {
    assert.strictEqual(bucketMidpoint([-999, 70]), 70);
  });

  await test("bucketMidpoint of an 'or higher' tail uses the lower bound", () => {
    assert.strictEqual(bucketMidpoint([70, 999]), 70);
  });

  console.log(`\n${passed} passed${process.exitCode ? " (with failures)" : ""}`);
}

main();
