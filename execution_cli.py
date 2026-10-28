"""Account ledger and guarded execution. Writes require --enable-orders.

Default execution DB is separate from the manual portfolio. Never called by
Streamlit or daily_job. No command implicitly enables the account or places orders.
"""
from contextlib import closing
import argparse
import json
import os
from pathlib import Path
import sqlite3
import threading

import live_core
from broker.engine import Engine
from broker.ledger import Ledger
from broker.execution import TradingService
from broker.stream import OrderStream
from broker.toss import TossClient
from broker.security import public_error_code, private_database
from safety import SafetyError


def read_json(path):
    if not path:raise SafetyError('INPUT_FILE_REQUIRED')
    with open(path,encoding='utf-8') as file:
        text=file.read(2_000_001)
    if len(text)>2_000_000:raise SafetyError('INPUT_TOO_LARGE')
    return json.loads(text)


def copy_signals(path,market_db):
    with closing(sqlite3.connect(f'file:{market_db}?mode=ro',uri=True)) as source:
        source.row_factory=sqlite3.Row
        if live_core.validate_market(source):raise SafetyError('MARKET_NOT_READY')
        latest=source.execute('SELECT s.signal_date FROM live_signal_snapshots s JOIN signal_provenance p USING(signal_date) WHERE s.is_month_end=1 ORDER BY s.signal_date DESC LIMIT 1').fetchone()
        if not latest:raise SafetyError('CONFIRMED_SIGNAL_REQUIRED')
        if source.execute('SELECT 1 FROM signal_revisions WHERE signal_date=?',(latest[0],)).fetchone():raise SafetyError('SIGNAL_REVIEW_REQUIRED')
        with closing(sqlite3.connect(path)) as target, target:
            for table in ('live_signal_snapshots','signal_provenance'):
                row=source.execute(f'SELECT * FROM {table} WHERE signal_date=?',(latest[0],)).fetchone()
                old=target.execute(f'SELECT * FROM {table} WHERE signal_date=?',(latest[0],)).fetchone()
                if old and tuple(row)!=tuple(old):raise SafetyError('SIGNAL_COPY_CONFLICT')
                if not old:
                    columns=','.join(row.keys());parameters=','.join('?' for _ in row)
                    target.execute(f'INSERT INTO {table}({columns}) VALUES({parameters})',tuple(row))
        return latest[0]


