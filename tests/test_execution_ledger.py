import json
from decimal import Decimal
import random
import sqlite3

import pytest

from broker.ledger import Ledger
from safety import SafetyError

NOW='2025-04-01T15:00:00Z'
CUTOVER='2025-04-01T14:00:00Z'


def order(order_id='a',quantity='5',filled='0',amount=None,fee='0',tax='0',status=None,side='BUY',due='2025-04-02',ws=False):
    row={'orderId':order_id,'symbol':'XLK','side':side,'orderType':'LIMIT','timeInForce':'DAY',
         'status':status or ('FILLED' if filled==quantity else 'PARTIAL_FILLED' if Decimal(filled) else 'PENDING'),
         'quantity':quantity,'price':'100','currency':'USD','orderedAt':'2025-04-01T14:30:00Z','canceledAt':None,
         'execution':{'filledQuantity':filled,'averageFilledPrice':'100' if Decimal(filled) else None,
                      'filledAmount':amount if amount is not None else str(Decimal(filled)*100),
                      'commission':fee,'tax':tax,'filledAt':NOW if Decimal(filled) else None,'settlementDate':due}}
    if ws:del row['execution']['filledAt']
    return row


@pytest.fixture
def ledger(tmp_path):
    ledger=Ledger(tmp_path/'ledger.db',clock=lambda:NOW)
    ledger.bootstrap('1',holdings={},settled={'USD':'1000','KRW':'100000'},unsettled=[],cutover=CUTOVER,source='verified opening statement')
    return ledger


def evidence(ledger):
    b=ledger.balances('1')
    return dict(holdings=b['holdings'],settled=b['settled'],unsettled=b['unsettled'],observed_at=NOW,source='verified independent statement')


def test_cumulative_and_cross_channel_dedup(ledger):
    for filled in ['0','2','5']:
        ledger.observe('1',order(filled=filled,fee='1' if filled=='5' else '0'))
    ledger.observe('1',order(filled='5',fee='1',ws=True),source='WS')
    ledger.observe('1',order(filled='5',fee='1'))
    b=ledger.balances('1')
    assert b['holdings']=={'USD:XLK':'5'}
    assert Decimal(b['unsettled']['USD'])==-501
    assert Decimal(b['settled']['USD'])==1000
    assert ledger.reconcile('1',**evidence(ledger))


def test_ws_has_no_execution_timestamp(ledger):
    ledger.observe('1',order(filled='2',ws=True),source='WS')
    with ledger.transaction() as conn:
        row=json.loads(conn.execute('SELECT payload FROM ledger_orders').fetchone()[0])
    assert row['execution']['filledAt'] is None
    ledger.observe('1',order(filled='2'),source='REST')
    assert ledger.balances('1')['holdings']=={'USD:XLK':'2'}


def test_1000_seeded_duplicate_reverse_sequences(ledger):
    rng=random.Random(20261027)
    ledger.bootstrap('2',holdings={},settled={'USD':'1000000','KRW':'0'},unsettled=[],cutover=CUTOVER,source='large test balance')
    for iteration in range(1000):
        rows=[order(order_id=f'order-{iteration}',filled=str(q),ws=True) for q in range(6)]
        rows+=rng.sample(rows,2)
        rng.shuffle(rows)
        for row in rows:ledger.observe('2',row,source='WS')
    assert ledger.balances('2')['holdings']=={'USD:XLK':'5000'}
    assert Decimal(ledger.balances('2')['unsettled']['USD'])==-500000


def test_rest_regression_requires_evidence(ledger):
    ledger.observe('1',order(filled='5'))
    assert ledger.observe('1',order(filled='2'))=='CUMULATIVE_REGRESSION'
    assert not ledger.reconcile('1',**evidence(ledger))
    assert ledger.observe('1',order(filled='2'),correction_evidence='broker correction receipt 12')=='APPLIED'
    assert ledger.balances('1')['holdings']=={'USD:XLK':'2'}


def test_fee_only_update_then_refund(ledger):
    ledger.observe('1',order(filled='2',fee='1'))
    ledger.observe('1',order(filled='2',fee='2'))
    assert Decimal(ledger.balances('1')['unsettled']['USD'])==-202
    assert ledger.observe('1',order(filled='2',fee='1'))=='DUPLICATE'
    # Explicit evidence can re-apply a previously seen economic correction.
    ledger.observe('1',order(filled='2',fee='1'),correction_evidence='broker final fee')
    assert Decimal(ledger.balances('1')['unsettled']['USD'])==-201


