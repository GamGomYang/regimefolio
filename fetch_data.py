"""Fetch daily data needed for the Inflation Compass backtest and store in SQLite."""

import sqlite3
import io
from urllib.request import urlopen

import numpy as np
from safety import SafetyError
from pathlib import Path

import pandas as pd
import yfinance as yf

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "inflation_compass.db"

TICKERS = ["SPY", "XLE", "XLK", "XLU", "XLP", "IEF", "XLI", "XLF", "XLB", "XLV"]
FRED_SERIES = "T5YIE"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (date, ticker)
);
CREATE TABLE IF NOT EXISTS fred_series (
    date TEXT NOT NULL,
    series_id TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (date, series_id)
);
"""


def fetch_prices(tickers, start="1998-01-01"):
    raw = yf.download(tickers, start=start, auto_adjust=True, group_by="ticker", progress=False, timeout=15)
    closes = pd.DataFrame({t: raw[t]["Close"] for t in tickers})
    closes.index.name = "Date"
    return closes.dropna(how="all")


def fetch_fred_series(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    with urlopen(url, timeout=15) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise SafetyError("FRED_RESPONSE_TOO_LARGE")
    df = pd.read_csv(io.BytesIO(raw), na_values=".")
    df.columns = ["Date", series_id]
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date")[series_id]


def upsert_prices(conn, prices):
    if prices.empty or not prices.index.is_unique or not prices.columns.is_unique:
        raise SafetyError("INVALID_PRICE_BATCH")
    values = prices.to_numpy(dtype=float)
    if np.isinf(values).any() or (values <= 0).any():
        raise SafetyError("INVALID_PRICE_BATCH")
    long = prices.reset_index().melt(id_vars="Date", var_name="ticker", value_name="close")
    long = long.dropna(subset=["close"])
    long["date"] = long["Date"].dt.strftime("%Y-%m-%d")
    rows = list(long[["date", "ticker", "close"]].itertuples(index=False, name=None))
    conn.executemany("INSERT OR REPLACE INTO prices (date, ticker, close) VALUES (?, ?, ?)", rows)
    record_ingestion(conn, "yahoo", "adjusted_close", prices.index.max())


def upsert_fred_series(conn, series_id, series):
    if series.dropna().empty or not series.index.is_unique or np.isinf(series.to_numpy(dtype=float)).any():
        raise SafetyError("INVALID_FRED_BATCH")
    df = series.dropna().reset_index()
    df.columns = ["Date", "value"]
    df["date"] = df["Date"].dt.strftime("%Y-%m-%d")
    rows = [(d, series_id, v) for d, v in zip(df["date"], df["value"])]
    conn.executemany("INSERT OR REPLACE INTO fred_series (date, series_id, value) VALUES (?, ?, ?)", rows)
    record_ingestion(conn, "fred", series_id, series.dropna().index.max())


def main():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    prices = fetch_prices(TICKERS)
    upsert_prices(conn, prices)
    print(f"prices: {prices.index.min().date()} ~ {prices.index.max().date()}")
    for t in TICKERS:
        first_valid = prices[t].first_valid_index()
        print(f"  {t}: from {first_valid.date() if first_valid is not None else 'N/A'}")

    t5yie = fetch_fred_series(FRED_SERIES)
    upsert_fred_series(conn, FRED_SERIES, t5yie)
    print(f"{FRED_SERIES}: {t5yie.dropna().index.min().date()} ~ {t5yie.dropna().index.max().date()}")

    conn.commit()
    conn.close()
    print(f"saved to {DB_PATH}")


def record_ingestion(conn, source, kind, observed):
    conn.execute("""CREATE TABLE IF NOT EXISTS market_ingestions(
        id INTEGER PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
        observed_date TEXT NOT NULL, fetched_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO market_ingestions(source,kind,observed_date,fetched_at) VALUES(?,?,?,?)",
                 (source, kind, pd.Timestamp(observed).date().isoformat(), pd.Timestamp.now(tz="UTC").isoformat()))


if __name__ == "__main__":
    main()
