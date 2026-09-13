"""Core persistence and portfolio accounting for the local live test."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

import backtest as strategy


ROOT = Path(__file__).parent
DEFAULT_DB = ROOT / "data" / "inflation_compass.db"
TICKERS = ["SPY", "XLE", "XLK", "XLU", "XLP", "IEF", "XLI", "XLF", "XLB", "XLV"]
TRADE_TICKERS = ["XLE", "XLK", "XLU", "XLP", "IEF"]

LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    latest_price_date TEXT,
    latest_fred_date TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS live_signal_snapshots (
    signal_date TEXT PRIMARY KEY,
    calculated_at TEXT NOT NULL,
    is_month_end INTEGER NOT NULL,
    spy_close REAL NOT NULL,
    spy_sma200 REAL NOT NULL,
    growth_on INTEGER NOT NULL,
    t5yie REAL NOT NULL,
    t5yie_60d_ago REAL NOT NULL,
    level_on INTEGER NOT NULL,
    breakeven_momentum_on INTEGER NOT NULL,
    asset_slope REAL NOT NULL,
    asset_momentum_on INTEGER NOT NULL,
    inflation_on INTEGER NOT NULL,
    target_position TEXT NOT NULL,
    target_weights TEXT NOT NULL,
    data_max_date TEXT NOT NULL,
    code_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_trades (
    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date TEXT,
    trade_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    quantity REAL NOT NULL CHECK(quantity > 0),
    price REAL NOT NULL CHECK(price > 0),
    fee REAL NOT NULL DEFAULT 0 CHECK(fee >= 0),
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_daily_nav (
    date TEXT NOT NULL,
    portfolio TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    daily_return REAL,
    positions TEXT NOT NULL,
    PRIMARY KEY(date, portfolio)
);
CREATE TABLE IF NOT EXISTS live_data_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    date TEXT,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    resolved_at TEXT
);
"""


def connect(db_path=DEFAULT_DB):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize(conn):
    conn.executescript(LIVE_SCHEMA)
    now = pd.Timestamp.now(tz="UTC").isoformat()
    defaults = {"initial_capital": "1000", "shadow_cost_bp": "10", "live_start_date": ""}
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO live_settings(key,value,updated_at) VALUES(?,?,?)", (key, value, now)
        )
    conn.commit()


def setting(conn, key, cast=str):
    row = conn.execute("SELECT value FROM live_settings WHERE key=?", (key,)).fetchone()
    return cast(row[0]) if row else None


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO live_settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (key, str(value), pd.Timestamp.now(tz="UTC").isoformat()),
    )
    conn.commit()


def load_market(conn):
    prices_long = pd.read_sql("SELECT date,ticker,close FROM prices ORDER BY date", conn, parse_dates=["date"])
    fred = pd.read_sql(
        "SELECT date,value FROM fred_series WHERE series_id='T5YIE' ORDER BY date", conn, parse_dates=["date"]
    )
    prices = prices_long.pivot(index="date", columns="ticker", values="close").sort_index()
    t5yie = fred.set_index("date")["value"].reindex(prices.index).ffill()
    return prices, t5yie


def signal_details(prices, t5yie):
    returns = prices.pct_change(fill_method=None)
    sma = prices["SPY"].rolling(200).mean()
    pos = sum(returns[t] * w for t, w in strategy.POS_BASKET.items())
    neg = sum(returns[t] * w for t, w in strategy.NEG_BASKET.items())
    valid_basket = pos.notna() & neg.notna()
    indicator = (1 + pos.where(valid_basket, 0)).cumprod().where(valid_basket) / (
        1 + neg.where(valid_basket, 0)
    ).cumprod().where(valid_basket)
    slope = strategy.rolling_slope(indicator, 60)
    prior = t5yie.shift(60)
    result = pd.DataFrame(index=prices.index)
    result["spy_close"] = prices["SPY"]
    result["spy_sma200"] = sma
    result["growth_on"] = prices["SPY"] > sma
    result["t5yie"] = t5yie
    result["t5yie_60d_ago"] = prior
    result["level_on"] = t5yie > 2
    result["breakeven_momentum_on"] = t5yie > prior
    result["asset_slope"] = slope
    result["asset_momentum_on"] = slope > 0
    result["inflation_on"] = result.level_on & (
        result.breakeven_momentum_on | result.asset_momentum_on
    )
    return result.dropna()


