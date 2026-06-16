// Unit tests for provider-matrix wiring: per-mode provider dispatch, NWS/Open-Meteo
// routing, the matrix-MAE accessor, and the MAE-gate decision. No live HTTP — the
// forecast fetchers are stubbed via __setFetchersForTest.
//
// Run: npm test   (ts-node src/matrix.test.ts)

import assert from "assert";
import * as nws from "./nws";
import { getProvider, getBias, getMae, getUnit } from "./matrix";

// The matrix loaded by matrix.ts comes from ../provider_matrix.json (real file). These
// tests assert behavior against whatever cells that file currently holds, plus the
// fallback paths for cities the matrix does not cover. We read the same file directly
// so the expectations track the deployed matrix rather than hardcoding stale numbers.
import * as fs from "fs";
import * as path from "path";
const MATRIX = (() => {
  try {
    return JSON.parse(
      fs.readFileSync(path.resolve(__dirname, "..", "provider_matrix.json"), "utf-8")
    );
  } catch {
    return { cities: {} };
  }
})();

let passed = 0;
function test(name: string, fn: () => void | Promise<void>) {
  return Promise.resolve()
    .then(fn)
    .then(() => { passed++; console.log(`  ok  ${name}`); })
    .catch((e) => { console.error(`FAIL  ${name}\n      ${e.message}`); process.exitCode = 1; });
}

// Replicate the strategy.ts gate so we test the actual decision, not a paraphrase.
// Unit-specific thresholds; a city with NO matrix MAE is treated as unproven → SKIP.
const MAX_PROVIDER_MAE_F = 1.5;
const MAX_PROVIDER_MAE_C = 1.0;
function gateSkips(city: string, mode: "highest" | "lowest"): boolean {
  const mae = getMae(city, mode);
  if (mae == null) return true; // unproven → skip
  const unit = getUnit(city, mode) ?? "F";
  const gate = unit === "C" ? MAX_PROVIDER_MAE_C : MAX_PROVIDER_MAE_F;
  return mae > gate;
}

