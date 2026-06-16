import { buyYesFok, fetchAskPrice, getClobClient, sellYesLimit } from "./clob";
import { BotConfig, getActiveLocations } from "./config";
import { badge, C, divider, info, ok, panel, progressBar, skip, stat, warn } from "./colors";
import { DailyForecasts, FORECAST_BIAS, LOCATIONS, getForecast } from "./nws";
import { getBias, getMae } from "./matrix";
import { parseTempRange, bucketMidpoint } from "./parsing";
import { selectEntryPair } from "./selection";
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
import { tomorrowInTz } from "./time";
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

// Max provider forecast error (debiased MAE, °F) the matrix-chosen provider may carry
// before we refuse to trade that city/mode. Polymarket buckets are 2°F wide, so a provider
// whose typical error exceeds this cannot reliably land in the right bucket. Cells with no
// matrix entry (getMae → null) are NOT gated, preserving pre-matrix behavior.
const MAX_PROVIDER_MAE = 2.5;

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
    if (resolvedWin === null && isExpired) {
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
    } else if (resolvedWin === false || (isExpired && currentPrice <= 0.02)) {
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
      const { dateStr, month, day, year } = tomorrowInTz(locData.tz);

      for (const marketMode of ["highest", "lowest"] as const) {
        const forecastTemp = forecasts[marketMode === "highest" ? "max" : "min"][dateStr];
        if (forecastTemp == null) continue;

        const biasOffset = getBias(citySlug, marketMode); // matrix → FORECAST_BIAS fallback → 0
        const adjustedForecastTemp = forecastTemp + biasOffset;

        // Provider-accuracy gate: if the matrix's chosen provider for this city/mode has a
        // debiased MAE wider than one ~2°F bucket, no bucket pick is reliable — skip.
        const providerMae = getMae(citySlug, marketMode);
        if (providerMae != null && providerMae > MAX_PROVIDER_MAE) {
          skip(`Provider MAE too high for ${citySlug} ${marketMode} — ${providerMae.toFixed(2)}°F > ${MAX_PROVIDER_MAE}°F gate`);
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

        // Compute hours to forecast peak/trough
        const peakTimeStr = forecasts[marketMode === "highest" ? "maxTime" : "minTime"][dateStr];
        if (!peakTimeStr) {
          skip(`No forecast peak time available for ${dateStr}`);
          continue;
        }
        const peakDt = new Date(peakTimeStr);
        const hoursToPeak = (peakDt.getTime() - Date.now()) / (1000 * 3600);
        const { openH: entryOpenH, closeH: entryCloseH } = entryWindow(citySlug);

        const localFormatter = new Intl.DateTimeFormat('en-US', {
          hour: '2-digit',
          minute: '2-digit',
          hourCycle: 'h23',
          timeZone: locData.tz
        });
        const localPeakStr = localFormatter.format(peakDt);

        console.log(
          "\n" +
            panel(
              `${locData.name} • ${dateStr} (${marketMode.toUpperCase()})`,
              [
                stat(`Forecast ${marketMode}`, biasOffset !== 0
                  ? `${forecastTemp}°F → ${adjustedForecastTemp}°F (bias ${biasOffset > 0 ? "+" : ""}${biasOffset}°F)`
                  : `${forecastTemp}°F`, "cyan"),
                stat("Peak time", `${localPeakStr} local`, "blue"),
                stat("Hours to peak", `${hoursToPeak.toFixed(1)}h`,
                  (hoursToPeak < entryCloseH || hoursToPeak > entryOpenH) ? "red" : "green"),
                stat("Entry window", `${entryCloseH}–${entryOpenH}h before peak`, "blue")
              ],
              "blue"
            )
        );

        if (hoursToPeak > entryOpenH) {
          skip(`Too early — ${hoursToPeak.toFixed(1)}h to peak (entry opens at ${entryOpenH}h)`);
          continue;
        }
        if (hoursToPeak < entryCloseH) {
          skip(`Entry window closed — peak in ${hoursToPeak.toFixed(1)}h (window was ${entryCloseH}–${entryOpenH}h)`);
          continue;
        }

        interface ScoredBucket {
          market: PolymarketMarket;
          question: string;
          price: number;
          range: [number, number];
          midpointDistance: number;
        }

        // Price gate bounds. Floor (min) applies to the NEIGHBOR only — F (the
        // forecast-center bucket) is always kept regardless of price; the
        // ceiling (max) applies to both legs.
        const DUAL_BUCKET_MIN_PRICE = MIN_YES_PRICE;
        const DUAL_BUCKET_MAX_PRICE = 0.35;

        // Parse every market to {range, price} WITHOUT a floor filter (the floor
        // is enforced neighbour-only inside selectEntryPair). Keep the index so
        // we can recover the originating market/question after selection.
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

        // Select F + the higher-priced neighbour (F+1 / F−1), "let the market decide".
        const selection = selectEntryPair(parsed, adjustedForecastTemp, {
          minPrice: DUAL_BUCKET_MIN_PRICE,
          maxPrice: DUAL_BUCKET_MAX_PRICE,
        });
        if (!selection.ok) {
          skip(`Selection: ${selection.reason}`);
          continue;
        }
        // F first ⇒ snapshot bucket1 = F. midpointDistance is display-only now.
        const topBuckets: ScoredBucket[] = selection.pair.map((b) => ({
          market: b.market,
          question: b.question,
          price: b.price,
          range: b.range,
          midpointDistance: Math.abs(bucketMidpoint(b.range) - adjustedForecastTemp),
        }));

        const _fmtRange = (r: [number, number]): string =>
          r[0] === -999 ? `le${r[1]}` : r[1] === 999 ? `ge${r[0]}` : `${r[0]}-${r[1]}`;
        const snapshotKey = `${new Date().toISOString().slice(0, 19)}_${citySlug}_${marketMode}`;
        let snapEntered = false;
        const snapData: SignalSnapshot = {
          snapshot_key:  snapshotKey,
          snapped_at:    new Date().toISOString(),
          city:          citySlug,
          mode:          marketMode,
          market_date:   dateStr,
          hours_to_peak: Number(hoursToPeak.toFixed(1)),
          nws_forecast:  forecastTemp,
          adj_forecast:  adjustedForecastTemp,
          bucket1_range: _fmtRange(topBuckets[0].range),
          bucket1_price: topBuckets[0].price,
          bucket2_range: _fmtRange(topBuckets[1].range),
          bucket2_price: topBuckets[1].price,
          entered:       false,
        };

        // --- Per-city live/shadow gate ---
        // Shadow cities are evaluated and snapshotted for calibration but NEVER place real
        // orders, even under --execute. Only promoted (live) cities reach the CLOB.
        const cityLive = isLive(citySlug, cityStatus);
        snapData.status = cityLive ? "live" : "shadow";
        const willExecute = mode === "execute" && cityLive;
        const willPaper = mode === "paper" && cityLive;

        // --- Both-buckets Go/No-Go on live CLOB asks (symmetric for highest & lowest) ---
        // The dual entry is all-or-nothing: fetch BOTH buckets' live asks first; if either
        // exceeds the ceiling (or can't be priced), abort BOTH. This removes the lone-fill
        // case where one bucket dropped at the old per-bucket ask>$0.35 re-check while its
        // sibling filled. Only runs when we can actually price asks (live execute).
        const askByMarket: Record<string, number> = {};
        const tokenByMarket: Record<string, string> = {};
        if (willExecute) {
          if (!clob) {
            warn("CLOB client not initialised — skipping live entry for this city");
            continue;
          }
          let dualGo = true;
          for (const matched of topBuckets) {
            const tokenId = getYesTokenId(matched.market);
            if (!tokenId) {
              skip(`Go/No-Go: a bucket has no CLOB token — aborting dual entry`);
              dualGo = false;
              break;
            }
            let ask: number;
            try {
              ask = await fetchAskPrice(tokenId);
            } catch (e) {
              skip(`Go/No-Go: could not price a bucket (${String(e)}) — aborting dual entry`);
              dualGo = false;
              break;
            }
            if (ask > DUAL_BUCKET_MAX_PRICE) {
              skip(`Go/No-Go: live ask $${ask.toFixed(3)} > $${DUAL_BUCKET_MAX_PRICE} on a bucket — aborting BOTH`);
              dualGo = false;
              break;
            }
            askByMarket[matched.market.id] = ask;
            tokenByMarket[matched.market.id] = tokenId;
          }
          if (!dualGo) {
            snapData.would_enter = false;
            snapData.entered = false;
            await appendSnapshot(snapData);
            continue; // next mode (highest/lowest)
          }
        }
        // Passed Gamma city gate (and live-ask gate when executing) → would enter the pair.
        snapData.would_enter = true;

        // Guard: if we already hold ≥2 positions for this city/date (from a prior run where
        // the NWS forecast was different), don't add a 3rd bucket due to forecast drift.
        const alreadyHeld = Object.values(positions).filter(
          p => p.location === citySlug && p.date === dateStr
        );
        if (alreadyHeld.length >= 2) {
          skip(`Already hold ${alreadyHeld.length} positions for ${citySlug} ${dateStr} — skipping re-entry`);
          snapData.would_enter = false;
          snapData.entered = false;
          await appendSnapshot(snapData);
          continue;
        }

        for (const matched of topBuckets) {
          const price = matched.price;
          const marketId = matched.market.id;
          const question = matched.question;
          const tone = priceTone(price, DUAL_BUCKET_MAX_PRICE, config.exit_threshold);
          console.log(
            panel(
              `Matched Bucket • ${shortQuestion(question, 52)}`,
              [
                stat(`Forecast ${marketMode}`, biasOffset !== 0
                  ? `${forecastTemp}°F → ${adjustedForecastTemp}°F adj`
                  : `${forecastTemp}°F`, "cyan"),
                stat("Distance to adj. forecast", `${matched.midpointDistance.toFixed(1)}°F`, "cyan"),
                stat("YES price", `$${price.toFixed(3)}`, tone),
                stat("Entry gate", `[$${DUAL_BUCKET_MIN_PRICE}, $${DUAL_BUCKET_MAX_PRICE}]`, "green"),
                `${C.DIM("Market odds")}   ${progressBar(price, 1, 26, tone)}`
              ],
              tone
            )
          );

          // DUAL_BUCKET_MAX_PRICE gate already applied above.

          if (positions[marketId]) {
            skip(`Already in this market`);
            continue;
          }

          if (tradesExecuted >= config.max_trades_per_run) {
            skip(`Max trades (${config.max_trades_per_run}) reached`);
            break;
          }

            if (Object.keys(positions).length >= config.max_open_positions) {
              skip(`Max open positions (${config.max_open_positions}) reached. Exposure cap limits risk.`);
              break;
            }

          const positionSize = FIXED_POSITION_SIZE;

          if (balance < positionSize) {
            skip(
              `Wallet balance $${balance.toFixed(2)} is below the fixed order size of $${positionSize.toFixed(2)}`
            );
            break;
          }

          const shares = positionSize / price;
          console.log(
            panel(
              `Entry Signal • ${locData.name} (${marketMode.toUpperCase()})`,
              [
                stat("Action", `${mode === "execute" ? "BUY YES" : "BUY SETUP"}`, "green"),
                stat("Price", `$${price.toFixed(3)}`, "green"),
                stat("Position size", `$${positionSize.toFixed(2)} (Fixed)`, "yellow"),
                stat("Estimated shares", shares.toFixed(1), "blue"),
                `${C.DIM("Sizing gauge")}  ${progressBar(positionSize, Math.max(balance, 1), 26, "green")}`
              ],
              "green"
            )
          );

          if (willExecute) {
            const tokenId = tokenByMarket[marketId];
            const askPrice = askByMarket[marketId];
            if (!tokenId || askPrice == null || !clob) {
              warn("Missing pre-checked ask/token for bucket — skipping leg");
              continue;
            }
            // Both buckets already cleared the Go/No-Go ceiling above; use the pre-fetched ask.
            // Bid 1¢ above ask to cross the spread; FOK fills immediately or cancels.
            // createAndPostMarketOrder(BUY) takes amount in USDC, not shares
            const limitPx = Math.min(askPrice + 0.01, 0.99);
            const actualShares = Math.floor((positionSize / askPrice) * 100) / 100;
            let filled = false;
            try {
              const result = await buyYesFok(clob, tokenId, limitPx, positionSize);
              filled = result.filled;
              if (filled) {
                ok(`CLOB FOK filled @ ask $${askPrice.toFixed(3)} limit $${limitPx.toFixed(3)} (order ${result.orderId.slice(0, 10)}…)`);
              } else {
                warn(`CLOB FOK cancelled post-Go/No-Go — ask raced after precheck on ${shortQuestion(question, 40)}. No position recorded; sibling leg may have filled (residual lone-fill risk — review).`);
                continue;
              }
            } catch (e) {
              warn(`CLOB buy failed: ${String(e)}`);
              continue;
            }
            // Record position only after confirmed fill
            const pos: Position = {
              question,
              entry_price: askPrice,
              shares: actualShares,
              cost: positionSize,
              date: dateStr,
              location: citySlug,
              forecast_temp: forecastTemp,
              opened_at: new Date().toISOString(),
              token_id: tokenId
            };
            positions[marketId] = pos;
            sim.total_trades += 1;
            const trade: Trade = {
              type: "entry",
              question,
              entry_price: askPrice,
              shares: actualShares,
              cost: positionSize,
              opened_at: pos.opened_at
            };
            sim.trades.push(trade);
            tradesExecuted += 1;
            snapEntered = true;
            balance -= positionSize;
          } else if (willPaper) {
            balance -= positionSize;
            const pos: Position = {
              question,
              entry_price: price,
              shares,
              cost: positionSize,
              date: dateStr,
              location: citySlug,
              forecast_temp: forecastTemp,
              opened_at: new Date().toISOString()
            };
            positions[marketId] = pos;
            sim.total_trades += 1;
            const trade: Trade = {
              type: "entry",
              question,
              entry_price: price,
              shares,
              cost: positionSize,
              opened_at: pos.opened_at
            };
            sim.trades.push(trade);
            tradesExecuted += 1;
            snapEntered = true;
            ok(
              `Position opened — $${positionSize.toFixed(2)} deducted from balance`
            );
          } else {
            if (!cityLive && mode === "execute") {
              skip(`Shadow city (status=shadow) — calibration only, no real orders`);
            } else {
              skip("Dry-run — not buying");
            }
            tradesExecuted += 1;
          }
        }  // end topBuckets loop
        snapData.entered = snapEntered;
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
