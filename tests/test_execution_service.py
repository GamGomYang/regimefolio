from copy import deepcopy
from decimal import Decimal
import json
import sqlite3

import pytest

from broker.execution import TradingService
from broker.toss import TossClient,BrokerError
from safety import SafetyError
from test_order_engine import prepare,QUOTE,NOW
from test_execution_ledger import order,CUTOVER


class FakeToss:
    account='1'
    allow_orders=True
    def __init__(self):
        self.rows={};self.calls=[];self.lose=False;self.reject_cancel=False;self.reject_modify=False
    def accounts(self):return [{'accountSeq':1}]
    def holdings(self):return {'items':[]}
    def buying_power(self,currency):return Decimal('10000')
    def sellable(self,symbol):return Decimal('10')
    def orders(self,status):
        active={'PENDING','PARTIAL_FILLED','PENDING_CANCEL','PENDING_REPLACE'}
        return [deepcopy(r) for r in self.rows.values() if (r['status'] in active)==(status=='OPEN')]
    def order(self,order_id):return deepcopy(self.rows[order_id])
    def submit(self,intent):
        self.calls.append(('CREATE',intent['id']))
        key='created-'+intent['id']
        self.rows[key]=order(key,quantity=intent['quantity'])
        if self.lose:raise TimeoutError('FAKE_SECRET')
        return key
    def cancel(self,order_id):
        self.calls.append(('CANCEL',order_id))
        new='cancel-'+str(len(self.calls))
        original=self.rows[order_id]
        response=deepcopy(original);response['orderId']=new
        if self.reject_cancel:response['status']='CANCEL_REJECTED'
        else:original['status']='CANCELED';response['status']='CANCELED'
        self.rows[new]=response
        if self.lose:raise TimeoutError('FAKE_SECRET')
        return new
    def modify(self,order_id,price,quantity=None):
        assert quantity is None
        self.calls.append(('MODIFY',order_id))
        new='modify-'+str(len(self.calls))
        original=self.rows[order_id]
        response=deepcopy(original);response['orderId']=new
        if self.reject_modify:response['status']='REPLACE_REJECTED'
        else:original['status']='REPLACED';response['status']='PENDING';response['price']=str(price)
        self.rows[new]=response
        if self.lose:raise TimeoutError('FAKE_SECRET')
        return new


def cash_evidence():
    return {'account':'1','basis':'SETTLED_AND_UNSETTLED','settled':{'USD':'10000','KRW':'0'},'unsettled':{'USD':'0','KRW':'0'},'source':'independent statement','observed_at':NOW}


@pytest.fixture
def service(tmp_path):
    path=tmp_path/'exec.db';prepare(path)
    service=TradingService(path,FakeToss(),clock=lambda:NOW,market_guard=lambda:True,replacement_semantics='CARRY_FORWARD',replacement_evidence='verified fake protocol')
    service.ledger.bootstrap('1',holdings={},settled={'USD':'10000','KRW':'0'},unsettled=[],cutover=CUTOVER,source='opening statement')
    assert service.reconcile(cash_evidence=cash_evidence())
    with service.ledger.transaction() as conn:
        conn.execute('INSERT INTO stream_state(account,state,generation,updated_at) VALUES(?,?,?,?)',('1','READY','test',NOW))
    service.enable()
    return service


def planned(service):return service.engine.plan('1','2025-03-31','XLK','BUY',2,100)


def accepted(service):
    intent=planned(service);op=service.create(intent,QUOTE)
    assert service.operation(op)['state']=='ACKNOWLEDGED'
    assert service.recover(op)=='CONFIRMED'
    assert service.reconcile(cash_evidence=cash_evidence())
    return intent,op


def test_create_ack_is_not_fill_and_repeated_call_not_resent(service):
    intent,op=accepted(service)
    assert service.ledger.balances('1')['holdings']=={}
    assert service.create(intent,QUOTE)==op
    assert len(service.client.calls)==1


