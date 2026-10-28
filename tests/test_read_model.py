from decimal import Decimal
import json
import sqlite3

import pytest

import live_core
from broker.read_model import sync, preview
from safety import SafetyError
from test_live_blackbox import seed_market
from test_toss_client import order

NOW = '2025-04-01T15:00:00Z'


class ReadBroker:
    account = '1'
    def __init__(self):
        self.closed = [order()]
        self.opened = []
        self.held = []
    def accounts(self):
        return [{'accountSeq': 1}]
    def holdings(self):
        return {'items': self.held}
    def prices(self, symbols):
        return [{'symbol': s, 'currency':'USD', 'lastPrice':'100','timestamp':NOW} for s in symbols]
    def buying_power(self, currency):
        return Decimal('10000')
    def sellable(self, symbol):
        return Decimal('10')
    def orders(self, status):
        return self.opened if status == 'OPEN' else self.closed


def test_sync_deduplicates_order_observations_and_allowlists(tmp_path):
    client = ReadBroker()
    client.closed[0]['authorization'] = 'FAKE_SECRET'
    path = tmp_path/'read.db'
    sync(client,path,now=NOW)
    sync(client,path,now='2025-04-01T15:00:01Z')
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM order_observations').fetchone()[0] == 1
        assert 'FAKE_SECRET' not in str(conn.execute('SELECT payload FROM order_observations').fetchall())
        assert conn.execute('SELECT count(*) FROM observations').fetchone()[0] == 2


def test_execution_snapshots_preserved_not_invented_as_individual_fills(tmp_path):
    client = ReadBroker()
    path = tmp_path/'read.db'
    client.closed[0]['execution'].update(filledQuantity='1',averageFilledPrice='100',filledAmount='100',filledAt=NOW)
    sync(client,path,now=NOW)
    client.closed[0]['execution'].update(filledQuantity='2',filledAmount='200')
    sync(client,path,now='2025-04-01T15:00:01Z')
    client.closed[0]['execution'].update(filledQuantity='1',filledAmount='100')
    sync(client,path,now='2025-04-01T15:00:02Z')
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM order_observations').fetchone()[0] == 2
        assert conn.execute('SELECT code FROM read_issues').fetchone()[0] == 'ORDER_REVIEW_REQUIRED'


def test_unknown_order_status_preserved_and_flagged(tmp_path):
    client = ReadBroker()
    client.closed[0]['status']='NEW_STATUS'
    path = tmp_path/'read.db'
    sync(client,path,now=NOW)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT status FROM order_observations').fetchone()[0] == 'NEW_STATUS'
        assert conn.execute('SELECT count(*) FROM read_issues').fetchone()[0] == 1


def test_account_authorization(tmp_path):
    client = ReadBroker()
    client.account='2'
    with pytest.raises(SafetyError, match='AUTHORIZED'):
        sync(client,tmp_path/'read.db',now=NOW)


def make_preview(tmp_path):
    path=tmp_path/'read.db'
    broker=ReadBroker()
    sync(broker,path,now=NOW)
    market=tmp_path/'market.db'
    seed_market(market)
    conn=live_core.connect(market)
    live_core.initialize(conn)
    live_core.freeze_signals(conn,now='2025-03-31T21:00:00Z')
    return broker,path,conn


def test_preview_never_submits(tmp_path):
    broker,path,conn=make_preview(tmp_path)
    try:
        result=preview(conn,path,'1',1000,now=NOW)
        assert result['mode']=='PREVIEW_ONLY' and not result['submitted']
        assert result['orders']
        assert sum(Decimal(r['quantity'])*Decimal(r['reference_price']) for r in result['orders']) <= 1000
    finally:
        conn.close()


@pytest.mark.parametrize('case', ['stale','open','fractional','cash','review'])
def test_preview_blocks_unsafe_state(tmp_path,case):
    broker,path,conn=make_preview(tmp_path)
    if case=='open': broker.opened=[order()]
    if case=='fractional': broker.held=[{'symbol':'XLK','currency':'USD','quantity':'0.5'}]
    if case=='cash': broker.buying_power=lambda currency: Decimal('1')
    if case=='review': broker.closed[0]['status']='UNKNOWN_NEW'
    sync(broker,path,now=NOW)
    try:
        with pytest.raises(SafetyError):
            preview(conn,path,'1',1000,now='2025-04-01T15:00:31Z' if case=='stale' else NOW)
    finally:
        conn.close()
