"""Cumulative broker observations and a currency/settlement-aware account ledger.

Observations are not individual fills. All changes and projections commit together.
Cash statements are imported with provenance; buying power is never treated as cash.
"""
from __future__ import annotations
from contextlib import contextmanager, closing, nullcontext
from decimal import Decimal
import hashlib
import json
import sqlite3

import pandas as pd

from safety import SafetyError, decimal, utc, fresh
from broker.toss import TossClient
from broker.security import private_database

TERMINAL = {'FILLED', 'CANCELED', 'REJECTED', 'REPLACED'}
KNOWN = TERMINAL | {'PENDING', 'PARTIAL_FILLED', 'PENDING_CANCEL', 'PENDING_REPLACE', 'CANCEL_REJECTED', 'REPLACE_REJECTED'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_schema(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO execution_schema VALUES(1);
CREATE TABLE IF NOT EXISTS ledger_accounts(
 account TEXT PRIMARY KEY, baseline TEXT NOT NULL, cutover TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 0, checked_generation INTEGER NOT NULL DEFAULT -1,
 checked_at TEXT, checked_cash TEXT, halted INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS ledger_baseline_orders(
 account TEXT NOT NULL, order_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account,order_id));
CREATE TABLE IF NOT EXISTS ledger_orders(
 account TEXT NOT NULL, order_id TEXT NOT NULL, payload TEXT NOT NULL,
 baseline TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'TRADE',
 PRIMARY KEY(account,order_id), FOREIGN KEY(account) REFERENCES ledger_accounts(account));
CREATE TABLE IF NOT EXISTS ledger_links(
 account TEXT NOT NULL, order_id TEXT NOT NULL, role TEXT NOT NULL,
 parent TEXT, semantics TEXT, evidence TEXT, PRIMARY KEY(account,order_id));
CREATE TABLE IF NOT EXISTS ledger_observations(
 seq INTEGER PRIMARY KEY, account TEXT NOT NULL, order_id TEXT NOT NULL,
 source TEXT NOT NULL, observed_at TEXT NOT NULL, digest TEXT NOT NULL,
 payload TEXT NOT NULL, disposition TEXT NOT NULL, correction_evidence TEXT,
 UNIQUE(account,order_id,source,digest));
CREATE UNIQUE INDEX IF NOT EXISTS unique_correction_evidence ON ledger_observations(account,order_id,correction_evidence)
 WHERE correction_evidence IS NOT NULL;
CREATE TABLE IF NOT EXISTS ledger_settlements(
 account TEXT NOT NULL, reference TEXT NOT NULL, confirmed_at TEXT NOT NULL,
 source TEXT NOT NULL, cash TEXT NOT NULL, PRIMARY KEY(account,reference));
CREATE TABLE IF NOT EXISTS ledger_settlement_history(
 account TEXT NOT NULL, reference TEXT NOT NULL, confirmed_at TEXT NOT NULL, source TEXT NOT NULL, cash TEXT NOT NULL,
 PRIMARY KEY(account,reference,confirmed_at));
CREATE TABLE IF NOT EXISTS ledger_movements(
 account TEXT NOT NULL, movement_id TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(account,movement_id), FOREIGN KEY(account) REFERENCES ledger_accounts(account));
CREATE TABLE IF NOT EXISTS ledger_issues(
 id INTEGER PRIMARY KEY, account TEXT NOT NULL, order_id TEXT, code TEXT NOT NULL,
 at TEXT NOT NULL, resolved_at TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS stream_state(
 account TEXT PRIMARY KEY, state TEXT NOT NULL, generation TEXT,
 updated_at TEXT NOT NULL, last_rx TEXT, last_ping TEXT);
"""


def signed(value):
    try:
        result = Decimal(str(value))
        decimal(abs(result), zero=True)
        if isinstance(value, bool):
            raise ValueError
        return result
    except (ValueError, ArithmeticError):
        raise SafetyError('INVALID_SIGNED_NUMBER') from None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def amounts(row):
    execution = row['execution']
    return tuple(decimal(execution.get(key) or '0', zero=True)
                 for key in ('filledQuantity', 'filledAmount', 'commission', 'tax'))


def normalized(row, source):
    TossClient.validate_order(row, source=source)
    result = {k: row.get(k) for k in ('orderId','symbol','side','orderType','timeInForce','status','quantity','price','currency','orderedAt','canceledAt')}
    result['execution'] = {k: row['execution'].get(k) for k in ('filledQuantity','averageFilledPrice','filledAmount','commission','tax','filledAt','settlementDate')}
    for key in ('quantity','price'):
        if result[key] is not None:
            result[key] = str(decimal(result[key], zero=(key=='quantity')))
    for key in ('filledQuantity','averageFilledPrice','filledAmount','commission','tax'):
        if result['execution'][key] is not None:
            result['execution'][key] = str(decimal(result['execution'][key], zero=True))
    due = result['execution']['settlementDate']
    if due is not None and (pd.isna(pd.Timestamp(due)) or str(pd.Timestamp(due).date()) != due):
        raise SafetyError('INVALID_SETTLEMENT_DATE')
    return result


class Ledger:
    def __init__(self, path, *, clock=lambda: utc()):
        self.path, self.clock = str(path), clock
        with closing(self.connect()) as conn:
            conn.executescript(SCHEMA)
            if conn.execute('SELECT max(version) FROM execution_schema').fetchone()[0] != 1:
                raise SafetyError('UNSUPPORTED_EXECUTION_SCHEMA')

    def connect(self):
        private_database(self.path)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
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

    def bootstrap(self, account, *, holdings, settled, unsettled, cutover, source, orders=(), connection=None):
        if not isinstance(account,str) or not account.isdigit() or not source:
            raise SafetyError('BASELINE_PROVENANCE_REQUIRED')
        at = utc(cutover)
        if at > self.now():
            raise SafetyError('FUTURE_BASELINE')
        baseline = {'holdings': self._holdings(holdings), 'settled': self._cash(settled),
                    'unsettled': self._pending(unsettled), 'source': source}
        if set(baseline['settled'])!={'USD','KRW'}:raise SafetyError('COMPLETE_CURRENCY_BALANCES_REQUIRED')
        seeds = [normalized(r, 'REST') for r in orders]
        with (nullcontext(connection) if connection is not None else self.transaction()) as conn:
            conn.execute('INSERT INTO ledger_accounts(account,baseline,cutover) VALUES(?,?,?)', (account,canonical(baseline),at.isoformat()))
            for row in seeds:
                if utc(row['orderedAt']) > at or (row['execution'].get('filledAt') and utc(row['execution']['filledAt'])>at):
                    raise SafetyError('BASELINE_ORDER_AFTER_CUTOVER')
                conn.execute('INSERT INTO ledger_baseline_orders VALUES(?,?,?)',(account,row['orderId'],canonical(row)))
                conn.execute('INSERT INTO ledger_orders VALUES(?,?,?,?,?)', (account,row['orderId'],canonical(row),canonical(row),'TRADE'))

    @staticmethod
    def _cash(values):
        if not isinstance(values,dict) or set(values)-{'USD','KRW'}:
            raise SafetyError('INVALID_CASH_CURRENCIES')
        return {c:str(signed(v)) for c,v in values.items()}

    @staticmethod
    def _holdings(values):
        result = {}
        for key,value in values.items():
            currency, sep, symbol = key.partition(':')
            if not sep or currency not in {'USD','KRW'} or not symbol:
                raise SafetyError('HOLDINGS_REQUIRE_CURRENCY_SYMBOL')
            quantity=decimal(value,zero=True)
            if quantity:
                result[key]=str(quantity)
        return result

    @classmethod
    def _pending(cls, values):
        result=[]
        for item in values:
            due=item['date']
            if str(pd.Timestamp(due).date()) != due:
                raise SafetyError('INVALID_SETTLEMENT_DATE')
            result.append({'date':due,'cash':cls._cash(item['cash'])})
        return result

    def _issue(self, conn, account, order, code):
        conn.execute('INSERT INTO ledger_issues(account,order_id,code,at) VALUES(?,?,?,?)', (account,order,code,self.now().isoformat()))
        conn.execute('UPDATE ledger_accounts SET halted=1,checked_generation=-1 WHERE account=?',(account,))

    def issue(self, account, code, order=None):
        with self.transaction() as conn:
            self._issue(conn,account,order,code)

    def halt(self, account):
        with self.transaction() as conn:
            conn.execute('UPDATE ledger_accounts SET halted=1,checked_generation=-1 WHERE account=?',(account,))

    def observe(self, account, raw, *, source='REST', observed_at=None, correction_evidence=None):
        if source not in {'REST','WS'}:
            raise SafetyError('INVALID_OBSERVATION_SOURCE')
        if correction_evidence and (source!='REST' or not isinstance(correction_evidence,str)):
            raise SafetyError('REST_CORRECTION_EVIDENCE_REQUIRED')
        at=utc(observed_at or self.now())
        if at > self.now():
            raise SafetyError('FUTURE_OBSERVATION')
        row=normalized(raw, source)
        digest=hashlib.sha256((canonical(row)+(str(correction_evidence) if correction_evidence else '')).encode()).hexdigest()
        with self.transaction() as conn:
            account_row=conn.execute('SELECT * FROM ledger_accounts WHERE account=?',(account,)).fetchone()
            if account_row is None:
                raise SafetyError('UNKNOWN_LEDGER_ACCOUNT')
            previous=conn.execute('SELECT * FROM ledger_orders WHERE account=? AND order_id=?',(account,row['orderId'])).fetchone()
            duplicate=conn.execute('SELECT disposition FROM ledger_observations WHERE account=? AND order_id=? AND source=? AND digest=?',(account,row['orderId'],source,digest)).fetchone()
            if duplicate:
                return 'DUPLICATE'
            if correction_evidence and conn.execute('SELECT 1 FROM ledger_observations WHERE account=? AND order_id=? AND correction_evidence=?',(account,row['orderId'],str(correction_evidence))).fetchone():raise SafetyError('CORRECTION_EVIDENCE_CONFLICT')
            disposition='APPLIED'
            base=None
            link=conn.execute('SELECT * FROM ledger_links WHERE account=? AND order_id=?',(account,row['orderId'])).fetchone()
            if row['status'] not in KNOWN:
                disposition='UNKNOWN_STATUS'
            if previous:
                old=json.loads(previous['payload']); base=json.loads(previous['baseline']) if previous['baseline']!='null' else None
                if any(old[k]!=row[k] for k in ('symbol','side','currency','quantity','orderedAt')):
                    disposition='ORDER_IDENTITY_CONFLICT'
                old_q,old_amount,old_fee,old_tax=amounts(old)
                q,amount,fee,tax=amounts(row)
                if q<old_q:
                    disposition='CUMULATIVE_REGRESSION'
                elif q==old_q and (amount<old_amount or (row['execution']['commission'] is not None and fee<old_fee) or (row['execution']['tax'] is not None and tax<old_tax)):
                    disposition='COST_CORRECTION_REQUIRED'
                if correction_evidence and source=='REST' and disposition in {'CUMULATIVE_REGRESSION','COST_CORRECTION_REQUIRED'}:
                    disposition='APPLIED'
                # Unknown cost/time is not zero. Preserve REST facts in WS snapshots.
                for key in ('commission','tax','filledAt','settlementDate'):
                    if row['execution'].get(key) is None:
                        row['execution'][key]=old['execution'].get(key)
                if old['status'] in TERMINAL and row['status'] not in TERMINAL and q<=old_q:
                    row['status']=old['status']
                if old['status']=='FILLED' and row['status'] in TERMINAL and q==old_q:
                    row['status']='FILLED'
            else:
                # A pre-cutover terminal snapshot is already included in the baseline.
                if utc(row['orderedAt'])<=utc(account_row['cutover']):
                    filled_at=row['execution'].get('filledAt')
                    if row['status'] in TERMINAL and (not amounts(row)[0] or (filled_at and utc(filled_at)<=utc(account_row['cutover']))):
                        base=row.copy()
                    else:
                        disposition='BASELINE_ORDER_MISSING'
            conn.execute('INSERT OR IGNORE INTO ledger_observations(account,order_id,source,observed_at,digest,payload,disposition,correction_evidence) VALUES(?,?,?,?,?,?,?,?)',
                         (account,row['orderId'],source,at.isoformat(),digest,canonical(row),disposition,str(correction_evidence) if correction_evidence else None))
            if source=='WS' and disposition in {'CUMULATIVE_REGRESSION','COST_CORRECTION_REQUIRED'}:
                return 'STALE'
            if disposition!='APPLIED':
                self._issue(conn,account,row['orderId'],disposition)
                return disposition
            role=link['role'] if link else 'TRADE'
            if link and link['semantics']=='CARRY_FORWARD' and not previous:
                parent=conn.execute('SELECT payload FROM ledger_orders WHERE account=? AND order_id=?',(account,link['parent'])).fetchone()
                if not parent: raise SafetyError('REPLACEMENT_PARENT_REQUIRED')
                base=json.loads(parent[0])
                if amounts(row)[0]<amounts(base)[0]:
                    self._issue(conn,account,row['orderId'],'REPLACEMENT_SEMANTICS_CONFLICT')
                    return 'REPLACEMENT_SEMANTICS_CONFLICT'
            conn.execute("INSERT INTO ledger_orders(account,order_id,payload,baseline,role) VALUES(?,?,?,?,?) ON CONFLICT(account,order_id) DO UPDATE SET payload=excluded.payload,role=excluded.role",
                         (account,row['orderId'],canonical(row),canonical(base),role))
            conn.execute('UPDATE ledger_accounts SET generation=generation+1,checked_generation=-1 WHERE account=?',(account,))
            if correction_evidence:
                conn.execute("UPDATE ledger_issues SET resolved_at=?,evidence=? WHERE account=? AND order_id=? AND code IN ('CUMULATIVE_REGRESSION','COST_CORRECTION_REQUIRED') AND resolved_at IS NULL",
                             (self.now().isoformat(),str(correction_evidence),account,row['orderId']))
            return disposition

    def bind(self,account,order_id,*,role='TRADE',parent=None,semantics=None,evidence=None):
        if role not in {'TRADE','OPERATION'} or semantics not in {None,'INDEPENDENT','CARRY_FORWARD'}:
            raise SafetyError('INVALID_ORDER_RELATION')
        if semantics and (not parent or not evidence):
            raise SafetyError('REPLACEMENT_CONTRACT_REQUIRED')
        with self.transaction() as conn:
            old=conn.execute('SELECT * FROM ledger_links WHERE account=? AND order_id=?',(account,order_id)).fetchone()
            values=(account,order_id,role,parent,semantics,evidence)
            if old and tuple(old)!=values: raise SafetyError('ORDER_RELATION_CONFLICT')
            conn.execute('INSERT OR IGNORE INTO ledger_links VALUES(?,?,?,?,?,?)',values)
            if semantics=='CARRY_FORWARD':
                previous=conn.execute('SELECT payload FROM ledger_orders WHERE account=? AND order_id=?',(account,parent)).fetchone()
                if not previous: raise SafetyError('REPLACEMENT_PARENT_REQUIRED')
                conn.execute('UPDATE ledger_orders SET baseline=?,role=? WHERE account=? AND order_id=?',(previous[0],role,account,order_id))
            else:
                conn.execute('UPDATE ledger_orders SET role=? WHERE account=? AND order_id=?',(role,account,order_id))
            conn.execute('UPDATE ledger_accounts SET generation=generation+1,checked_generation=-1 WHERE account=?',(account,))

    def movement(self, account, movement_id, *, kind, effective_at, source, cash=None, holdings=None, replaces=None):
        """Import verified statement lines. FX legs and fees belong to one atomic line."""
        if kind not in {'DEPOSIT','WITHDRAWAL','FX','DIVIDEND','FEE','CORPORATE_ACTION','CORRECTION'} or not source or not movement_id:
            raise SafetyError('INVALID_STATEMENT_MOVEMENT')
        at=utc(effective_at)
        if at>self.now(): raise SafetyError('FUTURE_MOVEMENT')
        cash=self._cash(cash or {})
        quantities={k:str(signed(v)) for k,v in (holdings or {}).items()}
        for key in quantities:
            currency,sep,symbol=key.partition(':')
            if not sep or currency not in {'USD','KRW'} or not symbol:
                raise SafetyError('INVALID_HOLDINGS_MOVEMENT')
        if not cash and not quantities: raise SafetyError('EMPTY_MOVEMENT')
        if kind=='DEPOSIT' and (not cash or any(signed(v)<=0 for v in cash.values()) or quantities): raise SafetyError('INVALID_DEPOSIT')
        if kind in {'WITHDRAWAL','FEE'} and (not cash or any(signed(v)>=0 for v in cash.values()) or quantities): raise SafetyError('INVALID_DEBIT')
        if kind=='FX' and (set(cash)!={'USD','KRW'} or signed(cash['USD'])*signed(cash['KRW'])>=0 or quantities): raise SafetyError('FX_REQUIRES_TWO_OPPOSITE_LEGS')
        payload={'kind':kind,'effective_at':at.isoformat(),'source':source,'cash':cash,'holdings':quantities,'replaces':replaces}
        with self.transaction() as conn:
            baseline=conn.execute('SELECT cutover FROM ledger_accounts WHERE account=?',(account,)).fetchone()
            if not baseline or at<=utc(baseline['cutover']): raise SafetyError('MOVEMENT_BEFORE_BASELINE')
            old=conn.execute('SELECT payload FROM ledger_movements WHERE account=? AND movement_id=?',(account,movement_id)).fetchone()
            if old:
                if old[0]!=canonical(payload):
                    self._issue(conn,account,None,'MOVEMENT_ID_CONFLICT')
                    return 'CONFLICT'
                return 'DUPLICATE'
            if kind=='CORRECTION':
                if not replaces or not conn.execute('SELECT 1 FROM ledger_movements WHERE account=? AND movement_id=?',(account,replaces)).fetchone():
                    raise SafetyError('CORRECTION_REFERENCE_REQUIRED')
            conn.execute('INSERT INTO ledger_movements VALUES(?,?,?)',(account,movement_id,canonical(payload)))
            conn.execute('UPDATE ledger_accounts SET generation=generation+1,checked_generation=-1 WHERE account=?',(account,))
            return 'APPLIED'

    def confirm_settlement(self,account,reference,*,confirmed_at,source):
        at=utc(confirmed_at)
        if not source or at>self.now():raise SafetyError('SETTLEMENT_EVIDENCE_REQUIRED')
        with self.transaction() as conn:
            if reference.startswith('baseline:'):
                base=conn.execute('SELECT baseline FROM ledger_accounts WHERE account=?',(account,)).fetchone()
                try: entry=json.loads(base[0])['unsettled'][int(reference.split(':')[1])]
                except (IndexError,ValueError,TypeError):raise SafetyError('UNKNOWN_SETTLEMENT_REFERENCE') from None
                due=entry['date']; cash=entry['cash']
            else:
                item=conn.execute('SELECT * FROM ledger_orders WHERE account=? AND order_id=?',(account,reference)).fetchone()
                if not item or item['role']!='TRADE':raise SafetyError('UNKNOWN_SETTLEMENT_REFERENCE')
                row=json.loads(item['payload']);baseline=json.loads(item['baseline'])
                link=conn.execute('SELECT * FROM ledger_links WHERE account=? AND order_id=?',(account,reference)).fetchone()
                if link and link['semantics']=='CARRY_FORWARD':
                    baseline=json.loads(conn.execute('SELECT payload FROM ledger_orders WHERE account=? AND order_id=?',(account,link['parent'])).fetchone()[0])
                if any(row['execution'].get(k) is None for k in ('filledAmount','commission','tax')):
                    raise SafetyError('INCOMPLETE_SETTLEMENT_COST')
                _,amount,fee,tax=amounts(row);_,ba,bf,bt=amounts(baseline) if baseline else (Decimal(0),)*4
                cash={row['currency']:str((amount-ba)*(-1 if row['side']=='BUY' else 1)-(fee-bf)-(tax-bt))}
                due=row['execution']['settlementDate']
            if not due or due>at.tz_convert('Asia/Seoul').date().isoformat():raise SafetyError('SETTLEMENT_BEFORE_DUE_DATE')
            old=conn.execute('SELECT confirmed_at,source,cash FROM ledger_settlements WHERE account=? AND reference=?',(account,reference)).fetchone()
            if old:
                if tuple(old)==(at.isoformat(),source,canonical(cash)):return 'DUPLICATE'
                if at<=utc(old['confirmed_at']):raise SafetyError('SETTLEMENT_EVIDENCE_CONFLICT')
            values=(account,reference,at.isoformat(),source,canonical(cash))
            conn.execute('INSERT INTO ledger_settlement_history VALUES(?,?,?,?,?)',values)
            conn.execute('INSERT INTO ledger_settlements VALUES(?,?,?,?,?) ON CONFLICT(account,reference) DO UPDATE SET confirmed_at=excluded.confirmed_at,source=excluded.source,cash=excluded.cash',values)
            conn.execute('UPDATE ledger_accounts SET generation=generation+1,checked_generation=-1 WHERE account=?',(account,))
            return 'APPLIED'

    def _balances(self,conn,account,as_of):
        record=conn.execute('SELECT * FROM ledger_accounts WHERE account=?',(account,)).fetchone()
        if not record: raise SafetyError('UNKNOWN_LEDGER_ACCOUNT')
        if utc(as_of)<utc(record['cutover']): raise SafetyError('AS_OF_BEFORE_BASELINE')
        base=json.loads(record['baseline'])
        held={k:Decimal(v) for k,v in base['holdings'].items()}
        settled={c:Decimal(v) for c,v in base['settled'].items()}
        pending={}; complete=True
        day=utc(as_of).tz_convert('Asia/Seoul').date().isoformat()
        def cash(currency,amount,due,reference):
            confirmation=conn.execute('SELECT cash FROM ledger_settlement_history WHERE account=? AND reference=? AND confirmed_at<=? ORDER BY confirmed_at DESC LIMIT 1',(account,reference,utc(as_of).isoformat())).fetchone()
            confirmed=Decimal(json.loads(confirmation[0]).get(currency,'0')) if confirmation else Decimal(0)
            settled[currency]=settled.get(currency,Decimal(0))+confirmed
            pending[currency]=pending.get(currency,Decimal(0))+amount-confirmed
        for index,entry in enumerate(base['unsettled']):
            for currency,value in entry['cash'].items(): cash(currency,Decimal(value),entry['date'],'baseline:'+str(index))
        for item in conn.execute('SELECT * FROM ledger_orders WHERE account=?',(account,)):
            if item['role']=='OPERATION': continue
            row=json.loads(item['payload']); baseline=json.loads(item['baseline'])
            if row['status'] in {'CANCEL_REJECTED','REPLACE_REJECTED'}:continue
            link=conn.execute('SELECT parent,semantics FROM ledger_links WHERE account=? AND order_id=?',(account,item['order_id'])).fetchone()
            if link and link['semantics']=='CARRY_FORWARD':
                parent=conn.execute('SELECT payload FROM ledger_orders WHERE account=? AND order_id=?',(account,link['parent'])).fetchone()
                if not parent:raise SafetyError('REPLACEMENT_PARENT_REQUIRED')
                baseline=json.loads(parent[0])
            quantity,amount,fee,tax=amounts(row)
            bq,ba,bf,bt=amounts(baseline) if baseline else (Decimal(0),)*4
            if quantity<bq:complete=False
            sign=1 if row['side']=='BUY' else -1
            key=row['currency']+':'+row['symbol']
            held[key]=held.get(key,Decimal(0))+(quantity-bq)*sign
            execution=row['execution']
            if quantity and any(execution.get(k) is None for k in ('filledAmount','commission','tax','settlementDate')):
                complete=False
            amount_delta=-(amount-ba)*sign-(fee-bf)-(tax-bt)
            cash(row['currency'],amount_delta,execution.get('settlementDate'),item['order_id'])
        for item in conn.execute('SELECT payload FROM ledger_movements WHERE account=?',(account,)):
            row=json.loads(item[0])
            if utc(row['effective_at'])>utc(as_of):
                complete=False
                continue
            for currency,value in row['cash'].items(): settled[currency]=settled.get(currency,Decimal(0))+Decimal(value)
            for symbol,value in row['holdings'].items(): held[symbol]=held.get(symbol,Decimal(0))+Decimal(value)
        if any(q<0 for q in held.values()): complete=False
        return {'holdings':{k:str(v) for k,v in held.items() if v},
                'settled':{c:str(settled.get(c,Decimal(0))) for c in ('USD','KRW')},
                'unsettled':{c:str(pending.get(c,Decimal(0))) for c in ('USD','KRW')},'complete':complete}

    def rebuild_projection(self,account):
        """Rebuild materialized orders from the immutable accepted journal and seeds."""
        with self.transaction() as conn:
            account_row=conn.execute('SELECT cutover FROM ledger_accounts WHERE account=?',(account,)).fetchone()
            if not account_row:raise SafetyError('UNKNOWN_LEDGER_ACCOUNT')
            seeds={r['order_id']:json.loads(r['payload']) for r in conn.execute('SELECT * FROM ledger_baseline_orders WHERE account=?',(account,))}
            first={};latest={}
            for event in conn.execute("SELECT order_id,payload FROM ledger_observations WHERE account=? AND disposition='APPLIED' ORDER BY seq",(account,)):
                row=json.loads(event['payload']);first.setdefault(event['order_id'],row);latest[event['order_id']]=row
            conn.execute('DELETE FROM ledger_orders WHERE account=?',(account,))
            for identifier in seeds.keys()|latest.keys():
                row=latest.get(identifier,seeds.get(identifier));baseline=seeds.get(identifier)
                if baseline is None and identifier in first:
                    initial=first[identifier];filled_at=initial['execution'].get('filledAt')
                    if utc(initial['orderedAt'])<=utc(account_row['cutover']) and initial['status'] in TERMINAL and (not amounts(initial)[0] or (filled_at and utc(filled_at)<=utc(account_row['cutover']))):baseline=initial
                link=conn.execute('SELECT role FROM ledger_links WHERE account=? AND order_id=?',(account,identifier)).fetchone()
                conn.execute('INSERT INTO ledger_orders VALUES(?,?,?,?,?)',(account,identifier,canonical(row),canonical(baseline),link['role'] if link else 'TRADE'))
            conn.execute('UPDATE ledger_accounts SET generation=generation+1,checked_generation=-1,halted=1 WHERE account=?',(account,))

    def balances(self,account,as_of=None):
        with self.transaction() as conn:
            return self._balances(conn,account,as_of or self.now())

    def reconcile(self,account, *, holdings, settled, unsettled, observed_at, source):
        if not source: raise SafetyError('CASH_PROVENANCE_REQUIRED')
        fresh(observed_at,self.now(),30)
        actual_h=self._holdings(holdings); actual_s=self._cash(settled); actual_u=self._cash(unsettled)
        # Explicit zeros required: omitting a currency is not evidence of zero balance.
        if set(actual_s)!={'USD','KRW'} or set(actual_u)!={'USD','KRW'}:
            raise SafetyError('COMPLETE_CURRENCY_BALANCES_REQUIRED')
        with self.transaction() as conn:
            expected=self._balances(conn,account,observed_at)
            issues=conn.execute("SELECT 1 FROM ledger_issues WHERE account=? AND resolved_at IS NULL AND code!='BALANCE_MISMATCH'",(account,)).fetchone()
            match=expected['complete'] and not issues and {k:Decimal(v) for k,v in expected['holdings'].items()}=={k:Decimal(v) for k,v in actual_h.items()}
            for field,actual in [('settled',actual_s),('unsettled',actual_u)]:
                match=match and all(Decimal(expected[field][c])==Decimal(actual[c]) for c in ('USD','KRW'))
            if match:
                conn.execute('UPDATE ledger_accounts SET checked_generation=generation,checked_at=?,checked_cash=? WHERE account=?',(utc(observed_at).isoformat(),canonical({'source':source,'settled':actual_s,'unsettled':actual_u}),account))
                conn.execute("UPDATE ledger_issues SET resolved_at=? WHERE account=? AND code='BALANCE_MISMATCH' AND resolved_at IS NULL",(self.now().isoformat(),account))
            else: self._issue(conn,account,None,'BALANCE_MISMATCH')
            return bool(match)

    def ready(self,account,conn=None):
        if conn is None:
            with self.transaction() as own: return self.ready(account,own)
        row=conn.execute('SELECT * FROM ledger_accounts WHERE account=?',(account,)).fetchone()
        if not row or row['checked_generation']!=row['generation'] or not row['checked_at']:
            raise SafetyError('LEDGER_RECONCILIATION_REQUIRED')
        fresh(row['checked_at'],self.now(),30)
        if conn.execute('SELECT 1 FROM ledger_issues WHERE account=? AND resolved_at IS NULL',(account,)).fetchone():
            raise SafetyError('LEDGER_REVIEW_REQUIRED')
        return True
