"""Bounded WebSocket receiver with REST catch-up before READY.

No order is sent here. Socket reception is isolated from ledger writes and REST.
Disconnect, queue overflow or invalid subscription always invalidates readiness.
"""
from __future__ import annotations
import json
import logging
import queue
import threading
import time
import uuid

from websockets.sync.client import connect

from broker.ledger import canonical
from broker.security import public_error_code
from safety import SafetyError, utc

URL='wss://openapi-ws.tossinvest.com/ws/v1'
# Never let a caller's DEBUG root logger expose handshake Authorization headers.
QUIET_LOGGER=logging.Logger('regimefolio.websocket.private',level=logging.CRITICAL+1)


class OrderStream:
    def __init__(self,ledger,client,*,synchronize,ingest=None,connector=connect,
                 queue_size=1000,monotonic=time.monotonic,sleep=time.sleep,
                 ping_seconds=20,pong_timeout=15):
        if queue_size<1 or not 0<ping_seconds<=60 or not 0<pong_timeout<=30:
            raise SafetyError('INVALID_STREAM_LIMITS')
        self.ledger,self.client,self.account=ledger,client,str(client.account)
        self.synchronize=synchronize
        self.ingest=ingest or (lambda row,source:self.ledger.observe(self.account,row,source=source))
        self.connector,self.queue_size=connector,queue_size
        self.monotonic,self.sleep=monotonic,sleep
        self.ping_seconds,self.pong_timeout=ping_seconds,pong_timeout
        self.generation=None
        self.last_error=None

    @property
    def topic(self):return 'personal:order:'+self.account

    def _claim(self):
        generation=str(uuid.uuid4());now=self.ledger.now()
        with self.ledger.transaction() as conn:
            row=conn.execute('SELECT * FROM stream_state WHERE account=?',(self.account,)).fetchone()
            if row and row['state']!='DISCONNECTED' and (now-utc(row['updated_at'])).total_seconds()<90:
                raise SafetyError('STREAM_ALREADY_RUNNING')
            conn.execute('INSERT INTO stream_state(account,state,generation,updated_at) VALUES(?,?,?,?) ON CONFLICT(account) DO UPDATE SET state=excluded.state,generation=excluded.generation,updated_at=excluded.updated_at',
                         (self.account,'CONNECTING',generation,now.isoformat()))
            conn.execute('UPDATE ledger_accounts SET halted=1,checked_generation=-1 WHERE account=?',(self.account,))
        self.generation=generation

    def _state(self,state):
        with self.ledger.transaction() as conn:
            cursor=conn.execute('UPDATE stream_state SET state=?,updated_at=? WHERE account=? AND generation=?',
                                (state,self.ledger.now().isoformat(),self.account,self.generation))
            if cursor.rowcount!=1:raise SafetyError('STREAM_LEASE_LOST')
            if state=='DISCONNECTED':
                conn.execute('UPDATE ledger_accounts SET halted=1,checked_generation=-1 WHERE account=?',(self.account,))
                conn.execute('UPDATE broker_accounts SET stopped=1,reconciled=0 WHERE account=?',(self.account,)) if conn.execute("SELECT 1 FROM sqlite_master WHERE name='broker_accounts'").fetchone() else None

    def _message(self,frame):
        if not isinstance(frame,dict):raise SafetyError('INVALID_STREAM_FRAME')
        kind=frame.get('type')
        if kind=='pong':return 'PONG'
        if kind=='error':raise SafetyError('STREAM_SERVER_ERROR')
        if kind=='subscriptions':
            if frame.get('id')!=self.generation or frame.get('rejected') or set(frame.get('subscribed',[]))!={self.topic}:
                raise SafetyError('REQUIRED_SUBSCRIPTION_REJECTED')
            return 'ACK'
        if kind!='message' or frame.get('topic')!=self.topic:
            raise SafetyError('UNEXPECTED_STREAM_TOPIC')
        data=frame.get('data')
        if not isinstance(data,dict) or str(data.get('accountSeq'))!=self.account or not isinstance(data.get('order'),dict):
            raise SafetyError('STREAM_ACCOUNT_MISMATCH')
        known={'PENDING','PARTIAL_FILL','FILL','CANCELING','CANCELED','REPLACING','REPLACED','REJECTED','CANCEL_REJECTED','REPLACE_REJECTED'}
        if data.get('event') not in known:
            # Preserve a structurally valid snapshot, but don't infer the unknown event.
            self.ingest(data['order'],'WS')
            self.ledger.issue(self.account,'UNKNOWN_STREAM_EVENT',data['order'].get('orderId'))
            raise SafetyError('UNKNOWN_STREAM_EVENT')
        self.ingest(data['order'],'WS')
        return 'ORDER'

    def session(self,stop=None):
        stop=stop or threading.Event()
        self._claim()
        self.last_error=None
        frames=queue.Queue(maxsize=self.queue_size)
        failed=threading.Event();finished=threading.Event()
        reader=None
        def receive(ws):
            try:
                while not stop.is_set() and not finished.is_set():
                    try: raw=ws.recv(timeout=.2)
                    except TimeoutError:continue
                    if not isinstance(raw,(str,bytes)) or len(raw)>1_000_000:
                        raise SafetyError('STREAM_FRAME_TOO_LARGE')
                    frame=json.loads(raw)
                    frames.put_nowait(frame)
            except Exception:
                failed.set()
                ws.close()
        try:
            token=self.client._auth()
            with self.connector(URL,additional_headers={'Authorization':'Bearer '+token},
                    open_timeout=10,close_timeout=2,ping_interval=None,max_size=1_000_000,
                    max_queue=16,proxy=None,logger=QUIET_LOGGER) as ws:
                self._state('SUBSCRIBING')
                reader=threading.Thread(target=receive,args=(ws,),daemon=True)
                reader.start()
                ws.send(canonical([{'id':self.generation},{'type':'personal:order','codes':[self.account]}]))
                last_ping=self.monotonic();waiting=None;ack=False
                subscribed_deadline=last_ping+10
                while not stop.is_set():
                    if failed.is_set():raise SafetyError('STREAM_DISCONNECTED_OR_OVERFLOW')
                    now=self.monotonic()
                    if not ack and now>subscribed_deadline:raise SafetyError('SUBSCRIPTION_TIMEOUT')
                    if waiting is not None and now-waiting>=self.pong_timeout:raise SafetyError('PONG_TIMEOUT')
                    if now-last_ping>=self.ping_seconds:
                        ws.send('PING');last_ping=now;waiting=now
                    try:frame=frames.get(timeout=.1)
                    except queue.Empty:continue
                    self._state('SYNCING')
                    kind=self._message(frame)
                    if kind=='PONG':waiting=None
                    if kind=='ACK':ack=True
                    if not ack:continue
                    # REST runs while reception continues into a bounded queue. Drain
                    # all buffered events, then reconfirm REST. Never declare READY
                    # on an incomplete scan or a perpetually growing backlog.
                    for _ in range(3):
                        if failed.is_set():raise SafetyError('STREAM_DISCONNECTED_OR_OVERFLOW')
                        ok=self.synchronize()
                        count=0
                        while not frames.empty() and count<self.queue_size:
                            queued=frames.get_nowait()
                            queued_kind=self._message(queued)
                            if queued_kind=='PONG':waiting=None
                            count+=1
                        if failed.is_set():raise SafetyError('STREAM_DISCONNECTED_OR_OVERFLOW')
                        if count==0:
                            self._state('READY' if ok else 'SYNCING')
                            break
                    else:
                        self._state('SYNCING')
                    # Heartbeat remains independent of incoming market/order volume.
                    if self.monotonic()-last_ping>=self.ping_seconds:
                        ws.send('PING');last_ping=self.monotonic();waiting=last_ping
                finished.set();ws.close()
        except Exception as exc:
            self.last_error=public_error_code(exc, 'STREAM_CONNECTION_FAILED')
        finally:
            finished.set()
            if reader:reader.join(timeout=3)
            try:self._state('DISCONNECTED')
            except SafetyError:pass # A newer lease owner must not be overwritten.
        return self.last_error

    def run(self,stop=None,max_reconnects=5):
        if not 0<=max_reconnects<=100:raise SafetyError('INVALID_RECONNECT_LIMIT')
        stop=stop or threading.Event()
        for attempt in range(max_reconnects+1):
            self.session(stop)
            if stop.is_set():return
            self.client._token=None # Expired authentication is refreshed on reconnect.
            if attempt<max_reconnects:
                if stop.wait(min(30,2**attempt)):return
