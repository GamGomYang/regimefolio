"""Idempotent daily catch-up job for the local live test."""

from __future__ import annotations

import argparse
import sqlite3
import traceback

import pandas as pd

import fetch_data
import live_core


def refresh_market(conn, force=False):
    last = conn.execute("SELECT max(date) FROM prices").fetchone()[0]
    start = (pd.Timestamp(last) - pd.Timedelta(days=10)).strftime("%Y-%m-%d") if last else "1998-01-01"
    prices = fetch_data.fetch_prices(fetch_data.TICKERS, start=start)
    fetch_data.upsert_prices(conn, prices)
    fred = fetch_data.fetch_fred_series(fetch_data.FRED_SERIES)
    fetch_data.upsert_fred_series(conn, fetch_data.FRED_SERIES, fred)
    conn.commit()
    return prices.index.max() if not prices.empty else pd.Timestamp(last)


def run(db_path=live_core.DEFAULT_DB, skip_fetch=False):
    conn = live_core.connect(db_path)
    live_core.initialize(conn)
    started = pd.Timestamp.now(tz="UTC").isoformat()
    run_id = conn.execute(
        "INSERT INTO live_runs(started_at,status,message) VALUES(?,?,?)", (started, "RUNNING", "")
    ).lastrowid
    conn.commit()
    try:
        if not skip_fetch:
            refresh_market(conn)
        issues = live_core.validate_market(conn)
        now = pd.Timestamp.now(tz="UTC").isoformat()
        for severity, category, message in issues:
            conn.execute(
                "INSERT INTO live_data_issues(detected_at,severity,category,message) VALUES(?,?,?,?)",
                (now, severity, category, message),
            )
        if any(i[0] == "ERROR" for i in issues):
            raise RuntimeError("; ".join(i[2] for i in issues))
        inserted = live_core.freeze_signals(conn)
        live_core.rebuild_shadow_and_benchmark(conn)
        live_core.rebuild_live_nav(conn)
        latest_price = conn.execute("SELECT max(date) FROM prices").fetchone()[0]
        latest_fred = conn.execute("SELECT max(date) FROM fred_series WHERE series_id='T5YIE'").fetchone()[0]
        conn.execute(
            "UPDATE live_runs SET finished_at=?,status='SUCCESS',latest_price_date=?,latest_fred_date=?,message=? WHERE run_id=?",
            (pd.Timestamp.now(tz="UTC").isoformat(), latest_price, latest_fred,
             f"completed; {inserted} new frozen signal rows", run_id),
        )
        conn.commit()
        live_core.backup_database(conn, db_path)
        return 0
    except Exception as exc:
        conn.execute(
            "UPDATE live_runs SET finished_at=?,status='FAILED',message=? WHERE run_id=?",
            (pd.Timestamp.now(tz="UTC").isoformat(), f"{exc}\n{traceback.format_exc()}", run_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(live_core.DEFAULT_DB))
    parser.add_argument("--skip-fetch", action="store_true", help="Use existing DB data; useful for a dry run")
    args = parser.parse_args()
    raise SystemExit(run(args.db, args.skip_fetch))
