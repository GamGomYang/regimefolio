import json
import sqlite3

import pandas as pd
import pytest

import live_core
import daily_job
from safety import SafetyError
from test_live_blackbox import seed_market

NOW = '2025-03-31T21:00:00Z'


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'market.db'
    seed_market(path)
    conn = live_core.connect(path)
    live_core.initialize(conn)
    yield conn
    conn.close()


def codes(db, **kwargs):
    return {i[1] for i in live_core.validate_market(db, now=kwargs.pop('now', NOW), **kwargs)}


def test_stale_price_and_macro_block(db):
    assert not codes(db)
    assert 'STALE_PRICES' in codes(db, now='2025-04-02T21:00:00Z')
    db.execute("DELETE FROM fred_series WHERE date>'2024-01-01'")
    assert 'STALE_T5YIE' in codes(db)


@pytest.mark.parametrize('value', [0, -1, float('inf')])
def test_invalid_price_block(db, value):
    db.execute("UPDATE prices SET close=? WHERE date='2025-03-31' AND ticker='XLK'", (value,))
    assert 'INVALID_PRICE' in codes(db)


def test_history_gap_block(db):
    db.execute("DELETE FROM prices WHERE date='2025-03-28' AND ticker='XLK'")
    assert 'HISTORY_GAP' in codes(db)


def test_empty_market_is_structured_error(db):
    db.execute('DELETE FROM prices')
    assert codes(db) == {'EMPTY_MARKET'}


def test_fred_observation_on_nonprice_day_preserved(db):
    db.execute("INSERT INTO fred_series VALUES('2025-03-30','T5YIE',2.99)")
    db.execute("DELETE FROM fred_series WHERE date='2025-03-31'")
    assert live_core.load_market(db)[1].iloc[-1] == 2.99


@pytest.mark.parametrize('now, expected', [
    ('2025-03-31T19:59:00Z', 0), ('2025-03-31T20:14:59Z', 0), ('2025-03-31T20:15:00Z', 1)])
def test_close_boundary(db, now, expected):
    live_core.freeze_signals(db, now=now)
    assert db.execute("SELECT count(*) FROM live_signal_snapshots WHERE signal_date='2025-03-31'").fetchone()[0] == expected


def test_revision_preserves_frozen_signal(db):
    live_core.freeze_signals(db, now=NOW)
    before = db.execute('SELECT * FROM live_signal_snapshots').fetchall()
    db.execute("UPDATE prices SET close=close*1.1 WHERE date='2025-03-31' AND ticker='SPY'")
    live_core.freeze_signals(db, now=NOW)
    assert db.execute('SELECT * FROM live_signal_snapshots').fetchall() == before
    assert db.execute('SELECT count(*) FROM signal_revisions').fetchone()[0] == 1


def test_insufficient_cash_rolls_back(db):
    with pytest.raises(ValueError, match='Insufficient cash'):
        live_core.add_trade(db, '2025-03-28', 'XLK', 'BUY', 1000, 1000, now=NOW)
    assert db.execute('SELECT count(*) FROM live_trades').fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM live_daily_nav WHERE portfolio='live'").fetchone()[0] == 0