def nyse_calendar():
    return xcals.get_calendar("XNYS")


def is_month_end_session(date):
    cal = nyse_calendar()
    session = pd.Timestamp(date).tz_localize(None)
    if not cal.is_session(session):
        return False
    return cal.next_session(session).month != session.month


def next_session(date):
    return nyse_calendar().next_session(pd.Timestamp(date).tz_localize(None)).tz_localize(None)


def freeze_signals(conn, code_version="live-v1"):
    prices, t5yie = load_market(conn)
    details = signal_details(prices, t5yie)
    live_start = setting(conn, "live_start_date")
    cutoff = pd.Timestamp(live_start) if live_start else details.index.max()
    details = details.loc[cutoff:]
    now = pd.Timestamp.now(tz="UTC").isoformat()
    inserted = 0
    for date, row in details.iterrows():
        month_end = is_month_end_session(date)
        regime = (bool(row.growth_on), bool(row.inflation_on))
        weights = strategy.REGIME_POSITIONS[regime]
        cursor = conn.execute(
            """INSERT OR IGNORE INTO live_signal_snapshots(
            signal_date,calculated_at,is_month_end,spy_close,spy_sma200,growth_on,t5yie,t5yie_60d_ago,
            level_on,breakeven_momentum_on,asset_slope,asset_momentum_on,inflation_on,target_position,
            target_weights,data_max_date,code_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (date.strftime("%Y-%m-%d"), now, int(month_end), float(row.spy_close), float(row.spy_sma200),
             int(row.growth_on), float(row.t5yie), float(row.t5yie_60d_ago), int(row.level_on),
             int(row.breakeven_momentum_on), float(row.asset_slope), int(row.asset_momentum_on),
             int(row.inflation_on), "+".join(weights), json.dumps(weights, sort_keys=True),
             prices.index.max().strftime("%Y-%m-%d"), code_version),
        )
        inserted += cursor.rowcount
    conn.commit()
    return inserted


def add_trade(conn, trade_date, ticker, side, quantity, price, fee=0, signal_date=None, note=""):
    if ticker not in TRADE_TICKERS:
        raise ValueError(f"Unsupported live ticker: {ticker}")
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    conn.execute(
        "INSERT INTO live_trades(signal_date,trade_date,ticker,side,quantity,price,fee,note,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (signal_date, str(trade_date), ticker, side, float(quantity), float(price), float(fee), note,
         pd.Timestamp.now(tz="UTC").isoformat()),
    )
    conn.commit()


def _store_nav(conn, portfolio, nav):
    conn.execute("DELETE FROM live_daily_nav WHERE portfolio=?", (portfolio,))
    conn.executemany(
        "INSERT INTO live_daily_nav(date,portfolio,equity,cash,daily_return,positions) VALUES(?,?,?,?,?,?)",
        [(d.strftime("%Y-%m-%d"), portfolio, float(r.equity), float(r.cash),
          None if pd.isna(r.daily_return) else float(r.daily_return), r.positions)
         for d, r in nav.iterrows()],
    )


def rebuild_shadow_and_benchmark(conn):
    prices, _ = load_market(conn)
    live_start = setting(conn, "live_start_date")
    if not live_start:
        return
    signals = pd.read_sql(
        "SELECT signal_date,target_weights FROM live_signal_snapshots "
        "WHERE is_month_end=1 AND signal_date>=? ORDER BY signal_date",
        conn, params=(live_start,), parse_dates=["signal_date"]
    )
    if signals.empty:
        return
    initial = setting(conn, "initial_capital", float)
    cost = setting(conn, "shadow_cost_bp", float) / 10_000
    events = []
    for row in signals.itertuples(index=False):
        execution = next_session(row.signal_date)
        if execution in prices.index:
            events.append((execution, json.loads(row.target_weights)))
    if not events:
        return
    start = events[0][0]
    dates = prices.loc[start:].index
    cash, shares, previous = initial, {t: 0.0 for t in TRADE_TICKERS}, {}
    event_map = dict(events)
    records = []
    for date in dates:
        if date in event_map:
            value = cash + sum(shares[t] * prices.loc[date, t] for t in TRADE_TICKERS)
            target = event_map[date]
            turnover = sum(abs(target.get(t, 0) - previous.get(t, 0)) for t in set(target) | set(previous))
            value *= 1 - turnover * cost
            shares = {t: value * target.get(t, 0) / prices.loc[date, t] for t in TRADE_TICKERS}
            cash = value - sum(shares[t] * prices.loc[date, t] for t in TRADE_TICKERS)
            previous = target
        equity = cash + sum(shares[t] * prices.loc[date, t] for t in TRADE_TICKERS)
        records.append((date, equity, cash, json.dumps(shares, sort_keys=True)))
    shadow = pd.DataFrame(records, columns=["date", "equity", "cash", "positions"]).set_index("date")
    shadow["daily_return"] = shadow.equity.pct_change(fill_method=None)
    _store_nav(conn, "shadow", shadow)

    spy_shares = initial / prices.loc[start, "SPY"]
    bench = pd.DataFrame(index=dates)
    bench["equity"] = prices.loc[dates, "SPY"] * spy_shares
    bench["cash"] = 0.0
    bench["positions"] = json.dumps({"SPY": spy_shares})
    bench["daily_return"] = bench.equity.pct_change(fill_method=None)
    _store_nav(conn, "spy", bench)
    conn.commit()


def rebuild_live_nav(conn):
    prices, _ = load_market(conn)
    trades = pd.read_sql("SELECT * FROM live_trades ORDER BY trade_date,trade_id", conn, parse_dates=["trade_date"])
    if trades.empty:
        return
    initial = setting(conn, "initial_capital", float)
    start = trades.trade_date.min()
    dates = prices.loc[start:].index
    cash, shares = initial, {t: 0.0 for t in TRADE_TICKERS}
    records = []
    for date in dates:
        for trade in trades[trades.trade_date == date].itertuples(index=False):
            amount = trade.quantity * trade.price
            if trade.side == "BUY":
                if amount + trade.fee > cash + 1e-8:
                    raise ValueError(f"Insufficient cash for trade_id={trade.trade_id}")
                cash -= amount + trade.fee
                shares[trade.ticker] += trade.quantity
            else:
                if trade.quantity > shares[trade.ticker] + 1e-8:
                    raise ValueError(f"Insufficient shares for trade_id={trade.trade_id}")
                cash += amount - trade.fee
                shares[trade.ticker] -= trade.quantity
        equity = cash + sum(shares[t] * prices.loc[date, t] for t in TRADE_TICKERS)
        records.append((date, equity, cash, json.dumps(shares, sort_keys=True)))
    nav = pd.DataFrame(records, columns=["date", "equity", "cash", "positions"]).set_index("date")
    nav["daily_return"] = nav.equity.pct_change(fill_method=None)
    _store_nav(conn, "live", nav)
    conn.commit()


def validate_market(conn):
    prices, t5yie = load_market(conn)
    issues = []
    required = set(TICKERS)
    missing = required - set(prices.columns)
    if missing:
        issues.append(("ERROR", "MISSING_TICKER", f"Missing tickers: {sorted(missing)}"))
    last = prices.index.max()
    absent = [t for t in required & set(prices.columns) if pd.isna(prices.loc[last, t])]
    if absent:
        issues.append(("ERROR", "INCOMPLETE_LATEST_DAY", f"Missing latest prices: {absent}"))
    if pd.isna(t5yie.loc[last]):
        issues.append(("ERROR", "MISSING_T5YIE", f"No T5YIE at or before {last.date()}"))
    return issues


def backup_database(conn, db_path=DEFAULT_DB, keep=7):
    db_path = Path(db_path)
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{db_path.stem}_{pd.Timestamp.now().strftime('%Y-%m-%d')}.db"
    destination = sqlite3.connect(target)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    backups = sorted(backup_dir.glob(f"{db_path.stem}_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        old.unlink()
    return target