def test_unknown_costs_block_reconciliation(ledger):
    ledger.observe('1',order(filled='2',fee=None))
    assert not ledger.balances('1')['complete']
    assert not ledger.reconcile('1',**evidence(ledger))
    ledger.observe('1',order(filled='2',fee='1'))
    assert ledger.reconcile('1',**evidence(ledger))


@pytest.mark.parametrize('status',['CANCELED','REJECTED','REPLACED'])
def test_terminal_partial_fills_are_preserved(ledger,status):
    ledger.observe('1',order(filled='2',status=status))
    assert ledger.balances('1')['holdings']=={'USD:XLK':'2'}
    ledger.observe('1',order(filled='2',status='PENDING',ws=True),source='WS')
    with ledger.transaction() as conn:
        assert json.loads(conn.execute('SELECT payload FROM ledger_orders').fetchone()[0])['status']==status


def test_settlement_date_and_initial_pending(ledger):
    ledger.observe('1',order(filled='2',fee='1'))
    ledger.clock=lambda:'2025-04-02T15:00:00Z'
    assert Decimal(ledger.balances('1')['unsettled']['USD'])==-201
    ledger.confirm_settlement('1','a',confirmed_at=ledger.now(),source='settlement statement')
    b=ledger.balances('1')
    assert Decimal(b['settled']['USD'])==799 and Decimal(b['unsettled']['USD'])==0


def test_statement_movements_fx_and_duplicate(ledger):
    kwargs=dict(kind='FX',effective_at=NOW,source='statement FX row',cash={'KRW':'-14000','USD':'10'})
    assert ledger.movement('1','fx-1',**kwargs)=='APPLIED'
    assert ledger.movement('1','fx-1',**kwargs)=='DUPLICATE'
    ledger.movement('1','deposit',kind='DEPOSIT',effective_at=NOW,source='bank receipt',cash={'USD':'50'})
    ledger.movement('1','withdraw',kind='WITHDRAWAL',effective_at=NOW,source='bank receipt',cash={'USD':'-20'})
    ledger.movement('1','fee',kind='FEE',effective_at=NOW,source='fee statement',cash={'USD':'-1'})
    ledger.movement('1','div',kind='DIVIDEND',effective_at=NOW,source='div statement',cash={'USD':'2'})
    b=ledger.balances('1')
    assert Decimal(b['settled']['USD'])==1041 and Decimal(b['settled']['KRW'])==86000


def test_statement_conflict_preserves_original_and_blocks(ledger):
    kwargs=dict(kind='DEPOSIT',effective_at=NOW,source='bank',cash={'USD':'50'})
    ledger.movement('1','same',**kwargs)
    assert ledger.movement('1','same',**{**kwargs,'cash':{'USD':'500'}})=='CONFLICT'
    assert Decimal(ledger.balances('1')['settled']['USD'])==1050
    assert not ledger.reconcile('1',**evidence(ledger))


def test_correction_and_corporate_action(ledger):
    ledger.movement('1','split',kind='CORPORATE_ACTION',effective_at=NOW,source='split notice',holdings={'USD:XLK':'3'})
    ledger.movement('1','fix',kind='CORRECTION',effective_at=NOW,source='correction notice',holdings={'USD:XLK':'-1'},replaces='split')
    assert ledger.balances('1')['holdings']=={'USD:XLK':'2'}


def test_movement_rollback_on_storage_failure(ledger):
    with ledger.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_generation BEFORE UPDATE ON ledger_accounts BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.movement('1','fx',kind='FX',effective_at=NOW,source='bank',cash={'KRW':'-14000','USD':'10'})
    with ledger.transaction() as conn:assert conn.execute('SELECT count(*) FROM ledger_movements').fetchone()[0]==0


