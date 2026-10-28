"""Persist validated read-only broker observations, never invent individual fills."""
from contextlib import closing
from decimal import Decimal
import hashlib
import json
import sqlite3

import pandas as pd

from safety import SafetyError, decimal, fresh, utc
from broker.toss import TossClient
from broker.security import private_database

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations(
 account TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(account,observed_at));
CREATE TABLE IF NOT EXISTS order_observations(
 account TEXT NOT NULL, order_id TEXT NOT NULL, digest TEXT NOT NULL,
 observed_at TEXT NOT NULL, status TEXT NOT NULL, filled_quantity TEXT NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(account,order_id,digest));
CREATE TABLE IF NOT EXISTS read_issues(
 id INTEGER PRIMARY KEY, account TEXT NOT NULL, code TEXT NOT NULL, observed_at TEXT NOT NULL);
"""
KNOWN = {'PENDING','PARTIAL_FILLED','PENDING_CANCEL','PENDING_REPLACE','FILLED','CANCELED','REJECTED','REPLACED','CANCEL_REJECTED','REPLACE_REJECTED'}


def safe_order(row):
    TossClient.validate_order(row)
    # Allowlist persistence: unsolicited response fields cannot carry tokens into the DB.
    keys = ('orderId','symbol','side','orderType','timeInForce','status','price','quantity','currency','orderedAt','canceledAt')
    result = {k: row.get(k) for k in keys}
    result['execution'] = {k: row['execution'].get(k) for k in ('filledQuantity','averageFilledPrice','filledAmount','commission','tax','filledAt','settlementDate')}
    return result


def sync(client, path, now=None):
    account = client.account
    if not account or account not in {str(r['accountSeq']) for r in client.accounts()}:
        raise SafetyError('ACCOUNT_NOT_AUTHORIZED')
    # Timestamp the start, not the end, so a slow multi-request snapshot cannot look fresh.
    started = utc(now)
    holdings = client.holdings()['items']
    normalized = []
    for item in holdings:
        try:
            symbol, currency = item['symbol'], item['currency']
            if not isinstance(symbol, str) or currency not in {'USD','KRW'}:
                raise ValueError
            normalized.append({'symbol': symbol, 'currency': currency,
                               'quantity': str(decimal(item['quantity'], zero=True))})
        except (KeyError, ValueError, TypeError):
            raise SafetyError('INVALID_HOLDINGS') from None
    if len({(r['symbol'],r['currency']) for r in normalized}) != len(normalized):
        raise SafetyError('DUPLICATE_HOLDINGS')
    import live_core
    prices = client.prices(live_core.TRADE_TICKERS)
    prices = [{k: row[k] for k in ('symbol','currency','timestamp','lastPrice')} for row in prices]
    buying_power = str(client.buying_power('USD'))
    sellable = {t: str(client.sellable(t)) for t in live_core.TRADE_TICKERS}
    opened = [safe_order(r) for r in client.orders('OPEN')]
    closed = [safe_order(r) for r in client.orders('CLOSED')]
    payload = {'holdings': normalized, 'prices': prices, 'buying_power': buying_power,
               'sellable': sellable, 'open_orders': opened, 'mode': 'READ_ONLY'}
    private_database(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA)
        with conn:
            for row in opened + closed:
                text = json.dumps(row, sort_keys=True)
                digest = hashlib.sha256(text.encode()).hexdigest()
                previous = conn.execute('SELECT filled_quantity FROM order_observations WHERE account=? AND order_id=? ORDER BY observed_at DESC LIMIT 1', (account,row['orderId'])).fetchone()
                if row['status'] not in KNOWN or (previous and decimal(row['execution']['filledQuantity'], zero=True) < Decimal(previous[0])):
                    conn.execute('INSERT INTO read_issues(account,code,observed_at) VALUES(?,?,?)', (account,'ORDER_REVIEW_REQUIRED',started.isoformat()))
                conn.execute('INSERT OR IGNORE INTO order_observations VALUES(?,?,?,?,?,?,?)',
                             (account,row['orderId'],digest,started.isoformat(),row['status'],row['execution']['filledQuantity'],text))
            conn.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?)', (account,started.isoformat(),json.dumps(payload,sort_keys=True)))
    return {'mode': 'READ_ONLY', 'holdings_count': len(normalized), 'open_orders': len(opened), 'closed_orders': len(closed), 'observed_at': started.isoformat()}


def preview(market_conn, path, account, budget, now=None):
    """No submission. A portfolio target requiring sales must be recomputed after fills."""
    import live_core
    now, budget = utc(now), decimal(budget)
    if live_core.validate_market(market_conn, now=now):
        raise SafetyError('MARKET_NOT_READY')
    signal = market_conn.execute('SELECT s.signal_date,s.target_weights,p.confirmed_at FROM live_signal_snapshots s JOIN signal_provenance p USING(signal_date) WHERE s.is_month_end=1 ORDER BY s.signal_date DESC LIMIT 1').fetchone()
    if not signal or (now.tz_localize(None).normalize() - pd.Timestamp(signal[0])).days > 7 or utc(signal[2]) > now:
        raise SafetyError('CONFIRMED_RECENT_SIGNAL_REQUIRED')
    if market_conn.execute('SELECT 1 FROM signal_revisions WHERE signal_date=?', (signal[0],)).fetchone():
        raise SafetyError('SIGNAL_REVIEW_REQUIRED')
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as conn:
        if conn.execute('SELECT 1 FROM read_issues WHERE account=?', (account,)).fetchone():
            raise SafetyError('BROKER_REVIEW_REQUIRED')
        observation = conn.execute('SELECT observed_at,payload FROM observations WHERE account=? ORDER BY observed_at DESC LIMIT 1', (account,)).fetchone()
    if not observation:
        raise SafetyError('ACCOUNT_SNAPSHOT_REQUIRED')
    fresh(observation[0], now, 30)
    snapshot = json.loads(observation[1])
    if snapshot['open_orders']:
        raise SafetyError('OPEN_ORDERS_REQUIRE_REVIEW')
    quotes = {r['symbol']: r for r in snapshot['prices']}
    held = {r['symbol']: decimal(r['quantity'], zero=True) for r in snapshot['holdings'] if r['currency']=='USD'}
    weights = {t: decimal(w, zero=True) for t,w in json.loads(signal[1]).items()}
    if set(weights) - set(live_core.TRADE_TICKERS) or sum(weights.values()) != 1:
        raise SafetyError('INVALID_TARGET_WEIGHTS')
    rows = []
    for ticker in live_core.TRADE_TICKERS:
        quote = quotes[ticker]
        fresh(quote['timestamp'],now,30)
        if quote['currency'] != 'USD':
            raise SafetyError('QUOTE_CURRENCY_MISMATCH')
        price = decimal(quote['lastPrice'])
        quantity = held.get(ticker,Decimal(0))
        if quantity != quantity.to_integral_value():
            raise SafetyError('FRACTIONAL_POSITION_REQUIRES_REVIEW')
        target = (budget * weights.get(ticker,Decimal(0)) / price).to_integral_value(rounding='ROUND_FLOOR')
        difference = target - quantity
        if difference:
            if difference < 0 and -difference > decimal(snapshot['sellable'].get(ticker,0),zero=True):
                raise SafetyError('INSUFFICIENT_SELLABLE_QUANTITY')
            tick = Decimal('0.01') if price >= 1 else Decimal('0.0001')
            rows.append({'symbol':ticker,'side':'BUY' if difference>0 else 'SELL',
                         'quantity':str(abs(difference)),'reference_price':str(price.quantize(tick)), 'currency':'USD'})
    buy_cost = sum((Decimal(r['quantity'])*Decimal(r['reference_price'])*Decimal('1.005') for r in rows if r['side']=='BUY'),Decimal(0))
    if buy_cost > decimal(snapshot['buying_power'],zero=True):
        raise SafetyError('INSUFFICIENT_BUYING_POWER_REPLAN_AFTER_SALES')
    return {'mode':'PREVIEW_ONLY','signal_date':signal[0], 'orders':rows, 'submitted':False}