def test_unknown_create_never_automatically_retries(service):
    intent=planned(service);service.client.lose=True
    op=service.create(intent,QUOTE)
    assert service.operation(op)['state']=='UNKNOWN'
    assert service.recover(op)=='UNKNOWN'
    assert service.create(intent,QUOTE)==op
    assert len(service.client.calls)==1
    with pytest.raises(SafetyError):service.enable()
    proof={'kind':'BROKER_RECEIPT','request_hash':service.operation(op)['request_hash'],
           'reference':'confirmed broker receipt','order_id':next(iter(service.client.rows))}
    assert service.recover(op,proof=proof)=='CONFIRMED'
    assert len(service.client.calls)==1


def test_recovery_rejects_unverified_receipt(service):
    service.client.lose=True;op=service.create(planned(service),QUOTE)
    with pytest.raises(SafetyError,match='RECEIPT'):service.recover(op,proof={'order_id':'x'})


def test_cancel_operation_id_and_original_preserved(service):
    intent,_=accepted(service);original=service.engine.get(intent)['broker_id']
    op=service.cancel(intent)
    operation=service.operation(op)
    assert operation['predecessor']==original and operation['response_id']!=original
    assert operation['state']=='ACKNOWLEDGED'
    assert service.recover(op)=='CONFIRMED'
    assert service.engine.get(intent)['state']=='CANCELED'
    assert service.ledger.balances('1')['holdings']=={}


def test_cancel_rejection_restores_original(service):
    intent,_=accepted(service);service.client.reject_cancel=True
    op=service.cancel(intent)
    assert service.recover(op)=='REJECTED'
    assert service.engine.get(intent)['state']=='ACCEPTED'


def test_cancel_unknown_does_not_retry(service):
    intent,_=accepted(service);service.client.lose=True
    op=service.cancel(intent)
    assert service.operation(op)['state']=='UNKNOWN'
    assert service.cancel(intent)==op
    assert len(service.client.calls)==2


def test_price_modification_links_new_order(service):
    intent,_=accepted(service);original=service.engine.get(intent)['broker_id']
    op=service.modify(intent,'101',QUOTE)
    assert service.recover(op)=='CONFIRMED'
    new=service.engine.get(intent)['broker_id']
    assert new!=original
    with service.ledger.transaction() as conn:
        assert conn.execute('SELECT count(*) FROM execution_links').fetchone()[0]==2
    assert service.ledger.balances('1')['holdings']=={}


def test_replace_rejected_keeps_original_order(service):
    intent,_=accepted(service);original=service.engine.get(intent)['broker_id']
    service.client.reject_modify=True
    op=service.modify(intent,'101',QUOTE)
    assert service.recover(op)=='REJECTED'
    assert service.engine.get(intent)['broker_id']==original
    assert service.engine.get(intent)['price']=='100'


def test_unknown_replace_blocks_new_replacement(service):
    intent,_=accepted(service);service.client.lose=True
    op=service.modify(intent,'101',QUOTE)
    assert service.operation(op)['state']=='UNKNOWN'
    with pytest.raises(SafetyError):service.modify(intent,'102',QUOTE)
    assert len(service.client.calls)==2


def test_us_quantity_modify_and_unverified_contract_block(service):
    intent,_=accepted(service)
    with pytest.raises(SafetyError,match='QUANTITY'):service.modify(intent,101,QUOTE,quantity=2)
    service.replacement_evidence=None
    with pytest.raises(SafetyError,match='CONTRACT'):service.modify(intent,101,QUOTE)
    assert len(service.client.calls)==1


def test_stream_and_ledger_gates_block_create(service):
    intent=planned(service)
    with service.ledger.transaction() as conn:conn.execute("UPDATE stream_state SET state='SYNCING'")
    with pytest.raises(SafetyError,match='STREAM'):service.create(intent,QUOTE)
    assert not service.client.calls


def test_atomic_claim_and_operation_creation(service):
    intent=planned(service)
    with service.ledger.transaction() as conn:
        conn.execute("CREATE TRIGGER op_fail BEFORE INSERT ON execution_operations BEGIN SELECT RAISE(ABORT,'disk'); END")
    with pytest.raises(sqlite3.IntegrityError):service.create(intent,QUOTE)
    assert service.engine.get(intent)['state']=='PLANNED' and not service.client.calls


