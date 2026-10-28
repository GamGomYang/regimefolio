import json
import sqlite3

from broker.ledger import Ledger
from execution_cli import main,backup
from test_execution_service import service,planned,QUOTE


def test_cli_initializes_dedicated_db_and_blocks_implicit_write(tmp_path,capsys):
    db=tmp_path/'exec.db';file=tmp_path/'baseline.json'
    file.write_text(json.dumps({'account':'1','cutover':'2025-04-01T00:00:00Z','source':'bank statement',
                                'holdings':{},'settled':{'USD':'1000','KRW':'0'},'unsettled':[]}))
    assert main(['init','--db',str(db),'--account','1','--file',str(file)])==0
    assert main(['create','--db',str(db),'--account','1','--intent','anything'])==2
    assert 'LIVE_ORDER_TRANSPORT_DISABLED' in capsys.readouterr().out
    assert db.stat().st_mode & 0o777==0o600


def test_restore_requires_fresh_stream_and_reconciliation(service,tmp_path):
    intent=planned(service);operation=service.create(intent,QUOTE)
    path=tmp_path/'restore.db'
    backup(service.engine.path,path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT halted,checked_generation FROM ledger_accounts').fetchone()==(1,-1)
        assert conn.execute('SELECT state FROM stream_state').fetchone()[0]=='DISCONNECTED'
        assert conn.execute('SELECT state FROM execution_operations WHERE id=?',(operation,)).fetchone()[0]=='ACKNOWLEDGED'
    assert service.operation(operation)['state']=='ACKNOWLEDGED'


def test_init_rollback_on_baseline_failure(tmp_path):
    db=tmp_path/'exec.db';file=tmp_path/'baseline.json'
    file.write_text(json.dumps({'account':'1','cutover':'2025-04-01T00:00:00Z','source':'',
                                'holdings':{},'settled':{'USD':'1000','KRW':'0'},'unsettled':[]}))
    assert main(['init','--db',str(db),'--account','1','--file',str(file)])==2
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM broker_accounts').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM ledger_accounts').fetchone()[0]==0
