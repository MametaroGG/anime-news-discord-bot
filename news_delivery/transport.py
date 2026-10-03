"""Official APIs only. No redirect, automatic POST retry, secret logging or scraping."""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from .schema import iso


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Outcome:
    status: str
    reason: str
    remote_id: str | None = None
    next_attempt_at: str | None = None
    channel_id: str | None = None
    guild_id: str | None = None


def response_json(response):
    try:
        result = response.json()
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def rate_limit_time(response, data, now):
    waits = [300.0]  # A missing rate-limit header does not justify a tight retry loop.
    if response.headers.get('Retry-After'):
        raw = response.headers['Retry-After']
        try:
            wait = float(raw)
        except ValueError:
            try:
                stamp = parsedate_to_datetime(raw)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                wait = (stamp - now).total_seconds()
            except (ValueError, TypeError, OverflowError):
                wait = 300
        waits.append(wait)
    if 'retry_after' in data:
        try:
            waits.append(float(data['retry_after']))
        except (ValueError, TypeError):
            pass
    if response.headers.get('x-rate-limit-reset'):
        try:
            waits.append(float(response.headers['x-rate-limit-reset']) - now.timestamp())
        except ValueError:
            pass
    if any(not math.isfinite(value) or value > 7 * 86400 for value in waits):
        return None
    return iso(now + timedelta(seconds=max(1, *waits)))


def interpret(response, destination, now):
    data = response_json(response)
    code = response.status_code
    if code in (200, 201):
        candidate = data.get('data', {}).get('id') if destination == 'x' and isinstance(data.get('data'), dict) else data.get('id') if destination == 'discord' else None
        if isinstance(candidate, str) and re.fullmatch(r'[0-9]+', candidate):
            def discord_id(field):
                value = data.get(field) if destination == 'discord' else None
                return value if isinstance(value, str) and re.fullmatch(r'[0-9]{1,25}', value) else None
            return Outcome('sent', 'confirmed', candidate,
                           channel_id=discord_id('channel_id'), guild_id=discord_id('guild_id'))
        return Outcome('uncertain', 'success_response_without_message_id')
    if code == 429:
        next_time = rate_limit_time(response, data, now)
        if next_time is None:
            return Outcome('blocked', 'rate_limit_requires_review')
        return Outcome('ready', 'rate_limited', next_attempt_at=next_time)
    if code in (400, 401, 403, 404, 405, 410, 413, 415, 422):
        return Outcome('blocked', 'request_rejected_' + str(code))
    if 300 <= code < 400:
        return Outcome('blocked', 'redirect_refused')
    # 408/5xx/unknown responses may follow an accepted write. Never blindly retry.
    return Outcome('uncertain', 'ambiguous_http_' + str(code))


class ApiClient:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.trust_env = False  # No .netrc or environment-injected proxy auth.

    def post(self, url, payload, destination, now, auth=None):
        started = time.monotonic()
        try:
            response = self.session.request('POST', url, json=payload, auth=auth,
                                            headers={'User-Agent': 'AnimeNewsReviewedPublisher/1.0'},
                                            allow_redirects=False, timeout=(10, 30))
            return interpret(response, destination, now + timedelta(seconds=time.monotonic() - started))
        except requests.RequestException:
            return Outcome('uncertain', 'transport_error_after_possible_send')


class DiscordClient(ApiClient):
    def __init__(self, webhook, session=None):
        super().__init__(session)
        try:
            parsed = urlsplit(webhook)
            if parsed.scheme != 'https' or parsed.hostname not in ('discord.com', 'discordapp.com') or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
                raise ValueError
            if not re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+', parsed.path):
                raise ValueError
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            if any(key not in ('wait', 'thread_id') for key, _ in pairs) or len({key for key, _ in pairs}) != len(pairs):
                raise ValueError
            params = dict(pairs)
            if 'thread_id' in params and not re.fullmatch(r'\d+', params['thread_id']):
                raise ValueError
            params['wait'] = 'true'
            self.url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ''))
        except (ValueError, TypeError):
            raise ConfigurationError('invalid DISCORD_WEBHOOK_URL; secret value hidden') from None

    def send(self, content, now):
        # Only the internal reviewed renderer supplies dictionaries; legacy text remains supported.
        payload = dict(content) if isinstance(content, dict) else {'content': content}
        payload['allowed_mentions'] = {'parse': []}
        return self.post(self.url, payload, 'discord', now)


class XClient(ApiClient):
    def __init__(self, env, session=None):
        super().__init__(session)
        required = ('X_API_KEY', 'X_API_KEY_SECRET', 'X_ACCESS_TOKEN', 'X_ACCESS_TOKEN_SECRET', 'X_EXPECTED_USER_ID')
        if any(not env.get(name) for name in required):
            raise ConfigurationError('X user-context credentials and expected user ID are required')
        if not re.fullmatch(r'\d+', env['X_EXPECTED_USER_ID']):
            raise ConfigurationError('X_EXPECTED_USER_ID must be a verified numeric account ID')
        from requests_oauthlib import OAuth1
        self.auth = OAuth1(env['X_API_KEY'], env['X_API_KEY_SECRET'],
                           env['X_ACCESS_TOKEN'], env['X_ACCESS_TOKEN_SECRET'])
        self.expected_id = env['X_EXPECTED_USER_ID']
        self.verified = False

    def verify_identity(self):
        try:
            response = self.session.request('GET', 'https://api.x.com/2/users/me',
                                            auth=self.auth, allow_redirects=False, timeout=(10, 30))
            data = response_json(response).get('data')
            if response.status_code != 200 or not isinstance(data, dict):
                raise ConfigurationError('X account verification failed; no X post attempted')
            if data.get('id') != self.expected_id or str(data.get('username', '')).casefold() != 'animeka_fast':
                raise ConfigurationError('X account identity mismatch; no X post attempted')
            self.verified = True
        except requests.RequestException:
            raise ConfigurationError('X identity request failed; credentials hidden') from None

    def send(self, content, now):
        if not self.verified:
            raise ConfigurationError('X account has not been verified')
        return self.post('https://api.x.com/2/tweets', {'text': content}, 'x', now, self.auth)
