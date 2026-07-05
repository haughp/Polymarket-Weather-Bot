import fs from "fs/promises";
import path from "path";

/**
 * Per-(city, mode) live/shadow gating for the consensus-48 strategy.
 *
 * Distinct from cityStatus.ts (which gates the OLD dual-bucket model per city). The
 * consensus model qualifies each (city, mode) COMBO independently on real forward EV;
 * the Python qualifier (promote_combos.py) owns the decision and writes
 * `strategy_status.json`. The TS bot only READS it here to decide whether a combo may
 * place real-money orders ("live") or must stay in dry calibration ("shadow").
 *
 * Fail-safe: a missing/garbled file, an unknown combo, or a non-"live" status all resolve
 * to shadow — an unqualified combo must never place a real order. There is NO grandfathering:
 * only combos explicitly written "live" (the seed set, then whatever the qualifier promotes)
 * trade with real money.
 *
 * Key format: `"${city}|${mode}"` where mode is "highest" | "lowest" (the bot's market modes).
 */

export type ComboState = "live" | "shadow";

export interface ComboStatusEntry {
  status: ComboState;
  n?: number;
  ev?: number;
  promoted_at?: string | null;
}

export type StrategyStatusMap = Record<string, ComboStatusEntry>;

const STATUS_FILE = path.resolve(__dirname, "..", "strategy_status.json");

export function comboKey(city: string, mode: string): string {
  return `${city}|${mode}`;
}

/**
 * Load strategy_status.json. Returns `null` when the file is absent or unreadable, which
 * (via isComboLive) resolves every combo to shadow — the fail-safe default.
 */
export async function loadStrategyStatus(): Promise<StrategyStatusMap | null> {
  try {
    const raw = await fs.readFile(STATUS_FILE, "utf8");
    const parsed = JSON.parse(raw) as StrategyStatusMap;
    if (parsed && typeof parsed === "object") return parsed;
    return null;
  } catch {
    return null;
  }
}

/**
 * Decide whether a (city, mode) combo may trade with real money under the consensus model.
 * Everything not explicitly "live" (missing file, unknown combo, "shadow") → false.
 */
export function isComboLive(city: string, mode: string, status: StrategyStatusMap | null): boolean {
  if (status === null) return false;
  const entry = status[comboKey(city, mode)];
  return !!entry && entry.status === "live";
}
