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

let _matrixPath = path.resolve(__dirname, "..", "provider_matrix.json");
let _matrix: ProviderMatrix | null = null;
let _matrixMtimeMs: number | null = null; // mtime of the file backing _matrix, or null if unloaded

/**
 * Loads the matrix, reloading whenever the file's mtime has changed since the last
 * load. provider_matrix.json is rebuilt daily (09:00, com.sniff.provider-matrix-rebuild)
 * by a process separate from this bot; caching it for the life of the process meant a
 * long-running daemon traded on a stale (up to 24h old) matrix until its next restart.
 * An mtime check is a cheap stat() per call — cheaper than the network calls this
 * function's callers gate — so this reloads on every rebuild without a restart.
 */
function loadMatrix(): ProviderMatrix | null {
  let mtimeMs: number;
  try {
    mtimeMs = fs.statSync(_matrixPath).mtimeMs;
  } catch {
    _matrix = null;
    _matrixMtimeMs = null;
    return null;
  }
  if (_matrix !== null && mtimeMs === _matrixMtimeMs) return _matrix;
  try {
    const raw = fs.readFileSync(_matrixPath, "utf-8");
    _matrix = JSON.parse(raw) as ProviderMatrix;
    _matrixMtimeMs = mtimeMs;
    return _matrix;
  } catch {
    _matrix = null;
    _matrixMtimeMs = null;
    return null;
  }
}

/** Test-only: point loadMatrix() at a different file and clear cached state. */
export function __setMatrixPathForTest(p: string): void {
  _matrixPath = p;
  _matrix = null;
  _matrixMtimeMs = null;
}

/** Test-only: restore the real matrix path and clear cached state. */
export function __resetMatrixPathForTest(): void {
  _matrixPath = path.resolve(__dirname, "..", "provider_matrix.json");
  _matrix = null;
  _matrixMtimeMs = null;
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
