import fs from "fs/promises";
import path from "path";

/**
 * Per-city live/shadow gating.
 *
 * The TS bot is file-based; the Python sidecar (`promote_cities.py`) owns the promotion
 * decision and writes `city_status.json`. The TS bot only READS it here to decide whether a
 * city may place real-money orders ("live") or must stay in dry calibration ("shadow").
 *
 * Fail-safe rules (a missing/garbled file must never put real money at risk on an unproven city):
 *   - Unknown / missing slug            -> shadow
 *   - city_status.json entirely absent  -> only the GRANDFATHERED_LIVE cities are live
 *                                          (so production is never broken by a missing file)
 */

export type CityState = "live" | "shadow";

export interface CityStatusEntry {
  status: CityState;
  resolved_samples?: number;
  sim_pnl?: number;
  hit_rate?: number;
  promoted_at?: string | null;
}

export type CityStatusMap = Record<string, CityStatusEntry>;

/** Cities that were already trading live before the shadow framework existed. */
export const GRANDFATHERED_LIVE: ReadonlySet<string> = new Set([
  "nyc",
  "chicago",
  "miami",
  "dallas",
  "seattle",
  "atlanta",
]);

const STATUS_FILE = path.resolve(__dirname, "..", "city_status.json");

/**
 * Load city_status.json. Returns `null` when the file is absent or unreadable, which signals
 * callers to apply the grandfathered fallback rather than treating every city as shadow.
 */
export async function loadCityStatus(): Promise<CityStatusMap | null> {
  try {
    const raw = await fs.readFile(STATUS_FILE, "utf8");
    const parsed = JSON.parse(raw) as CityStatusMap;
    if (parsed && typeof parsed === "object") return parsed;
    return null;
  } catch {
    return null;
  }
}

/**
 * Decide whether a city may trade with real money.
 *
 * @param slug   city slug (e.g. "nyc", "los-angeles")
 * @param status result of loadCityStatus(); `null` means the file was absent.
 */
export function isLive(slug: string, status: CityStatusMap | null): boolean {
  if (status === null) {
    // No status file: only previously-live cities trade; everything else stays shadow.
    return GRANDFATHERED_LIVE.has(slug);
  }
  const entry = status[slug];
  if (!entry) return false; // unknown city -> shadow (never spend)
  return entry.status === "live";
}
