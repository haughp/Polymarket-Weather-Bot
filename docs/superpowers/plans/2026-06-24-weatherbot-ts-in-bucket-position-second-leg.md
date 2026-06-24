# weatherbot-ts In-Bucket-Position Second Leg Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the price-based second-leg neighbour pick in `selectEntryPair` with the in-bucket-position rule, matching the ECMWF bot.

**Architecture:** One pure function changes — `selectEntryPair` in `src/selection.ts`. The neighbour is chosen by where the bias-adjusted forecast sits inside F's 2°F interval (`pos = (adjForecast − fLow)/2`; `pos ≥ 0.5 → upper`, else lower), preferring the position side and falling back to whichever neighbour is listed. All gates, ordering, and Leg-1 sourcing are unchanged.

**Tech Stack:** TypeScript, `ts-node` standalone test scripts (`npm test`), `npm run build` (tsc).

## Global Constraints

- US-only bot: buckets are **2°F** wide; no °C/width-1 branch.
- `selectEntryPair` must remain **pure** (no network) — it is unit-tested directly.
- Floor applies to the **neighbour only**; ceiling applies to **both** legs; **F is always kept**.
- Return shape unchanged: `{ ok: true, pair: [F, neighbor] }` with **F first**.
- `price` stays on `SelectableBucket` (used by gates), even though it no longer selects the neighbour.
- Leg-1 forecast sourcing is **out of scope** — do not touch `nws.ts`, `matrix.ts`, `strategy.ts`, `provider_backtest.py`, or `provider_matrix.json`.
- Spec: `docs/superpowers/specs/2026-06-24-weatherbot-ts-in-bucket-position-second-leg-design.md`.

---

### Task 1: Swap the neighbour-selection rule in `selectEntryPair`

**Files:**
- Modify: `src/selection.ts` (the neighbour-pick block, currently lines 63-66; module header comment lines 1-19)
- Test: `src/selection.test.ts`

**Interfaces:**
- Consumes: `selectEntryPair(buckets: SelectableBucket[], adjustedForecastTemp: number, gate: { minPrice, maxPrice }): { ok: true, pair: [T,T] } | { ok: false, reason }` — signature **unchanged**.
- Produces: same signature; only the neighbour-selection internals change. `above` / `below` (lines 56-57) and the both-null guard (lines 59-61) are reused as-is.

- [ ] **Step 1: Update the four affected tests in `src/selection.test.ts`**

Three existing tests encode the OLD price-based rule and must be rewritten to the position rule; one is renamed/recommented. Replace the whole `main()` body's first three `test(...)` blocks (the Dallas regression, the "higher-priced neighbour wins", and the "abort when the chosen (higher) neighbour exceeds the ceiling" tests) with the blocks below. Leave the other tests (floor-abort, outside-all-buckets, tail-F, no-neighbour) as they are — verified still correct under the new rule.

Replace the module header (lines 1-7) first:

```ts
// Unit tests for selectEntryPair — F + in-bucket-position-neighbour entry rule.
//
// The neighbour is the F±1 bucket on the side of F's interval that the
// bias-adjusted forecast sits in: HIGH half (pos >= 0.5) -> upper (F+1);
// LOW half -> lower (F-1). Price no longer selects the neighbour, but the
// floor (neighbour-only) and ceiling (both legs) gates still apply, and F is
// always kept (the 2026-06-14 Dallas loss: a correct-but-cheap F must survive).
```

Replace the Dallas regression test (currently lines 38-44):

```ts
  await test("REGRESSION: F is kept even when priced below the floor (Dallas)", () => {
    // adj 85.95 → F = [84,85]; pos = (85.95-84)/2 = 0.975 ≥ 0.5 → upper neighbour [86,87].
    const r = selectEntryPair(dallas, 85.95, GATE);
    assert.strictEqual(r.ok, true);
    if (!r.ok) return;
    assert.deepStrictEqual(r.pair[0].range, [84, 85]); // F first, despite $0.05 < $0.12
    assert.deepStrictEqual(r.pair[1].range, [86, 87]); // upper neighbour by position (not by price)
  });
```

Replace the "higher-priced neighbour wins" test (currently lines 46-57) — this is the
DISCRIMINATING case that flips under the new rule:

```ts
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
```

Replace the "abort when the chosen (higher) neighbour exceeds the ceiling" test (currently
lines 59-67) so the ceiling-busting bucket is the POSITION-chosen one:

```ts
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
```

Add a fallback test (preferred side missing) after the existing tail-F test:

```ts
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
```

- [ ] **Step 2: Run the tests to verify they FAIL against the old code**

Run: `cd /Users/padraighaughey/Polymarket-Weather-Bot && npm test 2>&1 | sed -n '/selection/,/passed/p'`
Expected: FAIL — the rewritten "position picks the LOW-half neighbour" test fails because the old code returns the pricier `[72,73]`; the ceiling test and HIGH-half/fallback tests also fail under the old rule.

- [ ] **Step 3: Implement the in-bucket-position rule in `src/selection.ts`**

