import { buyYesFok, fetchAskPrice, getClobClient, sellYesLimit } from "./clob";
import { BotConfig, getActiveLocations } from "./config";
import { badge, C, divider, info, ok, panel, progressBar, skip, stat, warn } from "./colors";
import { DailyForecasts, FORECAST_BIAS, LOCATIONS, getForecast } from "./nws";
import { getBias, getMae, getUnit } from "./matrix";
import { parseTempRange, bucketMidpoint } from "./parsing";
import { selectEntryPair } from "./selection";
import { selectConsensusBucket } from "./consensus";
import {
  PolymarketEvent,
  PolymarketMarket,
  getPolymarketEvent,
  getMarketYesPrice,
  getMarketResolution,
  getYesTokenId
} from "./polymarket";
import { Position, Trade, SignalSnapshot, loadSim, saveSim, appendSnapshot } from "./simState";
import { isLive, loadCityStatus } from "./cityStatus";
import { isComboLive, loadStrategyStatus } from "./strategyStatus";
import { tomorrowInTz, targetDatesForLead } from "./time";
import type { ClobClient } from "@polymarket/clob-client-v2";

const FIXED_POSITION_SIZE = 1.05;

// Minimum YES price — filter out near-zero (effectively-resolved) markets
// Minimum YES price — filter out near-zero (effectively-resolved) markets
// Raised from $0.05 to $0.12. At 36h out, if the market prices a bucket under 12¢, 
// smart money fundamentally disagrees with our model. Do not catch falling knives.
const MIN_YES_PRICE = 0.12;
// Entry window relative to peak temperature time
const PEAK_ENTRY_OPEN_H = 36;   // start entry window 36h before forecast peak
const PEAK_ENTRY_CLOSE_H = 30;  // close entry window 30h before forecast peak

// Per-city entry-window overrides [openH, closeH]. Atlanta's forecast peak is 23:00
// local — the latest of any city — so its 36–30h window opens before the Polymarket
// market is even listed and is already closed the first time the bot sees it (the
// market never appeared earlier than ~26h to peak). A later [30, 24] window catches
// the Atlanta listing. Only override cities listed here; everyone else uses the
// global [36, 30] window. See memory project_weatherbot_atlanta_entry_window_miss.
const ENTRY_WINDOW_OVERRIDES: Record<string, { openH: number; closeH: number }> = {
  atlanta: { openH: 30, closeH: 24 },
};

function entryWindow(citySlug: string): { openH: number; closeH: number } {
  return ENTRY_WINDOW_OVERRIDES[citySlug] ?? { openH: PEAK_ENTRY_OPEN_H, closeH: PEAK_ENTRY_CLOSE_H };
}

// Max provider forecast error (debiased MAE) the matrix-chosen provider may carry before we
// refuse to trade that city/mode. Unit-specific: US markets quote 2°F buckets, non-US markets
// quote 1°C buckets (≈1.8°F), so the °C ceiling is tighter. A provider whose typical error
// exceeds these cannot reliably land in the right bucket. Tightened 2026-06-16 (was a single
// 2.5°F gate) after NYC + Dallas June-15 losses where mae_debiased 1.6–1.9°F still traded and
// missed by a bucket. Cities with NO matrix cell (getMae → null) are now SKIPPED, not traded:
// an unproven provider is not trusted.
const MAX_PROVIDER_MAE_F = 1.5;
// 0.85°C (tightened from 1.0 on 2026-06-30): a 1.0 cap == a full 1°C bucket, too loose.
// Note: TS bot is US-only (°F), so this °C ceiling only bites if a °C city is ever added.
const MAX_PROVIDER_MAE_C = 0.85;

// ── Consensus-48 model (replaces the dual-bucket strategy) ──────────────────
// Enter ~48h before forecast peak; buy the single consensus bucket (forecast bucket F
// only when F is the market's top-priced bucket). Price cap replaces the old $0.35
// ceiling (this model buys the favourite): above ~$0.60 the q/p−1 edge is ≤0 for q≈0.45.
const CONSENSUS_MAX_PRICE = 0.60;
const CONSENSUS_ENTRY_OPEN_H = 50;   // window opens 50h before peak
const CONSENSUS_ENTRY_CLOSE_H = 46;  // window closes 46h before peak (centred on 48)

