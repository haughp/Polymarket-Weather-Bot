# weatherbot-ts — In-Bucket-Position Second Leg (migrate ECMWF rule)

**Date:** 2026-06-24
**Status:** Design — awaiting review
**Scope:** the live TypeScript bot `weatherbot-ts` only — one function in `src/selection.ts`.
No ECMWF Python bot, no skew tracker, no EV ledger, no live/shadow gate.

---

## 1. Problem

The live weatherbot-ts dual-bucket strategy buys two buckets per city/mode: **F** (the
bucket containing the bias-adjusted forecast) plus one adjacent **neighbour**. The
neighbour is currently chosen by **which side the market prices higher**
(`selectEntryPair` in `src/selection.ts`, lines 63-66).

The sibling ECMWF Python bot replaced that "market picks the higher-priced neighbour"
rule with an **in-bucket-position** rule on 2026-06-23 (commit `92aeb22`, spec
`2026-06-23-ecmwf-forecast-source-and-second-leg-design.md` §3.4), validated to beat the
price-based rule by **+25–27pp** of P(either-leg-hits) on a 90-day backtest. This spec
migrates that same rule into weatherbot-ts so both bots select the second leg identically.

### Already correct — Leg 1 (no change)
Leg-1 forecast sourcing is **not** part of this change and needs no edit. It was verified
this session: `getForecast(city)` → `getModeForecast(city, mode)` → `getProvider(city,
mode)` already fetches each leg-1 forecast from the **provider matrix's best-of-window
provider per (city, mode)** (Open-Meteo for non-NWS, NWS only when the matrix has no
cell). This is the matrix-driven selection — not a single default provider for all
forecasts — and has been live since 2026-06-13. This spec confirms it by end-to-end log
check during testing but changes no Leg-1 code.

---

## 2. The change (Leg 2 only)

One function: `selectEntryPair` in `src/selection.ts`. The neighbour **selection** rule
changes; everything else in the function is unchanged.

### Before (price-based pick)
```ts
// Higher YES price wins — "let the market decide".
candidates.sort((a, b) => safePrice(b.price) - safePrice(a.price));
const neighbor = candidates[0];
```

### After (in-bucket-position pick)
```ts
// In-bucket position of the bias-adjusted forecast picks the neighbour:
// HIGH half of F (pos >= 0.5) -> upper neighbour (F+1); LOW half -> lower (F-1).
// Prefer the position side; use whichever neighbour is listed if the
// preferred side is absent (grid edge / missing bucket / tail F).
const pos = fLow === -999 ? 0 : (adjustedForecastTemp - fLow) / 2; // 2°F (US)
const neighbor = pos >= 0.5 ? (above ?? below)! : (below ?? above)!;
```

`above` / `below` are the already-computed adjacent buckets (lines 56-57). The cast is
safe: the existing guard at lines 59-61 returns early when **both** are null, so at least
one is non-null at this point.

**Tail F has no position to compute, and that is fine.** A finite F has `fLow` set, so
`pos = (adjustedForecastTemp − fLow) / 2` is the fraction of the way `adjustedForecastTemp`
sits through F's `[fLow, fLow+2)` interval — exactly the ECMWF formula. A tail F
(`fLow === -999`, "X or below") has only an **upper** neighbour listed (`below` is null),
and an "X or higher" F (where the existing code already sets `above = null`) has only a
**lower** neighbour — so for any tail, exactly one of `above`/`below` is non-null and the
`?? otherSide` fallback returns that single neighbour regardless of `pos`. The `pos = 0`
shortcut for the `-999` tail just avoids computing a meaningless fraction from the sentinel;
it never changes which neighbour is returned.

### Unchanged in the same function
- F lookup (`bucketContains`), tail-F open-side handling (`fLow === -999`).
- The "no neighbour bucket adjacent to F" early-abort (lines 59-61).
- Ceiling on **both** legs; floor on the **neighbour only**; F always kept.
- Return shape `{ ok: true, pair: [F, neighbor] }` (F first ⇒ snapshot bucket1 = F).
- `price` stays on `SelectableBucket` — still read by the gates, just no longer used to
  *select* the neighbour.

### Mapping to the ECMWF rule
`adjustedForecastTemp` here is the analogue of ECMWF's `corrected_temp`; both bots use
2°F buckets for US cities (weatherbot-ts is US-only, so there is no °C/width-1 branch).
`pos >= 0.5 -> upper` is identical to ECMWF `polymarket_dry_run.py::select_entry_pair`.

---

## 3. Testing

`src/selection.test.ts` is a standalone `ts-node` script (run via `npm test`).

1. **Update** existing assertions that expect the higher-priced neighbour → expect the
   in-bucket-position neighbour.
2. **Add** the discriminating case: forecast in the HIGH half of F, with F−1 priced
   higher than F+1 → still selects **F+1** (proves position overrides price).
3. **Add** `pos < 0.5 -> F-1`.
4. **Add** preferred-side-missing fallback: position says F+1 but only F−1 is listed →
   returns F−1 (no crash, second leg preserved).
5. Floor/ceiling behaviour unchanged — existing gate tests still pass.

`npm test` and `npm run build` must be green before completion.

### Verification (not a unit test)
Tail the live log after a restart and confirm: (a) leg-1 forecasts still print their
matrix provider per city/mode (Leg-1 unchanged), and (b) the selected neighbour matches
the bias-adjusted forecast's half of F rather than the higher-priced side.

---

## 4. Out of scope (explicitly not in this spec)

- Leg-1 forecast sourcing (already matrix-driven; verified, not edited).
- Skew tracker, single/dual leg-mode switch, EV ledger, EV-gated live/shadow
  auto-demotion — those belong to `2026-06-22-weatherbot-skew-leg-and-ev-ledger-design.md`
  (separate, unimplemented).
- A single-leg-only path. Out of scope by decision: in normal operation both neighbours
  are listed, so the position rule always has its preferred side; the `?? otherSide`
  fallback only covers the existing missing-bucket case the code already tolerates.
- Any change to `provider_backtest.py`, the matrix schema, or the daily rebuild.
```
