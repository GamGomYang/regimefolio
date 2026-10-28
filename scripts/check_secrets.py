#!/usr/bin/env python3
"""Offline heuristic secret check. Findings never include matching values."""
import argparse
import fnmatch
from pathlib import Path
import re
import subprocess

PATTERNS = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b'),
    'aws-key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'jwt': re.compile(r'\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b'),
    'literal-credential': re.compile(r'''(?im)\b(?:TOSS_CLIENT_ID|TOSS_CLIENT_SECRET|TOSS_ACCOUNT_SEQ|api_key|client_secret|access_token|refresh_token|password)\b["']?\s*[:=]\s*["']([A-Za-z0-9_./+=-]{12,})["']'''),
    'dotenv-credential': re.compile(r'(?m)^\s*(?:export\s+)?(?:TOSS_CLIENT_ID|TOSS_CLIENT_SECRET|TOSS_ACCOUNT_SEQ|[A-Z_]*(?:API_KEY|SECRET|TOKEN|PASSWORD))\s*=\s*([A-Za-z0-9_./+=-]{8,})\s*$'),
}
SENSITIVE = ('.env', '.env.*', '*.env', '.envrc*', '*.pem', '*.key', '*.p12', '*.pfx',
             'credentials*.json', 'client_secret*.json', 'service-account*.json',
             'service_account*.json', 'secrets.toml', 'secrets.json', 'secrets.yaml', '*.db', '*.sqlite*', '*.har')


def findings(path, data):
    result = []
    name = Path(path).name
    if name != '.env.example' and any(fnmatch.fnmatch(name, p) for p in SENSITIVE):
        result.append((0, 'sensitive-filename'))
    for number, line in enumerate(data.decode('utf-8', errors='replace').splitlines(), 1):
        for rule, pattern in PATTERNS.items():
            for match in pattern.finditer(line):
                value = match.group(1) if match.lastindex else match.group()
                if rule.endswith('credential') and (value.lower().startswith(('fake_', 'example_', 'dummy_', 'your_')) or value.startswith('${')):
                    continue
                result.append((number, rule))
    return result


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--staged', action='store_true')
    modes.add_argument('--history', action='store_true')
    args = parser.parse_args()
    root = Path(git('rev-parse', '--show-toplevel').decode().strip())
    entries = []
    if args.history:
        for line in git('rev-list', '--objects', '--all').decode().splitlines():
            oid, _, path = line.partition(' ')
            if path and git('cat-file', '-t', oid).strip() == b'blob':
                entries.append((oid[:12] + ':' + path, git('cat-file', 'blob', oid)))
    else:
        command = ('ls-files', '--cached', '-z') if args.staged else ('ls-files', '--cached', '--others', '--exclude-standard', '-z')
        for raw in sorted(set(git(*command).split(b'\0')) - {b''}):
            path = raw.decode()
            if args.staged:
                entries.append((path, git('show', ':' + path)))
            elif (root / path).is_file() and not (root / path).is_symlink():
                entries.append((path, (root / path).read_bytes()))
    count = 0
    for path, data in entries:
        for line, rule in findings(path, data):
            print(f'{path}:{line}: {rule}')
            count += 1
    print(f'Checked {len(entries)} files/blobs; findings={count} (heuristic scan).')
    return 1 if count else 0


if __name__ == '__main__':
    raise SystemExit(main())
