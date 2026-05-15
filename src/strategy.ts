import { buyYesLimit, getClobClient, sellYesLimit } from "./clob";
import { BotConfig, getActiveLocations } from "./config";
import { badge, C, divider, info, ok, panel, progressBar, skip, stat, warn } from "./colors";
import { DailyForecasts, LOCATIONS, getForecast } from "./nws";
import { parseTempRange } from "./parsing";
import {
  PolymarketEvent,
  PolymarketMarket,
  getPolymarketEvent,
  getMarketYesPrice,
  getMarketResolution,
  getYesTokenId
} from "./polymarket";
import { Position, Trade, loadSim, saveSim } from "./simState";
import { MONTHS } from "./time";
import type { ClobClient } from "@polymarket/clob-client";

const FIXED_POSITION_SIZE = 2.0;

// ECMWF ±1°C confidence band converted to Fahrenheit
const CONFIDENCE_BAND_F = 1.8;

// Maximum YES price to enter on any bucket (including adjacent ones)
const DUAL_BUCKET_MAX_PRICE = 0.35;

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

function bandOverlap(
  rangeMin: number,
  rangeMax: number,
  bandMin: number,
  bandMax: number
): number {
  const lo = Math.max(rangeMin, bandMin);
  const hi = Math.min(rangeMax, bandMax);
  if (hi <= lo) return 0;
  const bandWidth = bandMax - bandMin;
  return bandWidth > 0 ? (hi - lo) / bandWidth : 0;
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
          stat("Exit threshold", `>= $${config.exit_threshold.toFixed(2)}`, "red"),
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

    if (resolvedWin === true || (resolvedWin === null && currentPrice >= config.exit_threshold)) {
      exitsFound += 1;
      const exitPrice = resolvedWin === true ? 1.0 : currentPrice;
      const pnl = (exitPrice - pos.entry_price) * pos.shares;
      console.log(
        panel(
          `Exit Candidate • ${shortQuestion(pos.question, 56)}`,
          [
            stat(resolvedWin !== null ? "Settlement price" : "Current price", `$${exitPrice.toFixed(3)}`, "red"),
            stat("Exit threshold", `$${config.exit_threshold.toFixed(2)}`, "yellow"),
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
        if (resolvedWin === true) {
          ok("Market resolved YES. Platform will auto-settle.");
        } else {
          const sellPx = Math.max(currentPrice - 0.01, 0.01);
          try {
            await sellYesLimit(clob, pos.token_id, sellPx, pos.shares);
            ok("CLOB sell order submitted");
          } catch (e) {
            warn(`CLOB sell failed: ${String(e)}`);
            continue;
          }
        }
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
    } else if (resolvedWin === false || (resolvedWin === null && currentPrice <= 0.02)) {
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
            `${C.DIM("Reason")}         ${resolvedWin === false ? "Market resolved NO" : "Price dropped to ≤ $0.02"}`
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

    // Align with ECMWF bot: strictly trade tomorrow's market only (24-36h alpha window)
    for (let i = 1; i <= 1; i++) {
      const date = new Date();
      date.setDate(date.getDate() + i);
      const dateStr = date.toISOString().slice(0, 10);
      const month = MONTHS[date.getMonth()];
      const day = date.getDate();
      const year = date.getFullYear();

      for (const marketMode of ["highest", "lowest"] as const) {
        const forecastTemp = forecasts[marketMode === "highest" ? "max" : "min"][dateStr];
        if (forecastTemp == null) continue;

        const event: PolymarketEvent | null = await getPolymarketEvent(
          citySlug,
          month,
          day,
          year,
          marketMode
        );
        if (!event) continue;

        // Calculate actual hours left until Polymarket locks trading
        let hoursLeft = 0;
        if (event.endDate) {
          const endDt = new Date(event.endDate);
          hoursLeft = Math.max(0, (endDt.getTime() - Date.now()) / (1000 * 3600));
        } else {
          // Fallback if API doesn't provide endDate: assume 12:00:00 UTC
          const targetEndDt = new Date(Date.UTC(year, date.getMonth(), day, 12, 0, 0));
          hoursLeft = Math.max(0, (targetEndDt.getTime() - Date.now()) / (1000 * 3600));
        }

        console.log(
          "\n" +
            panel(
              `${locData.name} • ${dateStr} (${marketMode.toUpperCase()})`,
              [
                stat(`Forecast ${marketMode}`, `${forecastTemp}°F`, "cyan"),
                stat("Resolves in", `${hoursLeft.toFixed(0)}h`, (hoursLeft < config.min_hours_to_resolution || hoursLeft > config.max_hours_to_resolution) ? "red" : "green"),
                stat("Market date", `${month} ${day}, ${year}`, "blue")
              ],
              "blue"
            )
        );

        if (hoursLeft < config.min_hours_to_resolution) {
          skip(`Resolves in ${hoursLeft.toFixed(0)}h — too close to expiry (<${config.min_hours_to_resolution}h)`);
          continue;
        }

        if (hoursLeft > config.max_hours_to_resolution) {
          skip(`Resolves in ${hoursLeft.toFixed(0)}h — waiting for execution window (≤${config.max_hours_to_resolution}h)`);
          continue;
        }

        interface ScoredBucket {
          market: PolymarketMarket;
          question: string;
          price: number;
          range: [number, number];
          score: number;
        }

        const bandLow  = forecastTemp - CONFIDENCE_BAND_F;
        const bandHigh = forecastTemp + CONFIDENCE_BAND_F;

        const scoredBuckets: ScoredBucket[] = [];

        for (const market of event.markets ?? []) {
          const question = market.question ?? "";
          const rng = parseTempRange(question);
          if (!rng) continue;

          const rMin = rng[0] === -999 ? bandLow - 10 : rng[0];
          const rMax = rng[1] ===  999 ? bandHigh + 10 : rng[1];

          const score = bandOverlap(rMin, rMax, bandLow, bandHigh);
          if (score <= 0) continue;

          try {
            const pricesStr = market.outcomePrices ?? "[0.5,0.5]";
            const prices = JSON.parse(pricesStr) as number[];
            const yesPrice = Number(prices[0]);
            if (!isFinite(yesPrice)) continue;
            if (yesPrice > DUAL_BUCKET_MAX_PRICE) continue;
            scoredBuckets.push({ market, question, price: yesPrice, range: rng, score });
          } catch {
            continue;
          }
        }

        scoredBuckets.sort((a, b) => b.score - a.score);
        const topBuckets = scoredBuckets.slice(0, 2);

        if (topBuckets.length === 0) {
          skip(`No bucket within ±${CONFIDENCE_BAND_F}°F of ${forecastTemp}°F at price ≤ $${DUAL_BUCKET_MAX_PRICE}`);
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
                stat(`Forecast ${marketMode}`, `${forecastTemp}°F`, "cyan"),
                stat("Band", `[${bandLow.toFixed(1)}, ${bandHigh.toFixed(1)}]°F`, "blue"),
                stat("Overlap score", `${(matched.score * 100).toFixed(0)}%`, "cyan"),
                stat("YES price", `$${price.toFixed(3)}`, tone),
                stat("Entry gate", `≤ $${DUAL_BUCKET_MAX_PRICE.toFixed(2)}`, "green"),
                `${C.DIM("Market odds")}   ${progressBar(price, 1, 26, tone)}`
              ],
              tone
            )
          );

          // DUAL_BUCKET_MAX_PRICE gate already applied during scoring above.

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

          if (mode === "execute") {
            const tokenId = getYesTokenId(matched.market);
            if (!tokenId || !clob) {
              warn("No clobTokenIds on market — cannot trade this market on CLOB");
              continue;
            }
            const limitPx = Math.min(price + 0.03, 0.99);
            try {
              await buyYesLimit(clob, tokenId, limitPx, shares);
              ok(`CLOB buy order submitted @ limit $${limitPx.toFixed(3)}`);
            } catch (e) {
              warn(`CLOB buy failed: ${String(e)}`);
              continue;
            }
            const pos: Position = {
              question,
              entry_price: price,
              shares,
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
              entry_price: price,
              shares,
              cost: positionSize,
              opened_at: pos.opened_at
            };
            sim.trades.push(trade);
            tradesExecuted += 1;
            balance -= positionSize;
          } else if (mode === "paper") {
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
            ok(
              `Position opened — $${positionSize.toFixed(2)} deducted from balance`
            );
          } else {
            skip("Dry-run — not buying");
            tradesExecuted += 1;
          }
        }  // end topBuckets loop
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
