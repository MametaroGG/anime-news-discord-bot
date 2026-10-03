"""Durable journal. A remote checkpoint MUST precede every external mutation."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .schema import ID, Invalid, read_json, timestamp

STATES = {'ready', 'pending', 'sent', 'uncertain', 'blocked', 'legacy_hold', 'expired'}


class StateError(RuntimeError):
    pass


def load(path):
    # Missing state is an error, not a reset. A committed empty ledger ships with the app.
    ledger = read_json(path, 64 * 1024 * 1024)
    if not isinstance(ledger, dict) or set(ledger) != {'schema_version', 'events', 'keys', 'audit'} or ledger['schema_version'] != 1:
        raise StateError('invalid ledger structure')
    if not isinstance(ledger['events'], dict) or not isinstance(ledger['keys'], dict) or not isinstance(ledger['audit'], list):
        raise StateError('invalid ledger mappings')
    for event_id, row in ledger['events'].items():
        if not isinstance(event_id, str) or not ID.fullmatch(event_id):
            raise StateError('invalid event ID in ledger')
        if not isinstance(row, dict) or set(row) != {'fingerprint', 'keys', 'queued_at', 'published_at', 'deliveries'}:
            raise StateError('invalid event state')
        if not isinstance(row['fingerprint'], str) or not re.fullmatch(r'[a-f0-9]{64}', row['fingerprint']):
            raise StateError('invalid event fingerprint')
        if not isinstance(row['keys'], list) or not row['keys'] or not isinstance(row['deliveries'], dict) or not row['deliveries']:
            raise StateError('invalid event keys or deliveries')
        timestamp(row['queued_at'])
        timestamp(row['published_at'])
        for key in row['keys']:
            if not isinstance(key, str):
                raise StateError('invalid dedupe key')
            if ledger['keys'].get(key) != event_id:
                raise StateError('ledger dedupe index inconsistent')
        for destination, delivery in row['deliveries'].items():
            if destination not in {'discord', 'x'} or not isinstance(delivery, dict) or delivery.get('status') not in STATES:
                raise StateError('invalid delivery state')
            if type(delivery.get('attempts')) is not int or delivery['attempts'] < 0:
                raise StateError('invalid attempt count')
            timestamp(delivery.get('updated_at'))
            if delivery.get('next_attempt_at'):
                timestamp(delivery['next_attempt_at'])
            if delivery['status'] == 'sent' and not re.fullmatch(r'\d+', str(delivery.get('remote_id', ''))):
                raise StateError('sent delivery missing remote ID')
    for key, owner in ledger['keys'].items():
        if owner not in ledger['events'] or key not in ledger['events'][owner]['keys']:
            raise StateError('orphaned dedupe index')
    return ledger


def atomic_write(path, value):
    path = Path(path)
    if path.is_symlink():
        raise StateError('ledger may not be a symlink')
    temporary = path.with_suffix('.tmp')
    if temporary.is_symlink():
        raise StateError('temporary ledger may not be a symlink')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(path):
    """Additional local guard; GitHub concurrency and remote CAS guard runners."""
    with Path(path).open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StateError('another publisher holds the local lock') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class GitCheckpoint:
    def __init__(self, root, branch, relative='data/delivery-state.json'):
        self.root = Path(root).resolve()
        self.relative = relative
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', branch) or '..' in branch or branch.endswith('/'):
            raise StateError('invalid state branch')
        self.branch = branch
        self.path = self.root / relative
        self.git('check-ref-format', 'refs/heads/' + branch)
        if self.git('status', '--porcelain', '--untracked-files=no'):
            raise StateError('live checkout must start clean')
        self.assert_remote_current()

    def git(self, *args):
        try:
            result = subprocess.run(['git', *args], cwd=self.root, check=True,
                                    capture_output=True, text=True, timeout=60)
            return result.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            # Never print git stderr: remote/config URLs could contain credentials.
            raise StateError('git checkpoint failed; stop and inspect durable remote ledger') from None

    def assert_remote_current(self):
        self.git('fetch', '--no-tags', 'origin', 'refs/heads/' + self.branch)
        if self.git('rev-parse', 'HEAD') != self.git('rev-parse', 'FETCH_HEAD'):
            raise StateError('remote branch changed; restart with fresh checkout, never rebase a journal')

    def __call__(self, ledger):
        self.assert_remote_current()
        if self.git('diff', '--cached', '--name-only'):
            raise StateError('unexpected staged changes')
        atomic_write(self.path, ledger)
        changed = self.git('diff', '--name-only').splitlines()
        if changed != [self.relative]:
            raise StateError('checkpoint must change only the delivery ledger')
        self.git('add', '--', self.relative)
        self.git('-c', 'user.name=github-actions[bot]',
                 '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                 'commit', '-m', 'chore: checkpoint delivery ledger [skip ci]')
        # Ordinary fast-forward push is a compare-and-swap. Never force/rebase/retry.
        self.git('push', 'origin', 'HEAD:refs/heads/' + self.branch)
        self.assert_remote_current()


def legacy_match(event, legacy):
    """Old 'seen' means observed, NOT necessarily delivered. Hold, never mark sent."""
    for source in event['sources']:
        p = urlsplit(source['url'])
        query = urlencode([(k, v) for k, v in parse_qsl(p.query)
                           if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')])
        old_url = urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/', query, ''))
        if hashlib.sha256(old_url.encode()).hexdigest() in legacy['seen']:
            return True
    labels = {'new_adaptation': 'TVアニメ化', 'sequel': '続編制作', 'film_production': '劇場版制作'}
    work = re.sub(r'[\s　・･!！?？:：～〜\-]+', '', event['work_title']).casefold()
    for kind in event['announcement_kinds']:
        if kind in labels:
            key = hashlib.sha256((labels[kind] + ':' + work).encode()).hexdigest()
            if key in legacy['stories']:
                return True
    return False


def load_legacy(path):
    legacy = read_json(path, 64 * 1024 * 1024)
    if not isinstance(legacy, dict) or type(legacy.get('initialized')) is not bool or not isinstance(legacy.get('seen'), dict) or not isinstance(legacy.get('stories'), dict):
        raise StateError('legacy history missing or malformed; do not reset it')
    return legacy