def test_write_adapter_payloads_and_single_attempt():
    calls=[]
    def send(method,path,headers,body):
        calls.append((method,path,json.loads(body) if body and path!='/oauth2/token' else None))
        if path=='/oauth2/token':return 200,{},json.dumps({'access_token':'FAKE','token_type':'Bearer','expires_in':3600}).encode()
        payload=json.loads(body)
        return 200,{},json.dumps({'result':{'orderId':'new-id','clientOrderId':payload.get('clientOrderId')}}).encode()
    client=TossClient('fake','FAKE_SECRET','1',allow_orders=True,send=send)
    row={'account':'1','id':'intent-key','symbol':'XLK','side':'BUY','quantity':'2','price':'100','currency':'USD'}
    assert client.submit(row)=='new-id'
    assert client.modify('parent',101)=='new-id'
    assert client.cancel('parent')=='new-id'
    assert calls[1][2]['clientOrderId']=='intent-key'
    assert calls[2][2]=={'orderType':'LIMIT','price':'101'}
    assert calls[3][2]=={}
    with pytest.raises(BrokerError,match='QUANTITY'):client.modify('parent',101,quantity=2)
    def lost(*args):raise TimeoutError('FAKE_SECRET')
    client._send=lost
    with pytest.raises(BrokerError,match='NETWORK'):client.submit(row)


@pytest.mark.parametrize('status',[401,409,422,429,500,503])
def test_write_errors_never_retry(status):
    calls=[]
    def send(method,path,headers,body):
        calls.append(path)
        if path=='/oauth2/token':return 200,{},json.dumps({'access_token':'FAKE','token_type':'Bearer','expires_in':3600}).encode()
        return status,{},b'FAKE_SECRET'
    client=TossClient('fake','FAKE_SECRET','1',allow_orders=True,send=send,sleep=lambda _:pytest.fail('POST retried'))
    with pytest.raises(BrokerError) as exc:client.cancel('parent')
    assert 'FAKE_SECRET' not in str(exc.value) and len(calls)==2


def test_actual_market_provenance_mismatch_blocks_before_post(service,tmp_path):
    import live_core
    from test_live_blackbox import seed_market
    market=tmp_path/'market.db';seed_market(market)
    with sqlite3.connect(market) as conn:
        live_core.initialize(conn)
        live_core.freeze_signals(conn,now='2025-03-31T21:00:00Z')
    service.market_db=market;service.market_guard=None
    with pytest.raises(SafetyError,match='PROVENANCE'):
        service.create(planned(service),QUOTE)
    assert not service.client.calls


def test_daily_snapshot_does_not_invalidate_monthly_modify(service):
    intent,_=accepted(service)
    with service.ledger.transaction() as conn:
        conn.execute("INSERT INTO signal_provenance VALUES('2025-04-01','daily','2025-04-01',?,'test')",(NOW,))
    operation=service.modify(intent,'101',QUOTE)
    assert service.operation(operation)['state']=='ACKNOWLEDGED'


def test_replacement_chain_overfill_preserved_and_blocked(service):
    intent,_=accepted(service);original=service.engine.get(intent)['broker_id']
    service.client.rows[original]=order(original,quantity='2',filled='1')
    service.client.holdings=lambda:{'items':[{'currency':'USD','symbol':'XLK','quantity':'1'}]}
    cash=cash_evidence();cash['unsettled']['USD']='-100'
    assert service.reconcile(cash_evidence=cash)
    service.replacement_semantics='INDEPENDENT'
    operation=service.modify(intent,101,QUOTE)
    replacement=service.operation(operation)['response_id']
    service.client.rows[replacement]=order(replacement,quantity='2',filled='2')
    service.recover(operation)
    assert service.ledger.balances('1')['holdings']=={'USD:XLK':'3'}
    with service.ledger.transaction() as conn:
        assert conn.execute("SELECT code FROM ledger_issues WHERE code='INTENT_OVERFILL'").fetchone()
    with pytest.raises(SafetyError):service.enable()