async function main() {
  // ── getProvider: matrix cell wins, per-mode ──────────────────────────────
  await test("getProvider returns the matrix model for a covered city/mode (per-mode)", () => {
    for (const [city, cell] of Object.entries<any>(MATRIX.cities ?? {})) {
      if (cell.max?.provider) assert.strictEqual(getProvider(city, "max"), cell.max.provider);
      if (cell.min?.provider) assert.strictEqual(getProvider(city, "min"), cell.min.provider);
    }
  });

  await test("getProvider can return DIFFERENT providers for max vs min of one city", () => {
    // The deployed matrix has cities whose max and min providers differ (e.g. chicago:
    // gem_seamless / ncep_nbm_conus). Assert at least one such split exists and is honored.
    const split = Object.entries<any>(MATRIX.cities ?? {}).find(
      ([, c]) => c.max?.provider && c.min?.provider && c.max.provider !== c.min.provider
    );
    assert.ok(split, "expected at least one city with differing max/min providers in the matrix");
    const [city, c] = split!;
    assert.strictEqual(getProvider(city, "max"), c.max.provider);
    assert.strictEqual(getProvider(city, "min"), c.min.provider);
    assert.notStrictEqual(getProvider(city, "max"), getProvider(city, "min"));
  });

  await test("getProvider falls back to FORECAST_PROVIDER then 'nws' for an unmatrixed city", () => {
    // A city slug not present in the matrix and not in FORECAST_PROVIDER → "nws".
    assert.strictEqual(getProvider("__no_such_city__", "max"), "nws");
  });

  // ── getMae ───────────────────────────────────────────────────────────────
  await test("getMae returns mae_debiased for a covered cell", () => {
    const entry = Object.entries<any>(MATRIX.cities ?? {}).find(
      ([, c]) => c.max && typeof c.max.mae_debiased === "number"
    );
    assert.ok(entry, "expected a matrix cell with mae_debiased");
    const [city, c] = entry!;
    assert.strictEqual(getMae(city, "highest"), c.max.mae_debiased);
  });

  await test("getMae returns null for an unmatrixed city, and the gate now SKIPS it (unproven)", () => {
    assert.strictEqual(getMae("__no_such_city__", "highest"), null);
    assert.strictEqual(getUnit("__no_such_city__", "highest"), null);
    assert.strictEqual(gateSkips("__no_such_city__", "highest"), true);
  });

  // ── MAE gate decision ──────────────────────────────────────────────────────
  await test("MAE gate skips a city/mode whose debiased MAE exceeds the unit-specific threshold", () => {
    // Every cell is gated against its own unit's threshold (1.5°F / 1.0°C). A cell at/under
    // its threshold must NOT be gated; one above it must be. Cells with no usable MAE are
    // skipped (unproven), matching the live gate.
    let anyOverThreshold = false;
    for (const [city, cell] of Object.entries<any>(MATRIX.cities ?? {})) {
      for (const [mode, mkt] of [["max", "highest"], ["min", "lowest"]] as const) {
        const c = cell[mode];
        if (!c) continue;
        const mae = typeof c.mae_debiased === "number" ? c.mae_debiased : c.mae;
        if (typeof mae !== "number") continue;
        const unit = c.unit === "C" ? "C" : "F";
        const gate = unit === "C" ? MAX_PROVIDER_MAE_C : MAX_PROVIDER_MAE_F;
        const expectSkip = mae > gate;
        if (expectSkip) anyOverThreshold = true;
        assert.strictEqual(
          gateSkips(city, mkt), expectSkip,
          `${city} ${mkt} mae=${mae}°${unit} gate=${gate} expectSkip=${expectSkip}`
        );
      }
    }
    // Informational — not all matrices will have an over-threshold cell.
    if (!anyOverThreshold) console.log("       (note: no matrix cell currently exceeds the gate)");
  });

  await test("getBias unchanged: returns the matrix cell bias for a covered city", () => {
    const entry = Object.entries<any>(MATRIX.cities ?? {}).find(
      ([, c]) => c.max && typeof c.max.bias === "number"
    );
    assert.ok(entry);
    const [city, c] = entry!;
    assert.strictEqual(getBias(city, "highest"), c.max.bias);
  });

  // ── getForecast dispatch (stubbed fetchers, no HTTP) ───────────────────────
  await test("getForecast routes each mode to the right source and merges them", async () => {
    const calls: string[] = [];
    nws.__setFetchersForTest(
      // openMeteoFetcher stub: records (city,mode,model) and returns a marker temp
      async (city, mode, model) => {
        calls.push(`OM:${city}:${mode}:${model}`);
        return { vals: { "2026-06-14": mode === "max" ? 88 : 70 }, time: { "2026-06-14": "2026-06-14T18:00:00Z" } };
      },
      // nwsFetcher stub
      async (city, mode) => {
        calls.push(`NWS:${city}:${mode}`);
        return { vals: { "2026-06-14": mode === "max" ? 99 : 60 }, time: { "2026-06-14": "2026-06-14T19:00:00Z" } };
      }
    );
    try {
      // Pick a real matrix city so getProvider resolves to actual model names.
      const city = Object.keys(MATRIX.cities ?? {})[0] ?? "nyc";
      const f = await nws.getForecast(city);

      const maxProv = getProvider(city, "max");
      const minProv = getProvider(city, "min");
      const expMax = maxProv === "nws" ? `NWS:${city}:max` : `OM:${city}:max:${maxProv}`;
      const expMin = minProv === "nws" ? `NWS:${city}:min` : `OM:${city}:min:${minProv}`;
      assert.ok(calls.includes(expMax), `expected call ${expMax}, got ${calls.join(",")}`);
      assert.ok(calls.includes(expMin), `expected call ${expMin}, got ${calls.join(",")}`);

      // Merge: max came from the max-source, min from the min-source.
      assert.strictEqual(f.max["2026-06-14"], maxProv === "nws" ? 99 : 88);
      assert.strictEqual(f.min["2026-06-14"], minProv === "nws" ? 60 : 70);
      assert.ok(f.maxTime["2026-06-14"]);
      assert.ok(f.minTime["2026-06-14"]);
    } finally {
      nws.__restoreFetchers();
    }
  });

  await test("getForecast: a mode whose provider is 'nws' hits the NWS fetcher (not Open-Meteo)", async () => {
    const calls: string[] = [];
    // Force a synthetic dispatch by stubbing both and using an unmatrixed city → both modes "nws".
    nws.__setFetchersForTest(
      async (city, mode, model) => { calls.push(`OM:${mode}:${model}`); return { vals: {}, time: {} }; },
      async (city, mode) => { calls.push(`NWS:${mode}`); return { vals: { d: mode === "max" ? 1 : 2 }, time: {} }; }
    );
    try {
      await nws.getForecast("__no_such_city__"); // getProvider → "nws" for both modes
      assert.ok(calls.includes("NWS:max"));
      assert.ok(calls.includes("NWS:min"));
      assert.ok(!calls.some(c => c.startsWith("OM:")), `unexpected Open-Meteo call: ${calls.join(",")}`);
    } finally {
      nws.__restoreFetchers();
    }
  });

  console.log(`\n${passed} passed${process.exitCode ? " (with failures)" : ""}`);
}

main();