def test_observation_and_projection_atomic(ledger):
    with ledger.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_projection BEFORE INSERT ON ledger_orders BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):ledger.observe('1',order(filled='2'))
    with ledger.transaction() as conn:
        assert conn.execute('SELECT count(*) FROM ledger_observations').fetchone()[0]==0
    with ledger.transaction() as conn:conn.execute('DROP TRIGGER fail_projection')
    ledger.observe('1',order(filled='2'))
    assert ledger.balances('1')['holdings']=={'USD:XLK':'2'}


def test_open_order_baseline_counts_only_new_fills(tmp_path):
    baseline=order(filled='2');baseline['orderedAt']='2025-04-01T13:00:00Z';baseline['execution']['filledAt']='2025-04-01T13:30:00Z'
    ledger=Ledger(tmp_path/'base.db',clock=lambda:NOW)
    ledger.bootstrap('1',holdings={'USD:XLK':'2'},settled={'USD':'1000','KRW':'0'},unsettled=[{'date':'2025-04-02','cash':{'USD':'-200'}}],cutover=CUTOVER,source='opening',orders=[baseline])
    latest={**baseline,'execution':{**baseline['execution'],'filledQuantity':'5','filledAmount':'500','filledAt':NOW},'status':'FILLED'}
    ledger.observe('1',latest)
    b=ledger.balances('1')
    assert b['holdings']=={'USD:XLK':'5'} and Decimal(b['unsettled']['USD'])==-500


@pytest.mark.parametrize('semantics,child_q,expected',[('INDEPENDENT','3','5'),('CARRY_FORWARD','5','5')])
def test_replacement_accounting_contract(ledger,semantics,child_q,expected):
    ledger.observe('1',order('parent',filled='2',status='REPLACED'))
    ledger.bind('1','child',parent='parent',semantics=semantics,evidence='verified contract')
    ledger.observe('1',order('child',filled=child_q))
    assert ledger.balances('1')['holdings']=={'USD:XLK':expected}
    assert Decimal(ledger.balances('1')['unsettled']['USD'])==-500


def test_cancel_record_is_not_second_execution(ledger):
    ledger.observe('1',order('parent',filled='2',status='CANCELED'))
    ledger.bind('1','cancel-id',role='OPERATION',parent='parent')
    ledger.observe('1',order('cancel-id',filled='2',status='CANCELED'))
    assert ledger.balances('1')['holdings']=={'USD:XLK':'2'}


def test_cash_currency_and_provenance_required(ledger):
    args=evidence(ledger);args['source']=''
    with pytest.raises(SafetyError,match='PROVENANCE'):ledger.reconcile('1',**args)
    args=evidence(ledger);args['settled'].pop('KRW')
    with pytest.raises(SafetyError,match='CURRENCY'):ledger.reconcile('1',**args)


def test_partial_settlement_does_not_settle_later_fill(ledger):
    ledger.observe('1',order(filled='2'))
    ledger.confirm_settlement('1','a',confirmed_at=NOW,source='partial settlement receipt')
    ledger.observe('1',order(filled='5'))
    b=ledger.balances('1')
    assert Decimal(b['settled']['USD'])==800
    assert Decimal(b['unsettled']['USD'])==-300
    ledger.clock=lambda:'2025-04-01T15:01:00Z'
    ledger.confirm_settlement('1','a',confirmed_at=ledger.now(),source='final settlement receipt')
    b=ledger.balances('1')
    assert Decimal(b['settled']['USD'])==500 and Decimal(b['unsettled']['USD'])==0


def test_buying_power_cannot_replace_cash_statement(ledger):
    args=evidence(ledger);args['settled']['USD']='500'
    assert not ledger.reconcile('1',**args)
    with pytest.raises(SafetyError):ledger.ready('1')


def test_rebuild_projection_preserves_evidenced_correction(ledger):
    ledger.observe('1',order(filled='2',fee='1'))
    ledger.observe('1',order(filled='2',fee='2'))
    ledger.observe('1',order(filled='2',fee='1'),correction_evidence='final-cost-receipt')
    expected=ledger.balances('1')
    with ledger.transaction() as conn:
        conn.execute('UPDATE ledger_orders SET payload=?',(json.dumps(order(filled='5',fee='99')),))
    ledger.rebuild_projection('1')
    assert ledger.balances('1')==expected
    assert Decimal(expected['unsettled']['USD'])==-201
    with pytest.raises(SafetyError):ledger.ready('1')
