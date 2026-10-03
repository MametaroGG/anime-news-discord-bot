import unittest
from datetime import timedelta
from unittest.mock import Mock

import requests

from helpers import NOW
from news_delivery.schema import timestamp
from news_delivery.transport import ConfigurationError, DiscordClient, XClient, interpret

WEBHOOK = 'https://discord.com/api/webhooks/123/fake-test-only-token'
ENV = {'X_API_KEY': 'fake-key', 'X_API_KEY_SECRET': 'fake-secret',
       'X_ACCESS_TOKEN': 'fake-access', 'X_ACCESS_TOKEN_SECRET': 'fake-access-secret',
       'X_EXPECTED_USER_ID': '987'}


def response(status=200, body=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = body or {}
    return result


class TransportTests(unittest.TestCase):
    def test_discord_wait_and_no_mentions_no_redirects(self):
        session = Mock()
        session.request.return_value = response(body={'id': '1234'})
        outcome = DiscordClient(WEBHOOK + '?thread_id=456&wait=false', session).send('@everyone test', NOW)
        self.assertEqual(outcome.status, 'sent')
        args, kwargs = session.request.call_args
        self.assertEqual(args[0], 'POST')
        self.assertIn('wait=true', args[1])
        self.assertIn('thread_id=456', args[1])
        self.assertEqual(kwargs['json']['allowed_mentions'], {'parse': []})
        self.assertFalse(kwargs['allow_redirects'])
        self.assertFalse(session.trust_env)

    def test_webhook_host_and_path_validation_redacts_secret(self):
        for url in ('https://discord.com.evil.example/api/webhooks/123/secret',
                    WEBHOOK + '/messages/1', WEBHOOK + '?redirect=evil',
                    'https://evil.example/webhooks/secret', WEBHOOK.replace('https', 'http')):
            with self.subTest(url=url), self.assertRaises(ConfigurationError) as caught:
                DiscordClient(url, Mock())
            self.assertNotIn(url, str(caught.exception))
            self.assertNotIn('fake-test-only-token', str(caught.exception))

    def test_timeout_5xx_redirect_and_missing_id_do_not_retry(self):
        for remote in (requests.Timeout(WEBHOOK), response(500), response(302), response(204), response(200)):
            session = Mock()
            if isinstance(remote, BaseException):
                session.request.side_effect = remote
            else:
                session.request.return_value = remote
            outcome = DiscordClient(WEBHOOK, session).send('test', NOW)
            self.assertIn(outcome.status, {'uncertain', 'blocked'})
            self.assertNotIn('fake-test-only-token', outcome.reason)
            self.assertEqual(session.request.call_count, 1)

    def test_rate_limit_respects_headers_without_sleeping_or_reposting(self):
        remote = response(429, {'retry_after': 120}, {'Retry-After': '900', 'x-rate-limit-reset': str(int(NOW.timestamp()) + 600)})
        outcome = interpret(remote, 'discord', NOW)
        self.assertEqual(outcome.status, 'ready')
        self.assertEqual(timestamp(outcome.next_attempt_at), NOW + timedelta(seconds=900))
        outcome = interpret(response(429, {'retry_after': float('inf')}), 'discord', NOW)
        self.assertEqual(outcome.status, 'blocked')

    def test_auth_rejection_blocked_not_uncertain(self):
        for code in (400, 401, 403, 404):
            self.assertEqual(interpret(response(code), 'x', NOW).status, 'blocked')

    def test_x_requires_expected_id_and_account_verification(self):
        with self.assertRaises(ConfigurationError):
            XClient({}, Mock())
        client = XClient(ENV, Mock())
        with self.assertRaises(ConfigurationError):
            client.send('test', NOW)
        client.session.request.assert_not_called()

    def test_x_wrong_numeric_id_even_right_username_stops_post(self):
        session = Mock()
        session.request.return_value = response(body={'data': {'id': 'wrong', 'username': 'animeka_fast'}})
        client = XClient(ENV, session)
        with self.assertRaises(ConfigurationError):
            client.verify_identity()
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(session.request.call_args.args[0], 'GET')

    def test_x_user_context_identity_then_official_post(self):
        session = Mock()
        session.request.side_effect = [response(body={'data': {'id': '987', 'username': 'animeka_fast'}}),
                                       response(201, {'data': {'id': '456'}})]
        client = XClient(ENV, session)
        client.verify_identity()
        result = client.send('approved text', NOW)
        self.assertEqual(result.remote_id, '456')
        self.assertEqual(session.request.call_args.args, ('POST', 'https://api.x.com/2/tweets'))
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
        self.assertEqual(session.request.call_args.kwargs['json'], {'text': 'approved text'})
        self.assertIsNotNone(session.request.call_args.kwargs['auth'])


if __name__ == '__main__':
    unittest.main()
