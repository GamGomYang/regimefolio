from concurrent.futures import ProcessPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import multiprocessing
import os
import threading
from urllib.request import Request,urlopen

import pytest

from broker.execution import TradingService
from test_order_engine import prepare,NOW,QUOTE
from test_execution_ledger import order,CUTOVER
from test_execution_service import FakeToss,cash_evidence


class HttpWriteClient:
    account='1';allow_orders=True
    def __init__(self,url,stage=None):self.url,self.stage=url,stage
    def submit(self,intent):
        if self.stage=='before_send':os._exit(17)
        request=Request(self.url,json.dumps(intent).encode(),method='POST')
        with urlopen(request,timeout=5) as response:result=json.load(response)['orderId']
        if self.stage=='after_remote':os._exit(17)
        return result
    def order(self,order_id):
        with urlopen(self.url+'?id='+order_id,timeout=5) as response:return json.load(response)


def create_child(path,intent,url,stage=None):
    service=TradingService(path,HttpWriteClient(url,stage),clock=lambda:NOW,market_guard=lambda:True)
    if stage=='after_ack':
        service.ledger.bind=lambda *args,**kwargs:os._exit(17)
    return service.create(intent,QUOTE)


def make_ready(path):
    prepare(path)
    service=TradingService(path,FakeToss(),clock=lambda:NOW,market_guard=lambda:True)
    service.ledger.bootstrap('1',holdings={},settled={'USD':'10000','KRW':'0'},unsettled=[],cutover=CUTOVER,source='baseline')
    assert service.reconcile(cash_evidence=cash_evidence())
    with service.ledger.transaction() as conn:conn.execute('INSERT INTO stream_state(account,state,generation,updated_at) VALUES(?,?,?,?)',('1','READY','fixture',NOW))
    service.enable()
    intent=service.engine.plan('1','2025-03-31','XLK','BUY',2,100)
    return service,intent


@pytest.fixture
def http_orders():
    requests=[];orders={};lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            row=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with lock:
                requests.append(row['id'])
                identifier='broker-'+row['id']
                orders[identifier]=order(identifier,quantity=row['quantity'])
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'orderId':identifier}).encode())
        def do_GET(self):
            identifier=self.path.split('?id=')[-1]
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(orders[identifier]).encode())
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield 'http://127.0.0.1:'+str(server.server_port),requests,orders
    server.shutdown();server.server_close();thread.join()


def test_100_rounds_four_processes_no_duplicate_orders(tmp_path,http_orders):
    url,requests,_=http_orders
    with ProcessPoolExecutor(max_workers=4,mp_context=multiprocessing.get_context('spawn')) as pool:
        for iteration in range(100):
            path=tmp_path/f'round-{iteration}.db'
            service,intent=make_ready(path)
            futures=[pool.submit(create_child,str(path),intent,url) for _ in range(4)]
            operations={future.result(timeout=20) for future in futures}
            assert len(operations)==1
            assert requests.count(intent)==1
    assert len(requests)==100


@pytest.mark.parametrize('stage',['before_send','after_remote','after_ack'])
def test_restart_at_durable_boundaries(tmp_path,http_orders,stage):
    url,requests,orders=http_orders
    service,intent=make_ready(tmp_path/'crash.db')
    process=multiprocessing.get_context('spawn').Process(target=create_child,args=(str(service.engine.path),intent,url,stage))
    process.start();process.join(15)
    assert process.exitcode==17
    restarted=TradingService(service.engine.path,HttpWriteClient(url),clock=lambda:NOW,market_guard=lambda:True)
    with restarted.ledger.transaction() as conn:operation_id=conn.execute('SELECT id FROM execution_operations').fetchone()[0]
    if stage=='before_send':
        assert restarted.recover(operation_id)=='SUBMITTING'
        assert not requests
    elif stage=='after_remote':
        assert restarted.recover(operation_id)=='SUBMITTING'
        op=restarted.operation(operation_id)
        proof={'kind':'BROKER_RECEIPT','request_hash':op['request_hash'],'order_id':next(iter(orders)),'reference':'test receipt'}
        assert restarted.recover(operation_id,proof=proof)=='CONFIRMED'
        assert len(requests)==1
    else:
        assert restarted.recover(operation_id)=='CONFIRMED'
        assert len(requests)==1
    assert restarted.create(intent,QUOTE)==operation_id
    assert len(requests)==(0 if stage=='before_send' else 1)
