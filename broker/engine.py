"""Durable, simulator-only order lifecycle. Live activation is deliberately absent.

Every mutation owns a short SQLite transaction. Network I/O is outside it.
A process dying after SUBMITTING leaves an unresolved order, never a retry.
"""
from __future__ import annotations

from contextlib import contextmanager, closing, nullcontext
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import uuid

import pandas as pd

import live_core
from safety import SafetyError, decimal, fresh, utc
from broker.security import private_database

SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_schema(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO broker_schema VALUES(1);
CREATE TABLE IF NOT EXISTS broker_accounts(
 account TEXT PRIMARY KEY, stopped INTEGER NOT NULL DEFAULT 1,
 baseline TEXT NOT NULL, snapshot TEXT, snapshot_at TEXT, reconciled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS order_intents(
 id TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES broker_accounts(account),
 signal_date TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
 quantity TEXT NOT NULL, price TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency='USD'),
 state TEXT NOT NULL CHECK(state IN ('PLANNED','SUBMITTING','UNKNOWN','ACCEPTED','PARTIALLY_FILLED','FILLED','CANCEL_PENDING','CANCELED','REJECTED')),
 broker_id TEXT, created_at TEXT NOT NULL, submitted_at TEXT,
 UNIQUE(account,signal_date,symbol,side), UNIQUE(account,broker_id));
CREATE TABLE IF NOT EXISTS fills(
 account TEXT NOT NULL REFERENCES broker_accounts(account), fill_id TEXT NOT NULL,
 intent_id TEXT NOT NULL REFERENCES order_intents(id), quantity TEXT NOT NULL, price TEXT NOT NULL,
 fee TEXT NOT NULL, filled_at TEXT NOT NULL, trade_date TEXT NOT NULL,
 PRIMARY KEY(account,fill_id));
CREATE TABLE IF NOT EXISTS fill_conflicts(
 conflict_id INTEGER PRIMARY KEY, account TEXT NOT NULL, fill_id TEXT NOT NULL,
 incoming TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_events(
 event_id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES order_intents(id),
 at TEXT NOT NULL, code TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reconciliation_issues(
 issue_id INTEGER PRIMARY KEY, account TEXT NOT NULL REFERENCES broker_accounts(account),
 code TEXT NOT NULL, at TEXT NOT NULL, resolved_at TEXT);
"""
ACTIVE = {'SUBMITTING', 'UNKNOWN', 'ACCEPTED', 'PARTIALLY_FILLED', 'CANCEL_PENDING'}
TERMINAL = {'FILLED', 'CANCELED', 'REJECTED'}


@dataclass(frozen=True)
class Limits:
    order_usd: Decimal = Decimal('1000')
    daily_usd: Decimal = Decimal('2000')
    quote_age_seconds: int = 30
    account_age_seconds: int = 30
    max_price_deviation: Decimal = Decimal('0.03')
    fee_buffer: Decimal = Decimal('0.005')

    def __post_init__(self):
        for value in (self.order_usd, self.daily_usd):
            decimal(value)
        if not 0 <= self.max_price_deviation <= 1 or not 0 <= self.fee_buffer <= 1:
            raise SafetyError('INVALID_LIMITS')
        if not 0 < self.quote_age_seconds <= 300 or not 0 < self.account_age_seconds <= 300:
            raise SafetyError('INVALID_LIMITS')


class Engine:
    def __init__(self, path, *, limits=None, clock=lambda: utc()):
        self.path, self.limits, self.clock = Path(path), limits or Limits(), clock
        with closing(self.connection()) as conn:
            live_core.initialize(conn)
            conn.executescript(SCHEMA)
            conn.commit()
            if conn.execute('SELECT max(version) FROM broker_schema').fetchone()[0] != 1:
                raise SafetyError('UNSUPPORTED_SCHEMA')

    def connection(self):
        private_database(self.path)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def now(self):
        return utc(self.clock())

    def register_account(self, account, holdings, connection=None):
        if not account or not isinstance(account, str):
            raise SafetyError('INVALID_ACCOUNT')
        baseline = {t: str(decimal(q, zero=True)) for t, q in holdings.items()}
        with (nullcontext(connection) if connection is not None else self.transaction()) as conn:
            conn.execute('INSERT INTO broker_accounts(account,baseline) VALUES(?,?)', (account, json.dumps(baseline, sort_keys=True)))

    def stop(self, account, stopped=True):
        with self.transaction() as conn:
            if not stopped:
                row = conn.execute('SELECT reconciled,snapshot_at FROM broker_accounts WHERE account=?', (account,)).fetchone()
                if row is None or not row['reconciled']:
                    raise SafetyError('RECONCILIATION_REQUIRED')
                fresh(row['snapshot_at'], self.now(), self.limits.account_age_seconds)
                if conn.execute("SELECT 1 FROM order_intents WHERE account=? AND state IN ('SUBMITTING','UNKNOWN','CANCEL_PENDING')", (account,)).fetchone():
                    raise SafetyError('UNRESOLVED_ORDER')
            conn.execute('UPDATE broker_accounts SET stopped=? WHERE account=?', (int(stopped), account))

    def _positions(self, conn, account):
        row = conn.execute('SELECT baseline FROM broker_accounts WHERE account=?', (account,)).fetchone()
        if row is None:
            raise SafetyError('UNKNOWN_ACCOUNT')
        positions = {t: Decimal(q) for t, q in json.loads(row['baseline']).items()}
        for fill in conn.execute('SELECT f.quantity,o.symbol,o.side FROM fills f JOIN order_intents o ON o.id=f.intent_id WHERE f.account=?', (account,)):
            positions[fill['symbol']] = positions.get(fill['symbol'], Decimal(0)) + Decimal(fill['quantity']) * (1 if fill['side'] == 'BUY' else -1)
        return {t: q for t, q in positions.items() if q != 0}

    def positions(self, account):
        with self.transaction() as conn:
            return self._positions(conn, account)

    def reconcile(self, account, *, holdings, buying_power, sellable, observed_at, open_order_ids):
        fresh(observed_at, self.now(), self.limits.account_age_seconds)
        actual = {t: decimal(q, zero=True) for t, q in holdings.items() if decimal(q, zero=True)}
        snapshot = {'buying_power': str(decimal(buying_power, zero=True)),
                    'sellable': {t: str(decimal(q, zero=True)) for t, q in sellable.items()}}
        with self.transaction() as conn:
            expected = self._positions(conn, account)
            active = conn.execute("SELECT broker_id,state FROM order_intents WHERE account=? AND state NOT IN ('PLANNED','FILLED','CANCELED','REJECTED')", (account,)).fetchall()
            unresolved = any(r['state'] in {'SUBMITTING', 'UNKNOWN', 'CANCEL_PENDING'} for r in active)
            expected_open = {r['broker_id'] for r in active if r['broker_id']}
            hard_issue = conn.execute("SELECT 1 FROM reconciliation_issues WHERE account=? AND resolved_at IS NULL AND code!='ACCOUNT_MISMATCH'", (account,)).fetchone()
            ok = actual == expected and expected_open == set(open_order_ids) and not unresolved and not hard_issue
            conn.execute('UPDATE broker_accounts SET snapshot=?,snapshot_at=?,reconciled=? WHERE account=?',
                         (json.dumps(snapshot), utc(observed_at).isoformat(), int(ok), account))
            if ok:
                conn.execute("UPDATE reconciliation_issues SET resolved_at=? WHERE account=? AND resolved_at IS NULL AND code='ACCOUNT_MISMATCH'", (self.now().isoformat(), account))
            else:
                conn.execute('UPDATE broker_accounts SET stopped=1 WHERE account=?', (account,))
                conn.execute('INSERT INTO reconciliation_issues(account,code,at) VALUES(?,?,?)', (account, 'ACCOUNT_MISMATCH', self.now().isoformat()))
            return ok

    def plan(self, account, signal_date, symbol, side, quantity, price):
        quantity, price = decimal(quantity), decimal(price)
        if symbol not in live_core.TRADE_TICKERS or side not in {'BUY', 'SELL'}:
            raise SafetyError('UNSUPPORTED_ORDER')
        tick = Decimal('0.01') if price >= 1 else Decimal('0.0001')
        if quantity != quantity.to_integral_value() or price % tick:
            raise SafetyError('INVALID_ORDER_INCREMENT')
        now = self.now()
        with self.transaction() as conn:
            signal = conn.execute('SELECT s.target_weights,p.confirmed_at FROM live_signal_snapshots s JOIN signal_provenance p USING(signal_date) WHERE s.signal_date=? AND s.is_month_end=1', (signal_date,)).fetchone()
            if signal is None or utc(signal['confirmed_at']) > now:
                raise SafetyError('CONFIRMED_SIGNAL_REQUIRED')
            if conn.execute('SELECT 1 FROM signal_revisions WHERE signal_date=?', (signal_date,)).fetchone():
                raise SafetyError('SIGNAL_REVIEW_REQUIRED')
            if side == 'BUY' and symbol not in json.loads(signal['target_weights']):
                raise SafetyError('SIGNAL_TARGET_MISMATCH')
            latest = conn.execute('SELECT max(s.signal_date) FROM live_signal_snapshots s JOIN signal_provenance p USING(signal_date) WHERE s.is_month_end=1').fetchone()[0]
            if signal_date != latest or (now.tz_localize(None).normalize() - pd.Timestamp(signal_date)).days > 7:
                raise SafetyError('STALE_SIGNAL')
            existing = conn.execute('SELECT * FROM order_intents WHERE account=? AND signal_date=? AND symbol=? AND side=?', (account, signal_date, symbol, side)).fetchone()
            if existing:
                if Decimal(existing['quantity']) != quantity or Decimal(existing['price']) != price:
                    raise SafetyError('INTENT_CONFLICT')
                return existing['id']
            intent = str(uuid.uuid4())
            conn.execute('INSERT INTO order_intents(id,account,signal_date,symbol,side,quantity,price,currency,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (intent, account, signal_date, symbol, side, str(quantity), str(price), 'USD', 'PLANNED', now.isoformat()))
            self._event(conn, intent, 'PLANNED')
            return intent

    def get(self, intent):
        with self.transaction() as conn:
            row = conn.execute('SELECT * FROM order_intents WHERE id=?', (intent,)).fetchone()
            if row is None:
                raise SafetyError('UNKNOWN_INTENT')
            return dict(row)

    def _event(self, conn, intent, code):
        conn.execute('INSERT INTO order_events(intent_id,at,code) VALUES(?,?,?)', (intent, self.now().isoformat(), code))

    def _claim(self, intent, quote, connection=None):
        now = self.now()
        fresh(quote['timestamp'], now, self.limits.quote_age_seconds)
        quote_price = decimal(quote['lastPrice'])
        cal = live_core.nyse_calendar()
        day = now.tz_convert('America/New_York').tz_localize(None).normalize()
        if not cal.is_session(day) or not cal.session_open(day) <= now < cal.session_close(day):
            raise SafetyError('OUTSIDE_REGULAR_SESSION')
        with (nullcontext(connection) if connection is not None else self.transaction()) as conn:
            row = conn.execute('SELECT * FROM order_intents WHERE id=?', (intent,)).fetchone()
            if row is None:
                raise SafetyError('UNKNOWN_INTENT')
            if row['state'] != 'PLANNED':
                return None
            account = conn.execute('SELECT * FROM broker_accounts WHERE account=?', (row['account'],)).fetchone()
            if account['stopped'] or not account['reconciled']:
                raise SafetyError('ACCOUNT_BLOCKED')
            fresh(account['snapshot_at'], now, self.limits.account_age_seconds)
            if quote['symbol'] != row['symbol'] or quote.get('currency') != 'USD':
                raise SafetyError('QUOTE_MISMATCH')
            if conn.execute('SELECT 1 FROM signal_revisions WHERE signal_date=?', (row['signal_date'],)).fetchone():
                raise SafetyError('SIGNAL_REVIEW_REQUIRED')
            latest = conn.execute('SELECT max(s.signal_date) FROM live_signal_snapshots s JOIN signal_provenance p USING(signal_date) WHERE s.is_month_end=1').fetchone()[0]
            if latest != row['signal_date'] or (day - pd.Timestamp(row['signal_date'])).days > 7:
                raise SafetyError('STALE_SIGNAL')
            quantity, price = Decimal(row['quantity']), Decimal(row['price'])
            amount = quantity * price
            if abs(price - quote_price) / quote_price > self.limits.max_price_deviation:
                raise SafetyError('PRICE_DEVIATION')
            if amount > self.limits.order_usd:
                raise SafetyError('ORDER_LIMIT')
            pending = conn.execute("SELECT * FROM order_intents WHERE account=? AND id!=? AND state!='PLANNED'", (row['account'], intent)).fetchall()
            if any(r['state'] in {'SUBMITTING', 'UNKNOWN', 'CANCEL_PENDING'} for r in pending):
                raise SafetyError('UNRESOLVED_ORDER')
            # Conservative: all submitted intents count toward daily gross turnover, even cancellations.
            spent = sum((Decimal(r['quantity']) * Decimal(r['price']) for r in pending
                         if r['submitted_at'] and utc(r['submitted_at']).tz_convert('America/New_York').date() == day.date()), Decimal(0))
            if spent + amount > self.limits.daily_usd:
                raise SafetyError('DAILY_LIMIT')
            snapshot = json.loads(account['snapshot'])
            if row['side'] == 'BUY':
                reserved = sum((Decimal(r['quantity']) * Decimal(r['price']) * (1 + self.limits.fee_buffer)
                                for r in pending if r['side'] == 'BUY' and r['state'] in ACTIVE), Decimal(0))
                if amount * (1 + self.limits.fee_buffer) + reserved > Decimal(snapshot['buying_power']):
                    raise SafetyError('INSUFFICIENT_BUYING_POWER')
            else:
                reserved = sum((Decimal(r['quantity']) for r in pending if r['symbol'] == row['symbol'] and r['side'] == 'SELL' and r['state'] in ACTIVE), Decimal(0))
                if quantity + reserved > Decimal(snapshot['sellable'].get(row['symbol'], '0')):
                    raise SafetyError('INSUFFICIENT_SELLABLE_QUANTITY')
            conn.execute("UPDATE order_intents SET state='SUBMITTING',submitted_at=? WHERE id=?", (now.isoformat(), intent))
            self._event(conn, intent, 'SUBMITTING')
            return dict(row)

    def submit(self, intent, broker, quote):
        if getattr(broker, 'simulation', False) is not True:
            raise SafetyError('LIVE_ORDER_TRANSPORT_DISABLED')
        row = self._claim(intent, quote)
        if row is None:
            return self.get(intent)['state']
        try:
            broker_id = broker.submit(row)
            if not isinstance(broker_id, str) or not broker_id:
                raise SafetyError('INVALID_ORDER_RESPONSE')
            with self.transaction() as conn:
                conn.execute("UPDATE order_intents SET broker_id=?,state='ACCEPTED' WHERE id=? AND state='SUBMITTING'", (broker_id, intent))
                self._event(conn, intent, 'ACCEPTED')
        except Exception:
            with self.transaction() as conn:
                conn.execute("UPDATE order_intents SET state='UNKNOWN' WHERE id=? AND state='SUBMITTING'", (intent,))
                self._event(conn, intent, 'UNKNOWN')
            # No raw broker error is persisted or exposed.
        return self.get(intent)['state']

    def recover(self, intent, broker):
        if getattr(broker, 'simulation', False) is not True:
            raise SafetyError('LIVE_RECOVERY_REQUIRES_VERIFIED_ORDER_ID')
        row = self.get(intent)
        if row['state'] not in {'SUBMITTING', 'UNKNOWN'}:
            return row['state']
        # Simulator supports exact intent lookup. Do not infer a production order by price/time.
        broker_id = broker.find(row['id'])
        if not broker_id:
            return row['state']
        with self.transaction() as conn:
            conn.execute("UPDATE order_intents SET broker_id=?,state='ACCEPTED' WHERE id=? AND state IN ('SUBMITTING','UNKNOWN')", (broker_id, intent))
            self._event(conn, intent, 'RECOVERED')
        return self.get(intent)['state']

    def record_fill(self, intent, fill_id, quantity, price, fee, filled_at):
        quantity, price, fee = decimal(quantity), decimal(price), decimal(fee, zero=True)
        at = utc(filled_at)
        if at > self.now() or not isinstance(fill_id, str) or not fill_id:
            raise SafetyError('INVALID_FILL')
        with self.transaction() as conn:
            row = conn.execute('SELECT * FROM order_intents WHERE id=?', (intent,)).fetchone()
            if row is None:
                raise SafetyError('UNKNOWN_INTENT')
            values = (row['account'], fill_id, intent, str(quantity), str(price), str(fee), at.isoformat(), str(at.tz_convert('America/New_York').date()))
            previous = conn.execute('SELECT * FROM fills WHERE account=? AND fill_id=?', values[:2]).fetchone()
            if previous:
                if tuple(previous) != values:
                    conn.execute('INSERT INTO fill_conflicts(account,fill_id,incoming,at) VALUES(?,?,?,?)', (row['account'], fill_id, json.dumps(values), self.now().isoformat()))
                    conn.execute('UPDATE broker_accounts SET stopped=1,reconciled=0 WHERE account=?', (row['account'],))
                    conn.execute('INSERT INTO reconciliation_issues(account,code,at) VALUES(?,?,?)', (row['account'], 'FILL_CONFLICT', self.now().isoformat()))
                    return 'CONFLICT'
                return 'DUPLICATE'
            conn.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)', values)
            total = sum((Decimal(r[0]) for r in conn.execute('SELECT quantity FROM fills WHERE intent_id=?', (intent,))), Decimal(0))
            # Preserve the broker fact, even if it violates our local model.
            anomaly = total > Decimal(row['quantity']) or row['state'] == 'PLANNED'
            if anomaly:
                conn.execute('UPDATE broker_accounts SET stopped=1 WHERE account=?', (row['account'],))
                conn.execute('INSERT INTO reconciliation_issues(account,code,at) VALUES(?,?,?)', (row['account'], 'UNEXPECTED_FILL', self.now().isoformat()))
            elif row['state'] not in {'CANCELED', 'CANCEL_PENDING', 'REJECTED'}:
                state = 'FILLED' if total == Decimal(row['quantity']) else 'PARTIALLY_FILLED'
                conn.execute('UPDATE order_intents SET state=? WHERE id=?', (state, intent))
            elif total == Decimal(row['quantity']) and row['state'] != 'REJECTED':
                conn.execute("UPDATE order_intents SET state='FILLED' WHERE id=?", (intent,))
            conn.execute('UPDATE broker_accounts SET reconciled=0 WHERE account=?', (row['account'],))
            self._event(conn, intent, 'FILL_STORED')
            return 'ANOMALY' if anomaly else 'STORED'

    def cancel(self, intent, broker):
        if getattr(broker, 'simulation', False) is not True:
            raise SafetyError('LIVE_ORDER_TRANSPORT_DISABLED')
        with self.transaction() as conn:
            row = conn.execute('SELECT * FROM order_intents WHERE id=?', (intent,)).fetchone()
            if row is None or row['state'] not in {'ACCEPTED', 'PARTIALLY_FILLED'}:
                raise SafetyError('CANCEL_NOT_ALLOWED')
            conn.execute("UPDATE order_intents SET state='CANCEL_PENDING' WHERE id=?", (intent,))
            self._event(conn, intent, 'CANCEL_PENDING')
        try:
            broker.cancel(row['broker_id'])
        except Exception:
            return 'CANCEL_PENDING'
        # A successful cancel request is not a confirmed cancellation.
        return 'CANCEL_PENDING'

    def confirm_terminal(self, intent, state):
        if state not in {'CANCELED', 'REJECTED'}:
            raise SafetyError('INVALID_TERMINAL_STATE')
        with self.transaction() as conn:
            row = conn.execute('SELECT * FROM order_intents WHERE id=?', (intent,)).fetchone()
            if row is None:
                raise SafetyError('UNKNOWN_INTENT')
            if row['state'] in TERMINAL:
                return row['state']
            if row['state'] == 'PLANNED':
                raise SafetyError('INVALID_STATE_TRANSITION')
            conn.execute('UPDATE order_intents SET state=? WHERE id=?', (state, intent))
            conn.execute('UPDATE broker_accounts SET reconciled=0 WHERE account=?', (row['account'],))
            self._event(conn, intent, state)
            return state

    def mark_restored(self):
        with self.transaction() as conn:
            conn.execute('UPDATE broker_accounts SET stopped=1,reconciled=0')

    def backup(self, destination):
        destination = Path(destination)
        if destination.exists():
            raise SafetyError('BACKUP_TARGET_EXISTS')
        private_database(destination, exclusive=True)
        with closing(self.connection()) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise SafetyError('BACKUP_INVALID')
            target.execute('UPDATE broker_accounts SET stopped=1,reconciled=0')
            tables={r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'ledger_accounts' in tables:target.execute('UPDATE ledger_accounts SET halted=1,checked_generation=-1')
            if 'stream_state' in tables:target.execute("UPDATE stream_state SET state='DISCONNECTED',generation=NULL")
            target.commit()
        return destination
