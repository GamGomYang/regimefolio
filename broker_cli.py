"""Explicit read-only sync and no-order preview. Never invoked by daily_job."""
import argparse
import json
import os
from pathlib import Path
import sqlite3

import live_core
from broker.read_model import sync, preview
from broker.toss import TossClient
from broker.security import public_error_code, private_database
from safety import SafetyError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['sync','preview'])
    parser.add_argument('--db', type=Path, default=live_core.ROOT/'data'/'toss_readonly.db')
    parser.add_argument('--market-db', type=Path, default=live_core.DEFAULT_DB)
    parser.add_argument('--budget-usd')
    args = parser.parse_args()
    try:
        if args.command == 'sync':
            args.db.parent.mkdir(parents=True, exist_ok=True)
            private_database(args.db)
            result = sync(TossClient(), args.db)
            args.db.chmod(0o600)
        else:
            if not args.budget_usd:
                raise SafetyError('BUDGET_REQUIRED')
            with sqlite3.connect(f'file:{args.market_db}?mode=ro', uri=True) as conn:
                result = preview(conn, args.db, os.environ.get('TOSS_ACCOUNT_SEQ',''), args.budget_usd)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except SafetyError as error:
        # Only stable local codes; no raw HTTP bodies or credentials.
        print(json.dumps({'status':'BLOCKED','code':public_error_code(error)}))
        return 2
    except Exception:
        print(json.dumps({'status':'BLOCKED','code':'LOCAL_DATA_OR_IO_ERROR'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
