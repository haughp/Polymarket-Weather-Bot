// Unit tests for selectConsensusBucket — single consensus-bucket (agreement)
// selection rule: buy F only when F is ALSO the market's highest-priced
// bucket. Single leg, no neighbour (contrast with selectEntryPair).

import assert from "assert";
import { selectConsensusBucket } from "./consensus";
import { SelectableBucket } from "./selection";

let passed = 0;
function test(name: string, fn: () => void) {
  return Promise.resolve()
    .then(fn)
    .then(() => { passed++; console.log(`  ok  ${name}`); })
    .catch((e) => { console.error(`FAIL  ${name}\n      ${e.message}`); process.exitCode = 1; });
}

const MAX_PRICE = 0.6;

async function main() {
  await test("agreement + price <= cap -> ok, returns F", () => {
    const buckets: SelectableBucket[] = [
      { range: [68, 69], price: 0.10 },
      { range: [70, 71], price: 0.55 }, // F — also the highest-priced bucket
      { range: [72, 73], price: 0.20 },
    ];
    const r = selectConsensusBucket(buckets, 70.5, MAX_PRICE);
    assert.strictEqual(r.ok, true);
    if (!r.ok) return;
    assert.deepStrictEqual(r.bucket.range, [70, 71]);
  });

  await test("disagreement (a different bucket is highest-priced) -> ok:false, no-agreement reason", () => {
    const buckets: SelectableBucket[] = [
      { range: [68, 69], price: 0.10 },
      { range: [70, 71], price: 0.30 }, // F — not the crowd's favourite
      { range: [72, 73], price: 0.55 }, // top — crowd disagrees with the model
    ];
    const r = selectConsensusBucket(buckets, 70.5, MAX_PRICE);
    assert.strictEqual(r.ok, false);
    if (r.ok) return;
    assert.match(r.reason, /no agreement|disagreement/i);
  });

  await test("agreement but F.price > cap -> ok:false", () => {
    const buckets: SelectableBucket[] = [
      { range: [68, 69], price: 0.10 },
      { range: [70, 71], price: 0.75 }, // F and top, but over the cap
      { range: [72, 73], price: 0.15 },
    ];
    const r = selectConsensusBucket(buckets, 70.5, MAX_PRICE);
    assert.strictEqual(r.ok, false);
  });

  await test("forecast outside all buckets -> ok:false", () => {
    const buckets: SelectableBucket[] = [
      { range: [68, 69], price: 0.10 },
      { range: [70, 71], price: 0.55 },
    ];
    const r = selectConsensusBucket(buckets, 200, MAX_PRICE);
    assert.strictEqual(r.ok, false);
    if (r.ok) return;
    assert.match(r.reason, /outside all listed buckets/);
  });

  await test("non-finite prices are treated as 0 when computing argmax", () => {
    const buckets: SelectableBucket[] = [
      { range: [68, 69], price: NaN },
      { range: [70, 71], price: 0.05 }, // F — small but positive beats NaN-as-0
    ];
    const r = selectConsensusBucket(buckets, 70.5, MAX_PRICE);
    assert.strictEqual(r.ok, true);
    if (!r.ok) return;
    assert.deepStrictEqual(r.bucket.range, [70, 71]);
  });

  await test("tail bucket ('or higher') can be the consensus bucket", () => {
    const buckets: SelectableBucket[] = [
      { range: [74, 75], price: 0.20 },
      { range: [76, 999], price: 0.60 }, // F (forecast 80 → or-higher) and top
    ];
    const r = selectConsensusBucket(buckets, 80, MAX_PRICE);
    assert.strictEqual(r.ok, true);
    if (!r.ok) return;
    assert.deepStrictEqual(r.bucket.range, [76, 999]);
  });

  console.log(`\n${passed} passed${process.exitCode ? " (with failures)" : ""}`);
}

main();
