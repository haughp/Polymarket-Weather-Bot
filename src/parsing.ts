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

