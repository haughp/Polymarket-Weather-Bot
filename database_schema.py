#!/usr/bin/env python3
"""
PostgreSQL Database Schema for Polymarket Weather Arbitrage
"""

import datetime
from sqlalchemy import create_engine, Column, Integer, Numeric, String, DateTime, Float, JSON, Boolean, Date, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

Base = declarative_base()


class Forecast(Base):
    __tablename__ = "forecasts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    location_id = Column(String(32), index=True)
    location_name = Column(String(128))
    forecast_temp = Column(Numeric(4, 1))
    lower_band = Column(Numeric(4, 1))
    upper_band = Column(Numeric(4, 1))
    horizon_hours = Column(Integer)
    mode = Column(String(16), default='max', index=True)
    ecmwf_run = Column(DateTime, index=True)
    units = Column(String(16))
    confidence = Column(Float)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class MarketState(Base):
    __tablename__ = "market_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    forecast_id = Column(Integer, index=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    location_id = Column(String(32), index=True)
    market_date = Column(Date, index=True)       # settlement date of the market
    mode = Column(String(16), default='max', index=True)

    # Gamma outcomePrices for best lower/predicted/upper bucket
    lower_yes_price = Column(Numeric(5, 4))
    lower_no_price = Column(Numeric(5, 4))
    lower_spread = Column(Numeric(5, 4))
    lower_volume_24h = Column(Numeric(12, 2))
    lower_clob_token_no = Column(String(128))    # CLOB token ID for NO side
    lower_no_best_ask = Column(Numeric(5, 4))    # live CLOB ask for NO
    lower_no_best_bid = Column(Numeric(5, 4))    # live CLOB bid for NO
    lower_no_volume_24h = Column(Numeric(12, 2)) # 24h traded volume (NO token)

    predicted_yes_price = Column(Numeric(5, 4))
    predicted_no_price = Column(Numeric(5, 4))
    predicted_spread = Column(Numeric(5, 4))
    predicted_volume_24h = Column(Numeric(12, 2))

    upper_yes_price = Column(Numeric(5, 4))
    upper_no_price = Column(Numeric(5, 4))
    upper_spread = Column(Numeric(5, 4))
    upper_volume_24h = Column(Numeric(12, 2))
    upper_clob_token_no = Column(String(128))
    upper_no_best_ask = Column(Numeric(5, 4))
    upper_no_best_bid = Column(Numeric(5, 4))
    upper_no_volume_24h = Column(Numeric(12, 2))

    order_book_depth = Column(JSON)              # raw CLOB order book snapshot

    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class TradeSimulation(Base):
    __tablename__ = "trade_simulations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    forecast_id = Column(Integer, index=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    location_id = Column(String(32), index=True)
    market_date = Column(Date, index=True)
    mode = Column(String(16), default='max', index=True)
    clob_token_id = Column(String(128))
    question_text = Column(String(512))

    market_side = Column(String(16))  # lower / upper
    order_type = Column(String(8))    # NO
    price = Column(Numeric(5, 4))
    size = Column(Numeric(12, 2))
    simulated_pnl = Column(Numeric(12, 2))

    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class MarketBucket(Base):
    """One row per temperature bucket per scan — replaces the opaque order_book_depth JSON."""
    __tablename__ = "market_buckets"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    market_state_id = Column(Integer, index=True)      # FK to market_state.id
    location_id     = Column(String(32), index=True)
    market_date     = Column(Date, index=True)
    bucket_type     = Column(String(16))               # lower / predicted / upper
    is_best         = Column(Boolean, default=False)   # selected best for this side
    question_text   = Column(String(512))
    clob_token_yes  = Column(String(128))
    clob_token_no   = Column(String(128))
    yes_price       = Column(Numeric(5, 4))            # from Gamma outcomePrices[0]
    no_price        = Column(Numeric(5, 4))            # from Gamma outcomePrices[1]
    no_best_ask     = Column(Numeric(5, 4))            # live CLOB ask  (is_best only)
    no_best_bid     = Column(Numeric(5, 4))            # live CLOB bid  (is_best only)
    no_volume_24h   = Column(Numeric(12, 2))           # 24h volume     (is_best only)

    created_at      = Column(DateTime, default=datetime.datetime.utcnow)


