import json
import sqlite3

import numpy as np
import pandas as pd

import daily_job
import live_core


def seed_market(db_path):
    cal = live_core.nyse_calendar()
    sessions = cal.sessions_in_range("2023-01-03", "2025-03-31").tz_localize(None)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE prices(date TEXT NOT NULL,ticker TEXT NOT NULL,close REAL NOT NULL,
                            PRIMARY KEY(date,ticker));
        CREATE TABLE fred_series(date TEXT NOT NULL,series_id TEXT NOT NULL,value REAL NOT NULL,
                                 PRIMARY KEY(date,series_id));
        """
    )
    rows = []
    for j, ticker in enumerate(live_core.TICKERS):
        values = 80 + j * 3 + np.arange(len(sessions)) * (0.04 + j * 0.002)
        rows.extend((d.strftime("%Y-%m-%d"), ticker, float(v)) for d, v in zip(sessions, values))
    conn.executemany("INSERT INTO prices VALUES(?,?,?)", rows)
    fred = 2.15 + np.arange(len(sessions)) * 0.0005
    conn.executemany(
        "INSERT INTO fred_series VALUES(?,'T5YIE',?)",
        [(d.strftime("%Y-%m-%d"), float(v)) for d, v in zip(sessions, fred)],
    )
    conn.commit()
    conn.close()
    return sessions


def test_end_to_end_live_job_is_idempotent(tmp_path):
    db = tmp_path / "blackbox.db"
    sessions = seed_market(db)
    conn = live_core.connect(db)
    live_core.initialize(conn)
    live_core.set_setting(conn, "live_start_date", "2024-10-01")
    live_core.set_setting(conn, "initial_capital", "1000")
    conn.close()

    assert daily_job.run(db, skip_fetch=True) == 0
    conn = live_core.connect(db)
    signal_count = conn.execute("SELECT count(*) FROM live_signal_snapshots").fetchone()[0]
    month_end_count = conn.execute(
        "SELECT count(*) FROM live_signal_snapshots WHERE is_month_end=1"
    ).fetchone()[0]
    portfolios = {r[0] for r in conn.execute("SELECT DISTINCT portfolio FROM live_daily_nav")}
    assert signal_count > 0
    assert month_end_count >= 5
    assert portfolios == {"shadow", "spy"}
    assert conn.execute("SELECT status FROM live_runs ORDER BY run_id DESC").fetchone()[0] == "SUCCESS"

    assert daily_job.run(db, skip_fetch=True) == 0
    assert conn.execute("SELECT count(*) FROM live_signal_snapshots").fetchone()[0] == signal_count

    signal_date, weights_json = conn.execute(
        "SELECT signal_date,target_weights FROM live_signal_snapshots WHERE is_month_end=1 ORDER BY signal_date LIMIT 1"
    ).fetchone()
    execution = live_core.next_session(signal_date)
    target = json.loads(weights_json)
    ticker = next(iter(target))
    price = conn.execute(
        "SELECT close FROM prices WHERE date=? AND ticker=?", (execution.strftime("%Y-%m-%d"), ticker)
    ).fetchone()[0]
    live_core.add_trade(conn, execution.date(), ticker, "BUY", 900 / price, price, signal_date=signal_date)
    live_core.rebuild_live_nav(conn)
    live_rows = conn.execute("SELECT count(*) FROM live_daily_nav WHERE portfolio='live'").fetchone()[0]
    assert live_rows > 0
    first_equity, first_cash = conn.execute(
        "SELECT equity,cash FROM live_daily_nav WHERE portfolio='live' ORDER BY date LIMIT 1"
    ).fetchone()
    assert abs(first_equity - 1000) < 1e-6
    assert abs(first_cash - 100) < 1e-6
    conn.close()


def test_market_validation_blocks_missing_latest_ticker(tmp_path):
    db = tmp_path / "bad.db"
    sessions = seed_market(db)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM prices WHERE date=? AND ticker='XLV'", (sessions[-1].strftime("%Y-%m-%d"),))
    conn.commit()
    conn.close()
    try:
        daily_job.run(db, skip_fetch=True)
        assert False, "job should fail on an incomplete latest day"
    except RuntimeError as exc:
        assert "Missing latest prices" in str(exc)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM live_runs ORDER BY run_id DESC").fetchone()[0] == "FAILED"
    conn.close()
