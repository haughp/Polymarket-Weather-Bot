# Plan: Fix Missing weatherbot_ts DB Tables

## Context

Every daily review of weatherbot-ts skips SQL checks 1, 2, 4, and 5 because
`public.weatherbot_ts_positions` and `public.weatherbot_ts_trade_history` do not exist
in the `gmgn_trading` database. Four compounding root causes:

1. **No SQL schema file** — `sniff_schema/` covers btc15/h6/r1 etc. but not weatherbot-ts.
   The table definitions live only as a Python SQLAlchemy ORM in
   `Polymarket-Weather-Bot/database_schema.py`, which `init_db_schema.sh` cannot reach.

2. **Sync script never creates tables** — `sync_ts_bot_trades.py:36` calls
   `init_database()` without `auto_migrate=True`, so `Base.metadata.create_all()` never fires.

3. **Sync script never runs** — `sync_trades_periodic.sh` has no launchd plist and no
   crontab entry. Only a `# Add to crontab:` comment documents the intent.

4. **Sync reads wrong JSON key** — post-reset `simulation.json` stores the full 29-trade
   history in `trades_archive`, but the script reads `trades` (currently `[]`), so no
   history would be backfilled even if tables existed.

---

## Stage 1 — Create the SQL schema file and apply it (unblocks all SQL checks)

**New file:** `sniff_test_polymarket/sniff_schema/weatherbot_ts_tables.sql`

The daily review queries (and the ORM) both target `public` schema (no `sniff.` prefix).
Column types mirror the SQLAlchemy ORM exactly.

```sql
-- Open positions (live TS weather bot)
CREATE TABLE IF NOT EXISTS public.weatherbot_ts_positions (
    id             SERIAL PRIMARY KEY,
    clob_token_id  VARCHAR(128) NOT NULL UNIQUE,
    question       VARCHAR(512),
    location       VARCHAR(64),
    market_date    DATE,
    entry_price    NUMERIC(5,4),
    entry_cost     NUMERIC(12,2),
    shares         NUMERIC(16,8),
    current_price  NUMERIC(5,4),
    current_pnl    NUMERIC(12,2),
    opened_at      TIMESTAMPTZ,
    forecast_temp  NUMERIC(5,2),
    kelly_pct      NUMERIC(6,4),
    ev             NUMERIC(6,4),
    our_prob       NUMERIC(6,4),
    updated_at     TIMESTAMPTZ DEFAULT NOW(),
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wts_pos_location    ON public.weatherbot_ts_positions (location);
CREATE INDEX IF NOT EXISTS idx_wts_pos_opened_at   ON public.weatherbot_ts_positions (opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_wts_pos_market_date ON public.weatherbot_ts_positions (market_date);
CREATE INDEX IF NOT EXISTS idx_wts_pos_updated_at  ON public.weatherbot_ts_positions (updated_at DESC);

-- Closed trades (live TS weather bot)
CREATE TABLE IF NOT EXISTS public.weatherbot_ts_trade_history (
    id               SERIAL PRIMARY KEY,
    clob_token_id    VARCHAR(128),
    question         VARCHAR(512),
    location         VARCHAR(64),
    market_date      DATE,
    entry_price      NUMERIC(5,4),
    entry_cost       NUMERIC(12,2),
    shares           NUMERIC(16,8),
    exit_price       NUMERIC(5,4),
    realized_pnl     NUMERIC(12,2),
    opened_at        TIMESTAMPTZ,
    closed_at        TIMESTAMPTZ,
    resolved_outcome VARCHAR(64),
    kelly_pct        NUMERIC(6,4),
    ev               NUMERIC(6,4),
    our_prob         NUMERIC(6,4),
    forecast_temp    NUMERIC(5,2),
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wts_hist_location    ON public.weatherbot_ts_trade_history (location);
CREATE INDEX IF NOT EXISTS idx_wts_hist_closed_at   ON public.weatherbot_ts_trade_history (closed_at DESC);
CREATE INDEX IF NOT EXISTS idx_wts_hist_opened_at   ON public.weatherbot_ts_trade_history (opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_wts_hist_market_date ON public.weatherbot_ts_trade_history (market_date);
CREATE INDEX IF NOT EXISTS idx_wts_hist_token_id    ON public.weatherbot_ts_trade_history (clob_token_id);
```

Apply immediately:
```bash
psql -d gmgn_trading -f sniff_schema/weatherbot_ts_tables.sql
```

`init_db_schema.sh` will keep it idempotent on every future restart automatically.

---

## Stage 2 — Fix sync script to backfill from trades_archive

**Edit:** `Polymarket-Weather-Bot/sync_ts_bot_trades.py`, line 77

```python
# Before:
trades = sim_data.get("trades", [])

# After (merge live trades + full archive):
trades = sim_data.get("trades", []) + sim_data.get("trades_archive", [])
```

The existing `filter_by(closed_at=..., question=...)` deduplication guard on lines 81–84
already prevents double-inserts, so merging is safe. This backfills the 20 exit records
from `trades_archive` into `weatherbot_ts_trade_history` on the first sync run.

---

## Stage 3 — Register sync as a launchd job (runs every 5 minutes)

**New file:** `~/Library/LaunchAgents/com.sniff.weather-ts-sync.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sniff.weather-ts-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/padraighaughey/Polymarket-Weather-Bot/sync_trades_periodic.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/padraighaughey/Polymarket-Weather-Bot</string>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/weather_bot_sync.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/weather_bot_sync_err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/Users/padraighaughey</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
```

Load it:
```bash
launchctl load -w ~/Library/LaunchAgents/com.sniff.weather-ts-sync.plist
```

---

## Files changed

| File | Action |
|------|--------|
| `sniff_test_polymarket/sniff_schema/weatherbot_ts_tables.sql` | CREATE |
| `Polymarket-Weather-Bot/sync_ts_bot_trades.py` | EDIT line 77 (merge trades_archive) |
| `~/Library/LaunchAgents/com.sniff.weather-ts-sync.plist` | CREATE |

---

## Verification

```bash
# 1. Tables exist
psql -d gmgn_trading -c "\d public.weatherbot_ts_positions"
psql -d gmgn_trading -c "\d public.weatherbot_ts_trade_history"

# 2. History backfilled (expect 20 rows — the exits from trades_archive)
psql -d gmgn_trading -c "SELECT count(*), sum(realized_pnl) FROM public.weatherbot_ts_trade_history"

# 3. Sync daemon registered
launchctl list | grep weather-ts-sync

# 4. Run next daily review — SQL checks 1/2/4/5 should no longer be skipped
```