Replace the neighbour-pick block (currently lines 63-66):

```ts
  // Higher YES price wins — "let the market decide". Missing/NaN price ranks 0.
  const safePrice = (p: number) => (Number.isFinite(p) ? p : 0);
  candidates.sort((a, b) => safePrice(b.price) - safePrice(a.price));
  const neighbor = candidates[0];
```

with:

```ts
  // In-bucket position of the bias-adjusted forecast picks the neighbour:
  // HIGH half of F (pos >= 0.5) -> upper neighbour (F+1); LOW half -> lower (F-1).
  // Prefer the position side; fall back to whichever neighbour IS listed when the
  // preferred side is absent (grid edge / missing bucket / tail F — a tail F lists
  // only one neighbour, so the fallback returns it regardless of pos). 2°F buckets.
  const safePrice = (p: number) => (Number.isFinite(p) ? p : 0);
  const pos = fLow === -999 ? 0 : (adjustedForecastTemp - fLow) / 2;
  const neighbor = pos >= 0.5 ? (above ?? below)! : (below ?? above)!;
```

Note: `safePrice` stays (the gates below still use it). `above` / `below` are already in
scope from lines 56-57. `candidates` is still built at line 58 and still feeds the both-null
guard at lines 59-61 — leave that guard untouched.

Also update the module header comment in `src/selection.ts` (lines 3-7) from the
"market prices higher" description to the position rule:

```ts
// Rule (replaces the old top-2-by-midpoint-distance logic):
//   F        = the bucket whose interval contains the bias-adjusted forecast
//   neighbor = the F+1 / F−1 bucket on the side of F's interval the bias-adjusted
//              forecast sits in (pos >= 0.5 → upper F+1, else lower F−1);
//              falls back to whichever neighbour is listed if the preferred side
//              is absent. (Migrated from the ECMWF bot, 2026-06-24.)
//   pair     = [F, neighbor]  (F always first ⇒ snapshot bucket1 = F)
```

- [ ] **Step 4: Run the tests to verify they PASS**

Run: `cd /Users/padraighaughey/Polymarket-Weather-Bot && npm test 2>&1 | sed -n '/selection/,/passed/p'`
Expected: PASS — all `selection.test.ts` cases ok (Dallas regression, LOW-half, HIGH-half, ceiling-abort, floor-abort, outside-all, tail-F, no-neighbour, fallback).

- [ ] **Step 5: Run the FULL test suite + build to confirm no regressions elsewhere**

Run: `cd /Users/padraighaughey/Polymarket-Weather-Bot && npm test && npm run build`
Expected: full `npm test` green (selection + parsing + matrix tests), `tsc` build exits 0 with no type errors. If `matrix.test.ts` or `parsing.test.ts` reference the old rule, they should not — but if any fail, STOP and report (they were out of scope and indicate a hidden coupling).

- [ ] **Step 6: Commit**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot
git add src/selection.ts src/selection.test.ts
git commit -m "$(cat <<'EOF'
feat(weatherbot-ts): in-bucket-position 2nd-leg selection (migrate ECMWF rule)

Replace selectEntryPair's "market prices higher" neighbour pick with the
in-bucket-position rule: pos=(adjForecast-fLow)/2; pos>=0.5 -> upper (F+1),
else lower (F-1), preferring the position side and falling back to whichever
neighbour is listed. Matches the ECMWF bot's 2026-06-23 change (+25-27pp
P(either) on backtest). Floor/ceiling/F-always-kept/ordering unchanged.
Leg-1 matrix-provider sourcing untouched (already correct).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Live verification (Leg-1 sourcing + Leg-2 selection)

**Files:** none — observational only.

**Interfaces:** consumes the running daemon's log at `/tmp/weather_bot_live.log`.

This task confirms the spec's two claims against the live bot WITHOUT editing anything. It
does NOT restart the daemon — surface findings and let the user decide on a restart (the
daemon must restart to load the new `dist/` for the Leg-2 change to take live effect).

- [ ] **Step 1: Confirm Leg-1 is matrix-driven (unchanged claim)**

Run: `grep -hoE "provider=[a-z0-9_]+|getProvider|SKIP Provider MAE" /tmp/weather_bot_live.log | sort | uniq -c | tail -20`
Expected: heterogeneous providers per city (not a single provider for all), consistent with `provider_matrix.json`. Record what you see.

- [ ] **Step 2: Confirm the build picks up the Leg-2 change**

Run: `cd /Users/padraighaughey/Polymarket-Weather-Bot && ls -la dist/selection.js && git log -1 --format='%h %ci' -- src/selection.ts`
Expected: note whether `dist/` predates the new commit. If it does, the LIVE daemon is still
running the OLD rule until restarted. Report this; do NOT restart without user say-so
(restart is the documented gotcha — nohup/caffeinate daemons keep old in-memory `dist/`).

- [ ] **Step 3: Report**

Summarise: (a) Leg-1 providers observed, (b) whether `dist/` is stale vs the new commit,
(c) recommended next action (restart to deploy, or leave as-is). No commit.
