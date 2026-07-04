// Pure single-bucket "consensus" selection for the weather strategy.
//
// Rule (Phase-1 consensus48 model): buy the forecast bucket F only when F is
// ALSO the market's highest-priced bucket (the crowd agrees with the model).
// Single leg — no neighbour, unlike the dual-bucket selectEntryPair rule in
// selection.ts. When the crowd's top bucket differs from F, there is no
// agreement and we skip the city/mode entirely rather than chase either side.
//
// This module is pure (no network) so it is unit-testable.

import { SelectableBucket } from "./selection";
import { bucketContains } from "./parsing";

export type ConsensusResult<T extends SelectableBucket> =
  | { ok: true; bucket: T }
  | { ok: false; reason: string };

const sameRange = (a: SelectableBucket, b: SelectableBucket): boolean =>
  a.range[0] === b.range[0] && a.range[1] === b.range[1];

const safePrice = (p: number): number => (Number.isFinite(p) ? p : 0);

/**
 * Choose the consensus bucket: the forecast-center bucket F, but only when F
 * is also the highest-priced bucket on the market (agreement) and its price
 * does not exceed `maxPrice`.
 */
export function selectConsensusBucket<T extends SelectableBucket>(
  buckets: T[],
  adjustedForecast: number,
  maxPrice: number
): ConsensusResult<T> {
  const F = buckets.find((b) => bucketContains(b.range, adjustedForecast)) ?? null;
  if (!F) {
    return { ok: false, reason: `forecast ${adjustedForecast.toFixed(1)}°F outside all listed buckets` };
  }

  const top = buckets.reduce((best, b) => (safePrice(b.price) > safePrice(best.price) ? b : best), buckets[0]);

  if (!sameRange(F, top)) {
    return {
      ok: false,
      reason: `no agreement — top bucket [${top.range[0]}-${top.range[1]}] != F [${F.range[0]}-${F.range[1]}]`,
    };
  }

  if (safePrice(F.price) > maxPrice) {
    return { ok: false, reason: `consensus bucket price $${safePrice(F.price).toFixed(3)} > cap $${maxPrice}` };
  }

  return { ok: true, bucket: F };
}
