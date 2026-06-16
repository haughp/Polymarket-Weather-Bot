import { PolymarketEvent } from "./polymarket";

export function parseTempRange(
  question: string | undefined | null
): [number, number] | null {
  if (!question) return null;
  const q = question.toLowerCase();

  if (q.includes("or below")) {
    const m = /(\d+)°f or below/i.exec(question);
    if (m) return [-999, parseInt(m[1], 10)];
  }

  if (q.includes("or higher")) {
    const m = /(\d+)°f or higher/i.exec(question);
    if (m) return [parseInt(m[1], 10), 999];
  }

  const m = /between (\d+)-(\d+)°f/i.exec(question);
  if (m) return [parseInt(m[1], 10), parseInt(m[2], 10)];

  return null;
}

/**
 * Midpoint of a parsed bucket, used to rank buckets by distance to the forecast.
 *
 * US °F buckets are 2° wide and labelled by their lower bound: "between 62-63°F"
 * really resolves on a 62 or 63 reading => interval [62, 64) => midpoint 63
 * (lo + width/2, width 2). Tail markets use the finite bound directly.
 */
export function bucketMidpoint(rng: [number, number]): number {
  if (rng[0] === -999) return rng[1]; // "or below" → upper bound
  if (rng[1] === 999) return rng[0]; // "or higher" → lower bound
  return rng[0] + 1; // lo + width/2, width = 2°F
}

/**
 * True if `temp` resolves YES for the bucket `rng`, using the half-open
 * convention. A label "between 70-71°F" → [70,71] covers readings 70 or 71,
 * i.e. the interval [70, 72). A boundary reading lands in the UPPER bucket:
 * 72.0 is in [72,74), never [70,72).
 *
 * Tails:
 *  - "70°F or below"  → [-999, 70] covers readings ≤ 70 → temp < 71
 *  - "70°F or higher" → [70, 999]  covers readings ≥ 70 → temp ≥ 70
 */
export function bucketContains(rng: [number, number], temp: number): boolean {
  if (rng[0] === -999) return temp < rng[1] + 1; // "X or below" → readings ≤ X
  if (rng[1] === 999) return temp >= rng[0];     // "X or higher" → readings ≥ X
  return temp >= rng[0] && temp < rng[0] + 2;     // [lo, lo+2)
}

/** A parsed Polymarket bucket: its market index payload plus the [lo, hi] range. */
export interface RangedBucket {
  range: [number, number];
}

/**
 * The bucket whose interval contains `temp` (the forecast-center bucket F).
 * Returns the first match, or null when `temp` falls outside every listed
 * bucket (caller should skip the city/mode).
 */
export function findBucketForTemp<T extends RangedBucket>(
  buckets: T[],
  temp: number
): T | null {
  return buckets.find((b) => bucketContains(b.range, temp)) ?? null;
}

/**
 * The neighbouring bucket whose lower bound is `lo + step` (step = +2 for the
 * bucket above F, −2 for the bucket below). Matched by arithmetic, not array
 * order, since Gamma does not guarantee buckets are listed contiguously.
 * Tail buckets ("or below"/"or higher") are never returned as a neighbour
 * (their sentinel lower bound never equals a real `lo ± 2`). Returns null when
 * no such neighbour is listed (grid edge or missing bucket).
 */
export function neighborBucket<T extends RangedBucket>(
  buckets: T[],
  fLow: number,
  step: number
): T | null {
  const targetLo = fLow + step;
  return buckets.find((b) => b.range[0] === targetLo) ?? null;
}

export function hoursUntilResolution(event: PolymarketEvent): number {
  try {
    const endDate = (event as any).endDate ?? (event as any).end_date_iso;
    if (!endDate) return 999;
    const iso = String(endDate).replace("Z", "+00:00");
    const endDt = new Date(iso);
    const now = new Date();
    const deltaHours = (endDt.getTime() - now.getTime()) / (1000 * 3600);
    return Math.max(0, deltaHours);
  } catch {
    return 999;
  }
}