def backup(path,destination):
    destination=Path(destination)
    if destination.exists():raise SafetyError('BACKUP_TARGET_EXISTS')
    fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600);os.close(fd)
    with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
        tables={r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'broker_accounts' in tables:target.execute('UPDATE broker_accounts SET stopped=1,reconciled=0')
        if 'ledger_accounts' in tables:target.execute('UPDATE ledger_accounts SET halted=1,checked_generation=-1')
        if 'stream_state' in tables:target.execute("UPDATE stream_state SET state='DISCONNECTED',generation=NULL")
        target.commit()
        if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise SafetyError('BACKUP_INVALID')
    return str(destination)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['init','import-movements','settle','sync-signals','sync','stream','enable','halt','status','plan','create','cancel','modify','recover','backup'])
    parser.add_argument('--db',type=Path,default=live_core.ROOT/'data'/'toss_execution.db')
    parser.add_argument('--market-db',type=Path,default=live_core.DEFAULT_DB)
    parser.add_argument('--account',default=os.environ.get('TOSS_ACCOUNT_SEQ'))
    parser.add_argument('--file',type=Path)
    parser.add_argument('--cash-file',type=Path)
    parser.add_argument('--intent');parser.add_argument('--operation');parser.add_argument('--signal-date')
    parser.add_argument('--symbol',choices=live_core.TRADE_TICKERS);parser.add_argument('--side',choices=['BUY','SELL'])
    parser.add_argument('--quantity');parser.add_argument('--price')
    parser.add_argument('--replacement-semantics',choices=['INDEPENDENT','CARRY_FORWARD'])
    parser.add_argument('--replacement-evidence')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--enable-orders',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.db.resolve()==live_core.DEFAULT_DB.resolve():raise SafetyError('DEDICATED_EXECUTION_DATABASE_REQUIRED')
        if not args.account or not args.account.isdigit():raise SafetyError('ACCOUNT_REQUIRED')
        if args.command in {'create','cancel','modify'} and not args.enable_orders:raise SafetyError('LIVE_ORDER_TRANSPORT_DISABLED')
        if args.command=='backup':
            if not args.output:raise SafetyError('BACKUP_TARGET_REQUIRED')
            result={'backup':backup(args.db,args.output)}
        else:
            if args.command!='init' and not args.db.exists():raise SafetyError('INITIALIZE_ACCOUNT_FIRST')
            args.db.parent.mkdir(parents=True,exist_ok=True)
            private_database(args.db)
            engine=Engine(args.db);ledger=Ledger(args.db)
            if args.command=='init':
                data=read_json(args.file)
                if data['account']!=args.account:raise SafetyError('ACCOUNT_MISMATCH')
                with ledger.transaction() as conn:
                    ledger.bootstrap(args.account,**{k:data[k] for k in ('holdings','settled','unsettled','cutover','source')},orders=data.get('orders',[]),connection=conn)
                    engine.register_account(args.account,{k.split(':',1)[1]:v for k,v in data['holdings'].items() if k.startswith('USD:')},connection=conn)
                result={'initialized':True,'halted':True}
            elif args.command in {'import-movements','settle'}:
                data=read_json(args.file)
                if data['account']!=args.account:raise SafetyError('ACCOUNT_MISMATCH')
                result={'results':[]}
                for row in data['items']:
                    if args.command=='settle':result['results'].append(ledger.confirm_settlement(args.account,**row))
                    else:
                        entry=dict(row);identifier=entry.pop('id')
                        result['results'].append(ledger.movement(args.account,identifier,**entry))
            elif args.command=='sync-signals':result={'signal_date':copy_signals(args.db,args.market_db)}
            elif args.command=='halt':
                ledger.halt(args.account);engine.stop(args.account);result={'halted':True}
            elif args.command=='status':
                result=ledger.balances(args.account)
                with ledger.transaction() as conn:
                    result['safety']=dict(conn.execute('SELECT generation,checked_generation,checked_at,halted FROM ledger_accounts WHERE account=?',(args.account,)).fetchone())
            elif args.command=='plan':
                result={'intent':engine.plan(args.account,args.signal_date,args.symbol,args.side,args.quantity,args.price),'submitted':False}
            else:
                client=TossClient(account=args.account,allow_orders=args.enable_orders)
                service=TradingService(args.db,client,market_db=args.market_db,replacement_semantics=args.replacement_semantics,replacement_evidence=args.replacement_evidence)
                if args.command=='sync':result={'reconciled':service.reconcile(cash_evidence=read_json(args.cash_file))}
                elif args.command=='stream':
                    stream=OrderStream(ledger,client,synchronize=lambda:service.reconcile(cash_evidence=read_json(args.cash_file)),ingest=service.ingest)
                    stop=threading.Event()
                    try:stream.run(stop)
                    except KeyboardInterrupt:stop.set()
                    result={'stream':'DISCONNECTED','last_error':stream.last_error}
                elif args.command=='enable':service.enable();result={'account_enabled':True,'submitted':False}
                elif args.command=='recover':result={'state':service.recover(args.operation,proof=read_json(args.file) if args.file else None)}
                else:
                    if args.command=='cancel':operation=service.cancel(args.intent)
                    else:
                        intent=engine.get(args.intent);quote=client.prices([intent['symbol']])[0]
                        operation=service.create(args.intent,quote) if args.command=='create' else service.modify(args.intent,args.price,quote,quantity=args.quantity)
                    result={'operation':operation,'state':service.operation(operation)['state']}
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except SafetyError as exc:
        print(json.dumps({'status':'BLOCKED','code':public_error_code(exc)}));return 2
    except Exception:
        print(json.dumps({'status':'BLOCKED','code':'LOCAL_DATA_OR_IO_ERROR'}));return 2


if __name__=='__main__':raise SystemExit(main())