class Outcome(Base):
    __tablename__ = "outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    location_id = Column(String(32), index=True)
    date = Column(DateTime, index=True)
    actual_max_temp = Column(Numeric(4, 1))
    actual_min_temp = Column(Numeric(4, 1))
    actual_precipitation = Column(Numeric(8, 3))
    source = Column(String(64))  # Hong Kong Observatory / NOAA / etc

    verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Performance(Base):
    __tablename__ = "performance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(DateTime, index=True)
    location_id = Column(String(32), index=True)

    total_trades = Column(Integer)
    winning_trades = Column(Integer)
    win_rate = Column(Numeric(5, 4))
    auc_score = Column(Numeric(5, 4))
    total_pnl = Column(Numeric(12, 2))
    sharpe_ratio = Column(Numeric(8, 4))

    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class PrecipForecast(Base):
    """Monthly precipitation forecast per location (Open-Meteo archive + forecast)."""
    __tablename__ = "precip_forecasts"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    timestamp          = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    location_id        = Column(String(32), index=True)
    location_name      = Column(String(128))
    settlement_month   = Column(Date, index=True)       # first day of the month
    accumulated        = Column(Numeric(8, 2))           # actual mm/in so far (archive)
    forecast_remaining = Column(Numeric(8, 2))           # forecast mm/in for rest of month
    total_forecast     = Column(Numeric(8, 2))           # accumulated + forecast_remaining
    p05                = Column(Numeric(8, 2))           # 5th percentile month-end total
    p95                = Column(Numeric(8, 2))           # 95th percentile month-end total
    max_prob           = Column(Integer)                  # peak daily precipitation probability (0-100)
    days_elapsed       = Column(Integer)
    days_remaining     = Column(Integer)
    units              = Column(String(8))               # 'mm' or 'inches'
    source             = Column(String(64))              # 'open-meteo'
    model_name         = Column(String(64), index=True)  # e.g. ecmwf_ec46
    model_rmse_mm      = Column(Numeric(8, 3))
    model_auc          = Column(Numeric(6, 4))
    model_eligible     = Column(Boolean, default=False)
    created_at         = Column(DateTime, default=datetime.datetime.utcnow)


class PrecipMarketState(Base):
    """Summary market-price snapshot per location per scan for monthly precipitation markets."""
    __tablename__ = "precip_market_state"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    forecast_id           = Column(Integer, index=True)   # FK → precip_forecasts.id
    timestamp             = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    location_id           = Column(String(32), index=True)
    settlement_month      = Column(Date, index=True)

    lower_yes_price       = Column(Numeric(5, 4))
    lower_no_price        = Column(Numeric(5, 4))
    lower_clob_token_no   = Column(String(128))
    lower_no_best_ask     = Column(Numeric(5, 4))
    lower_no_best_bid     = Column(Numeric(5, 4))
    lower_no_volume_24h   = Column(Numeric(12, 2))

    predicted_yes_price   = Column(Numeric(5, 4))
    predicted_no_price    = Column(Numeric(5, 4))

    upper_yes_price       = Column(Numeric(5, 4))
    upper_no_price        = Column(Numeric(5, 4))
    upper_clob_token_no   = Column(String(128))
    upper_no_best_ask     = Column(Numeric(5, 4))
    upper_no_best_bid     = Column(Numeric(5, 4))
    upper_no_volume_24h   = Column(Numeric(12, 2))

    order_book_depth      = Column(JSON)
    created_at            = Column(DateTime, default=datetime.datetime.utcnow)


class PrecipMarketBucket(Base):
    """One row per precipitation bucket per scan."""
    __tablename__ = "precip_market_buckets"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    market_state_id  = Column(Integer, index=True)       # FK → precip_market_state.id
    location_id      = Column(String(32), index=True)
    settlement_month = Column(Date, index=True)
    bucket_type      = Column(String(16))                # lower / predicted / upper
    is_best          = Column(Boolean, default=False)
    question_text    = Column(String(512))
    low_threshold    = Column(Numeric(8, 2))             # lower bound (null = "less than")
    high_threshold   = Column(Numeric(8, 2))             # upper bound (null = "or more")
    clob_token_yes   = Column(String(128))
    clob_token_no    = Column(String(128))
    yes_price        = Column(Numeric(5, 4))
    no_price         = Column(Numeric(5, 4))
    no_best_ask      = Column(Numeric(5, 4))
    no_best_bid      = Column(Numeric(5, 4))
    no_volume_24h    = Column(Numeric(12, 2))
    created_at       = Column(DateTime, default=datetime.datetime.utcnow)


