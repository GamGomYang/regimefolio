from contextlib import contextmanager
import json
import queue
import threading
import time

import pytest
from websockets.sync.server import serve
from websockets.sync.client import connect

from broker.stream import OrderStream
from safety import SafetyError
from test_execution_ledger import ledger,order,NOW


class Client:
    account='1';_token='FAKE_ONLY'
    def _auth(self):return 'FAKE_ONLY'


def wait_for(predicate,seconds=4):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        if predicate():return
        time.sleep(.01)
    raise AssertionError('condition not reached within bounded deadline')


def state(ledger):
    with ledger.transaction() as conn:
        row=conn.execute('SELECT state FROM stream_state').fetchone()
        return row[0] if row else None


def frame(row,account='1',event='PARTIAL_FILL'):
    return {'type':'message','topic':'personal:order:'+account,'data':{'event':event,'accountSeq':account,'order':row}}


@contextmanager
def ws_server(*,reject=False,pong=True):
    outgoing=queue.Queue();received=[];connections=[]
    def handler(ws):
        connections.append(ws)
        declaration=json.loads(ws.recv(timeout=3));received.append(declaration)
        ack={'type':'subscriptions','id':declaration[0]['id'],'subscribed':[] if reject else ['personal:order:1'],'rejected':[{'code':'account-not-found'}] if reject else []}
        ws.send(json.dumps(ack))
        try:
            while True:
                while not outgoing.empty():
                    item=outgoing.get_nowait()
                    if item=='CLOSE':ws.close();return
                    ws.send(json.dumps(item))
                try:message=ws.recv(timeout=.02)
                except TimeoutError:continue
                received.append(message)
                if message=='PING' and pong:ws.send('{"type":"pong"}')
        except Exception:pass
    with serve(handler,'127.0.0.1',0,ping_interval=None) as server:
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        port=server.socket.getsockname()[1]
        def connector(url,**kwargs):
            assert url=='wss://openapi-ws.tossinvest.com/ws/v1'
            assert kwargs['additional_headers']=={'Authorization':'Bearer FAKE_ONLY'}
            return connect(f'ws://127.0.0.1:{port}',**kwargs)
        yield connector,outgoing,received,connections
        for ws in connections:ws.close()
        server.shutdown();worker.join(timeout=3)


def start(ledger,connector,*,sync=lambda:True,**kwargs):
    stop=threading.Event()
    stream=OrderStream(ledger,Client(),synchronize=sync,connector=connector,ping_seconds=.2,pong_timeout=.2,**kwargs)
    thread=threading.Thread(target=stream.session,args=(stop,),daemon=True);thread.start()
    return stream,thread,stop


def finish(thread,stop):
    stop.set();thread.join(timeout=4)
    assert not thread.is_alive()


def test_real_websocket_ack_heartbeat_and_fill(ledger):
    with ws_server() as (connector,outgoing,received,_):
        stream,thread,stop=start(ledger,connector)
        try:
            wait_for(lambda:state(ledger)=='READY')
            outgoing.put(frame(order(filled='2',ws=True)))
            wait_for(lambda:ledger.balances('1')['holdings']=={'USD:XLK':'2'})
            wait_for(lambda:'PING' in received)
            assert received[0][1]=={'type':'personal:order','codes':['1']}
        finally:finish(thread,stop)
    assert state(ledger)=='DISCONNECTED'


def test_rejected_subscription_cannot_be_ready(ledger):
    with ws_server(reject=True) as (connector,*_):
        stream,thread,stop=start(ledger,connector)
        thread.join(timeout=4)
        assert not thread.is_alive()
        assert stream.last_error=='REQUIRED_SUBSCRIPTION_REJECTED'
        assert state(ledger)=='DISCONNECTED'


def test_missing_pong_disconnects(ledger):
    with ws_server(pong=False) as (connector,*_):
        stream,thread,stop=start(ledger,connector)
        thread.join(timeout=4)
        assert not thread.is_alive()
        assert stream.last_error=='PONG_TIMEOUT'


def test_other_account_event_rejected(ledger):
    with ws_server() as (connector,outgoing,*_):
        stream,thread,stop=start(ledger,connector)
        wait_for(lambda:state(ledger)=='READY')
        outgoing.put(frame(order(filled='2',ws=True),account='2'))
        thread.join(timeout=4)
        assert stream.last_error=='UNEXPECTED_STREAM_TOPIC'
        assert ledger.balances('1')['holdings']=={}


def test_unknown_event_preserves_snapshot_and_blocks(ledger):
    with ws_server() as (connector,outgoing,*_):
        stream,thread,stop=start(ledger,connector)
        wait_for(lambda:state(ledger)=='READY')
        outgoing.put(frame(order(filled='2',ws=True),event='FUTURE_EVENT'))
        thread.join(timeout=4)
        assert stream.last_error=='UNKNOWN_STREAM_EVENT'
        assert ledger.balances('1')['holdings']=={'USD:XLK':'2'}
        assert state(ledger)=='DISCONNECTED'


