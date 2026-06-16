// Unit tests for temperature-bucket parsing and midpoint.
//
// A Polymarket US bucket labelled "between 62-63°F" resolves YES on a 62 or 63
// reading => true interval [62, 64) => midpoint 63 (NOT 62.5). The live TS bot is
// US-only: parseTempRange returns null for °C / "be X°" single-degree markets.
//
// Run: npm test   (ts-node src/parsing.test.ts)

import assert from "assert";
import {
  parseTempRange,
  bucketMidpoint,
  bucketContains,
  findBucketForTemp,
  neighborBucket,
} from "./parsing";

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

  // ── bucketContains: half-open [lo, lo+2); boundary lands in upper bucket ────
  await test("bucketContains [70,71] covers 70 and 71 but not 69.9 or 72", () => {
    assert.strictEqual(bucketContains([70, 71], 70), true);
    assert.strictEqual(bucketContains([70, 71], 71), true);
    assert.strictEqual(bucketContains([70, 71], 71.9), true);
    assert.strictEqual(bucketContains([70, 71], 69.9), false);
    assert.strictEqual(bucketContains([70, 71], 72), false); // boundary → upper bucket
  });

  await test("bucketContains: 72.0 lands in [72,73] not [70,71]", () => {
    assert.strictEqual(bucketContains([72, 73], 72.0), true);
    assert.strictEqual(bucketContains([70, 71], 72.0), false);
  });

  await test("bucketContains 'or below' [-999,70] covers ≤70 (incl 69), excludes 71", () => {
    assert.strictEqual(bucketContains([-999, 70], 70), true);
    assert.strictEqual(bucketContains([-999, 70], 69), true);
    assert.strictEqual(bucketContains([-999, 70], 71), false);
  });

  await test("bucketContains 'or higher' [70,999] covers ≥70, excludes 69", () => {
    assert.strictEqual(bucketContains([70, 999], 70), true);
    assert.strictEqual(bucketContains([70, 999], 99), true);
    assert.strictEqual(bucketContains([70, 999], 69), false);
  });

  // ── findBucketForTemp + neighborBucket over a synthetic ladder ─────────────
  const ladder: { range: [number, number] }[] = [
    { range: [-999, 67] },
    { range: [68, 69] },
    { range: [70, 71] },
    { range: [72, 73] },
    { range: [74, 75] },
    { range: [76, 999] },
  ];

  await test("findBucketForTemp returns the containing bucket (71.9 → [70,71])", () => {
    assert.deepStrictEqual(findBucketForTemp(ladder, 71.9)?.range, [70, 71]);
  });

  await test("findBucketForTemp returns the upper bucket on boundary (72.0 → [72,73])", () => {
    assert.deepStrictEqual(findBucketForTemp(ladder, 72.0)?.range, [72, 73]);
  });

  await test("findBucketForTemp can land in a tail bucket", () => {
    assert.deepStrictEqual(findBucketForTemp(ladder, 64)?.range, [-999, 67]);
    assert.deepStrictEqual(findBucketForTemp(ladder, 80)?.range, [76, 999]);
  });

  await test("neighborBucket matches by lower-bound arithmetic, not array order", () => {
    assert.deepStrictEqual(neighborBucket(ladder, 70, +2)?.range, [72, 73]);
    assert.deepStrictEqual(neighborBucket(ladder, 70, -2)?.range, [68, 69]);
  });

  await test("neighborBucket returns null at the grid edge / no such bucket", () => {
    // No bucket has lower bound 66 (the -999 tail's lo is the sentinel, not 66).
    assert.strictEqual(neighborBucket(ladder, 68, -2), null);
    // No bucket has lower bound 78 (the 999 tail's lo is 76).
    assert.strictEqual(neighborBucket(ladder, 76, +2), null);
  });

  console.log(`\n${passed} passed${process.exitCode ? " (with failures)" : ""}`);
}

main();