export type TradeMode = "dry-run" | "paper" | "execute";

export interface RunOptions {
  mode: TradeMode;
  config: BotConfig;
  /** Polymarket CLOB collateral balance (USDC) — used for sizing in execute mode */
  walletUsd?: number;
}

function modeTone(mode: TradeMode): "green" | "yellow" | "cyan" {
  if (mode === "execute") return "green";
  if (mode === "paper") return "yellow";
  return "cyan";
}

function modeText(mode: TradeMode): string {
  if (mode === "execute") return "LIVE EXECUTION";
  if (mode === "paper") return "PAPER TRADING";
  return "SIGNAL ONLY";
}

function priceTone(
  price: number,
  entry: number,
  exit: number
): "green" | "yellow" | "red" {
  if (price < entry) return "green";
  if (price >= exit) return "red";
  return "yellow";
}

function shortQuestion(question: string, max = 62): string {
  return question.length > max ? `${question.slice(0, max - 1)}…` : question;
}


export async function showPositions(): Promise<void> {
  const sim = await loadSim();
  const positions = sim.positions;
  console.log(
    "\n" +
      panel(
        "Open Positions",
        [
          stat("Virtual balance", `$${sim.balance.toFixed(2)}`, "cyan"),
          stat("Open positions", `${Object.keys(positions).length}`, "blue"),
          stat("Trades", `${sim.total_trades}`, "magenta"),
          stat("W/L", `${sim.wins}/${sim.losses}`, "yellow")
        ],
        "blue"
      )
  );
  const mids = Object.keys(positions);
  if (!mids.length) {
    console.log(panel("Portfolio Status", [C.GRAY("No open positions right now.")], "gray"));
    return;
  }

  let totalPnl = 0;
  for (const mid of mids) {
    const pos = positions[mid];
    const currentPrice =
      (await getMarketYesPrice(mid)) ?? pos.entry_price ?? 0;
    const pnl = (currentPrice - pos.entry_price) * pos.shares;
    totalPnl += pnl;
    const pnlStr =
      pnl >= 0
        ? C.GREEN(`+$${pnl.toFixed(2)}`)
        : C.RED(`-$${Math.abs(pnl).toFixed(2)}`);
    const tone = pnl >= 0 ? "green" : "red";
    console.log(
      "\n" +
        panel(
          shortQuestion(pos.question, 68),
          [
            stat("Entry", `$${pos.entry_price.toFixed(3)}`, "cyan"),
            stat("Now", `$${currentPrice.toFixed(3)}`, tone),
            stat("Shares", pos.shares.toFixed(1), "blue"),
            stat("Cost", `$${pos.cost.toFixed(2)}`, "yellow"),
            stat("PnL", pnlStr, tone),
            `${C.DIM("Market odds")}   ${progressBar(currentPrice, 1, 26, tone)}`
          ],
          tone
        )
    );
  }

  const pnlColor = totalPnl >= 0 ? C.GREEN : C.RED;
  console.log(
    "\n" +
      panel(
        "Portfolio Summary",
        [
          stat("Balance", `$${sim.balance.toFixed(2)}`, "cyan"),
          stat(
            "Open PnL",
            pnlColor(`${totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(2)}`),
            totalPnl >= 0 ? "green" : "red"
          ),
          stat("Total trades", `${sim.total_trades}`, "blue"),
          stat("W/L", `${sim.wins}/${sim.losses}`, "yellow")
        ],
        totalPnl >= 0 ? "green" : "red"
      )
  );
}