class PrecipTradeSimulation(Base):
    """Simulated NO bets on precipitation tail buckets."""
    __tablename__ = "precip_trade_simulations"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    forecast_id      = Column(Integer, index=True)
    timestamp        = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    location_id      = Column(String(32), index=True)
    settlement_month = Column(Date, index=True)
    clob_token_id    = Column(String(128))
    question_text    = Column(String(512))
    market_side      = Column(String(16))                # lower / upper
    order_type       = Column(String(8))                 # NO
    price            = Column(Numeric(5, 4))
    size             = Column(Numeric(12, 2))
    simulated_pnl    = Column(Numeric(12, 2))
    confidence_pct   = Column(Integer)                   # max_prob from forecast that gated trade
    created_at       = Column(DateTime, default=datetime.datetime.utcnow)


class PrecipOutcome(Base):
    """Verified monthly precipitation actuals (for backtesting)."""
    __tablename__ = "precip_outcomes"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    location_id      = Column(String(32), index=True)
    settlement_month = Column(Date, index=True)          # first day of month
    actual_precip    = Column(Numeric(8, 2))             # mm or inches
    units            = Column(String(8))
    source           = Column(String(64))
    verified         = Column(Boolean, default=False)
    created_at       = Column(DateTime, default=datetime.datetime.utcnow)


class PrecipForecastCalibration(Base):
    """As-of forecast vs resolved monthly actuals for model validation."""
    __tablename__ = "precip_forecast_calibration"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    location_id      = Column(String(32), index=True)
    model_name       = Column(String(64), index=True)
    settlement_month = Column(Date, index=True)
    asof_timestamp   = Column(DateTime, index=True)
    days_elapsed     = Column(Integer, index=True)
    days_remaining   = Column(Integer, index=True)
    units            = Column(String(8))
    predicted_total  = Column(Numeric(10, 3))
    actual_total     = Column(Numeric(10, 3))
    error_mm         = Column(Numeric(10, 3))
    abs_error_mm     = Column(Numeric(10, 3))
    within_5mm       = Column(Boolean, default=False)
    created_at       = Column(DateTime, default=datetime.datetime.utcnow)


class PrecipModelPerformance(Base):
    """Rolling model performance summary per city/model."""
    __tablename__ = "precip_model_performance"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    location_id        = Column(String(32), index=True)
    model_name         = Column(String(64), index=True)
    window_months      = Column(Integer, default=18)
    calibration_rows   = Column(Integer, default=0)
    rmse_mm            = Column(Numeric(10, 3))
    mae_mm             = Column(Numeric(10, 3))
    within_5mm_rate    = Column(Numeric(6, 4))
    auc_like           = Column(Numeric(6, 4))
    eligible           = Column(Boolean, default=False)
    evaluated_at       = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    created_at         = Column(DateTime, default=datetime.datetime.utcnow)


class WeatherBotTsPosition(Base):
    """Open positions in the TS weather bot (live trading)."""
    __tablename__ = "weatherbot_ts_positions"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    clob_token_id      = Column(String(128), unique=True, index=True)
    question           = Column(String(512))
    location           = Column(String(64), index=True)
    market_date        = Column(Date, index=True)
    entry_price        = Column(Numeric(5, 4))
    entry_cost         = Column(Numeric(12, 2))
    shares             = Column(Numeric(16, 8))
    current_price      = Column(Numeric(5, 4))
    current_pnl        = Column(Numeric(12, 2))
    opened_at          = Column(DateTime, index=True)
    forecast_temp      = Column(Numeric(5, 2))
    kelly_pct          = Column(Numeric(6, 4))
    ev                 = Column(Numeric(6, 4))
    our_prob           = Column(Numeric(6, 4))
    updated_at         = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, index=True)
    created_at         = Column(DateTime, default=datetime.datetime.utcnow)


class WeatherBotTsTradeHistory(Base):
    """Closed trades from TS weather bot (live trading)."""
    __tablename__ = "weatherbot_ts_trade_history"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    clob_token_id      = Column(String(128), index=True)
    question           = Column(String(512))
    location           = Column(String(64), index=True)
    market_date        = Column(Date, index=True)
    entry_price        = Column(Numeric(5, 4))
    entry_cost         = Column(Numeric(12, 2))
    shares             = Column(Numeric(16, 8))
    exit_price         = Column(Numeric(5, 4))
    realized_pnl       = Column(Numeric(12, 2))
    opened_at          = Column(DateTime, index=True)
    closed_at          = Column(DateTime, index=True)
    resolved_outcome   = Column(String(64))  # YES / NO / PENDING
    kelly_pct          = Column(Numeric(6, 4))
    ev                 = Column(Numeric(6, 4))
    our_prob           = Column(Numeric(6, 4))
    forecast_temp      = Column(Numeric(5, 2))
    created_at         = Column(DateTime, default=datetime.datetime.utcnow)


