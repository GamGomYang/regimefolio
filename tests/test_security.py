import importlib.util
import os
from pathlib import Path
import subprocess

import pytest
from broker.security import private_database, public_error_code
from safety import SafetyError

SCANNER = Path(__file__).resolve().parents[1] / 'scripts/check_secrets.py'
spec = importlib.util.spec_from_file_location('secret_check', SCANNER)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


def test_exception_body_is_not_public():
    assert public_error_code(SafetyError('response contains ' + 'sensitive-value')) == 'LOCAL_DATA_OR_IO_ERROR'
    assert public_error_code(SafetyError('ACCOUNT_HALTED')) == 'ACCOUNT_HALTED'


def test_database_permissions_and_symlink(tmp_path):
    db = tmp_path / 'account.db'
    db.write_bytes(b'')
    db.chmod(0o644)
    private_database(db)
    assert db.stat().st_mode & 0o777 == 0o600
    link = tmp_path / 'link.db'
    link.symlink_to(db)
    with pytest.raises(SafetyError):
        private_database(link)
    with pytest.raises(SafetyError):
        private_database(db, exclusive=True)


def test_scanner_detects_without_printing_value():
    token = 'gh' + 'p_' + 'a' * 36
    assert scanner.findings('app.py', token.encode()) == [(1, 'github-token')]
    assert scanner.findings('.env', b'') == [(0, 'sensitive-filename')]
    assert not scanner.findings('.env.example', b'TOSS_CLIENT_SECRET=\n')


def test_staged_scan_uses_index(tmp_path):
    def git(*args):
        subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True)
    git('init')
    token = 'gh' + 'p_' + 'b' * 36
    source = tmp_path / 'app.py'
    source.write_text('value = "' + token + '"\n')
    git('add', 'app.py')
    source.write_text('value = ""\n')
    result = subprocess.run(['python3', str(SCANNER), '--staged'], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert 'github-token' in result.stdout
    assert token not in result.stdout + result.stderr
    git('add', 'app.py')
    assert subprocess.run(['python3', str(SCANNER), '--staged'], cwd=tmp_path, capture_output=True).returncode == 0


def test_gitignore_protects_runtime_files():
    root = SCANNER.parents[1]
    paths = ['.env', '.env.production', 'local.pem', 'credentials-prod.json', 'baseline-real.json', 'backup/account.db', 'debug.har']
    for path in paths:
        assert subprocess.run(['git', 'check-ignore', '--no-index', '-q', path], cwd=root).returncode == 0
    for path in ['.env.example', 'broker/security.py', 'docs/security-check.md']:
        assert subprocess.run(['git', 'check-ignore', '--no-index', '-q', path], cwd=root).returncode == 1


def test_engine_ledger_and_backup_are_private(tmp_path):
    from broker.engine import Engine
    from broker.ledger import Ledger
    engine_path = tmp_path / 'engine.db'
    ledger_path = tmp_path / 'ledger.db'
    backup_path = tmp_path / 'backup.db'
    engine = Engine(engine_path)
    Ledger(ledger_path)
    engine.backup(backup_path)
    for path in [engine_path, ledger_path, backup_path]:
        assert path.stat().st_mode & 0o777 == 0o600