export async function run(options: RunOptions): Promise<void> {
  const { mode, config } = options;

  const sim = await loadSim();
  const walletUsd = options.walletUsd;

  let balance: number =
    mode === "execute" && walletUsd != null && Number.isFinite(walletUsd)
      ? walletUsd
      : sim.balance;

  const positions = sim.positions;
  let tradesExecuted = 0;
  let exitsFound = 0;

  // Per-city live/shadow gating. `null` => no status file => grandfathered-live fallback.
  const cityStatus = await loadCityStatus();
  // Per-(city,mode) live/shadow for the consensus-48 model (promote_combos.py writes it).
  const strategyStatus = await loadStrategyStatus();

  let clob: ClobClient | undefined;
  if (mode === "execute") {
    try {
      clob = await getClobClient(config);
    } catch (e) {
      warn(`Failed to init CLOB client: ${String(e)}`);
      return;
    }
  }

  const starting = sim.starting_balance;
  const totalReturn = ((balance - starting) / starting) * 100;
  const returnStr =
    totalReturn >= 0
      ? C.GREEN(`+${totalReturn.toFixed(1)}%`)
      : C.RED(`${totalReturn.toFixed(1)}%`);

  console.log(
    "\n" +
      panel(
        "Weather Trading Bot",
        [
          `${badge(modeText(mode), modeTone(mode))} ${C.DIM("Automated weather-market scanner")}`,
          "",
          stat(mode === "execute" ? "Wallet" : "Virtual balance", `$${balance.toFixed(2)}`, "cyan"),
          ...(mode !== "execute"
            ? [stat("Return vs start", `${returnStr}  ${C.DIM(`from $${starting.toFixed(2)}`)}`, totalReturn >= 0 ? "green" : "red")]
            : []),
          stat("Position size", `$${FIXED_POSITION_SIZE.toFixed(2)} Fixed`, "blue"),
          stat("Max open cap", `${config.max_open_positions} positions`, "yellow"),
          stat("Entry threshold", `< $${config.entry_threshold.toFixed(2)}`, "green"),
          stat("Exit strategy", "Hold to resolution", "green"),
          stat("Trade record", `${sim.wins} wins / ${sim.losses} losses`, "yellow")
        ],
        modeTone(mode)
      )
  );

  const persist = mode === "paper" || mode === "execute";

  // --- CHECK EXITS ---
  console.log(`\n${divider("EXIT SCAN", "magenta")}`);
  for (const [mid, pos] of Object.entries(positions)) {
    const posDate = new Date(pos.date);
    const now = new Date();
    const daysSinceTarget = (now.getTime() - posDate.getTime()) / (1000 * 3600 * 24);
    const isExpired = daysSinceTarget > 1.5;

    const rawPrice = await getMarketYesPrice(mid);
    let resolvedWin: boolean | null = null;

    if (rawPrice == null || isExpired) {
      resolvedWin = await getMarketResolution(mid);
      // Wait until Polymarket actually reports a winner before claiming a win or loss
      if (resolvedWin === null && !isExpired) continue;
    }

    const currentPrice = rawPrice ?? 0;

    // Polymarket API often lags official resolution by 12-24h even when outcomePrices
    // already shows ["1","0"] or ["0","1"]. Use price as a fallback once expired.
    // CRITICAL: Only use price as a fallback if we actually got a price (rawPrice != null).
    // Never infer resolution from currentPrice == 0 (which happens when API fails).
    if (resolvedWin === null && isExpired && rawPrice != null) {
      if (currentPrice >= 0.98) resolvedWin = true;
      else if (currentPrice <= 0.02) resolvedWin = false;
    }

    if (resolvedWin === true) {
      exitsFound += 1;
      const exitPrice = resolvedWin === true ? 1.0 : currentPrice;
      const pnl = (exitPrice - pos.entry_price) * pos.shares;
      console.log(
        panel(
          `Exit Candidate • ${shortQuestion(pos.question, 56)}`,
          [
            stat(resolvedWin !== null ? "Settlement price" : "Current price", `$${exitPrice.toFixed(3)}`, "red"),
            stat("Shares", pos.shares.toFixed(1), "blue"),
            stat(
              "Estimated PnL",
              `${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}`,
              pnl >= 0 ? "green" : "red"
            ),
            `${C.DIM("Odds gauge")}     ${progressBar(exitPrice, 1, 26, "red")}`
          ],
          "magenta"
        )
      );

      if (mode === "execute") {
        if (!pos.token_id || !clob) {
          warn("Missing CLOB token_id for this position — cannot sell on-chain");
          continue;
        }
        ok("Market resolved YES. Platform will auto-settle.");
        balance += pos.cost + pnl;
        const est = pnl;
        if (est > 0) sim.wins += 1;
        else sim.losses += 1;
        const trade: Trade = {
          type: "exit",
          question: pos.question,
          entry_price: pos.entry_price,
          exit_price: exitPrice,
          pnl: Number(est.toFixed(2)),
          cost: pos.cost,
          location: pos.location,
          date: pos.date,
          forecast_temp: pos.forecast_temp,
          closed_at: new Date().toISOString()
        };
        sim.trades.push(trade);
        delete positions[mid];
        ok(`Closed — est. PnL: ${est >= 0 ? "+" : ""}${est.toFixed(2)}`);
      } else if (mode === "paper") {
        balance += pos.cost + pnl;
        if (pnl > 0) sim.wins += 1;
        else sim.losses += 1;
        const trade: Trade = {
          type: "exit",
          question: pos.question,
          entry_price: pos.entry_price,
          exit_price: exitPrice,
          pnl: Number(pnl.toFixed(2)),
          cost: pos.cost,
          location: pos.location,
          date: pos.date,
          forecast_temp: pos.forecast_temp,
          closed_at: new Date().toISOString()
        };
        sim.trades.push(trade);
        delete positions[mid];
        ok(
          `Closed — PnL: ${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}`
        );
      } else {
        skip("Dry-run — not selling");
      }
    } else if (resolvedWin === false || (isExpired && rawPrice != null && currentPrice <= 0.02)) {
      // Only treat as confirmed loss if:
      // 1. Market explicitly resolved NO, OR
      // 2. Expired AND we got a real price (not null/0 from API failure) AND it's ≤$0.02
      exitsFound += 1;
      const pnl = -pos.cost;
      console.log(
        panel(
          `Dead Position / Loss • ${shortQuestion(pos.question, 54)}`,
          [
            stat("Current price", `$${currentPrice.toFixed(3)}`, "red"),
            stat("Entry price", `$${pos.entry_price.toFixed(3)}`, "cyan"),
            stat("Shares", pos.shares.toFixed(1), "blue"),
            stat("Realized PnL", `-$${Math.abs(pnl).toFixed(2)}`, "red"),
            `${C.DIM("Reason")}         ${resolvedWin === false ? "Market resolved NO" : "Expired + price ≤ $0.02"}`
          ],
          "red"
        )
      );

      if (mode === "execute") {
        if (pos.token_id && clob && currentPrice > 0 && resolvedWin === null) {
          try {
            await sellYesLimit(clob, pos.token_id, 0.01, pos.shares);
            ok("CLOB salvage sell order submitted");
          } catch (e) {
            // Ignore error, clear the position anyway to avoid zombie
          }
        }

        sim.losses += 1;
        const trade: Trade = {
          type: "exit",
          question: pos.question,
          entry_price: pos.entry_price,
          exit_price: currentPrice,
          pnl: Number(pnl.toFixed(2)),
          cost: pos.cost,
          location: pos.location,
          date: pos.date,
          forecast_temp: pos.forecast_temp,
          closed_at: new Date().toISOString()
        };
        sim.trades.push(trade);
        delete positions[mid];
        warn(`Cleared zombie position — Realized Loss: -$${Math.abs(pnl).toFixed(2)}`);
      } else if (mode === "paper") {
        sim.losses += 1;
        const trade: Trade = {
          type: "exit",
          question: pos.question,
          entry_price: pos.entry_price,
          exit_price: currentPrice,
          pnl: Number(pnl.toFixed(2)),
          cost: pos.cost,
          location: pos.location,
          date: pos.date,
          forecast_temp: pos.forecast_temp,
          closed_at: new Date().toISOString()
        };
        sim.trades.push(trade);
        delete positions[mid];
        warn(`Closed — Realized Loss: -$${Math.abs(pnl).toFixed(2)}`);
      } else {
        skip("Dry-run — not recording loss");
      }
    }
  }

  if (exitsFound === 0) {
    skip("No exit opportunities");
  }

  // --- SCAN ENTRIES ---
  console.log(`\n${divider("ENTRY SCAN", "cyan")}`);

  const activeLocations = getActiveLocations(config);
  for (const citySlug of activeLocations) {
    if (!(citySlug in LOCATIONS)) {
      continue;
    }

    const locData = LOCATIONS[citySlug];
    const forecasts: DailyForecasts = await getForecast(citySlug);
    if (!forecasts || Object.keys(forecasts.max).length === 0) continue;

    // Align with ECMWF bot: strictly trade tomorrow's market only (24-36h alpha window).
    // Date is computed in the city's IANA timezone so the NWS lookup key and the Polymarket
    // slug always agree on which observation day we're trading.
    {
      for (const marketMode of ["highest", "lowest"] as const) {
        // Consensus-48: pick the candidate observation date whose forecast peak lands
        // in the [46,50]h entry window (usually day-after-tomorrow, ~2 days out).
        let dateStr = "", month = "", day = 0, year = 0;
        let hoursToPeak = NaN;
        let peakDt: Date | null = null;
        let nearestLead = NaN; // candidate peak closest to the 48h window centre, for visibility
        for (const cand of targetDatesForLead(locData.tz, 3)) {
          const ft = forecasts[marketMode === "highest" ? "max" : "min"][cand.dateStr];
          const pk = forecasts[marketMode === "highest" ? "maxTime" : "minTime"][cand.dateStr];
          if (ft == null || !pk) continue;
          const pd = new Date(pk);
          const h = (pd.getTime() - Date.now()) / (1000 * 3600);
          if (Number.isNaN(nearestLead) || Math.abs(h - 48) < Math.abs(nearestLead - 48)) nearestLead = h;
          if (h >= CONSENSUS_ENTRY_CLOSE_H && h <= CONSENSUS_ENTRY_OPEN_H) {
            dateStr = cand.dateStr; month = cand.month; day = cand.day; year = cand.year;
            hoursToPeak = h; peakDt = pd; break;
          }
        }
        if (!peakDt) {
          // Out of the [46,50]h window right now (normal between-window state). Log for LIVE
          // combos only, so their approach to the window is visible without spamming all cities.
          if (isComboLive(citySlug, marketMode, strategyStatus)) {
            const lead = Number.isNaN(nearestLead) ? "no forecast" : `${nearestLead.toFixed(1)}h`;
            skip(`${citySlug}|${marketMode} LIVE — out of window (nearest peak ${lead}, window ${CONSENSUS_ENTRY_CLOSE_H}–${CONSENSUS_ENTRY_OPEN_H}h)`);
          }
          continue;
        }

        const forecastTemp = forecasts[marketMode === "highest" ? "max" : "min"][dateStr]!;
        const biasOffset = getBias(citySlug, marketMode); // matrix → FORECAST_BIAS fallback → 0
        const adjustedForecastTemp = forecastTemp + biasOffset;

        // Provider-accuracy gate: if the matrix's chosen provider for this city/mode has a
        // debiased MAE wider than one bucket, no bucket pick is reliable — skip. The threshold
        // is unit-specific (°F vs °C). A city with NO matrix cell is treated as unproven and
        // SKIPPED rather than traded through.
        const providerMae = getMae(citySlug, marketMode);
        if (providerMae == null) {
          skip(`No proven provider MAE for ${citySlug} ${marketMode} — unproven, skipping`);
          continue;
        }
        const maeUnit = getUnit(citySlug, marketMode) ?? "F";
        const maeGate = maeUnit === "C" ? MAX_PROVIDER_MAE_C : MAX_PROVIDER_MAE_F;
        if (providerMae > maeGate) {
          skip(`Provider MAE too high for ${citySlug} ${marketMode} — ${providerMae.toFixed(2)}°${maeUnit} > ${maeGate}°${maeUnit} gate`);
          continue;
        }

        const event: PolymarketEvent | null = await getPolymarketEvent(
          citySlug,
          month,
          day,
          year,
          marketMode
        );
        if (!event) continue;

        const localPeakStr = new Intl.DateTimeFormat('en-US', {
          hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: locData.tz,
        }).format(peakDt);
        const comboLive = isComboLive(citySlug, marketMode, strategyStatus);
        console.log(
          "\n" +
            panel(
              `${locData.name} • ${dateStr} (${marketMode.toUpperCase()}) • consensus-48`,
              [
                stat(`Forecast ${marketMode}`, `${forecastTemp}°F → ${adjustedForecastTemp}°F (bias ${biasOffset >= 0 ? "+" : ""}${biasOffset}°F)`, "cyan"),
                stat("Peak time", `${localPeakStr} local`, "blue"),
                stat("Hours to peak", `${hoursToPeak.toFixed(1)}h (window ${CONSENSUS_ENTRY_CLOSE_H}–${CONSENSUS_ENTRY_OPEN_H})`, "green"),
                stat("Combo status", comboLive ? "LIVE" : "shadow", comboLive ? "green" : "yellow"),
              ],
              "blue"
            )
        );

        // Parse every market to {range, mid-price}.
        const parsed: { market: PolymarketMarket; question: string; range: [number, number]; price: number }[] = [];
        for (const market of event.markets ?? []) {
          const question = market.question ?? "";
          const rng = parseTempRange(question);
          if (!rng) continue;
          let yesPrice = NaN;
          try {
            const prices = JSON.parse(market.outcomePrices ?? "[0.5,0.5]") as number[];
            yesPrice = Number(prices[0]);
          } catch {
            yesPrice = NaN;
          }
          parsed.push({ market, question, range: rng, price: yesPrice });
        }

        // Consensus selection: buy F only when F is ALSO the market's top-priced bucket
        // (agreement). Single leg. $0.60 cap replaces the old $0.35 dual-bucket ceiling.
        const selection = selectConsensusBucket(parsed, adjustedForecastTemp, CONSENSUS_MAX_PRICE);
        const _fmtRange = (r: [number, number]): string =>
          r[0] === -999 ? `le${r[1]}` : r[1] === 999 ? `ge${r[0]}` : `${r[0]}-${r[1]}`;
        const snapData: SignalSnapshot = {
          snapshot_key:  `${new Date().toISOString().slice(0, 19)}_${citySlug}_${marketMode}`,
          snapped_at:    new Date().toISOString(),
          city:          citySlug,
          mode:          marketMode,
          market_date:   dateStr,
          hours_to_peak: Number(hoursToPeak.toFixed(1)),
          nws_forecast:  forecastTemp,
          adj_forecast:  adjustedForecastTemp,
          bucket1_range: selection.ok ? _fmtRange(selection.bucket.range) : "",
          bucket1_price: selection.ok ? selection.bucket.price : 0,
          bucket2_range: "",
          bucket2_price: 0,
          entered:       false,
          status:        comboLive ? "live" : "shadow",
          lead_h:        Number(hoursToPeak.toFixed(1)),
          agree:         selection.ok,
        };

        if (!selection.ok) {
          skip(`Consensus: ${selection.reason}`); // disagreement / over-cap / F absent
          snapData.would_enter = false;
          await appendSnapshot(snapData);
          continue;
        }
        const F = selection.bucket;

        // Capture the REAL executable ask for every candidate (live AND shadow) — the
        // qualifier (promote_combos.py) computes forward EV from this ask, not the mid.
        // NO legacy dual-bucket gate remains on this path.
        const tokenId = getYesTokenId(F.market);
        let ask: number | null = null;
        if (tokenId) {
          try { ask = await fetchAskPrice(tokenId); }
          catch (e) { skip(`Could not price ask (${String(e)})`); }
        }
        if (ask != null) snapData.ask_price = ask;
        snapData.would_enter = ask != null && ask <= CONSENSUS_MAX_PRICE;

        // Single leg: don't re-buy THIS bucket. Keyed per-market (F.market.id), NOT per
        // (city,date) — miami runs both modes as SEPARATE markets, so a city/date guard would
        // let one mode silently block the other (opus review Important #1).
        if (positions[F.market.id]) {
          skip(`Already hold ${_fmtRange(F.range)} for ${citySlug} ${dateStr} — no re-entry`);
          await appendSnapshot(snapData);
          continue;
        }

        const willExecute = mode === "execute" && comboLive;
        const willPaper = mode === "paper" && comboLive;
        const positionSize = FIXED_POSITION_SIZE;

        if (willExecute) {
          if (!clob) { warn("CLOB client not initialised — skipping"); await appendSnapshot(snapData); continue; }
          if (!tokenId || ask == null) { skip("No CLOB token/ask — cannot enter"); await appendSnapshot(snapData); continue; }
          if (ask > CONSENSUS_MAX_PRICE) { skip(`Live ask $${ask.toFixed(3)} > cap $${CONSENSUS_MAX_PRICE}`); await appendSnapshot(snapData); continue; }
          if (tradesExecuted >= config.max_trades_per_run) { skip(`Max trades (${config.max_trades_per_run}) reached`); await appendSnapshot(snapData); continue; }
          if (Object.keys(positions).length >= config.max_open_positions) { skip(`Max open positions (${config.max_open_positions}) reached`); await appendSnapshot(snapData); continue; }
          if (balance < positionSize) { skip(`Balance $${balance.toFixed(2)} < order size $${positionSize.toFixed(2)}`); await appendSnapshot(snapData); continue; }
          console.log(panel(`Entry Signal • ${locData.name} (${marketMode.toUpperCase()}) • consensus`, [
            stat("Action", "BUY YES (single leg)", "green"),
            stat("Bucket", _fmtRange(F.range), "cyan"),
            stat("Ask", `$${ask.toFixed(3)}`, "green"),
            stat("Position size", `$${positionSize.toFixed(2)} (Fixed)`, "yellow"),
          ], "green"));
          const limitPx = Math.min(ask + 0.01, 0.99);
          const actualShares = Math.floor((positionSize / ask) * 100) / 100;
          try {
            const result = await buyYesFok(clob, tokenId, limitPx, positionSize);
            if (!result.filled) {
              warn(`CLOB FOK cancelled — ask raced on ${shortQuestion(F.question, 40)}. No position recorded.`);
              await appendSnapshot(snapData); continue;
            }
            ok(`CLOB FOK filled @ ask $${ask.toFixed(3)} limit $${limitPx.toFixed(3)} (order ${result.orderId.slice(0, 10)}…)`);
          } catch (e) {
            warn(`CLOB buy failed: ${String(e)}`);
            await appendSnapshot(snapData); continue;
          }
          const pos: Position = {
            question: F.question, entry_price: ask, shares: actualShares, cost: positionSize,
            date: dateStr, location: citySlug, forecast_temp: forecastTemp,
            opened_at: new Date().toISOString(), token_id: tokenId,
          };
          positions[F.market.id] = pos;
          sim.total_trades += 1;
          sim.trades.push({ type: "entry", question: F.question, entry_price: ask, shares: actualShares, cost: positionSize, opened_at: pos.opened_at });
          tradesExecuted += 1;
          balance -= positionSize;
          snapData.entered = true;
        } else if (willPaper) {
          const px = ask ?? F.price;
          balance -= positionSize;
          const pos: Position = {
            question: F.question, entry_price: px, shares: positionSize / px, cost: positionSize,
            date: dateStr, location: citySlug, forecast_temp: forecastTemp, opened_at: new Date().toISOString(),
          };
          positions[F.market.id] = pos;
          sim.total_trades += 1;
          sim.trades.push({ type: "entry", question: F.question, entry_price: px, shares: positionSize / px, cost: positionSize, opened_at: pos.opened_at });
          tradesExecuted += 1;
          snapData.entered = true;
          ok(`Paper position opened — $${positionSize.toFixed(2)}`);
        } else {
          skip(comboLive ? "Dry-run — not buying" : `Shadow combo ${citySlug}|${marketMode} — calibration only, no real orders`);
        }
        await appendSnapshot(snapData);
      } // end mode loop (highest/lowest)
    }
  }

  if (persist) {
    sim.balance = Number(balance.toFixed(2));
    sim.positions = positions;
    sim.peak_balance = Math.max(sim.peak_balance ?? balance, balance);
    await saveSim(sim);
  }

  console.log(
    "\n" +
      panel(
        "Run Summary",
        [
          stat("Ending balance", `$${balance.toFixed(2)}`, "cyan"),
          stat("Trades this run", `${tradesExecuted}`, tradesExecuted > 0 ? "green" : "gray"),
          stat("Exits found", `${exitsFound}`, exitsFound > 0 ? "magenta" : "gray"),
          stat("Open positions", `${Object.keys(positions).length}`, "blue"),
          `${C.DIM("Bot mode")}       ${badge(modeText(mode), modeTone(mode))}`
        ],
        modeTone(mode)
      )
  );

  if (mode === "dry-run") {
    console.log(
      "\n" +
        panel(
          "Dry-Run Reminder",
          [
            C.YELLOW("No orders were submitted in this run."),
            "Use `--live` for paper trading or `--execute` for real CLOB orders."
          ],
          "yellow"
        )
    );
  }
}
