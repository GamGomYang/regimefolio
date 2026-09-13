"""Fetch daily data needed for the Inflation Compass backtest and store in SQLite."""

import sqlite3
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
    raw = yf.download(tickers, start=start, auto_adjust=True, group_by="ticker", progress=False)
    closes = pd.DataFrame({t: raw[t]["Close"] for t in tickers})
    closes.index.name = "Date"
    return closes.dropna(how="all")


def fetch_fred_series(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    df = pd.read_csv(url, na_values=".")
    df.columns = ["Date", series_id]
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date")[series_id]


def upsert_prices(conn, prices):
    long = prices.reset_index().melt(id_vars="Date", var_name="ticker", value_name="close")
    long = long.dropna(subset=["close"])
    long["date"] = long["Date"].dt.strftime("%Y-%m-%d")
    rows = list(long[["date", "ticker", "close"]].itertuples(index=False, name=None))
    conn.executemany("INSERT OR REPLACE INTO prices (date, ticker, close) VALUES (?, ?, ?)", rows)


def upsert_fred_series(conn, series_id, series):
    df = series.dropna().reset_index()
    df.columns = ["Date", "value"]
    df["date"] = df["Date"].dt.strftime("%Y-%m-%d")
    rows = [(d, series_id, v) for d, v in zip(df["date"], df["value"])]
    conn.executemany("INSERT OR REPLACE INTO fred_series (date, series_id, value) VALUES (?, ?, ?)", rows)


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


if __name__ == "__main__":
    main()
