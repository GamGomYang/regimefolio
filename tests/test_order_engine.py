from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
import threading
from urllib.request import Request, urlopen

import pytest

from broker.engine import Engine, Limits
from safety import SafetyError

NOW = '2025-04-01T15:00:00Z'
QUOTE = {'symbol': 'XLK', 'lastPrice': '100', 'currency': 'USD', 'timestamp': NOW}


class SimBroker:
    simulation = True
    def __init__(self):
        self.orders, self.calls = {}, 0
        self.lose_response = False
    def submit(self, row):
        self.calls += 1
        self.orders[row['id']] = 'broker-' + row['id']
        if self.lose_response:
            raise TimeoutError('sensitive raw body')
        return self.orders[row['id']]
    def find(self, intent):
        return self.orders.get(intent)
    def cancel(self, order):
        return None


def prepare(path, **kwargs):
    engine = Engine(path, clock=lambda: NOW, **kwargs)
    engine.register_account('1', {})
    with engine.transaction() as conn:
        conn.execute("INSERT INTO live_signal_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ('2025-03-31', NOW, 1, 100, 99, 1, 2, 2, 0, 0, 1, 1, 0, 'XLK', '{"XLK":1}', '2025-03-31', 'test'))
        conn.execute("INSERT INTO signal_provenance VALUES(?,?,?,?,?)", ('2025-03-31', 'hash', '2025-03-31', '2025-03-31T21:00:00Z', 'test'))
    assert engine.reconcile('1', holdings={}, buying_power='10000', sellable={}, observed_at=NOW, open_order_ids=[])
    engine.stop('1', False)
    return engine


@pytest.fixture
def engine(tmp_path):
    return prepare(tmp_path / 'engine.db')


def plan(engine, quantity=2, price=100):
    return engine.plan('1', '2025-03-31', 'XLK', 'BUY', quantity, price)


def test_normal_order_fill_duplicate_and_cash_separation(engine):
    intent, broker = plan(engine), SimBroker()
    assert engine.submit(intent, broker, QUOTE) == 'ACCEPTED'
    assert engine.positions('1') == {}  # Acceptance does not create holdings.
    assert engine.record_fill(intent, 'fill-1', 2, 100, 1, NOW) == 'STORED'
    assert engine.record_fill(intent, 'fill-1', 2, 100, 1, NOW) == 'DUPLICATE'
    assert engine.positions('1') == {'XLK': Decimal(2)}
    assert engine.get(intent)['state'] == 'FILLED'
    assert engine.reconcile('1', holdings={'XLK': 2}, buying_power=9799, sellable={'XLK': 2}, observed_at=NOW, open_order_ids=[])


def test_lost_response_no_retry_even_after_restart(engine):
    intent, broker = plan(engine), SimBroker()
    broker.lose_response = True
    assert engine.submit(intent, broker, QUOTE) == 'UNKNOWN'
    restarted = Engine(engine.path, clock=lambda: NOW)
    assert restarted.submit(intent, broker, QUOTE) == 'UNKNOWN'
    assert broker.calls == 1
    assert restarted.recover(intent, broker) == 'ACCEPTED'
    assert broker.calls == 1
    with engine.transaction() as conn:
        assert 'sensitive' not in str(conn.execute('SELECT * FROM order_events').fetchall())


def test_unknown_unfindable_stays_blocked(engine):
    intent, broker = plan(engine), SimBroker()
    with engine.transaction() as conn:
        conn.execute("UPDATE order_intents SET state='SUBMITTING' WHERE id=?", (intent,))
    assert engine.recover(intent, broker) == 'SUBMITTING'
    assert not engine.reconcile('1', holdings={}, buying_power=10000, sellable={}, observed_at=NOW, open_order_ids=[])
    with pytest.raises(SafetyError):
        engine.stop('1', False)


def test_partial_cancel_late_fill_and_reverse_terminal(engine):
    intent, broker = plan(engine, quantity=3), SimBroker()
    engine.submit(intent, broker, QUOTE)
    engine.record_fill(intent, 'f1', 1, 100, 0, NOW)
    assert engine.cancel(intent, broker) == 'CANCEL_PENDING'
    engine.record_fill(intent, 'f2', 1, 100, 0, NOW)
    assert engine.get(intent)['state'] == 'CANCEL_PENDING'
    assert engine.confirm_terminal(intent, 'CANCELED') == 'CANCELED'
    assert engine.positions('1') == {'XLK': Decimal(2)}
    assert engine.confirm_terminal(intent, 'REJECTED') == 'CANCELED'


@pytest.mark.parametrize('sequence', [('f1', 'f2', 'f1'), ('f2', 'f1', 'f2')])
def test_fill_permutations(engine, sequence):
    intent, broker = plan(engine), SimBroker()
    engine.submit(intent, broker, QUOTE)
    for fill in sequence:
        engine.record_fill(intent, fill, 1, 100, 0, NOW)
    assert engine.positions('1') == {'XLK': Decimal(2)}
    assert engine.get(intent)['state'] == 'FILLED'


def test_anomalous_actual_fill_preserved_and_account_stopped(engine):
    intent, broker = plan(engine), SimBroker()
    engine.submit(intent, broker, QUOTE)
    assert engine.record_fill(intent, 'overfill', 3, 100, 0, NOW) == 'ANOMALY'
    assert engine.positions('1') == {'XLK': Decimal(3)}
    with engine.transaction() as conn:
        assert conn.execute('SELECT stopped,reconciled FROM broker_accounts').fetchone()[:] == (1, 0)


def test_conflicting_fill_id_blocks_account(engine):
    intent, broker = plan(engine), SimBroker()
    engine.submit(intent, broker, QUOTE)
    engine.record_fill(intent, 'f1', 1, 100, 0, NOW)
    assert engine.record_fill(intent, 'f1', 2, 100, 0, NOW) == 'CONFLICT'
    assert engine.positions('1') == {'XLK': Decimal(1)}


@pytest.mark.parametrize('quote,code', [
    ({**QUOTE, 'timestamp': '2025-04-01T14:59:29Z'}, 'STALE'),
    ({**QUOTE, 'timestamp': '2025-04-01T15:00:01Z'}, 'FUTURE'),
    ({**QUOTE, 'lastPrice': 1}, 'DEVIATION'),
    ({**QUOTE, 'symbol': 'XLP'}, 'MISMATCH'),
    ({**QUOTE, 'currency': 'KRW'}, 'MISMATCH'),
    ({**QUOTE, 'lastPrice': 'NaN'}, 'NUMBER')])
def test_invalid_quotes_never_submit(engine, quote, code):
    intent, broker = plan(engine), SimBroker()
    with pytest.raises(SafetyError, match=code):
        engine.submit(intent, broker, quote)
    assert broker.calls == 0
    assert engine.get(intent)['state'] == 'PLANNED'


def test_exact_quote_age_boundary_allowed(engine):
    broker = SimBroker()
    assert engine.submit(plan(engine), broker, {**QUOTE, 'timestamp': '2025-04-01T14:59:30Z'}) == 'ACCEPTED'


def test_stop_and_mismatch_block_new_orders(engine):
    intent, broker = plan(engine), SimBroker()
    engine.stop('1')
    with pytest.raises(SafetyError, match='BLOCKED'):
        engine.submit(intent, broker, QUOTE)
    assert not engine.reconcile('1', holdings={'XLK': 1}, buying_power=10000, sellable={}, observed_at=NOW, open_order_ids=[])
    with pytest.raises(SafetyError, match='RECONCILIATION'):
        engine.stop('1', False)
    assert broker.calls == 0


def test_external_open_order_blocks_account(engine):
    assert not engine.reconcile('1', holdings={}, buying_power=10000, sellable={}, observed_at=NOW, open_order_ids=['external'])


@pytest.mark.parametrize('quantity,price', [(1.5, 100), (1, '100.001'), (1, '0.00001'), (0, 100), ('NaN', 100)])
def test_order_increment_validation(engine, quantity, price):
    with pytest.raises(SafetyError):
        plan(engine, quantity, price)


def test_intent_conflict(engine):
    first = plan(engine)
    assert plan(engine) == first
    with pytest.raises(SafetyError, match='CONFLICT'):
        plan(engine, quantity=3)


def test_buying_power_buffer(engine):
    engine.reconcile('1', holdings={}, buying_power=200, sellable={}, observed_at=NOW, open_order_ids=[])
    broker = SimBroker()
    with pytest.raises(SafetyError, match='BUYING_POWER'):
        engine.submit(plan(engine), broker, QUOTE)
    assert broker.calls == 0


def test_order_limit(engine):
    broker = SimBroker()
    with pytest.raises(SafetyError, match='ORDER_LIMIT'):
        engine.submit(plan(engine, quantity=11), broker, QUOTE)
    assert broker.calls == 0


def test_daily_limit(tmp_path):
    engine = prepare(tmp_path/'daily.db', limits=Limits(daily_usd=Decimal(100)))
    with pytest.raises(SafetyError, match='DAILY_LIMIT'):
        engine.submit(plan(engine), SimBroker(), QUOTE)


def test_signal_revision_blocks_transmission(engine):
    intent = plan(engine)
    with engine.transaction() as conn:
        conn.execute("INSERT INTO signal_revisions VALUES('2025-03-31','changed',?)", (NOW,))
    with pytest.raises(SafetyError, match='REVIEW'):
        engine.submit(intent, SimBroker(), QUOTE)


def test_live_transport_not_allowed(engine):
    with pytest.raises(SafetyError, match='DISABLED'):
        engine.submit(plan(engine), object(), QUOTE)


def test_stale_account_blocks(engine):
    intent = plan(engine)
    with engine.transaction() as conn:
        conn.execute("UPDATE broker_accounts SET snapshot_at='2025-04-01T14:59:29Z'")
    with pytest.raises(SafetyError, match='STALE'):
        engine.submit(intent, SimBroker(), QUOTE)


def test_market_closed(engine):
    intent = plan(engine)
    engine.clock = lambda: '2025-04-01T21:00:00Z'
    with pytest.raises(SafetyError, match='SESSION'):
        engine.submit(intent, SimBroker(), {**QUOTE, 'timestamp': engine.clock()})


def test_backup_restores_blocked_and_preserves_orders(engine, tmp_path):
    intent, broker = plan(engine), SimBroker()
    engine.submit(intent, broker, QUOTE)
    backup = engine.backup(tmp_path/'backup.db')
    restored = Engine(backup, clock=lambda: NOW)
    assert restored.get(intent)['broker_id'] == engine.get(intent)['broker_id']
    with restored.transaction() as conn:
        assert conn.execute('SELECT stopped,reconciled FROM broker_accounts').fetchone()[:] == (1, 0)
    assert restored.submit(intent, broker, QUOTE) == 'ACCEPTED'
    assert broker.calls == 1


class HttpSim:
    simulation = True
    def __init__(self, url, crash=False):
        self.url, self.crash = url, crash
    def submit(self, row):
        req = Request(self.url, json.dumps({'intent': row['id']}).encode(), method='POST')
        with urlopen(req, timeout=5) as response:
            result = json.load(response)['id']
        if self.crash:
            os._exit(17)
        return result
    def find(self, intent):
        with urlopen(self.url + '?intent=' + intent, timeout=5) as response:
            return json.load(response)['id']


@pytest.fixture
def http_broker():
    orders, lock = [], threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with lock:
                orders.append(payload['intent'])  # Deliberately no server-side idempotency.
                result = 'id-' + payload['intent']
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'id': result}).encode())
        def do_GET(self):
            intent = self.path.split('intent=')[-1]
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'id': 'id-' + intent if intent in orders else None}).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield 'http://127.0.0.1:' + str(server.server_port), orders
    server.shutdown(); server.server_close(); thread.join()