def test_failure_mid_nav_save_rolls_back(db):
    live_core.add_trade(db, '2025-03-28', 'XLK', 'BUY', 1, 100, now=NOW)
    before = db.execute('SELECT * FROM live_daily_nav').fetchall()
    db.execute("CREATE TRIGGER fail_nav BEFORE INSERT ON live_daily_nav BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        live_core.add_trade(db, '2025-03-31', 'XLK', 'BUY', 1, 100, now=NOW)
    assert db.execute('SELECT count(*) FROM live_trades').fetchone()[0] == 1
    assert db.execute('SELECT * FROM live_daily_nav').fetchall() == before


def test_manual_duplicate_and_key_conflict(db):
    assert live_core.add_trade(db, '2025-03-28', 'XLK', 'BUY', 1, 100, now=NOW, request_key='one')
    assert not live_core.add_trade(db, '2025-03-28', 'XLK', 'BUY', 1, 100, now=NOW, request_key='one')
    with pytest.raises(SafetyError, match='CONFLICT'):
        live_core.add_trade(db, '2025-03-28', 'XLK', 'BUY', 2, 100, now=NOW, request_key='one')


@pytest.mark.parametrize('field,value', [('quantity', float('nan')), ('quantity', float('inf')), ('price', 0), ('fee', -1)])
def test_invalid_manual_numbers(db, field, value):
    values = dict(quantity=1, price=100, fee=0)
    values[field] = value
    with pytest.raises(SafetyError):
        live_core.add_trade(db, '2025-03-28', 'XLK', 'BUY', **values, now=NOW)
    assert db.execute('SELECT count(*) FROM live_trades').fetchone()[0] == 0


def test_weekend_rejected(db):
    with pytest.raises(SafetyError, match='SESSION'):
        live_core.add_trade(db, '2025-03-29', 'XLK', 'BUY', 1, 100, now=NOW)


def test_korean_timestamp_normalizes_to_us_trade_day(db):
    live_core.add_trade(db, '2025-03-29T03:00:00+09:00', 'XLK', 'BUY', 1, 100, now=NOW)
    assert db.execute('SELECT trade_date FROM live_trades').fetchone()[0] == '2025-03-28'


def test_legacy_nonprice_date_accounted_and_marked(db):
    db.execute("INSERT INTO live_trades(trade_date,ticker,side,quantity,price,fee,created_at) VALUES('2025-03-29','XLK','BUY',1,100,0,?)", (NOW,))
    live_core.rebuild_live_nav(db)
    cash, positions, status = db.execute("SELECT cash,positions,valuation_status FROM live_daily_nav WHERE date='2025-03-29'").fetchone()
    assert cash == 900 and json.loads(positions)['XLK'] == 1 and status == 'CARRIED_FORWARD'


@pytest.mark.parametrize('key,value', [('initial_capital', 0), ('initial_capital', 'NaN'), ('shadow_cost_bp', 5000), ('shadow_cost_bp', -1), ('live_start_date', 'bad')])
def test_invalid_settings(db, key, value):
    with pytest.raises(ValueError):
        live_core.set_setting(db, key, value)


def test_initialization_preserves_legacy(db):
    db.execute("INSERT INTO live_trades(trade_date,ticker,side,quantity,price,created_at) VALUES('2025-03-28','XLK','BUY',1,100,?)", (NOW,))
    db.commit()
    live_core.initialize(db)
    live_core.initialize(db)
    assert db.execute('SELECT count(*) FROM live_trades').fetchone()[0] == 1


def test_early_close_and_dst_calendar():
    cal = live_core.nyse_calendar()
    assert cal.session_close('2025-11-28') == pd.Timestamp('2025-11-28T18:00:00Z')
    assert cal.session_open('2025-03-07').hour == 14
    assert cal.session_open('2025-03-10').hour == 13


def test_settings_and_nav_rollback_together(db):
    live_core.add_trade(db, '2025-03-28', 'XLK', 'BUY', 1, 100, now=NOW)
    before = db.execute('SELECT * FROM live_daily_nav').fetchall()
    with pytest.raises(ValueError, match='Insufficient cash'):
        live_core.save_live_settings(db, '2024-10-01', 1, 10, now=NOW)
    assert live_core.setting(db, 'initial_capital', float) == 1000
    assert live_core.setting(db, 'live_start_date') == ''
    assert db.execute('SELECT * FROM live_daily_nav').fetchall() == before
    assert db.execute('SELECT count(*) FROM signal_provenance').fetchone()[0] == 0


def test_ingestion_records_source_and_rejects_infinity(db):
    import fetch_data
    frame = pd.DataFrame({'XLK': [100.]}, index=pd.DatetimeIndex(['2025-03-31'], name='Date'))
    fetch_data.upsert_prices(db, frame)
    assert db.execute('SELECT source,kind,observed_date FROM market_ingestions').fetchone() == ('yahoo','adjusted_close','2025-03-31')
    frame.iloc[0,0] = float('inf')
    with pytest.raises(SafetyError):
        fetch_data.upsert_prices(db, frame)


def test_multiple_trades_same_day_are_applied_once(db):
    live_core.add_trade(db,'2025-03-28','XLK','BUY',1,100,request_key='first',now=NOW)
    live_core.add_trade(db,'2025-03-28','XLK','BUY',2,100,request_key='second',now=NOW)
    cash,positions=db.execute("SELECT cash,positions FROM live_daily_nav WHERE portfolio='live' AND date='2025-03-28'").fetchone()
    assert cash==700 and json.loads(positions)['XLK']==3


def test_entire_missing_trading_session_blocks(db):
    db.execute("DELETE FROM prices WHERE date='2025-03-28'")
    assert 'MISSING_SESSION' in codes(db)


def test_daily_backup_disables_broker_accounts(db,tmp_path):
    db.execute('CREATE TABLE broker_accounts(account TEXT, stopped INTEGER, reconciled INTEGER)')
    db.execute("INSERT INTO broker_accounts VALUES('1',0,1)")
    db.commit()
    backup=live_core.backup_database(db,tmp_path/'portfolio.db')
    with sqlite3.connect(backup) as restored:
        assert restored.execute('SELECT stopped,reconciled FROM broker_accounts').fetchone()==(1,0)
    assert db.execute('SELECT stopped,reconciled FROM broker_accounts').fetchone()==(0,1)
