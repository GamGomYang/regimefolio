"""Small, reproducible mutation checks in disposable copies, never the workspace.

Run: .venv/bin/python scripts/check_safety_mutations.py
This checks ten named defenses, not whole-project mutation coverage.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = [
    ('stale-price-check', 'live_core.py', 'if not completed or last < completed[-1]:', 'if False:',
     'tests/test_safety_regressions.py::test_stale_price_and_macro_block'),
    ('duplicate-submit-check', 'broker/engine.py', "if row['state'] != 'PLANNED':", 'if False:',
     'tests/test_order_engine.py::test_lost_response_no_retry_even_after_restart'),
    ('manual-transaction', 'live_core.py', '        rebuild_live_nav(conn, commit=False)',
     '        conn.commit()\n        rebuild_live_nav(conn, commit=False)',
     'tests/test_safety_regressions.py::test_insufficient_cash_rolls_back'),
    ('holdings-reconciliation', 'broker/engine.py', 'ok = actual == expected and expected_open == set(open_order_ids)',
     'ok = True and expected_open == set(open_order_ids)',
     'tests/test_order_engine.py::test_stop_and_mismatch_block_new_orders'),
    ('acceptance-is-not-fill', 'broker/engine.py', '            return self._positions(conn, account)',
     "            return {r['symbol']: Decimal(r['quantity']) for r in conn.execute('SELECT * FROM order_intents WHERE account=?',(account,))}",
     'tests/test_order_engine.py::test_normal_order_fill_duplicate_and_cash_separation'),
    ('write-never-retry', 'broker/toss.py', 'attempts = 3 if method == "GET" else 1', 'attempts = 3',
     'tests/test_execution_service.py::test_write_errors_never_retry[500]'),
    ('cancel-record-not-economic-fill', 'broker/ledger.py', "if item['role']=='OPERATION': continue", "if False: continue",
     'tests/test_execution_ledger.py::test_cancel_record_is_not_second_execution'),
    ('stream-readiness-gate', 'broker/execution.py', "if not stream or stream['state']!='READY': raise SafetyError('STREAM_NOT_READY')", "if False: raise SafetyError('STREAM_NOT_READY')",
     'tests/test_execution_service.py::test_stream_and_ledger_gates_block_create'),
    ('cash-reconciliation', 'broker/ledger.py', "match=match and all(Decimal(expected[field][c])==Decimal(actual[c]) for c in ('USD','KRW'))", 'match=match and True',
     'tests/test_execution_ledger.py::test_buying_power_cannot_replace_cash_statement'),
    ('write-opt-in', 'broker/toss.py', 'self.allow_orders = allow_orders is True', 'self.allow_orders = True',
     'tests/test_toss_client.py::test_unsupported_live_methods_make_no_request'),

]


def main():
    failed = []
    for name, filename, before, after, test in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix='regimefolio-mutation-') as tmp:
            target = Path(tmp)
            for source in ROOT.glob('*.py'):
                shutil.copy2(source, target/source.name)
            for directory in ('broker','tests'):
                shutil.copytree(ROOT/directory,target/directory,ignore=shutil.ignore_patterns('__pycache__'))
            file = target/filename
            code = file.read_text()
            if before not in code:
                raise RuntimeError('Mutation anchor missing: ' + name)
            file.write_text(code.replace(before,after,1))
            result = subprocess.run([sys.executable,'-m','pytest','-q',test],cwd=target,capture_output=True,text=True,timeout=45)
            # Exit 1 means a test failure. Collection/syntax errors do not count as detection.
            detected = result.returncode == 1 and 'failed' in result.stdout
            print(('PASS ' if detected else 'FAIL ') + name)
            if not detected:
                failed.append(name)
                print(result.stdout[-2000:])
                print(result.stderr[-500:])
    print(f'{len(MUTATIONS)-len(failed)}/{len(MUTATIONS)} mutations detected')
    return int(bool(failed))


if __name__ == '__main__':
    raise SystemExit(main())
