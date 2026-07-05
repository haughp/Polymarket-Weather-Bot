// Unit tests for targetDatesForLead — multi-day horizon for 48h-lead entry targeting.
//
// tomorrowInTz already returns "tomorrow" (day+1) in the city's tz. The new
// ~48h-lead model needs to consider day+1..day+N candidates so the entry loop
// (Task 3) can pick whichever's forecast peak lands in the entry window.
// targetDatesForLead generalizes tomorrowInTz's +1 to a small range, reusing
// the exact same tz-formatting / UTC-noon date-math trick.

import assert from "assert";
import { targetDatesForLead, tomorrowInTz, MONTHS } from "./time";

let passed = 0;
function test(name: string, fn: () => void) {
  return Promise.resolve()
    .then(fn)
    .then(() => { passed++; console.log(`  ok  ${name}`); })
    .catch((e) => { console.error(`FAIL  ${name}\n      ${e.message}`); process.exitCode = 1; });
}

const TZ = "America/Chicago"; // fixed tz; "today" is dynamic so we assert relationships, not literal dates.

async function main() {
  await test("returns maxDaysAhead entries", () => {
    const dates = targetDatesForLead(TZ, 3);
    assert.strictEqual(dates.length, 3);
  });

  await test("default maxDaysAhead is 3", () => {
    const dates = targetDatesForLead(TZ);
    assert.strictEqual(dates.length, 3);
  });

  await test("first entry equals tomorrowInTz(tz)", () => {
    const dates = targetDatesForLead(TZ, 3);
    const tmr = tomorrowInTz(TZ);
    assert.deepStrictEqual(dates[0], tmr);
  });

  await test("maxDaysAhead=1 returns exactly [tomorrowInTz(tz)]", () => {
    const dates = targetDatesForLead(TZ, 1);
    assert.strictEqual(dates.length, 1);
    assert.deepStrictEqual(dates[0], tomorrowInTz(TZ));
  });

  await test("entries are consecutive calendar days in the tz (day N+1 = day N + 1 calendar day)", () => {
    const dates = targetDatesForLead(TZ, 3);
    const fmt = new Intl.DateTimeFormat("en-CA", {
      timeZone: TZ,
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    });
    for (let i = 1; i < dates.length; i++) {
      const prev = dates[i - 1];
      const cur = dates[i];
      const prevMonthIdx = MONTHS.indexOf(prev.month);
      // Same UTC-noon trick as the implementation: add 1 day to prev's date, reformat in tz.
      const nextDayUtcNoon = new Date(Date.UTC(prev.year, prevMonthIdx, prev.day + 1, 12, 0, 0));
      assert.strictEqual(fmt.format(nextDayUtcNoon), cur.dateStr, `entry ${i} should be entry ${i - 1} + 1 day`);
    }
  });

  await test("month/day/year fields are internally consistent with dateStr for every entry", () => {
    const dates = targetDatesForLead(TZ, 3);
    for (const d of dates) {
      const [y, m, dd] = d.dateStr.split("-").map(Number);
      assert.strictEqual(d.year, y);
      assert.strictEqual(d.month, MONTHS[m - 1]);
      assert.strictEqual(d.day, dd);
    }
  });

  await test("entries have no duplicate dateStr (strictly advancing)", () => {
    const dates = targetDatesForLead(TZ, 3);
    const uniq = new Set(dates.map((d) => d.dateStr));
    assert.strictEqual(uniq.size, dates.length);
  });

  console.log(`\n${passed} passed${process.exitCode ? " (with failures)" : ""}`);
}

main();