def child_submit(path, intent, url, crash=False):
    engine = Engine(path, clock=lambda: NOW)
    return engine.submit(intent, HttpSim(url, crash), QUOTE)


@pytest.mark.parametrize('iteration', range(3))
def test_four_processes_submit_once(engine, http_broker, iteration):
    intent = plan(engine)
    url, orders = http_broker
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context('spawn')) as pool:
        futures = [pool.submit(child_submit, str(engine.path), intent, url) for _ in range(4)]
        for future in futures:
            assert future.result(timeout=20) in {'ACCEPTED', 'SUBMITTING'}
    assert orders == [intent]


def test_process_crash_after_remote_acceptance(engine, http_broker):
    intent = plan(engine)
    url, orders = http_broker
    process = multiprocessing.get_context('spawn').Process(target=child_submit, args=(str(engine.path), intent, url, True))
    process.start(); process.join(15)
    assert process.exitcode == 17
    assert engine.get(intent)['state'] == 'SUBMITTING'
    assert engine.recover(intent, HttpSim(url)) == 'ACCEPTED'
    assert engine.submit(intent, HttpSim(url), QUOTE) == 'ACCEPTED'
    assert orders == [intent]


def test_anomaly_cannot_be_cleared_by_matching_holdings(engine):
    intent, broker = plan(engine), SimBroker()
    engine.submit(intent, broker, QUOTE)
    engine.record_fill(intent, 'over', 3, 100, 0, NOW)
    assert not engine.reconcile('1', holdings={'XLK':3}, buying_power=9700, sellable={'XLK':3}, observed_at=NOW, open_order_ids=[engine.get(intent)['broker_id']])
    with pytest.raises(SafetyError):
        engine.stop('1',False)


