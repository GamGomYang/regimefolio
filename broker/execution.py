"""Durable create/cancel/replace coordinator; no automatic retry of writes.

Use a dedicated execution DB, not the manual portfolio database. API writes remain
opt-in; reconciliation, stream readiness, account allowlisting and risk checks are
mandatory even when the low-level client is enabled.
"""
from contextlib import closing
from decimal import Decimal
import hashlib
import json
import sqlite3
import uuid

from broker.engine import Engine, ACTIVE
from broker.ledger import Ledger, canonical, amounts, TERMINAL
from safety import SafetyError, decimal, utc, fresh
import live_core

SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_operations(
 id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES order_intents(id),
 kind TEXT NOT NULL CHECK(kind IN ('CREATE','CANCEL','MODIFY')),
 state TEXT NOT NULL CHECK(state IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED','CONFIRMED','REJECTED')),
 predecessor TEXT, response_id TEXT, request TEXT NOT NULL, request_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, evidence TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_operation ON execution_operations(intent_id)
 WHERE state IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED');
CREATE UNIQUE INDEX IF NOT EXISTS one_create_per_intent ON execution_operations(intent_id) WHERE kind='CREATE';
CREATE TABLE IF NOT EXISTS execution_links(
 account TEXT NOT NULL, order_id TEXT NOT NULL, intent_id TEXT NOT NULL,
 operation_id TEXT NOT NULL REFERENCES execution_operations(id), role TEXT NOT NULL,
 PRIMARY KEY(account,order_id));
"""


class TradingService:
    def __init__(self,path,client,*,clock=lambda:utc(),limits=None,market_db=None,market_guard=None,replacement_semantics=None,replacement_evidence=None):
        self.engine=Engine(path,clock=clock,limits=limits)
        self.ledger=Ledger(path,clock=clock)
        self.client=client
        self.account=str(client.account)
        self.market_db=market_db
        self.market_guard=market_guard
        self.replacement_semantics=replacement_semantics
        self.replacement_evidence=replacement_evidence
        with closing(self.ledger.connect()) as conn: conn.executescript(SCHEMA)

    def _market_ready(self):
        if self.market_guard is not None:
            if self.market_guard() is not True:raise SafetyError('MARKET_NOT_READY')
            return
        if self.market_db is None:raise SafetyError('MARKET_DATABASE_REQUIRED')
        with closing(sqlite3.connect(f'file:{self.market_db}?mode=ro',uri=True)) as conn:
            if live_core.validate_market(conn,now=self.ledger.now()):raise SafetyError('MARKET_NOT_READY')
            sql='SELECT s.signal_date,p.input_hash,s.target_weights,s.code_version FROM live_signal_snapshots s JOIN signal_provenance p USING(signal_date) WHERE s.is_month_end=1 ORDER BY s.signal_date DESC LIMIT 1'
            current=conn.execute(sql).fetchone()
            if not current or conn.execute('SELECT 1 FROM signal_revisions WHERE signal_date=?',(current[0],)).fetchone():raise SafetyError('SIGNAL_REVIEW_REQUIRED')
            with closing(self.ledger.connect()) as execution:
                copied=execution.execute(sql).fetchone()
                if not copied or tuple(copied)!=tuple(current):raise SafetyError('SIGNAL_PROVENANCE_MISMATCH')

    def _enabled(self):
        if getattr(self.client,'allow_orders',False) is not True:
            raise SafetyError('LIVE_ORDER_TRANSPORT_DISABLED')
        if not self.account or self.client.account!=self.account:
            raise SafetyError('ACCOUNT_MISMATCH')

    def operation(self,operation_id):
        with self.ledger.transaction() as conn:
            row=conn.execute('SELECT * FROM execution_operations WHERE id=?',(operation_id,)).fetchone()
            if not row: raise SafetyError('UNKNOWN_OPERATION')
            return dict(row)

    def _ready(self,conn):
        self.ledger.ready(self.account,conn)
        account=conn.execute('SELECT * FROM ledger_accounts WHERE account=?',(self.account,)).fetchone()
        if account['halted']: raise SafetyError('ACCOUNT_HALTED')
        stream=conn.execute('SELECT * FROM stream_state WHERE account=?',(self.account,)).fetchone()
        if not stream or stream['state']!='READY': raise SafetyError('STREAM_NOT_READY')
        fresh(stream['updated_at'],self.ledger.now(),30)
        if conn.execute("SELECT 1 FROM execution_operations x JOIN order_intents o ON o.id=x.intent_id WHERE o.account=? AND x.state IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED')",(self.account,)).fetchone():
            raise SafetyError('UNRESOLVED_OPERATION')

    def enable(self):
        """Explicit local release after read-only verification. Doesn't send an order."""
        with self.ledger.transaction() as conn:
            self.ledger.ready(self.account,conn)
            stream=conn.execute('SELECT state,updated_at FROM stream_state WHERE account=?',(self.account,)).fetchone()
            if not stream or stream['state']!='READY':raise SafetyError('STREAM_NOT_READY')
            fresh(stream['updated_at'],self.ledger.now(),30)
            if conn.execute("SELECT 1 FROM execution_operations x JOIN order_intents o ON o.id=x.intent_id WHERE o.account=? AND x.state IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED')",(self.account,)).fetchone():raise SafetyError('UNRESOLVED_OPERATION')
            conn.execute('UPDATE ledger_accounts SET halted=0 WHERE account=?',(self.account,))
            conn.execute('UPDATE broker_accounts SET stopped=0 WHERE account=?',(self.account,))

    def _new_operation(self,conn,intent,kind,request,predecessor=None):
        operation_id=str(uuid.uuid4())
        text=canonical(request)
        conn.execute('INSERT INTO execution_operations VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (operation_id,intent,kind,'SUBMITTING',predecessor,None,text,hashlib.sha256(text.encode()).hexdigest(),self.ledger.now().isoformat(),None))
        # Any write invalidates the previously reconciled account snapshot.
        conn.execute('UPDATE ledger_accounts SET checked_generation=-1 WHERE account=?',(self.account,))
        return operation_id

    def create(self,intent_id,quote):
        self._enabled()
        with self.ledger.transaction() as conn:
            row=conn.execute('SELECT * FROM order_intents WHERE id=?',(intent_id,)).fetchone()
            if not row or row['account']!=self.account: raise SafetyError('ACCOUNT_MISMATCH')
            previous=conn.execute("SELECT id FROM execution_operations WHERE intent_id=? AND kind='CREATE'",(intent_id,)).fetchone()
            if previous:return previous[0]
            self._ready(conn)
            self._market_ready()
            row=self.engine._claim(intent_id,quote,connection=conn)
            if row is None:raise SafetyError('INTENT_ALREADY_SUBMITTED')
            operation=self._new_operation(conn,intent_id,'CREATE',row)
        self._transmit(operation)
        return operation

    def cancel(self,intent_id):
        self._enabled()
        with self.ledger.transaction() as conn:
            row=conn.execute('SELECT * FROM order_intents WHERE id=?',(intent_id,)).fetchone()
            if not row or row['account']!=self.account:raise SafetyError('ACCOUNT_MISMATCH')
            existing=conn.execute("SELECT id FROM execution_operations WHERE intent_id=? AND state IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED')",(intent_id,)).fetchone()
            if existing:return existing[0]
            if row['state'] not in {'ACCEPTED','PARTIALLY_FILLED'} or not row['broker_id']:raise SafetyError('CANCEL_NOT_ALLOWED')
            operation=self._new_operation(conn,intent_id,'CANCEL',{'order_id':row['broker_id']},row['broker_id'])
            conn.execute("UPDATE order_intents SET state='CANCEL_PENDING' WHERE id=?",(intent_id,))
        self._transmit(operation)
        return operation

    def modify(self,intent_id,price,quote,*,quantity=None):
        self._enabled()
        if quantity is not None:raise SafetyError('US_MODIFY_QUANTITY_NOT_SUPPORTED')
        if self.replacement_semantics not in {'INDEPENDENT','CARRY_FORWARD'} or not self.replacement_evidence:
            raise SafetyError('REPLACEMENT_CONTRACT_REQUIRED')
        self._market_ready()
        price=decimal(price)
        tick=Decimal('.01') if price>=1 else Decimal('.0001')
        if price%tick:raise SafetyError('INVALID_ORDER_INCREMENT')
        fresh(quote['timestamp'],self.ledger.now(),self.engine.limits.quote_age_seconds)
        with self.ledger.transaction() as conn:
            row=conn.execute('SELECT * FROM order_intents WHERE id=?',(intent_id,)).fetchone()
            if not row or row['account']!=self.account:raise SafetyError('ACCOUNT_MISMATCH')
            self._ready(conn)
            account=conn.execute('SELECT * FROM broker_accounts WHERE account=?',(self.account,)).fetchone()
            if not account or account['stopped'] or not account['reconciled']:raise SafetyError('ACCOUNT_BLOCKED')
            fresh(account['snapshot_at'],self.ledger.now(),30)
            if row['state'] not in {'ACCEPTED','PARTIALLY_FILLED'}:raise SafetyError('MODIFY_NOT_ALLOWED')
            latest=conn.execute('SELECT max(s.signal_date) FROM live_signal_snapshots s JOIN signal_provenance p USING(signal_date) WHERE s.is_month_end=1').fetchone()[0]
            if row['signal_date']!=latest or conn.execute('SELECT 1 FROM signal_revisions WHERE signal_date=?',(row['signal_date'],)).fetchone():raise SafetyError('SIGNAL_REVIEW_REQUIRED')
            if quote['symbol']!=row['symbol'] or quote.get('currency')!='USD':raise SafetyError('QUOTE_MISMATCH')
            reference=decimal(quote['lastPrice'])
            if abs(price-reference)/reference>self.engine.limits.max_price_deviation:raise SafetyError('PRICE_DEVIATION')
            now=self.ledger.now(); day=now.tz_convert('America/New_York').tz_localize(None).normalize();cal=live_core.nyse_calendar()
            if not cal.is_session(day) or not cal.session_open(day)<=now<cal.session_close(day):raise SafetyError('OUTSIDE_REGULAR_SESSION')
            amount=Decimal(row['quantity'])*price
            if amount>self.engine.limits.order_usd:raise SafetyError('ORDER_LIMIT')
            all_orders=conn.execute("SELECT quantity,price,submitted_at FROM order_intents WHERE account=? AND state!='PLANNED'",(self.account,)).fetchall()
            spent=sum((Decimal(r['quantity'])*Decimal(r['price']) for r in all_orders if r['submitted_at'] and utc(r['submitted_at']).tz_convert('America/New_York').date()==day.date()),Decimal(0))
            if spent+amount>self.engine.limits.daily_usd:raise SafetyError('DAILY_LIMIT')
            snapshot=json.loads(account['snapshot'])
            if row['side']=='BUY' and amount*(1+self.engine.limits.fee_buffer)>Decimal(snapshot['buying_power']):raise SafetyError('INSUFFICIENT_BUYING_POWER')
            operation=self._new_operation(conn,intent_id,'MODIFY',{'order_id':row['broker_id'],'price':str(price),'semantics':self.replacement_semantics,'contract':self.replacement_evidence},row['broker_id'])
        self._transmit(operation)
        return operation

    def _transmit(self,operation_id):
        operation=self.operation(operation_id);request=json.loads(operation['request'])
        try:
            if operation['kind']=='CREATE':order_id=self.client.submit(request)
            elif operation['kind']=='CANCEL':order_id=self.client.cancel(request['order_id'])
            else:order_id=self.client.modify(request['order_id'],request['price'])
            if not isinstance(order_id,str) or not order_id:raise SafetyError('INVALID_ORDER_RESPONSE')
            self._ack(operation_id,order_id)
        except Exception:
            with self.ledger.transaction() as conn:
                conn.execute("UPDATE execution_operations SET state='UNKNOWN' WHERE id=? AND state='SUBMITTING'",(operation_id,))
                if operation['kind']=='CREATE':conn.execute("UPDATE order_intents SET state='UNKNOWN' WHERE id=? AND state='SUBMITTING'",(operation['intent_id'],))
                conn.execute('UPDATE ledger_accounts SET halted=1,checked_generation=-1 WHERE account=?',(self.account,))
            # Broker errors may contain secrets. Never persist their text.

    def _ack(self,operation_id,order_id,evidence=None):
        with self.ledger.transaction() as conn:
            op=conn.execute('SELECT * FROM execution_operations WHERE id=?',(operation_id,)).fetchone()
            if op['response_id'] and op['response_id']!=order_id:raise SafetyError('RESPONSE_ID_CONFLICT')
            role='OPERATION' if op['kind']=='CANCEL' else 'TRADE'
            conn.execute('INSERT OR IGNORE INTO execution_links VALUES(?,?,?,?,?)',(self.account,order_id,op['intent_id'],operation_id,role))
            link=conn.execute('SELECT * FROM execution_links WHERE account=? AND order_id=?',(self.account,order_id)).fetchone()
            if link['operation_id']!=operation_id:raise SafetyError('ORDER_LINK_CONFLICT')
            conn.execute("UPDATE execution_operations SET response_id=?,state='ACKNOWLEDGED',evidence=? WHERE id=?",(order_id,evidence,operation_id))
            if op['kind']=='CREATE':conn.execute("UPDATE order_intents SET broker_id=?,state='ACCEPTED' WHERE id=?",(order_id,op['intent_id']))
        op=self.operation(operation_id); request=json.loads(op['request'])
        self.ledger.bind(self.account,order_id,role=role,parent=op['predecessor'],
                         semantics=request.get('semantics') if op['kind']=='MODIFY' else None,
                         evidence=request.get('contract') if op['kind']=='MODIFY' else None)

    def ingest(self,row,source='REST'):
        order_id=row['orderId']
        with self.ledger.transaction() as conn:
            link=conn.execute('SELECT * FROM execution_links WHERE account=? AND order_id=?',(self.account,order_id)).fetchone()
        if link:
            op=self.operation(link['operation_id'])
            # Crash between ACK storage and ledger relation binding is recoverable.
            request=json.loads(op['request'])
            with self.ledger.transaction() as conn:
                bound=conn.execute('SELECT 1 FROM ledger_links WHERE account=? AND order_id=?',(self.account,order_id)).fetchone()
            if not bound:self.ledger.bind(self.account,order_id,role=link['role'],parent=op['predecessor'],semantics=request.get('semantics') if op['kind']=='MODIFY' else None,evidence=request.get('contract') if op['kind']=='MODIFY' else None)
        result=self.ledger.observe(self.account,row,source=source)
        if result not in {'APPLIED','DUPLICATE'}:return result
        if not link:return result
        status=row['status']; q=amounts(row)[0]
        with self.ledger.transaction() as conn:
            intent=conn.execute('SELECT * FROM order_intents WHERE id=?',(link['intent_id'],)).fetchone()
            chain_quantity=Decimal(0)
            for economic in conn.execute("SELECT l.order_id,l.payload FROM ledger_orders l JOIN execution_links x ON x.account=l.account AND x.order_id=l.order_id WHERE x.intent_id=? AND l.role='TRADE'",(intent['id'],)):
                snapshot=json.loads(economic['payload'])
                if snapshot['status'] in {'CANCEL_REJECTED','REPLACE_REJECTED'}:continue
                quantity=amounts(snapshot)[0]
                relation=conn.execute('SELECT parent,semantics FROM ledger_links WHERE account=? AND order_id=?',(self.account,economic['order_id'])).fetchone()
                if relation and relation['semantics']=='CARRY_FORWARD':
                    parent=conn.execute('SELECT payload FROM ledger_orders WHERE account=? AND order_id=?',(self.account,relation['parent'])).fetchone()
                    if parent:quantity-=amounts(json.loads(parent[0]))[0]
                chain_quantity+=quantity
            if chain_quantity>Decimal(intent['quantity']):
                self.ledger._issue(conn,self.account,order_id,'INTENT_OVERFILL')
            if op['kind']=='CREATE':conn.execute("UPDATE execution_operations SET state='CONFIRMED' WHERE id=?",(op['id'],))
            elif status in {'CANCEL_REJECTED','REPLACE_REJECTED'}:
                conn.execute("UPDATE execution_operations SET state='REJECTED' WHERE id=?",(op['id'],))
            elif op['kind']=='CANCEL' and status=='CANCELED':
                conn.execute("UPDATE execution_operations SET state='CONFIRMED' WHERE id=?",(op['id'],))
            elif op['kind']=='MODIFY' and status not in {'PENDING_REPLACE'}:
                conn.execute("UPDATE execution_operations SET state='CONFIRMED' WHERE id=?",(op['id'],))
                conn.execute('UPDATE order_intents SET broker_id=?,price=? WHERE id=?',(order_id,json.loads(op['request'])['price'],intent['id']))
            if link['role']=='TRADE' and (intent['broker_id']==order_id or (op['kind']=='MODIFY' and status!='REPLACE_REJECTED')):
                state={'PENDING':'ACCEPTED','PARTIAL_FILLED':'PARTIALLY_FILLED','FILLED':'FILLED','CANCELED':'CANCELED','REJECTED':'REJECTED','PENDING_CANCEL':'CANCEL_PENDING'}.get(status)
                # Read the merged projection, not an old out-of-order raw status.
                projected=conn.execute('SELECT payload FROM ledger_orders WHERE account=? AND order_id=?',(self.account,order_id)).fetchone()
                state={'PENDING':'ACCEPTED','PARTIAL_FILLED':'PARTIALLY_FILLED','FILLED':'FILLED','CANCELED':'CANCELED','REJECTED':'REJECTED','PENDING_CANCEL':'CANCEL_PENDING'}.get(json.loads(projected[0])['status'])
                if state:conn.execute('UPDATE order_intents SET state=? WHERE id=?',(state,intent['id']))
        return result

    def recover(self,operation_id,*,proof=None):
        """Never guesses a missing broker ID or re-sends POST. Receipt proof is audited."""
        op=self.operation(operation_id)
        if not op['response_id']:
            if not proof:return op['state']
            if (proof.get('kind')!='BROKER_RECEIPT' or proof.get('request_hash')!=op['request_hash']
                    or not proof.get('reference') or not proof.get('order_id')):
                raise SafetyError('VERIFIED_RECEIPT_REQUIRED')
            row=self.client.order(proof['order_id'])
            intent=self.engine.get(op['intent_id'])
            if any(row[k]!=intent[k] for k in ('symbol','side','currency')):raise SafetyError('RECOVERY_ORDER_MISMATCH')
            if op['kind']=='CREATE' and (decimal(row['quantity'])!=decimal(intent['quantity']) or decimal(row['price'])!=decimal(intent['price'])):raise SafetyError('RECOVERY_ORDER_MISMATCH')
            self._ack(operation_id,proof['order_id'],canonical(proof))
            op=self.operation(operation_id)
        # Always observe the predecessor, too: operation records aren't economic fills.
        if op['predecessor']:self.ingest(self.client.order(op['predecessor']))
        self.ingest(self.client.order(op['response_id']))
        if op['predecessor']:self.ingest(self.client.order(op['predecessor']))
        return self.operation(operation_id)['state']

    def reconcile(self,*,cash_evidence):
        started=self.ledger.now()
        if cash_evidence.get('account')!=self.account or cash_evidence.get('basis')!='SETTLED_AND_UNSETTLED':raise SafetyError('CASH_ACCOUNT_OR_BASIS_MISMATCH')
        if self.account not in {str(r['accountSeq']) for r in self.client.accounts()}:raise SafetyError('ACCOUNT_NOT_AUTHORIZED')
        opened=self.client.orders('OPEN');closed=self.client.orders('CLOSED')
        for row in closed+opened:self.ingest(row)
        # Finish operations by querying linked IDs, including rejected cancel/replace records.
        with self.ledger.transaction() as conn:
            pending=conn.execute("SELECT x.id FROM execution_operations x JOIN order_intents o ON o.id=x.intent_id WHERE o.account=? AND x.state IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED')",(self.account,)).fetchall()
        for op in pending:self.recover(op[0])
        holdings=self.client.holdings()['items']
        actual={r['currency']+':'+r['symbol']:r['quantity'] for r in holdings}
        if len(actual)!=len(holdings):raise SafetyError('DUPLICATE_HOLDINGS')
        cash_ok=self.ledger.reconcile(self.account,holdings=actual,settled=cash_evidence['settled'],unsettled=cash_evidence['unsettled'],observed_at=cash_evidence['observed_at'],source=cash_evidence['source'])
        power=self.client.buying_power('USD');sellable={t:str(self.client.sellable(t)) for t in live_core.TRADE_TICKERS}
        fresh(started,self.ledger.now(),30)
        with self.ledger.transaction() as conn:
            pending=conn.execute("SELECT 1 FROM execution_operations x JOIN order_intents o ON o.id=x.intent_id WHERE o.account=? AND x.state IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED')",(self.account,)).fetchone()
            local={r[0] for r in conn.execute("SELECT broker_id FROM order_intents WHERE account=? AND state IN ('ACCEPTED','PARTIALLY_FILLED','CANCEL_PENDING')",(self.account,)) if r[0]}
            ok=cash_ok and not pending and local=={r['orderId'] for r in opened}
            conn.execute('UPDATE broker_accounts SET snapshot=?,snapshot_at=?,reconciled=? WHERE account=?',
                         (canonical({'buying_power':str(power),'sellable':sellable}),started.isoformat(),int(ok),self.account))
            if not ok:
                conn.execute('UPDATE broker_accounts SET stopped=1 WHERE account=?',(self.account,))
                conn.execute('UPDATE ledger_accounts SET halted=1,checked_generation=-1 WHERE account=?',(self.account,))
            return bool(ok)
