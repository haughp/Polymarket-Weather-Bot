// Unit tests for the per-(city,mode) consensus-model live/shadow gate.

import assert from "assert";
import { isComboLive, comboKey, StrategyStatusMap } from "./strategyStatus";

let passed = 0;
function test(name: string, fn: () => void) {
  return Promise.resolve()
    .then(fn)
    .then(() => { passed++; console.log(`  ok  ${name}`); })
    .catch((e) => { console.error(`FAIL  ${name}\n      ${e.message}`); process.exitCode = 1; });
}

async function main() {
  const status: StrategyStatusMap = {
    "atlanta|highest": { status: "live" },
    "miami|lowest": { status: "shadow" },
  };

  await test("comboKey formats city|mode", () => {
    assert.strictEqual(comboKey("atlanta", "highest"), "atlanta|highest");
  });

  await test("explicit live combo -> true", () => {
    assert.strictEqual(isComboLive("atlanta", "highest", status), true);
  });

  await test("explicit shadow combo -> false", () => {
    assert.strictEqual(isComboLive("miami", "lowest", status), false);
  });

  await test("unknown combo -> false (never spend on an unqualified combo)", () => {
    assert.strictEqual(isComboLive("houston", "highest", status), false);
  });

  await test("null status (missing/garbled file) -> false (fail-safe shadow)", () => {
    assert.strictEqual(isComboLive("atlanta", "highest", null), false);
  });

  await test("same city, different mode is gated independently", () => {
    const s: StrategyStatusMap = { "miami|highest": { status: "live" }, "miami|lowest": { status: "shadow" } };
    assert.strictEqual(isComboLive("miami", "highest", s), true);
    assert.strictEqual(isComboLive("miami", "lowest", s), false);
  });

  console.log(`\n${passed} passed${process.exitCode ? " (with failures)" : ""}`);
}

main();
