#!/usr/bin/env node
import dotenv from "dotenv";
import yargs from "yargs";
import { hideBin } from "yargs/helpers";
import { BotConfig, loadConfig } from "./config";
import { badge, panel, stat } from "./colors";
import { resetSim } from "./simState";
import { run, showPositions, type TradeMode } from "./strategy";
import { getWalletBalanceUsdViaClob } from "./walletBalance";

dotenv.config();
// Load shared wallet credentials (does not override local .env variables)
dotenv.config({ path: "/Users/padraighaughey/sniff_test_polymarket/.env" });

function validateKeys(cfg: BotConfig): void {
  const errors: string[] = [];
  let pk = (cfg.polymarket_private_key || "").trim();
  const addr = (cfg.polymarket_proxy_wallet_address || "").trim();

  if (!pk) {
    errors.push("Private Key missing (checked local .env and shared POLY_PRIVATE_KEY)");
  } else {
    const bare = pk.startsWith("0x") ? pk.slice(2) : pk;
    if (!/^[a-fA-F0-9]{64}$/.test(bare)) {
      errors.push(
        "Private Key must be 64 hex characters (with or without 0x prefix)"
      );
    } else {
      pk = "0x" + bare;
      cfg.polymarket_private_key = pk;
      process.env.POLYMARKET_PRIVATE_KEY = pk;
    }
  }

  if (!addr) {
    errors.push("Proxy Wallet Address missing (checked local .env and shared POLY_FUNDER_ADDRESS)");
  } else if (!/^0x[a-fA-F0-9]{40}$/.test(addr)) {
    errors.push(
      "Proxy Wallet Address must be a 0x-prefixed 40-hex address"
    );
  }

  if (errors.length) {
    console.error(
      "\n" +
        panel(
          "Configuration Error",
          errors.map((error, idx) => `${idx + 1}. ${error}`),
          "red"
        )
    );
    process.exit(1);
  }
}

async function main(): Promise<void> {
  const argv = await yargs(hideBin(process.argv))
    .scriptName("weatherbot-ts")
    .option("execute", {
      type: "boolean",
      default: false,
      describe:
        "Place real limit orders on Polymarket CLOB (requires USDC + allowance)"
    })
    .option("live", {
      type: "boolean",
      default: false,
      describe:
        "Paper trading: update simulation.json with virtual PnL (no on-chain orders)"
    })
    .option("interval", {
      type: "number",
      default: 0,
      describe:
        "With --execute or --live: run every N minutes (e.g. 30). Ctrl+C to stop."
    })
    .option("positions", {
      type: "boolean",
      default: false,
      describe: "Show open positions"
    })
    .option("reset", {
      type: "boolean",
      default: false,
      describe: "Reset simulation file to $1000 virtual balance"
    })
    .help()
    .parseAsync();

  const cfg = await loadConfig();
  validateKeys(cfg);

  if (argv.reset) {
    await resetSim();
    return;
  }

  if (argv.positions) {
    await showPositions();
    return;
  }

  const execute = Boolean(argv.execute);
  const paper = Boolean(argv.live);

  if (execute && paper) {
    console.error(
      "Choose one: --execute (real CLOB trades) or --live (paper simulation), not both."
    );
    process.exit(1);
  }

  const mode: TradeMode = execute ? "execute" : paper ? "paper" : "dry-run";

  const maskedPk = cfg.polymarket_private_key ? `${cfg.polymarket_private_key.substring(0, 6)}••••${cfg.polymarket_private_key.substring(cfg.polymarket_private_key.length - 4)}` : "MISSING";
  const proxyStr = cfg.polymarket_proxy_wallet_address ? `${cfg.polymarket_proxy_wallet_address.substring(0, 6)}••••${cfg.polymarket_proxy_wallet_address.substring(cfg.polymarket_proxy_wallet_address.length - 4)}` : "Not Used";

  console.info(
    "\n" +
      panel(
        "Configuration & Credentials Check",
        [
          stat("Trading Mode", mode.toUpperCase(), "cyan"),
          stat("Signer Wallet", maskedPk, "green"),
          stat("Proxy Funder", proxyStr, "green"),
          stat("Entry Gate", `≤ $${cfg.entry_threshold}`, "blue"),
          stat("Exit Target", `≥ $${cfg.exit_threshold}`, "magenta"),
          stat("Max Positions", `${cfg.max_open_positions}`, "yellow")
        ],
        "blue"
      )
  );

  let walletUsd: number | undefined;
  if (mode === "execute") {
    walletUsd = await getWalletBalanceUsdViaClob(cfg);
    console.info(
      "\n" +
        panel(
          "Live Wallet Check",
          [
            `${badge("EXECUTE", "green")} Real Polymarket CLOB trading is enabled`,
            stat("Wallet balance", `$${walletUsd.toFixed(2)} USD`, "cyan")
          ],
          "green"
        )
    );
  }

  const intervalMin =
    (execute || paper) &&
    typeof argv.interval === "number" &&
    argv.interval > 0
      ? argv.interval
      : 0;

  if (intervalMin > 0) {
    const intervalSec = intervalMin * 60;
    console.info(
      "\n" +
        panel(
          "Loop Mode Active",
          [
            stat("Run cadence", `Every ${intervalMin.toFixed(1)} min`, "blue"),
            stat("Stop", "Ctrl+C", "yellow")
          ],
          "blue"
        ) +
        "\n"
    );
    // eslint-disable-next-line no-constant-condition
    while (true) {
      if (mode === "execute") {
        walletUsd = await getWalletBalanceUsdViaClob(cfg);
      }
      await run({ mode, config: cfg, walletUsd });
      console.info(
        "\n" +
          panel(
            "Cooldown",
            [stat("Next run", `In ${intervalMin.toFixed(1)} min`, "cyan")],
            "cyan"
          ) +
          "\n"
      );
      await new Promise((res) => setTimeout(res, intervalSec * 1000));
    }
  } else {
    await run({ mode, config: cfg, walletUsd });
  }
}

main().catch((err) => {
  console.error(
    "\n" +
      panel(
        "Fatal Error",
        [String(err instanceof Error ? err.stack ?? err.message : err)],
        "red"
      )
  );
  process.exit(1);
});
