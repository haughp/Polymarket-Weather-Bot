import * as fs from "fs";
import * as path from "path";
import { FORECAST_PROVIDER, FORECAST_BIAS } from "./nws";

export interface ProviderCell {
  provider: string;
  bias: number;
  mae: number;
  mae_debiased: number;
  samples: number;
  unit?: string; // "F" | "C" — the temperature unit this cell was calibrated in
}

export interface ProviderMatrix {
  generated_at: string;
  window_days: number;
  lead_days: number;
  cities: Record<string, { max?: ProviderCell; min?: ProviderCell }>;
}

let _matrix: ProviderMatrix | null | undefined = undefined; // undefined = not yet loaded

function loadMatrix(): ProviderMatrix | null {
  if (_matrix !== undefined) return _matrix;
  const matrixPath = path.resolve(__dirname, "..", "provider_matrix.json");
  try {
    const raw = fs.readFileSync(matrixPath, "utf-8");
    _matrix = JSON.parse(raw) as ProviderMatrix;
    return _matrix;
  } catch {
    _matrix = null;
    return null;
  }
}

/**
 * Returns the best provider model name for (city, mode).
 * Falls back to FORECAST_PROVIDER[citySlug] → "nws".
 */
export function getProvider(citySlug: string, mode: "max" | "min"): string {
  const m = loadMatrix();
  const cell = m?.cities?.[citySlug]?.[mode];
  if (cell?.provider) return cell.provider;
  return FORECAST_PROVIDER[citySlug] ?? "nws";
}

/**
 * Returns the calibrated bias offset (°F) for (city, mode).
 * Bias = mean(actual − raw_model) — add to raw to get adjusted temp.
 * Falls back to FORECAST_BIAS[citySlug][marketMode] → 0.
 * marketMode is "highest" | "lowest" (strategy.ts convention).
 */
export function getBias(citySlug: string, marketMode: "highest" | "lowest"): number {
  const mode = marketMode === "highest" ? "max" : "min";
  const m = loadMatrix();
  const cell = m?.cities?.[citySlug]?.[mode];
  if (cell !== undefined && typeof cell.bias === "number") return cell.bias;
  return FORECAST_BIAS[citySlug]?.[marketMode] ?? 0;
}

/**
 * Returns the chosen provider's forecast error for (city, mode), in the cell's unit.
 * Prefers `mae_debiased` — the error AFTER the matrix bias is removed — because
 * strategy.ts applies getBias() before bucket selection, so the debiased error is
 * what actually governs whether the forecast lands in the right bucket. Falls back
 * to raw `mae`, then null when no matrix cell exists or the cell carries no usable
 * error figure. The MAE gate now SKIPS on null (a city with no proven error figure
 * is treated as unproven), so callers should gate on null rather than trade through.
 */
export function getMae(citySlug: string, marketMode: "highest" | "lowest"): number | null {
  const mode = marketMode === "highest" ? "max" : "min";
  const m = loadMatrix();
  const cell = m?.cities?.[citySlug]?.[mode];
  if (!cell) return null;
  if (typeof cell.mae_debiased === "number") return cell.mae_debiased;
  if (typeof cell.mae === "number") return cell.mae;
  return null;
}

/**
 * Returns the matrix cell's calibration unit ("F" | "C") for (city, mode), or null
 * when no cell exists. Cells without an explicit `unit` default to "F" (every such
 * legacy cell in the matrix is a US city). Used to pick the unit-specific MAE gate
 * threshold (tighter °C ceiling for the smaller non-US buckets).
 */
export function getUnit(citySlug: string, marketMode: "highest" | "lowest"): "F" | "C" | null {
  const mode = marketMode === "highest" ? "max" : "min";
  const m = loadMatrix();
  const cell = m?.cities?.[citySlug]?.[mode];
  if (!cell) return null;
  return cell.unit === "C" ? "C" : "F";
}
