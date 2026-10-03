"""The approved comparison sends exactly one bare X URL, with no custom embed."""
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
from news_delivery.state import atomic_write
from news_delivery.transport import ConfigurationError, DiscordClient, Outcome

CASE = 'x_link_only'
ID = 'discord-x-link-only-test-20261003-v1'
URL = 'https://x.com/hirayasumi0426/status/2104707978460803552/video/1'
LINK_ENV = dict(ENV, DISCORD_SMOKE_CONFIRMATION='send_one_x_link_only_test')


class XLinkOnlySmokeTests(unittest.TestCase):
    def setUp(self):
        self.ledger = empty()
        self.client = Mock()
        self.client.send.return_value = Outcome('sent', 'confirmed', '345', channel_id='234')
        for case in ('youtube', 'x_video'):
            env = dict(ENV, DISCORD_SMOKE_CONFIRMATION=smoke.CASES[case][1])
            smoke.send_once(self.ledger, lambda _: None, lambda: self.client, NOW, env, case=case)
        self.previous = copy.deepcopy(self.ledger['tests'])
        self.client.reset_mock()

    def test_payload_is_exact_url_only_and_previous_payload_hashes_are_stable(self):
        self.assertEqual(smoke.fixed_payload(CASE), {
            'content': URL, 'allowed_mentions': {'parse': []}})
        self.assertEqual(digest(smoke.fixed_payload('youtube')),
                         'e3fb0728fc9157809e3bb5280a259a441c48eb25bcbf7f0c7ec0e4f689e2fa81')
        self.assertEqual(digest(smoke.fixed_payload('x_video')),
                         'e2c8e8b7960f1dcc0b424592bc8791792861169ec3dce4a27c04b0f415018a62')

    def test_all_confirmation_pairs_are_case_specific(self):
        factory, checkpoint = Mock(), Mock()
        for case, spec in smoke.CASES.items():
            for other, other_spec in smoke.CASES.items():
                if case == other:
                    continue
                env = dict(ENV, DISCORD_SMOKE_CONFIRMATION=other_spec[1])
                with self.subTest(case=case, confirmation=other), self.assertRaises(ConfigurationError):
                    smoke.send_once(self.ledger, checkpoint, factory, NOW, env, case=case)
        factory.assert_not_called()
        checkpoint.assert_not_called()
        self.assertEqual(self.ledger['tests'], self.previous)

    def test_one_send_preserves_two_receipts_and_reserves_before_post(self):
        snapshots = []
        def checkpoint(value):
            snapshots.append(copy.deepcopy(value))
            for key, row in self.previous.items():
                self.assertEqual(value['tests'][key], row)
        def send(payload, now):
            self.assertEqual(snapshots[-1]['tests'][ID]['status'], 'pending')
            self.assertEqual(payload, {'content': URL, 'allowed_mentions': {'parse': []}})
            return Outcome('sent', 'confirmed', '456', channel_id='234')
        self.client.send.side_effect = send
        result = smoke.send_once(self.ledger, checkpoint, lambda: self.client, NOW, LINK_ENV, case=CASE)
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(result['smoke_id'], ID)
        self.assertEqual(result['message_id'], '456')
        self.assertEqual([snapshot['tests'][ID]['status'] for snapshot in snapshots], ['pending', 'sent'])
        forbidden = Mock(side_effect=AssertionError('must not resend'))
        repeated = smoke.send_once(self.ledger, forbidden, forbidden, NOW, LINK_ENV, case=CASE)
        self.assertTrue(repeated['existing_attempt'])
        self.client.send.assert_called_once()
        forbidden.assert_not_called()

    def test_each_terminal_or_pending_state_never_retries(self):
        for outcome in (Outcome('sent', 'confirmed', '456', channel_id='234'),
                        Outcome('uncertain', 'transport_error_after_possible_send'),
                        Outcome('blocked', 'request_rejected_403'),
                        Outcome('ready', 'rate_limited')):
            with self.subTest(status=outcome.status):
                ledger = {'schema_version': 1, 'tests': copy.deepcopy(self.previous)}
                snapshots, client = [], Mock()
                client.send.return_value = outcome
                smoke.send_once(ledger, lambda value: snapshots.append(copy.deepcopy(value)),
                                lambda: client, NOW, LINK_ENV, case=CASE)
                for state in (ledger, snapshots[0]):
                    frozen = copy.deepcopy(state)
                    forbidden = Mock(side_effect=AssertionError('must not retry'))
                    result = smoke.send_once(state, forbidden, forbidden, NOW, LINK_ENV, case=CASE)
                    self.assertTrue(result['existing_attempt'])
                    self.assertFalse(result['resend_allowed'])
                    self.assertEqual(state, frozen)
                client.send.assert_called_once()

    def test_mock_http_sends_exact_payload_once_with_wait_true(self):
        session = Mock()
        session.request.return_value = response(body={'id': '456', 'channel_id': '234'})
        with patch('requests.get', side_effect=AssertionError('no media fetch')):
            result = smoke.send_once(self.ledger, lambda _: None,
                                     lambda: DiscordClient(WEBHOOK, session), NOW, LINK_ENV, case=CASE)
        self.assertEqual(result['status'], 'sent')
        session.request.assert_called_once()
        args, kwargs = session.request.call_args
        self.assertEqual(args, ('POST', WEBHOOK + '?wait=true'))
        self.assertEqual(kwargs['json'], {'content': URL, 'allowed_mentions': {'parse': []}})
        self.assertNotIn('files', kwargs)
        self.assertFalse(kwargs['allow_redirects'])

    def test_link_only_dry_run_does_not_touch_previous_receipts_or_secrets(self):
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
            self.assertEqual(result['smoke_id'], ID)
            self.assertEqual(result['attempts'], 0)
            self.assertFalse(result['existing_attempt'])
            checkpoint.assert_not_called()
            client.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    def test_workflow_exposes_only_fixed_case_with_matching_confirmation(self):
        workflow = (smoke.ROOT / '.github/workflows/discord-smoke.yml').read_text()
        self.assertIn('          - x_link_only', workflow)
        self.assertIn('          - send_one_x_link_only_test', workflow)
        self.assertIn("inputs.test_case == 'x_link_only' && inputs.confirmation == 'send_one_x_link_only_test'", workflow)
        self.assertIn('default: preview_only', workflow)
        self.assertNotIn('type: string', workflow)


if __name__ == '__main__':
    unittest.main()
