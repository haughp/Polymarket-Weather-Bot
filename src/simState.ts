import fs from "fs/promises";
import path from "path";
import { ok } from "./colors";

const SIM_FILE = path.resolve(__dirname, "..", "simulation.json");
const SIM_BALANCE = 1000.0;

export interface Position {
  question: string;
  entry_price: number;
  shares: number;
  cost: number;
  date: string;
  location: string;
  forecast_temp: number;
  opened_at: string;
  /** CLOB conditional token for Yes outcome (real trades only) */
  token_id?: string;
  pnl?: number;
  current_price?: number;
  kelly_pct?: number;
  ev?: number;
  our_prob?: number;
}

export interface Trade {
  type: "entry" | "exit";
  question: string;
  entry_price: number;
  shares?: number;
  cost: number;
  opened_at?: string;
  exit_price?: number;
  pnl?: number;
  closed_at?: string;
  // Optional analytics fields
  kelly_pct?: number;
  ev?: number;
  our_prob?: number;
  location?: string;
  date?: string;
  forecast_temp?: number;
  /** Git revision of the running build that executed this trade (e.g. "d7079dd"
   *  or "d7079dd+dirty"). Captured once at process start; stamped on every
   *  entry/exit so the dashboard can attribute each trade to its source code. */
  code_rev?: string;
}

export interface SimulationState {
  balance: number;
  starting_balance: number;
  positions: Record<string, Position>;
  trades: Trade[];
  total_trades: number;
  wins: number;
  losses: number;
  peak_balance: number;
}

export interface SignalSnapshot {
  snapshot_key:  string;
  snapped_at:    string;
  city:          string;
  mode:          "highest" | "lowest";
  market_date:   string;
  hours_to_peak: number;
  nws_forecast:  number;
  adj_forecast:  number;
  bucket1_range: string;
  bucket1_price: number;
  bucket2_range: string;
  bucket2_price: number;
  entered:       boolean;
  /** "live" = real-money eligible; "shadow" = dry calibration only (see cityStatus.ts) */
  status?:       "live" | "shadow";
  /** True when the city would have entered had it been live (shadow accounting) */
  would_enter?:  boolean;
  // ── consensus-48 model fields (single-leg; bucket1_* carries the consensus bucket F) ──
  /** Hours-to-peak at decision time (the ~48h lead). */
  lead_h?:       number;
  /** Real executable YES ask fetched at decision time — the promote_combos qualifier
   *  computes forward EV from THIS, not the mid in bucket1_price. */
  ask_price?:    number;
  /** True when the forecast bucket F was ALSO the market's top-priced bucket (agreement). */
  agree?:        boolean;
}

const SNAPSHOTS_FILE = path.resolve(__dirname, "..", "snapshots.jsonl");

export async function appendSnapshot(snap: SignalSnapshot): Promise<void> {
  await fs.appendFile(SNAPSHOTS_FILE, JSON.stringify(snap) + "\n", "utf8");
}

export async function loadSim(): Promise<SimulationState> {
  try {
    const raw = await fs.readFile(SIM_FILE, "utf8");
    return JSON.parse(raw) as SimulationState;
  } catch {
    return {
      balance: SIM_BALANCE,
      starting_balance: SIM_BALANCE,
      positions: {},
      trades: [],
      total_trades: 0,
      wins: 0,
      losses: 0,
      peak_balance: SIM_BALANCE
    };
  }
}

export async function saveSim(sim: SimulationState): Promise<void> {
  const data = JSON.stringify(sim, null, 2);
  await fs.writeFile(SIM_FILE, data, "utf8");
}

export async function resetSim(): Promise<void> {
  try {
    await fs.unlink(SIM_FILE);
  } catch {
    // ignore
  }
  ok(`Simulation reset — balance back to $${SIM_BALANCE.toFixed(2)}`);
}

