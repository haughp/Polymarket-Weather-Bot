// Pure entry-pair selection for the weather dual-bucket strategy.
//
// Rule (replaces the old top-2-by-midpoint-distance logic):
//   F        = the bucket whose interval contains the bias-adjusted forecast
//   neighbor = the F+1 / F−1 bucket on the side of F's interval the bias-adjusted
//              forecast sits in (pos >= 0.5 → upper F+1, else lower F−1);
//              falls back to whichever neighbour is listed if the preferred side
//              is absent. (Migrated from the ECMWF bot, 2026-06-24.)
//   pair     = [F, neighbor]  (F always first ⇒ snapshot bucket1 = F)
//
// Floor/ceiling are split:
//   - The MIN price floor applies to the NEIGHBOR only. F is ALWAYS kept,
//     regardless of price — dropping the cheap-but-correct forecast bucket is
//     exactly the bug that lost the 2026-06-14 Dallas trade.
//   - The MAX price ceiling applies to BOTH F and the chosen neighbor: a
//     forecast-center priced above the ceiling makes the dual entry −EV, so we
//     abort the city rather than chase it. We do NOT fall back to the cheaper
//     neighbor when the higher-priced one busts the ceiling.
//
// This module is pure (no network) so it is unit-testable. The live CLOB ask
// re-check stays in strategy.ts and is the final go/no-go in execute mode.

import { bucketContains, neighborBucket } from "./parsing";

/** A candidate bucket as seen on the Gamma event (price = outcomePrices[0]). */
export interface SelectableBucket {
  range: [number, number];
  price: number;
}

export interface SelectionPriceGate {
  minPrice: number; // floor — applied to the neighbor only
  maxPrice: number; // ceiling — applied to both F and the chosen neighbor
}

export type SelectionResult<T extends SelectableBucket> =
  | { ok: true; pair: [T, T] }
  | { ok: false; reason: string };

/**
 * Choose the [F, neighbor] entry pair from a list of parsed Gamma buckets.
 *
 * `buckets` must already be filtered to parseable ranges (parseTempRange != null)
 * WITHOUT any price floor applied — the floor is enforced here, neighbor-only.
 */
export function selectEntryPair<T extends SelectableBucket>(
  buckets: T[],
  adjustedForecastTemp: number,
  gate: SelectionPriceGate
): SelectionResult<T> {
  const F = buckets.find((b) => bucketContains(b.range, adjustedForecastTemp)) ?? null;
  if (!F) {
    return { ok: false, reason: `forecast ${adjustedForecastTemp.toFixed(1)}°F outside all listed buckets` };
  }

  // Tail F (sentinel bounds) has no arithmetic neighbour on the open side.
  const fLow = F.range[0];
  const above = fLow === -999 ? null : neighborBucket(buckets, fLow, +2);
  const below = neighborBucket(buckets, fLow, -2);
  const candidates = [above, below].filter((b): b is T => b != null);
  if (candidates.length === 0) {
    return { ok: false, reason: `no neighbour bucket adjacent to F (${F.range[0]}-${F.range[1]})` };
  }

  // In-bucket position of the bias-adjusted forecast picks the neighbour:
  // HIGH half of F (pos >= 0.5) -> upper neighbour (F+1); LOW half -> lower (F-1).
  // Prefer the position side; fall back to whichever neighbour IS listed when the
  // preferred side is absent (grid edge / missing bucket / tail F — a tail F lists
  // only one neighbour, so the fallback returns it regardless of pos). 2°F buckets.
  const safePrice = (p: number) => (Number.isFinite(p) ? p : 0);
  const pos = fLow === -999 ? 0 : (adjustedForecastTemp - fLow) / 2;
  const neighbor = pos >= 0.5 ? (above ?? below)! : (below ?? above)!;

  // Ceiling applies to BOTH legs.
  if (safePrice(F.price) > gate.maxPrice) {
    return { ok: false, reason: `forecast bucket price $${safePrice(F.price).toFixed(3)} > ceiling $${gate.maxPrice}` };
  }
  if (safePrice(neighbor.price) > gate.maxPrice) {
    return { ok: false, reason: `chosen neighbour price $${safePrice(neighbor.price).toFixed(3)} > ceiling $${gate.maxPrice}` };
  }
  // Floor applies to the NEIGHBOR only — F is exempt.
  if (safePrice(neighbor.price) < gate.minPrice) {
    return { ok: false, reason: `chosen neighbour price $${safePrice(neighbor.price).toFixed(3)} < floor $${gate.minPrice}` };
  }

  return { ok: true, pair: [F, neighbor] };
}
