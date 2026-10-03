import contextlib
import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from helpers import NOW
from news_delivery import smoke
from news_delivery.schema import Invalid, digest
from news_delivery.state import GitCheckpoint, StateError, atomic_write, locked
from news_delivery.transport import ConfigurationError, DiscordClient, Outcome, interpret

WEBHOOK = 'https://discord.com/api/webhooks/123/fake-smoke-test-token'
ENV = {
    'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
    'GITHUB_REF': 'refs/heads/main', 'DISCORD_SMOKE_BRANCH': 'main',
    'DISCORD_SMOKE_CONFIRMATION': 'send_one_format_test',
    'DISCORD_WEBHOOK_URL': WEBHOOK,
    'DELIVERY_LIVE_ENABLED': 'false',
}


def empty():
    return {'schema_version': 1, 'tests': {}}


def response(code=200, body=None, headers=None):
    value = Mock(status_code=code, headers=headers or {})
    value.json.return_value = body or {}
    return value


def git(path, *args):
    return subprocess.run(['git', *args], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.ledger = empty()
        self.checkpoints = []
        self.client = Mock()
        self.client.send.return_value = Outcome('sent', 'confirmed', '345', channel_id='234', guild_id='123')
        self.factory = Mock(return_value=self.client)

    def checkpoint(self, ledger):
        smoke.validate_ledger(ledger)
        self.checkpoints.append(copy.deepcopy(ledger))

    def send(self, checkpoint=None):
        return smoke.send_once(self.ledger, checkpoint or self.checkpoint, self.factory, NOW, ENV)

    def test_fixed_payload_is_explicit_past_official_format_example(self):
        payload = smoke.fixed_payload()
        self.assertEqual(set(payload), {'content', 'embeds', 'allowed_mentions'})
        self.assertEqual(payload['allowed_mentions'], {'parse': []})
        self.assertIn('【動作確認】Discord投稿フォーマット', payload['content'])
        self.assertIn('画像・動画リンクとX手動コピー欄の表示確認です。ニュース速報ではありません', payload['content'])
        self.assertIn('\nhttps://www.youtube.com/watch?v=UmVTrrDVYV4', payload['content'])
        self.assertEqual(len(payload['embeds']), 1)
        embed = payload['embeds'][0]
        self.assertEqual(embed['title'], '『劇場版 魔法少女まどか☆マギカ〈ワルプルギスの廻天〉』予告第3弾')
        for forbidden in ('image', 'thumbnail', 'video', 'timestamp'):
            self.assertNotIn(forbidden, embed)
        fields = {row['name']: row['value'] for row in embed['fields']}
        self.assertEqual(fields['公式記事の日付（過去の情報）'], '2026年4月30日（公開時刻は不明）')
        self.assertEqual(fields['出典（アニプレックス公式）'], smoke.SOURCE_URL)
        self.assertIn('許諾未確認のため添付していません', fields['画像について'])
        self.assertIn(smoke.SOURCE_URL, fields['X投稿用（手動コピー・テスト例）'])
        self.assertIn('表示テスト・過去の情報', fields['X投稿用（手動コピー・テスト例）'])
        self.assertNotIn(smoke.VIDEO_URL, json.dumps(embed))
        self.assertNotIn('attachments', payload)
        payload['embeds'][0]['title'] = 'mutated'
        self.assertEqual(smoke.fixed_payload()['embeds'][0]['title'], smoke.TITLE)

    def test_payload_fits_discord_limits(self):
        payload = smoke.fixed_payload()
        size = lambda value: len(value.encode('utf-16-le')) // 2
        embed = payload['embeds'][0]
        self.assertLessEqual(size(payload['content']), 2000)
        self.assertLessEqual(size(embed['title']), 256)
        self.assertLessEqual(size(embed['description']), 4096)
        total = size(embed['title']) + size(embed['description'])
        for field in embed['fields']:
            self.assertLessEqual(size(field['name']), 256)
            self.assertLessEqual(size(field['value']), 1024)
            total += size(field['name']) + size(field['value'])
        self.assertLessEqual(total, 6000)

    def test_manual_guards_refuse_before_client_or_checkpoint(self):
        for key, value in (
            ('GITHUB_ACTIONS', ''), ('GITHUB_ACTIONS', 'false'),
            ('GITHUB_EVENT_NAME', 'schedule'), ('GITHUB_EVENT_NAME', 'push'),
            ('DISCORD_SMOKE_CONFIRMATION', 'preview_only'), ('DISCORD_SMOKE_CONFIRMATION', 'SEND_ONE_FORMAT_TEST'),
            ('DISCORD_SMOKE_CONFIRMATION', ''), ('DISCORD_SMOKE_BRANCH', ''),
            ('DISCORD_SMOKE_BRANCH', '../main'), ('GITHUB_REF', 'refs/heads/feature'),
        ):
            env = dict(ENV, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ConfigurationError):
                smoke.send_once(self.ledger, self.checkpoint, self.factory, NOW, env)
        self.factory.assert_not_called()
        self.assertEqual(self.ledger, empty())
        self.assertEqual(self.checkpoints, [])

    def test_normal_live_enablement_does_not_authorize_smoke(self):
        env = dict(ENV, DELIVERY_LIVE_ENABLED='true', DISCORD_SMOKE_CONFIRMATION='preview_only')
        with self.assertRaises(ConfigurationError):
            smoke.send_once(self.ledger, self.checkpoint, self.factory, NOW, env)
        self.factory.assert_not_called()

    def test_pending_is_durable_before_post_and_receipt_after(self):
        def send(payload, now):
            self.assertEqual(self.checkpoints[-1]['tests'][smoke.SMOKE_ID]['status'], 'pending')
            self.assertEqual(payload, smoke.fixed_payload())
            self.assertEqual(now, NOW)
            return Outcome('sent', 'confirmed', '345', channel_id='234', guild_id='123')
        self.client.send.side_effect = send
        result = self.send()
        self.assertEqual([state['tests'][smoke.SMOKE_ID]['status'] for state in self.checkpoints], ['pending', 'sent'])
        self.assertEqual(result['message_id'], '345')
        self.assertEqual(result['channel_id'], '234')
        self.assertEqual(result['permalink'], 'https://discord.com/channels/123/234/345')
        self.assertEqual(self.ledger['tests'][smoke.SMOKE_ID]['payload_sha256'], digest(smoke.fixed_payload()))

    def test_every_existing_state_returns_without_factory_checkpoint_or_send(self):
        for outcome in (
            Outcome('sent', 'confirmed', '345', channel_id='234'),
            Outcome('uncertain', 'transport_error_after_possible_send'),
            Outcome('blocked', 'request_rejected_403'),
            Outcome('ready', 'rate_limited', next_attempt_at='2027-01-01T00:00:00Z'),
        ):
            with self.subTest(status=outcome.status):
                self.ledger = empty()
                self.client.send.return_value = outcome
                self.send()
                frozen = copy.deepcopy(self.ledger)
                result = smoke.send_once(self.ledger, None, Mock(side_effect=AssertionError('called')), NOW, ENV)
                self.assertTrue(result['existing_attempt'])
                self.assertFalse(result['resend_allowed'])
                self.assertEqual(self.ledger, frozen)
        pending = self.checkpoints[-2]
        result = smoke.send_once(pending, None, Mock(side_effect=AssertionError('called')), NOW, ENV)
        self.assertEqual(result['status'], 'pending')

    def test_rate_limit_is_permanent_block_without_retry_metadata(self):
        self.client.send.return_value = Outcome('ready', 'rate_limited', next_attempt_at='2026-10-03T03:55:00Z')
        self.assertEqual(self.send()['reason'], 'rate_limited_no_retry')
        self.assertEqual(self.ledger['tests'][smoke.SMOKE_ID]['status'], 'blocked')
        self.assertNotIn('next_attempt_at', self.ledger['tests'][smoke.SMOKE_ID])
        self.send()
        self.client.send.assert_called_once()

    def test_missing_channel_is_uncertain_and_no_permalink_is_guessed(self):
        self.client.send.return_value = Outcome('sent', 'confirmed', '345')
        result = self.send()
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['message_id'], '345')
        self.assertNotIn('channel_id', result)
        self.assertNotIn('permalink', result)
        self.send()
        self.client.send.assert_called_once()

    def test_missing_guild_has_confirmed_ids_without_permalink(self):
        self.client.send.return_value = Outcome('sent', 'confirmed', '345', channel_id='234')
        result = self.send()
        self.assertEqual(result['status'], 'sent')
        self.assertNotIn('permalink', result)
        self.assertNotIn('guild_id', result)

    def test_checkpoint_is_mandatory(self):
        with self.assertRaises(StateError):
            smoke.send_once(self.ledger, None, self.factory, NOW, ENV)
        self.factory.assert_not_called()

    def test_failed_pending_checkpoint_never_posts(self):
        with self.assertRaises(StateError):
            self.send(checkpoint=Mock(side_effect=StateError('remote changed')))
        self.client.send.assert_not_called()
        self.assertEqual(self.ledger['tests'][smoke.SMOKE_ID]['status'], 'pending')

    def test_invalid_webhook_stops_without_reservation(self):
        self.factory.side_effect = ConfigurationError('invalid webhook; secret hidden')
        with self.assertRaises(ConfigurationError):
            self.send()
        self.assertEqual(self.ledger, empty())
        self.assertEqual(self.checkpoints, [])

    def test_unexpected_send_error_is_sanitized_and_never_retried(self):
        self.client.send.side_effect = RuntimeError('secret body ' + WEBHOOK)
        result = self.send()
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['reason'], 'unexpected_send_failure')
        self.assertNotIn('fake-smoke-test-token', json.dumps(self.ledger))
        self.send()
        self.client.send.assert_called_once()

    def test_process_interruption_leaves_durable_pending_and_no_retry(self):
        self.client.send.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.send()
        self.ledger = copy.deepcopy(self.checkpoints[-1])
        self.assertEqual(self.send()['status'], 'pending')
        self.client.send.assert_called_once()

    def test_result_checkpoint_failure_reports_receipt_but_never_retries(self):
        def checkpoint(ledger):
            if self.checkpoints:
                raise RuntimeError('secret git URL ' + WEBHOOK)
            self.checkpoint(ledger)
        with self.assertRaises(smoke.ResultCheckpointError) as caught:
            self.send(checkpoint=checkpoint)
        self.assertEqual(caught.exception.observed['message_id'], '345')
        self.assertEqual(caught.exception.observed['durable_result'], 'unverified')
        self.assertNotIn(WEBHOOK, str(caught.exception))
        self.ledger = copy.deepcopy(self.checkpoints[-1])
        self.assertEqual(self.send()['status'], 'pending')
        self.client.send.assert_called_once()

    def test_mock_http_uses_wait_true_native_link_and_no_retries(self):
        session = Mock()
        session.request.return_value = response(body={'id': '345', 'channel_id': '234', 'guild_id': '123'})
        self.factory.return_value = DiscordClient(WEBHOOK, session)
        result = self.send()
        self.assertEqual(result['status'], 'sent')
        session.request.assert_called_once()
        args, kwargs = session.request.call_args
        self.assertEqual(args, ('POST', WEBHOOK + '?wait=true'))
        self.assertEqual(kwargs['json'], smoke.fixed_payload())
        self.assertEqual(kwargs['json']['allowed_mentions'], {'parse': []})
        self.assertFalse(kwargs['allow_redirects'])
        self.assertFalse(session.trust_env)
        self.send()
        session.request.assert_called_once()

    def test_mock_http_429_timeout_5xx_bad_ids_are_terminal(self):
        cases = (
            (response(429, {'retry_after': 15}), 'blocked'),
            (response(429, {'retry_after': float('inf')}), 'blocked'),
            (response(403, {'message': WEBHOOK}), 'blocked'),
            (response(302), 'blocked'),
            (response(500, {'message': WEBHOOK}), 'uncertain'),
            (response(204), 'uncertain'),
            (response(200, {'id': '345', 'channel_id': 'not-numeric'}), 'uncertain'),
            (response(200, {'id': '１２３', 'channel_id': '234'}), 'uncertain'),
            (requests.Timeout(WEBHOOK), 'uncertain'),
        )
        for remote, expected in cases:
            with self.subTest(remote=type(remote).__name__, expected=expected):
                self.ledger = empty()
                session = Mock()
                if isinstance(remote, BaseException):
                    session.request.side_effect = remote
                else:
                    session.request.return_value = remote
                self.factory.return_value = DiscordClient(WEBHOOK, session)
                self.assertEqual(self.send()['status'], expected)
                self.send()
                session.request.assert_called_once()
                self.assertNotIn('fake-smoke-test-token', json.dumps(self.ledger))

    def test_transport_only_exposes_numeric_discord_receipt_ids(self):
        for bad in ('@me', '../bad', '１２３', 234, True, '9' * 26):
            with self.subTest(bad=bad):
                outcome = interpret(response(body={'id': '345', 'channel_id': bad, 'guild_id': bad}), 'discord', NOW)
                self.assertIsNone(outcome.channel_id)
                self.assertIsNone(outcome.guild_id)
        outcome = interpret(response(body={'data': {'id': '345'}, 'channel_id': '234', 'guild_id': '123'}), 'x', NOW)
        self.assertIsNone(outcome.channel_id)
        self.assertIsNone(outcome.guild_id)

    def test_strict_missing_corrupt_and_duplicate_key_ledgers_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ledger.json'
            for body in (None, '', '{}', 'null', '{"schema_version":1,"tests":{},"tests":{}}'):
                if body is not None:
                    path.write_text(body)
                with self.subTest(body=body), self.assertRaises((Invalid, StateError)):
                    smoke.load_ledger(path)
            atomic_write(path, empty())
            self.assertEqual(smoke.load_ledger(path), empty())
            link = Path(folder) / 'link.json'
            link.symlink_to(path)
            with self.assertRaises(Invalid):
                smoke.load_ledger(link)

    def test_strict_ledger_rejects_unknown_keys_types_ids_and_payload_changes(self):
        self.send()
        good = copy.deepcopy(self.ledger)
        bad_values = []
        for key, value in (
            ('status', 'ready'), ('status', []), ('reason', []), ('reason', WEBHOOK),
            ('attempts', 0), ('attempts', 2), ('attempts', True), ('message_id', None),
            ('message_id', 'abc'), ('channel_id', 123), ('guild_id', '@me'),
            ('payload_sha256', '0' * 64), ('updated_at', 'yesterday'),
            ('updated_at', '2000-01-01T00:00:00Z'),
        ):
            value_ledger = copy.deepcopy(good)
            value_ledger['tests'][smoke.SMOKE_ID][key] = value
            bad_values.append(value_ledger)
        for value in (True, 2, '1'):
            bad_values.append(dict(good, schema_version=value))
        bad_values.extend((dict(good, extra=True), dict(good, tests={'arbitrary-id': {}}), dict(good, tests=[])))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ledger.json'
            for value in bad_values:
                atomic_write(path, value)
                with self.subTest(value=value), self.assertRaises((Invalid, StateError)):
                    smoke.load_ledger(path)

    def test_cli_default_and_dry_run_do_not_read_secrets_or_write_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            atomic_write(root / smoke.LEDGER_RELATIVE, empty())
            before = (root / smoke.LEDGER_RELATIVE).read_bytes()
            for argv in ([], ['--dry-run']):
                with patch.object(smoke, 'ROOT', root), patch.object(smoke, 'GitCheckpoint') as cp, \
                        patch.object(smoke, 'DiscordClient') as client, patch.object(smoke.os, 'environ', {}), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(smoke.main(argv), 0)
                cp.assert_not_called()
                client.assert_not_called()
                result = json.loads(out.getvalue())
                self.assertEqual(result['status'], 'preview')
                self.assertNotIn('content', out.getvalue())
                self.assertNotIn(smoke.SOURCE_URL, out.getvalue())
                self.assertEqual((root / smoke.LEDGER_RELATIVE).read_bytes(), before)
                self.assertFalse((root / '.delivery.lock').exists())

    def test_cli_existing_attempt_needs_no_secret_or_git_and_shares_lock(self):
        self.send()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            atomic_write(root / smoke.LEDGER_RELATIVE, self.ledger)
            env = {key: value for key, value in ENV.items() if key != 'DISCORD_WEBHOOK_URL'}
            with patch.object(smoke, 'ROOT', root), patch.object(smoke, 'GitCheckpoint') as cp, \
                    patch.object(smoke, 'DiscordClient') as client, patch.dict(smoke.os.environ, env, clear=True), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(smoke.main(['--send-one']), 0)
                with locked(root / '.delivery.lock'), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(smoke.main(['--send-one']), 2)
            cp.assert_not_called()
            client.assert_not_called()
            self.assertTrue(json.loads(out.getvalue())['existing_attempt'])

    def test_cli_missing_ledger_stops_and_never_creates_it(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(smoke, 'ROOT', Path(folder)), \
                contextlib.redirect_stderr(io.StringIO()), patch.object(smoke, 'DiscordClient') as client:
            self.assertEqual(smoke.main([]), 2)
            self.assertFalse((Path(folder) / smoke.LEDGER_RELATIVE).exists())
            client.assert_not_called()

    def test_workflow_only_manual_default_preview_and_secret_only_final_step(self):
        workflow = (smoke.ROOT / '.github/workflows/discord-smoke.yml').read_text()
        self.assertIn('name: Discord one-time format test', workflow)
        self.assertIn('  workflow_dispatch:', workflow)
        self.assertNotIn('  schedule:', workflow)
        self.assertNotIn('  push:', workflow)
        self.assertNotIn('  pull_request:', workflow)
        self.assertIn('default: preview_only', workflow)
        self.assertIn("inputs.confirmation == 'send_one_format_test'", workflow)
        self.assertIn('group: anime-news-monitor', workflow)
        self.assertIn('cancel-in-progress: false', workflow)
        self.assertEqual(workflow.count("github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"), 2)
        before_send, send_step = workflow.split('      - name: Send the fixed smoke message at most once')
        self.assertNotIn('secrets.', before_send)
        self.assertEqual(send_step.count('${{ secrets.DISCORD_WEBHOOK_URL }}'), 1)
        self.assertIn('python -m unittest discover -s tests -v', before_send)
        self.assertIn('python -m news_delivery.smoke --dry-run', before_send)
        self.assertIn('python -m news_delivery.smoke --send-one', send_step)
        self.assertNotIn('DELIVERY_LIVE_ENABLED', workflow)
        self.assertNotIn('X_API', workflow)
        self.assertNotIn('--mode both', workflow)

    def test_local_git_late_conflict_retains_remote_pending_and_prevents_resend(self):
        # Real local Git compare-and-swap, but no network or API calls.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote, one, two = root / 'remote.git', root / 'one', root / 'two'
            git(root, 'init', '--bare', '--initial-branch=main', str(remote))
            git(root, 'clone', str(remote), str(one))
            git(one, 'config', 'user.name', 'Unit Test')
            git(one, 'config', 'user.email', 'test@example.invalid')
            (one / 'data').mkdir()
            atomic_write(one / smoke.LEDGER_RELATIVE, empty())
            git(one, 'add', '.')
            git(one, 'commit', '-m', 'initial smoke fixture')
            git(one, 'push', 'origin', 'main')
            git(root, 'clone', str(remote), str(two))
            stale = GitCheckpoint(two, 'main', smoke.LEDGER_RELATIVE)
            checkpoint = GitCheckpoint(one, 'main', smoke.LEDGER_RELATIVE)

            def send(payload, now):
                durable = json.loads(git(remote, 'show', 'main:' + smoke.LEDGER_RELATIVE))
                self.assertEqual(durable['tests'][smoke.SMOKE_ID]['status'], 'pending')
                with self.assertRaises(StateError):
                    stale(self.ledger)
                git(two, 'pull', '--ff-only')
                (two / 'unrelated.txt').write_text('concurrent maintainer change\n')
                git(two, 'add', 'unrelated.txt')
                git(two, '-c', 'user.name=Unit Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'unrelated change')
                git(two, 'push', 'origin', 'main')
                return Outcome('sent', 'confirmed', '345', channel_id='234')

            self.client.send.side_effect = send
            with self.assertRaises(smoke.ResultCheckpointError):
                self.send(checkpoint=checkpoint)
            durable = json.loads(git(remote, 'show', 'main:' + smoke.LEDGER_RELATIVE))
            self.assertEqual(durable['tests'][smoke.SMOKE_ID]['status'], 'pending')
            self.assertEqual(smoke.send_once(durable, None, self.factory, NOW, ENV)['status'], 'pending')
            self.client.send.assert_called_once()


if __name__ == '__main__':
    unittest.main()
