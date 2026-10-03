"""One fixed, explicitly manual Discord format check, separate from news delivery.

The committed ledger is never initialized or reset here. Any reservation, including
a rate limit or a failed/uncertain result, permanently consumes this smoke ID.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from .schema import Invalid, digest, iso, read_json, timestamp
from .state import GitCheckpoint, StateError, locked
from .transport import ConfigurationError, DiscordClient

ROOT = Path(__file__).resolve().parent.parent
LEDGER_RELATIVE = 'data/discord-smoke-state.json'
SMOKE_ID = 'discord-format-test-20261003-v1'
CONFIRMATION = 'send_one_format_test'
SOURCE_URL = 'https://www.aniplex.co.jp/news/detail/?id=70322'
VIDEO_URL = 'https://www.youtube.com/watch?v=UmVTrrDVYV4'
TITLE = '『劇場版 魔法少女まどか☆マギカ〈ワルプルギスの廻天〉』予告第3弾'


def fixed_payload():
    """Return a fresh copy of the only payload this command can send."""
    return {
        'content': (
            '【動作確認】Discord投稿フォーマット\n'
            '画像・動画リンクとX手動コピー欄の表示確認です。ニュース速報ではありません\n\n'
            '公式YouTube（アニプレックス / @aniplex）\n' + VIDEO_URL
        ),
        'allowed_mentions': {'parse': []},
        'embeds': [{
            'title': TITLE,
            'description': '過去の公式PVを使った表示テストです。新着ニュースとしての配信ではありません。',
            'color': 0x5865F2,
            'fields': [
                {'name': '用途', 'value': '動作確認のみ（ニュース速報ではありません）', 'inline': False},
                {'name': '公式記事の日付（過去の情報）', 'value': '2026年4月30日（公開時刻は不明）', 'inline': False},
                {'name': '出典（アニプレックス公式）', 'value': SOURCE_URL, 'inline': False},
                {'name': '画像について', 'value': '画像は許諾未確認のため添付していません。公式リンク先で確認できます。', 'inline': False},
                {'name': 'X投稿用（手動コピー・テスト例）', 'value': (
                    '```text\n【表示テスト・過去の情報】\n' + TITLE + '\n'
                    '公式記事：2026年4月30日。ニュース速報ではありません。\n' + SOURCE_URL + '\n```'
                ), 'inline': False},
            ],
        }],
    }


REASONS = {
    'pending': {'reserved_before_post'},
    'sent': {'confirmed'},
    'blocked': {
        'rate_limited_no_retry', 'rate_limit_requires_review', 'redirect_refused',
        'unexpected_outcome',
        *('request_rejected_' + str(code) for code in (400, 401, 403, 404, 405, 410, 413, 415, 422)),
    },
    'uncertain': {
        'transport_error_after_possible_send', 'success_response_without_message_id',
        'success_response_without_channel_id', 'invalid_success_response',
        'ambiguous_http_response', 'unexpected_send_failure',
    },
}
ROW_FIELDS = {
    'payload_sha256', 'status', 'reason', 'attempts', 'attempted_at', 'updated_at',
    'message_id', 'channel_id', 'guild_id',
}


def _numeric_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9]{1,25}', value) is not None


def validate_ledger(ledger):
    if (not isinstance(ledger, dict) or set(ledger) != {'schema_version', 'tests'} or
            type(ledger['schema_version']) is not int or ledger['schema_version'] != 1 or
            not isinstance(ledger['tests'], dict) or set(ledger['tests']) - {SMOKE_ID}):
        raise StateError('invalid smoke ledger structure; never reset the ledger')
    for row in ledger['tests'].values():
        if not isinstance(row, dict) or set(row) != ROW_FIELDS:
            raise StateError('invalid smoke attempt structure')
        if row['payload_sha256'] != digest(fixed_payload()):
            raise StateError('smoke payload differs from the reserved payload; do not resend')
        if (not isinstance(row['status'], str) or row['status'] not in REASONS or
                not isinstance(row['reason'], str) or row['reason'] not in REASONS[row['status']]):
            raise StateError('invalid smoke status or reason')
        if type(row['attempts']) is not int or row['attempts'] != 1:
            raise StateError('a smoke ID permits exactly one attempt')
        if timestamp(row['updated_at']) < timestamp(row['attempted_at']):
            raise StateError('smoke timestamps are out of order')
        for field in ('message_id', 'channel_id', 'guild_id'):
            if row[field] is not None and not _numeric_id(row[field]):
                raise StateError('smoke receipt IDs must be numeric strings')
        if row['status'] == 'sent' and (row['message_id'] is None or row['channel_id'] is None):
            raise StateError('confirmed smoke receipt requires message and channel IDs')
        if row['status'] in {'pending', 'blocked'} and any(row[k] is not None for k in ('message_id', 'channel_id', 'guild_id')):
            raise StateError('unconfirmed smoke receipt cannot contain IDs')
        if row['guild_id'] is not None and (row['message_id'] is None or row['channel_id'] is None):
            raise StateError('smoke guild ID requires a confirmed message and channel')
    return ledger


def load_ledger(path):
    return validate_ledger(read_json(path, 16 * 1024))


def require_manual_send(env):
    if env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        raise ConfigurationError('smoke send requires a manual GitHub Actions dispatch')
    if env.get('DISCORD_SMOKE_CONFIRMATION') != CONFIRMATION:
        raise ConfigurationError('smoke send requires the exact send_one_format_test confirmation')
    branch = env.get('DISCORD_SMOKE_BRANCH', '')
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', branch) or '..' in branch or branch.endswith('/') or
            env.get('GITHUB_REF') != 'refs/heads/' + branch):
        raise ConfigurationError('smoke send requires the configured default branch')
    return branch


def summary(ledger, *, dry_run, existing):
    """Only fixed labels and verified numeric receipts, never message bodies."""
    validate_ledger(ledger)
    row = ledger['tests'].get(SMOKE_ID)
    result = {
        'smoke_id': SMOKE_ID, 'dry_run': dry_run, 'existing_attempt': existing,
        'status': row['status'] if row else 'preview',
        'reason': row['reason'] if row else 'no_attempt',
        'attempts': row['attempts'] if row else 0,
        'resend_allowed': False,
    }
    if row:
        for field in ('message_id', 'channel_id', 'guild_id'):
            if row[field] is not None:
                result[field] = row[field]
        if row['status'] == 'sent' and row['guild_id'] is not None:
            result['permalink'] = 'https://discord.com/channels/{}/{}/{}'.format(
                row['guild_id'], row['channel_id'], row['message_id'])
    return result


class ResultCheckpointError(StateError):
    def __init__(self, observed):
        super().__init__('smoke result checkpoint failed; inspect the remote ledger and do not resend')
        self.observed = dict(observed, durable_result='unverified')


def _record_outcome(row, outcome):
    # Never propagate an arbitrary exception, body, URL or reason into public state.
    status, reason = outcome.status, outcome.reason
    if status == 'sent':
        if not _numeric_id(outcome.remote_id):
            row.update(status='uncertain', reason='invalid_success_response')
        elif not _numeric_id(outcome.channel_id):
            row.update(status='uncertain', reason='success_response_without_channel_id', message_id=outcome.remote_id)
        else:
            row.update(status='sent', reason='confirmed', message_id=outcome.remote_id,
                       channel_id=outcome.channel_id,
                       guild_id=outcome.guild_id if _numeric_id(outcome.guild_id) else None)
    elif status == 'ready' and reason == 'rate_limited':
        row.update(status='blocked', reason='rate_limited_no_retry')
    elif status == 'blocked' and reason in REASONS['blocked']:
        row.update(status='blocked', reason=reason)
    elif status == 'uncertain' and reason in REASONS['uncertain']:
        row.update(status='uncertain', reason=reason)
    elif status == 'uncertain' and isinstance(reason, str) and re.fullmatch(r'ambiguous_http_[0-9]{3}', reason):
        row.update(status='uncertain', reason='ambiguous_http_response')
    else:
        row.update(status='uncertain', reason='unexpected_send_failure')


def send_once(ledger, checkpoint, client_factory, now, env, clock=None):
    """Reserve remotely, POST once, persist the result; no retries in any state."""
    validate_ledger(ledger)
    require_manual_send(env)
    if SMOKE_ID in ledger['tests']:
        return summary(ledger, dry_run=False, existing=True)
    if checkpoint is None:
        raise StateError('smoke send requires a durable remote checkpoint')
    client = client_factory()  # Validate the existing webhook before reserving.
    row = {
        'payload_sha256': digest(fixed_payload()), 'status': 'pending',
        'reason': 'reserved_before_post', 'attempts': 1,
        'attempted_at': iso(now), 'updated_at': iso(now),
        'message_id': None, 'channel_id': None, 'guild_id': None,
    }
    ledger['tests'][SMOKE_ID] = row
    checkpoint(ledger)  # Stop before POST if push or the following remote check fails.
    try:
        _record_outcome(row, client.send(fixed_payload(), now))
    except Exception:
        row.update(status='uncertain', reason='unexpected_send_failure',
                   message_id=None, channel_id=None, guild_id=None)
    row['updated_at'] = iso(clock() if clock else now)
    validate_ledger(ledger)
    observed = summary(ledger, dry_run=False, existing=False)
    try:
        checkpoint(ledger)
    except Exception:
        # A confirmed HTTP receipt is useful, but a failed push is never a retry cue.
        raise ResultCheckpointError(observed) from None
    return observed


def main(argv=None):
    parser = argparse.ArgumentParser(description='Preview or explicitly send one fixed Discord format check')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='default; no secrets, API calls or state writes')
    mode.add_argument('--send-one', action='store_true', help='requires manual Actions send_one_format_test confirmation')
    args = parser.parse_args(argv)
    try:
        with locked(ROOT / '.delivery.lock') if args.send_one else nullcontext():
            ledger = load_ledger(ROOT / LEDGER_RELATIVE)
            if not args.send_one:
                result = summary(ledger, dry_run=True, existing=bool(ledger['tests']))
            else:
                branch = require_manual_send(os.environ)
                checkpoint = None if ledger['tests'] else GitCheckpoint(ROOT, branch, LEDGER_RELATIVE)
                result = send_once(
                    ledger, checkpoint,
                    lambda: DiscordClient(os.environ.get('DISCORD_WEBHOOK_URL', '')),
                    datetime.now(timezone.utc), os.environ,
                    clock=lambda: datetime.now(timezone.utc),
                )
            print(json.dumps(result, sort_keys=True))
            return 0 if not args.send_one or result['status'] == 'sent' else 1
    except ResultCheckpointError as exc:
        print('STOP: ' + str(exc), file=sys.stderr)
        print(json.dumps(exc.observed, sort_keys=True))
        return 2
    except (Invalid, StateError, ConfigurationError) as exc:
        print('STOP: ' + str(exc), file=sys.stderr)
        return 2
    except Exception:
        print('STOP: unexpected smoke failure; inspect the remote ledger and do not resend', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