def test_storage_failure_prevents_remote_submission(engine):
    intent, broker=plan(engine), SimBroker()
    with engine.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_order BEFORE UPDATE ON order_intents BEGIN SELECT RAISE(ABORT,'disk unavailable'); END")
    with pytest.raises(Exception,match='disk unavailable'):
        engine.submit(intent,broker,QUOTE)
    assert broker.calls==0
    assert engine.get(intent)['state']=='PLANNED'


def test_post_acceptance_storage_failure_leaves_recoverable_state(engine):
    intent, broker=plan(engine), SimBroker()
    with engine.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_accept BEFORE UPDATE ON order_intents WHEN NEW.state='ACCEPTED' BEGIN SELECT RAISE(ABORT,'injected'); END")
    assert engine.submit(intent,broker,QUOTE)=='UNKNOWN'
    assert broker.calls==1
    with engine.transaction() as conn:
        conn.execute('DROP TRIGGER fail_accept')
    assert engine.recover(intent,broker)=='ACCEPTED'
    assert broker.calls==1


def test_pending_buy_reservation_blocks_second_intent(engine):
    with engine.transaction() as conn:
        conn.execute("UPDATE live_signal_snapshots SET target_weights=?", ('{"XLK":0.5,"XLP":0.5}',))
    engine.reconcile('1',holdings={},buying_power=300,sellable={},observed_at=NOW,open_order_ids=[])
    first=plan(engine)
    broker=SimBroker()
    engine.submit(first,broker,QUOTE)
    second=engine.plan('1','2025-03-31','XLP','BUY',2,100)
    with pytest.raises(SafetyError,match='BUYING_POWER'):
        engine.submit(second,broker,{**QUOTE,'symbol':'XLP'})
    assert broker.calls==1


