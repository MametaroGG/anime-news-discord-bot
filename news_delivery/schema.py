"""Fail-closed structural checks. These do NOT establish factual truth or authority."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

KINDS = frozenset({
    'new_adaptation', 'sequel', 'film_production', 'new_pv', 'new_visual',
    'release_date', 'cast', 'staff', 'theme_song', 'studio', 'title', 'delay',
})
CHECKS = frozenset({'official_primary', 'original_timestamp', 'new_information',
                    'in_scope', 'not_duplicate'})
ID = re.compile(r'^[a-z0-9][a-z0-9._-]{2,119}$')
MAX_FILE_BYTES = 64 * 1024


class Invalid(ValueError):
    """Only fixed field names/reasons, never raw untrusted values or credentials."""


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value):
        raise Invalid('timestamp requires an explicit date, time, seconds and timezone')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        raise Invalid('invalid timestamp') from None


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def text(value, field, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Invalid(f'invalid {field}')
    if any(ord(c) < 32 and c not in '\n\t' for c in value) or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise Invalid(f'control characters in {field}')
    return value


def fields(value, expected, field):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise Invalid(f'unexpected or missing fields in {field}')


def canonical(url):
    text(url, 'URL', 2000)
    try:
        p = urlsplit(url)
        host = p.hostname
        if p.scheme != 'https' or not host or p.username or p.password or p.port not in (None, 443):
            raise ValueError
        if re.search(r'[\s\\]', url) or '%' in host or '.' not in host or host.endswith('.local'):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
    except ValueError:
        raise Invalid('URL must be public HTTPS without credentials') from None
    host = host.lower()
    if host in {'www.twitter.com', 'twitter.com', 'www.x.com'}:
        host = 'x.com'
    path = p.path.rstrip('/') or '/'
    if host == 'x.com' and re.fullmatch(r'/[^/]+/status/\d+', path):
        path = '/i/status/' + path.rsplit('/', 1)[1]
    # X tracking (?s=20, ?t=...) does not change the identity of a post.
    query = [] if host == 'x.com' and re.fullmatch(r'/[^/]+/status/\d+', path) else [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid'}]
    return urlunsplit(('https', host, path, urlencode(sorted(query)), ''))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def event_keys(event):
    return sorted({
        'announcement:' + event['work_key'] + ':' + event['announcement_key'],
        *('source:' + canonical(source['url']) for source in event['sources']),
        *('announcement:' + event['work_key'] + ':' + key for key in event['dedupe_aliases']),
    })


def validate(event, now=None):
    fields(event, {'schema_version', 'event_id', 'work_key', 'work_title',
                  'announcement_key', 'announcement_kinds', 'published_at',
                  'verified_at', 'queued_at', 'sources', 'facts', 'approval',
                  'destinations', 'texts', 'dedupe_aliases'}, 'event')
    if type(event['schema_version']) is not int or event['schema_version'] != 1:
        raise Invalid('unsupported schema_version')
    for field in ('event_id', 'work_key', 'announcement_key'):
        if not isinstance(event[field], str) or not ID.fullmatch(event[field]):
            raise Invalid(f'invalid {field}')
    text(event['work_title'], 'work_title', 200)
    kinds = event['announcement_kinds']
    if not isinstance(kinds, list) or not kinds or any(k not in KINDS for k in kinds if isinstance(k, str)) or any(not isinstance(k, str) for k in kinds) or len(kinds) != len(set(kinds)):
        raise Invalid('unsupported or duplicate announcement kind')
    aliases = event['dedupe_aliases']
    if not isinstance(aliases, list) or len(aliases) > 20 or any(not isinstance(k, str) or not ID.fullmatch(k) for k in aliases):
        raise Invalid('invalid dedupe_aliases')
    published, verified, queued = (timestamp(event[k]) for k in ('published_at', 'verified_at', 'queued_at'))
    if not published <= verified <= queued:
        raise Invalid('publication, verification and queue timestamps are out of order')
    if not timedelta(0) <= queued - published <= timedelta(hours=1):
        raise Invalid('original official announcement must be within the previous hour at queue time')
    if now is not None and queued > now:
        raise Invalid('future queue timestamp')
    sources = event['sources']
    if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
        raise Invalid('one to eight official primary sources required')
    source_urls, times = set(), []
    for source in sources:
        fields(source, {'url', 'title', 'publisher', 'published_at', 'timestamp_evidence',
                        'official_basis'}, 'source')
        source_urls.add(canonical(source['url']))
        for field in ('title', 'publisher', 'timestamp_evidence', 'official_basis'):
            text(source[field], 'source.' + field, 1500)
        times.append(timestamp(source['published_at']))
    if len(source_urls) != len(sources) or min(times) != published or any(t > verified for t in times):
        raise Invalid('sources must be distinct, original publication must be earliest, and verified afterward')
    facts = event['facts']
    if not isinstance(facts, list) or not 1 <= len(facts) <= 16:
        raise Invalid('one to sixteen verified facts required')
    covered = set()
    for fact in facts:
        fields(fact, {'kind', 'text', 'source_urls'}, 'fact')
        if fact['kind'] not in kinds:
            raise Invalid('fact kind not declared')
        covered.add(fact['kind'])
        text(fact['text'], 'fact.text', 800)
        urls = fact['source_urls']
        if not isinstance(urls, list) or not urls or any(canonical(url) not in source_urls for url in urls):
            raise Invalid('each fact requires a supplied official primary source')
    if covered != set(kinds):
        raise Invalid('every announced kind requires a verified fact')
    approval = event['approval']
    fields(approval, {'status', 'reviewed_by', 'approved_at', 'checks'}, 'approval')
    if approval['status'] != 'approved':
        raise Invalid('event is not approved')
    text(approval['reviewed_by'], 'reviewed_by', 100)
    if not verified <= timestamp(approval['approved_at']) <= queued:
        raise Invalid('approval must follow verification and precede queueing')
    fields(approval['checks'], CHECKS, 'approval.checks')
    if any(value is not True for value in approval['checks'].values()):
        raise Invalid('all editorial checks must be explicitly true')
    destinations = event['destinations']
    if not isinstance(destinations, list) or not destinations or any(d not in ('discord', 'x') for d in destinations) or len(destinations) != len(set(destinations)):
        raise Invalid('invalid destinations')
    fields(event['texts'], destinations, 'texts')
    for destination in destinations:
        content = text(event['texts'][destination], 'texts.' + destination, 1900 if destination == 'discord' else 1000)
        # Deliberately conservative: treat every non-URL Unicode scalar as weight 2.
        # This avoids accidentally exceeding X's standard 280 weighted limit.
        urls = re.findall(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s<>]+|\b(?:mailto|javascript|data|file|tel):[^\s<>]+', content)
        if not urls or any(canonical(url) not in source_urls for url in urls):
            raise Invalid('post links must use supplied official primary sources')
        if not any(source['url'] in content for source in sources):
            raise Invalid('post requires an exact official source link')
        if destination == 'x':
            non_urls = content
            for url in urls:
                non_urls = non_urls.replace(url, '')
            if len(non_urls) * 2 + len(urls) * 23 > 280:
                raise Invalid('X post exceeds conservative 280-weight budget')
            if re.search(r'(?<!\w)@[A-Za-z0-9_]+', non_urls) or '$' in non_urls:
                raise Invalid('automatic mentions and cashtags are not supported')
    return event


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Invalid('duplicate JSON object key')
        result[key] = value
    return result


def read_json(path, limit=MAX_FILE_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise Invalid('invalid, symlinked or oversized JSON file')
    try:
        return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(Invalid('non-finite JSON number')))
    except (UnicodeError, json.JSONDecodeError):
        raise Invalid('invalid JSON encoding or syntax') from None


def read_queue(directory, now=None):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise Invalid('queue directory missing or symlinked')
    events = []
    for path in sorted(directory.glob('*.json')):
        event = validate(read_json(path), now)
        if path.name != event['event_id'] + '.json':
            raise Invalid('queue filename must match event_id')
        events.append(event)
    return sorted(events, key=lambda e: (timestamp(e['queued_at']), e['event_id']))