def test_sync_failure_never_ready(ledger):
    with ws_server() as (connector,*_):
        stream,thread,stop=start(ledger,connector,sync=lambda:False)
        try:
            wait_for(lambda:state(ledger)=='SYNCING')
            assert state(ledger)!='READY'
        finally:finish(thread,stop)


def test_disconnect_then_rest_catches_missing_fill(ledger):
    with ws_server() as (connector,outgoing,*_):
        stream,thread,stop=start(ledger,connector)
        wait_for(lambda:state(ledger)=='READY')
        outgoing.put('CLOSE');thread.join(timeout=4)
        assert state(ledger)=='DISCONNECTED'
    calls=[]
    def resync():
        calls.append(1)
        ledger.observe('1',order(filled='5'),source='REST')
        return True
    with ws_server() as (connector,*_):
        stream,thread,stop=start(ledger,connector,sync=resync)
        try:
            wait_for(lambda:state(ledger)=='READY')
            assert ledger.balances('1')['holdings']=={'USD:XLK':'5'}
            assert calls
        finally:finish(thread,stop)


def test_events_during_rest_scan_are_drained_then_rescanned(ledger):
    entered=threading.Event();release=threading.Event();calls=[]
    def resync():
        calls.append(1)
        if len(calls)==1:
            entered.set();assert release.wait(3)
        return True
    with ws_server() as (connector,outgoing,*_):
        stream,thread,stop=start(ledger,connector,sync=resync)
        assert entered.wait(3)
        outgoing.put(frame(order(filled='2',ws=True)))
        time.sleep(.05);release.set()
        try:
            wait_for(lambda:state(ledger)=='READY')
            assert ledger.balances('1')['holdings']=={'USD:XLK':'2'}
            assert len(calls)>=2
        finally:finish(thread,stop)


def test_backpressure_bounded_queue_forces_resync(ledger):
    entered=threading.Event();release=threading.Event()
    def slow():entered.set();release.wait(3);return True
    with ws_server() as (connector,outgoing,*_):
        stream,thread,stop=start(ledger,connector,sync=slow,queue_size=2)
        assert entered.wait(3)
        for _ in range(20):outgoing.put(frame(order(filled='2',ws=True)))
        time.sleep(.15);release.set();thread.join(timeout=4)
        assert not thread.is_alive()
        assert state(ledger)=='DISCONNECTED'
        assert stream.last_error=='STREAM_DISCONNECTED_OR_OVERFLOW'


def test_single_consumer_lease(ledger):
    first=OrderStream(ledger,Client(),synchronize=lambda:True)
    second=OrderStream(ledger,Client(),synchronize=lambda:True)
    first._claim()
    with pytest.raises(SafetyError,match='RUNNING'):second._claim()
    first._state('DISCONNECTED')
    second._claim()
    with pytest.raises(SafetyError,match='LEASE'):first._state('READY')


from test_execution_service import service,accepted,cash_evidence


def test_service_ws_rest_ledger_integration(service):
    intent,_=accepted(service)
    identifier=service.engine.get(intent)['broker_id']
    evidence=cash_evidence()
    with service.ledger.transaction() as conn:conn.execute("UPDATE stream_state SET state='DISCONNECTED'")
    with ws_server() as (connector,outgoing,*_):
        stop=threading.Event()
        stream=OrderStream(service.ledger,service.client,synchronize=lambda:service.reconcile(cash_evidence=evidence),
                           ingest=service.ingest,connector=connector,ping_seconds=.2,pong_timeout=.2)
        service.client._auth=lambda:'FAKE_ONLY'
        thread=threading.Thread(target=stream.session,args=(stop,),daemon=True);thread.start()
        try:
            wait_for(lambda:state(service.ledger)=='READY')
            service.enable()
            filled=order(identifier,quantity='2',filled='2',fee='1')
            service.client.rows[identifier]=filled
            service.client.holdings=lambda:{'items':[{'currency':'USD','symbol':'XLK','quantity':'2'}]}
            evidence['unsettled']['USD']='-201'
            ws_filled=json.loads(json.dumps(filled));del ws_filled['execution']['filledAt']
            outgoing.put(frame(ws_filled,event='FILL'))
            wait_for(lambda:service.engine.get(intent)['state']=='FILLED')
            wait_for(lambda:state(service.ledger)=='READY')
            assert service.ledger.balances('1')['holdings']=={'USD:XLK':'2'}
            assert service.ledger.balances('1')['unsettled']['USD']=='-201'
            assert service.ledger.ready('1')
        finally:finish(thread,stop)
