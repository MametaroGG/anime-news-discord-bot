"""A second fixed smoke case must preserve the first case and never resend."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers import NOW
from test_smoke import ENV, WEBHOOK, empty, response
from news_delivery import smoke
from news_delivery.schema import digest
from news_delivery.state import StateError, atomic_write
from news_delivery.transport import ConfigurationError, DiscordClient, Outcome

CASE = 'x_video'
ID = 'discord-x-video-test-20261003-v1'
URL = 'https://x.com/hirayasumi0426/status/2104707978460803552/video/1'
X_ENV = dict(ENV, DISCORD_SMOKE_CONFIRMATION='send_one_x_video_test')
ORIGINAL_HASH = 'e3fb0728fc9157809e3bb5280a259a441c48eb25bcbf7f0c7ec0e4f689e2fa81'


class XVideoSmokeTests(unittest.TestCase):
    def setUp(self):
        self.ledger = empty()
        self.client = Mock()
        self.client.send.return_value = Outcome('sent', 'confirmed', '345', channel_id='234')
        # Establish the original one-shot receipt using only a mocked transport.
        smoke.send_once(self.ledger, lambda _: None, lambda: self.client, NOW, ENV)
        self.original = copy.deepcopy(self.ledger['tests'][smoke.SMOKE_ID])
        self.client.reset_mock()
        self.factory = Mock(return_value=self.client)
        self.checkpoints = []

    def checkpoint(self, ledger):
        smoke.validate_ledger(ledger)
        self.assertEqual(ledger['tests'][smoke.SMOKE_ID], self.original)
        self.checkpoints.append(copy.deepcopy(ledger))

    def send(self, checkpoint=None):
        return smoke.send_once(self.ledger, checkpoint or self.checkpoint,
                               self.factory, NOW, X_ENV, case=CASE)

    def test_original_payload_hash_and_receipt_stay_unchanged(self):
        self.assertEqual(digest(smoke.fixed_payload()), ORIGINAL_HASH)
        self.send()
        self.assertEqual(self.ledger['tests'][smoke.SMOKE_ID], self.original)
        self.assertEqual(self.ledger['tests'][smoke.SMOKE_ID]['payload_sha256'], ORIGINAL_HASH)
        result = smoke.send_once(self.ledger, None, Mock(side_effect=AssertionError()), NOW, ENV)
        self.assertEqual(result['smoke_id'], smoke.SMOKE_ID)
        self.assertTrue(result['existing_attempt'])

    def test_fixed_payload_preserves_exact_video_and_neutral_past_test_label(self):
        payload = smoke.fixed_payload(CASE)
        self.assertEqual(payload['allowed_mentions'], {'parse': []})
        self.assertIn('【動作確認】', payload['content'])
        self.assertIn('過去の投稿', payload['content'])
        self.assertIn('ニュース速報ではありません', payload['content'])
        self.assertTrue(payload['content'].endswith('\n' + URL))
        embed = payload['embeds'][0]
        copy_field = next(field['value'] for field in embed['fields']
                          if field['name'] == 'X投稿用（手動コピー・テスト例）')
        self.assertIn(URL, copy_field)
        for forbidden in ('video', 'image', 'thumbnail', 'timestamp'):
            self.assertNotIn(forbidden, embed)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('公式', encoded)
        self.assertNotIn('2026年', encoded)
        self.assertNotIn('fxtwitter', encoded)
        self.assertNotIn('attachments', payload)
        payload['content'] = 'mutated'
        self.assertTrue(smoke.fixed_payload(CASE)['content'].endswith(URL))

    def test_new_payload_fits_discord_limits(self):
        payload = smoke.fixed_payload(CASE)
        size = lambda value: len(value.encode('utf-16-le')) // 2
        card = payload['embeds'][0]
        self.assertLessEqual(size(payload['content']), 2000)
        self.assertLessEqual(size(card['title']), 256)
        self.assertLessEqual(size(card['description']), 4096)
        total = size(card['title']) + size(card['description'])
        for field in card['fields']:
            self.assertLessEqual(size(field['name']), 256)
            self.assertLessEqual(size(field['value']), 1024)
            total += size(field['name']) + size(field['value'])
        self.assertLessEqual(total, 6000)

    def test_each_case_requires_its_own_confirmation(self):
        for case, env in ((CASE, ENV), ('youtube', X_ENV),
                          (CASE, dict(X_ENV, DISCORD_SMOKE_CONFIRMATION='preview_only')),
                          (CASE, dict(X_ENV, GITHUB_EVENT_NAME='schedule')),
                          (CASE, dict(X_ENV, GITHUB_REF='refs/heads/feature')),
                          ('arbitrary', X_ENV)):
            with self.subTest(case=case, confirmation=env['DISCORD_SMOKE_CONFIRMATION']), \
                    self.assertRaises(ConfigurationError):
                smoke.send_once(self.ledger, self.checkpoint, self.factory, NOW, env, case=case)
        self.factory.assert_not_called()
        self.assertNotIn(ID, self.ledger['tests'])

    def test_new_case_reserves_once_and_preserves_old_receipt_before_post(self):
        def send(payload, now):
            self.assertEqual(self.checkpoints[-1]['tests'][ID]['status'], 'pending')
            self.assertEqual(self.checkpoints[-1]['tests'][smoke.SMOKE_ID], self.original)
            self.assertEqual(payload, smoke.fixed_payload(CASE))
            return Outcome('sent', 'confirmed', '456', channel_id='234')
        self.client.send.side_effect = send
        result = self.send()
        self.assertEqual(result['smoke_id'], ID)
        self.assertEqual(result['message_id'], '456')
        self.assertEqual(result['attempts'], 1)
        self.assertEqual([value['tests'][ID]['status'] for value in self.checkpoints], ['pending', 'sent'])
        repeat = smoke.send_once(self.ledger, None, Mock(side_effect=AssertionError()), NOW, X_ENV, case=CASE)
        self.assertTrue(repeat['existing_attempt'])
        self.client.send.assert_called_once()

    def test_all_new_case_outcomes_and_pending_never_retry(self):
        for outcome in (Outcome('sent', 'confirmed', '456', channel_id='234'),
                        Outcome('uncertain', 'transport_error_after_possible_send'),
                        Outcome('blocked', 'request_rejected_403'),
                        Outcome('ready', 'rate_limited')):
            with self.subTest(outcome=outcome.status):
                ledger = {'schema_version': 1, 'tests': {smoke.SMOKE_ID: copy.deepcopy(self.original)}}
                client = Mock()
                client.send.return_value = outcome
                snapshots = []
                smoke.send_once(ledger, lambda value: snapshots.append(copy.deepcopy(value)),
                                lambda: client, NOW, X_ENV, case=CASE)
                for state in (ledger, snapshots[0]):
                    frozen = copy.deepcopy(state)
                    result = smoke.send_once(state, None, Mock(side_effect=AssertionError()), NOW, X_ENV, case=CASE)
                    self.assertTrue(result['existing_attempt'])
                    self.assertFalse(result['resend_allowed'])
                    self.assertEqual(state, frozen)
                client.send.assert_called_once()

    def test_failed_reservation_and_result_checkpoint_do_not_resend(self):
        with self.assertRaises(StateError):
            self.send(Mock(side_effect=StateError('remote changed')))
        self.client.send.assert_not_called()
        self.assertEqual(self.ledger['tests'][ID]['status'], 'pending')
        self.assertEqual(self.send()['status'], 'pending')
        del self.ledger['tests'][ID]  # Reset only this in-memory test fixture.
        def checkpoint(value):
            if self.checkpoints:
                raise StateError('result push failed')
            self.checkpoint(value)
        with self.assertRaises(smoke.ResultCheckpointError) as caught:
            self.send(checkpoint)
        self.assertEqual(caught.exception.observed['smoke_id'], ID)
        self.assertEqual(caught.exception.observed['message_id'], '345')
        self.ledger = self.checkpoints[0]
        self.assertEqual(self.send()['status'], 'pending')
        self.client.send.assert_called_once()

    def test_case_specific_hashes_cannot_be_swapped(self):
        self.send()
        good = copy.deepcopy(self.ledger)
        self.ledger['tests'][ID]['payload_sha256'] = ORIGINAL_HASH
        with self.assertRaises(StateError):
            smoke.validate_ledger(self.ledger)
        good['tests'][smoke.SMOKE_ID]['payload_sha256'] = digest(smoke.fixed_payload(CASE))
        with self.assertRaises(StateError):
            smoke.validate_ledger(good)

    def test_x_cli_dry_run_with_old_receipt_is_still_preview_and_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            path = root / smoke.LEDGER_RELATIVE
            atomic_write(path, self.ledger)
            before = path.read_bytes()
            with patch.object(smoke, 'ROOT', root), patch.object(smoke, 'GitCheckpoint') as checkpoint, \
                    patch.object(smoke, 'DiscordClient') as client, patch.object(smoke.os, 'environ', {}), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(smoke.main(['--dry-run', '--case', CASE]), 0)
            result = json.loads(out.getvalue())
            self.assertEqual(result['status'], 'preview')
            self.assertFalse(result['existing_attempt'])
            self.assertEqual(result['attempts'], 0)
            checkpoint.assert_not_called()
            client.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    def test_x_cli_creates_checkpoint_even_when_old_case_was_sent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            atomic_write(root / smoke.LEDGER_RELATIVE, self.ledger)
            with patch.object(smoke, 'ROOT', root), patch.object(smoke, 'GitCheckpoint') as checkpoint, \
                    patch.object(smoke, 'DiscordClient', return_value=self.client), \
                    patch.dict(smoke.os.environ, X_ENV, clear=True), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(smoke.main(['--send-one', '--case', CASE]), 0)
            checkpoint.assert_called_once_with(root, 'main', smoke.LEDGER_RELATIVE)
            self.assertEqual(checkpoint.return_value.call_count, 2)
            self.client.send.assert_called_once_with(smoke.fixed_payload(CASE), unittest.mock.ANY)
            self.assertEqual(json.loads(out.getvalue())['smoke_id'], ID)

    def test_x_http_is_one_webhook_post_without_media_fetch_or_upload(self):
        session = Mock()
        session.request.return_value = response(body={'id': '456', 'channel_id': '234'})
        self.factory.return_value = DiscordClient(WEBHOOK, session)
        with patch('requests.get', side_effect=AssertionError('no media download')):
            self.assertEqual(self.send()['status'], 'sent')
        session.request.assert_called_once()
        args, kwargs = session.request.call_args
        self.assertEqual(args, ('POST', WEBHOOK + '?wait=true'))
        self.assertEqual(kwargs['json'], smoke.fixed_payload(CASE))
        self.assertNotIn('files', kwargs)
        self.assertFalse(kwargs['allow_redirects'])

    def test_workflow_allowlists_case_and_confirmation_pair(self):
        workflow = (smoke.ROOT / '.github/workflows/discord-smoke.yml').read_text()
        self.assertIn('default: youtube', workflow)
        self.assertIn('default: preview_only', workflow)
        self.assertIn("inputs.test_case == 'x_video' && inputs.confirmation == 'send_one_x_video_test'", workflow)
        self.assertIn("inputs.test_case == 'youtube' && inputs.confirmation == 'send_one_format_test'", workflow)
        self.assertIn('python -m news_delivery.smoke --send-one --case "$DISCORD_SMOKE_CASE"', workflow)
        self.assertNotIn('type: string', workflow)


if __name__ == '__main__':
    unittest.main()
