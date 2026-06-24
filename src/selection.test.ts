// Unit tests for selectEntryPair — F + in-bucket-position-neighbour entry rule.
//
// The neighbour is the F±1 bucket on the side of F's interval that the
// bias-adjusted forecast sits in: HIGH half (pos >= 0.5) -> upper (F+1);
// LOW half -> lower (F-1). Price no longer selects the neighbour, but the
// floor (neighbour-only) and ceiling (both legs) gates still apply, and F is
// always kept (the 2026-06-14 Dallas loss: a correct-but-cheap F must survive).

import assert from "assert";
import { selectEntryPair, SelectableBucket } from "./selection";

let passed = 0;
function test(name: string, fn: () => void) {
  return Promise.resolve()
    .then(fn)
    .then(() => { passed++; console.log(`  ok  ${name}`); })
    .catch((e) => { console.error(`FAIL  ${name}\n      ${e.message}`); process.exitCode = 1; });
}

const GATE = { minPrice: 0.12, maxPrice: 0.35 };

// A Dallas-style ladder. Forecast adj = 85.95 → F = [84,85].
const dallas: SelectableBucket[] = [
  { range: [-999, 77], price: 0.0 },
  { range: [78, 79], price: 0.0 },
  { range: [80, 81], price: 0.02 },
  { range: [82, 83], price: 0.08 },
  { range: [84, 85], price: 0.05 }, // F — cheap, the old floor dropped this
  { range: [86, 87], price: 0.19 }, // F+1
  { range: [88, 89], price: 0.28 }, // F+2 (not a neighbour)
  { range: [90, 91], price: 0.05 },
  { range: [92, 999], price: 0.0 },
];

async function main() {
  await test("REGRESSION: F is kept even when priced below the floor (Dallas)", () => {
    // adj 85.95 → F = [84,85]; pos = (85.95-84)/2 = 0.975 ≥ 0.5 → upper neighbour [86,87].
    const r = selectEntryPair(dallas, 85.95, GATE);
    assert.strictEqual(r.ok, true);
    if (!r.ok) return;
    assert.deepStrictEqual(r.pair[0].range, [84, 85]); // F first, despite $0.05 < $0.12
    assert.deepStrictEqual(r.pair[1].range, [86, 87]); // upper neighbour by position (not by price)
  });

  await test("position picks the LOW-half neighbour even when the high side is priced higher", () => {
    const buckets: SelectableBucket[] = [
      { range: [68, 69], price: 0.15 }, // below — chosen: forecast is in F's low half
      { range: [70, 71], price: 0.05 }, // F
      { range: [72, 73], price: 0.20 }, // above — pricier, but position ignores price
    ];
    // pos = (70.5-70)/2 = 0.25 < 0.5 → lower neighbour [68,69], NOT the pricier [72,73].
    const r = selectEntryPair(buckets, 70.5, GATE);
    assert.strictEqual(r.ok, true);
    if (!r.ok) return;
    assert.deepStrictEqual(r.pair[0].range, [70, 71]);
    assert.deepStrictEqual(r.pair[1].range, [68, 69]);
  });

  await test("position picks the HIGH-half neighbour (mirror)", () => {
    const buckets: SelectableBucket[] = [
      { range: [68, 69], price: 0.20 }, // below — pricier, but position ignores price
      { range: [70, 71], price: 0.05 }, // F
      { range: [72, 73], price: 0.15 }, // above — chosen: forecast is in F's high half
    ];
    // pos = (71.5-70)/2 = 0.75 ≥ 0.5 → upper neighbour [72,73].
    const r = selectEntryPair(buckets, 71.5, GATE);
    assert.strictEqual(r.ok, true);
    if (!r.ok) return;
    assert.deepStrictEqual(r.pair[1].range, [72, 73]);
  });

  await test("abort when the position-chosen neighbour exceeds the ceiling", () => {
    const buckets: SelectableBucket[] = [
      { range: [68, 69], price: 0.20 }, // below — would be fine, but not chosen
      { range: [70, 71], price: 0.10 }, // F (ok)
      { range: [72, 73], price: 0.42 }, // above — position picks this (pos 0.75) and it > 0.35
    ];
    // pos = (71.5-70)/2 = 0.75 → upper [72,73] @ $0.42 > ceiling → abort.
    const r = selectEntryPair(buckets, 71.5, GATE);
    assert.strictEqual(r.ok, false);
  });

  await test("abort when F itself exceeds the ceiling", () => {
    const buckets: SelectableBucket[] = [
      { range: [68, 69], price: 0.20 },
      { range: [70, 71], price: 0.50 }, // F > ceiling → −EV dual entry
      { range: [72, 73], price: 0.25 },
    ];
    const r = selectEntryPair(buckets, 70.5, GATE);
    assert.strictEqual(r.ok, false);
  });

  await test("abort when the chosen neighbour is below the floor", () => {
    const buckets: SelectableBucket[] = [
      { range: [68, 69], price: 0.03 }, // below — both neighbours cheap
      { range: [70, 71], price: 0.10 }, // F (kept, but no valid neighbour)
      { range: [72, 73], price: 0.05 }, // above — higher of the two but still < floor
    ];
    const r = selectEntryPair(buckets, 70.5, GATE);
    assert.strictEqual(r.ok, false);
  });

  await test("abort when the forecast lands outside all listed buckets", () => {
    const r = selectEntryPair(dallas, 200, GATE);
    assert.strictEqual(r.ok, false);
  });

  await test("tail F ('or higher') uses its single lower neighbour", () => {
    const buckets: SelectableBucket[] = [
      { range: [74, 75], price: 0.15 }, // F−1 (above floor)
      { range: [76, 999], price: 0.20 }, // F (forecast 80 → or-higher)
    ];
    const r = selectEntryPair(buckets, 80, GATE);
    assert.strictEqual(r.ok, true);
    if (!r.ok) return;
    assert.deepStrictEqual(r.pair[0].range, [76, 999]);
    assert.deepStrictEqual(r.pair[1].range, [74, 75]);
  });

  await test("preferred-by-position side missing → falls back to the listed neighbour", () => {
    const buckets: SelectableBucket[] = [
      { range: [68, 69], price: 0.15 }, // below — the only neighbour listed
      { range: [70, 71], price: 0.10 }, // F
      // no [72,73] — high side absent
    ];
    // pos = (71.5-70)/2 = 0.75 wants upper, but only lower is listed → [68,69].
    const r = selectEntryPair(buckets, 71.5, GATE);
    assert.strictEqual(r.ok, true);
    if (!r.ok) return;
    assert.deepStrictEqual(r.pair[1].range, [68, 69]);
  });

  await test("abort when F has no neighbour at all", () => {
    const buckets: SelectableBucket[] = [
      { range: [70, 71], price: 0.20 }, // F only, no [68,69] or [72,73]
    ];
    const r = selectEntryPair(buckets, 70.5, GATE);
    assert.strictEqual(r.ok, false);
  });

  console.log(`\n${passed} passed${process.exitCode ? " (with failures)" : ""}`);
}

main();