def test_sellable_quantity_and_actual_sell(engine):
    with engine.transaction() as conn:
        conn.execute('UPDATE broker_accounts SET baseline=?', ('{"XLK":"3"}',))
    engine.reconcile('1',holdings={'XLK':3},buying_power=10000,sellable={'XLK':1},observed_at=NOW,open_order_ids=[])
    intent=engine.plan('1','2025-03-31','XLK','SELL',2,100)
    broker=SimBroker()
    with pytest.raises(SafetyError,match='SELLABLE'):
        engine.submit(intent,broker,QUOTE)
    engine.reconcile('1',holdings={'XLK':3},buying_power=10000,sellable={'XLK':3},observed_at=NOW,open_order_ids=[])
    engine.submit(intent,broker,QUOTE)
    engine.record_fill(intent,'sell-fill',2,100,0,NOW)
    assert engine.positions('1')=={'XLK':Decimal(1)}


def test_rejected_order_preserves_partial_fill(engine):
    intent,broker=plan(engine),SimBroker()
    engine.submit(intent,broker,QUOTE)
    engine.record_fill(intent,'part',1,100,0,NOW)
    assert engine.confirm_terminal(intent,'REJECTED') == 'REJECTED'
    assert engine.positions('1') == {'XLK':Decimal(1)}