class PrecipStrategySignal(Base):
    """Decision snapshot per precipitation bucket for strategy v1."""
    __tablename__ = "precip_strategy_signals"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    timestamp        = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    mode             = Column(String(16), index=True)   # scan / backtest / calibrate
    location_id      = Column(String(32), index=True)
    settlement_month = Column(Date, index=True)
    forecast_id      = Column(Integer, index=True)
    question_text    = Column(String(512))
    clob_token_no    = Column(String(128))
    bucket_low       = Column(Numeric(8, 2))
    bucket_high      = Column(Numeric(8, 2))
    market_yes       = Column(Numeric(5, 4))
    market_no        = Column(Numeric(5, 4))
    p_no             = Column(Numeric(6, 5))
    fair_no          = Column(Numeric(6, 5))
    edge_no          = Column(Numeric(6, 5))
    max_entry_no     = Column(Numeric(6, 5))
    days_remaining   = Column(Integer)
    data_source      = Column(String(128))
    blocked_reason   = Column(String(128))
    created_at       = Column(DateTime, default=datetime.datetime.utcnow)


class PrecipStrategyTrade(Base):
    """Executed/simulated strategy trades for precipitation v1."""
    __tablename__ = "precip_strategy_trades"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    timestamp        = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    mode             = Column(String(16), index=True)   # scan / backtest
    location_id      = Column(String(32), index=True)
    settlement_month = Column(Date, index=True)
    signal_id        = Column(Integer, index=True)
    question_text    = Column(String(512))
    clob_token_no    = Column(String(128))
    side             = Column(String(8))                # NO
    size_usd         = Column(Numeric(12, 2))
    limit_price      = Column(Numeric(6, 5))
    fill_price       = Column(Numeric(6, 5))
    p_no             = Column(Numeric(6, 5))
    fair_no          = Column(Numeric(6, 5))
    edge_no          = Column(Numeric(6, 5))
    days_remaining   = Column(Integer)
    data_source      = Column(String(128))
    realized_pnl     = Column(Numeric(12, 2))
    resolved_win     = Column(Boolean)
    blocked_reason   = Column(String(128))
    created_at       = Column(DateTime, default=datetime.datetime.utcnow)


def migrate_schema(engine) -> None:
    """Add columns introduced after initial deployment. Idempotent — safe to re-run."""
    additions = [
        # (table, column, postgres_type)
        ("market_state",      "market_date",          "DATE"),
        ("market_state",      "lower_clob_token_no",  "VARCHAR(128)"),
        ("market_state",      "lower_no_best_ask",    "NUMERIC(5,4)"),
        ("market_state",      "lower_no_best_bid",    "NUMERIC(5,4)"),
        ("market_state",      "lower_no_volume_24h",  "NUMERIC(12,2)"),
        ("market_state",      "upper_clob_token_no",  "VARCHAR(128)"),
        ("market_state",      "upper_no_best_ask",    "NUMERIC(5,4)"),
        ("market_state",      "upper_no_best_bid",    "NUMERIC(5,4)"),
        ("market_state",      "upper_no_volume_24h",  "NUMERIC(12,2)"),
        ("trade_simulations", "market_date",          "DATE"),
        ("trade_simulations", "clob_token_id",        "VARCHAR(128)"),
        ("trade_simulations", "question_text",        "VARCHAR(512)"),
        ("precip_forecasts",  "model_name",           "VARCHAR(64)"),
        ("precip_forecasts",  "model_rmse_mm",        "NUMERIC(8,3)"),
        ("precip_forecasts",  "model_auc",            "NUMERIC(6,4)"),
        ("precip_forecasts",  "model_eligible",       "BOOLEAN"),
        ("forecasts",         "mode",                 "VARCHAR(16) DEFAULT 'max'"),
        ("market_state",      "mode",                 "VARCHAR(16) DEFAULT 'max'"),
        ("trade_simulations", "mode",                 "VARCHAR(16) DEFAULT 'max'"),
    ]
    with engine.connect() as conn:
        for table, col, typedef in additions:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typedef}"
            ))
        conn.commit()


def init_database(auto_migrate: bool = False):
    """Initialize database connection, create tables, and apply migrations."""
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://padraighaughey@localhost:5432/gmgn_trading")

    engine = create_engine(DATABASE_URL)
    if auto_migrate:
        Base.metadata.create_all(engine)   # creates new tables (market_buckets etc.)
        migrate_schema(engine)             # adds new columns to existing tables

    Session = sessionmaker(bind=engine)
    return Session()


if __name__ == "__main__":
    print("✅ Creating database schema...")
    session = init_database(auto_migrate=True)
    print("✅ All tables created successfully")
