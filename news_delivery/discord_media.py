"""Reviewed source links and rich Discord cards; no media fetching or rehosting."""
from __future__ import annotations

import re
from datetime import timedelta, timezone
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .schema import Invalid, X_POST_PATH, canonical, fields, text, timestamp

LABELS = {
    'new_adaptation': 'アニメ化決定', 'sequel': '続編決定',
    'film_production': '映画制作決定', 'new_pv': '新PV',
    'new_visual': '新ビジュアル', 'release_date': '放送・公開日',
    'cast': '追加キャスト', 'staff': 'スタッフ', 'theme_song': '主題歌',
    'studio': '制作会社', 'title': '正式タイトル', 'delay': '延期',
}
YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'}
X_HOSTS = {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'}
PLATFORM_HOSTS = YOUTUBE_HOSTS | X_HOSTS | {'pbs.twimg.com', 'video.twimg.com'}


def media_url(url, kind):
    """Small allowlist of direct public URLs; normalize video tracking only."""
    text(url, 'media.url', 1000)
    canonical(url)  # Reuse HTTPS, credentials, port and literal-IP checks.
    p = urlsplit(url)
    host = p.hostname.lower()
    if (p.fragment or p.port is not None or
            re.search(r'[<>`\[\]{}()"\x7f]', url) or
            re.search(r'%(?:0[0-9a-f]|1[0-9a-f]|7f|2f|5c)', url, re.I)):
        raise Invalid('unsafe media URL syntax')
    if kind == 'youtube':
        query = parse_qsl(p.query, keep_blank_values=True)
        if host not in YOUTUBE_HOSTS or len(dict(query)) != len(query):
            raise Invalid('unsupported YouTube URL')
        params = dict(query)
        if set(params) - {'v', 't', 'si', 'feature'}:
            raise Invalid('unsupported YouTube URL parameters')
        if host == 'youtu.be':
            video_id = p.path.lstrip('/') if p.path.count('/') == 1 else ''
            if 'v' in params:
                raise Invalid('ambiguous YouTube video ID')
        elif p.path == '/watch':
            video_id = params.get('v', '')
        else:
            match = re.fullmatch(r'/(?:shorts|live)/([A-Za-z0-9_-]{11})', p.path)
            video_id = match.group(1) if match else ''
            if 'v' in params:
                raise Invalid('ambiguous YouTube video ID')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise Invalid('YouTube URL must identify one video')
        return 'https://www.youtube.com/watch?v=' + video_id
    if kind == 'x':
        if host not in X_HOSTS or not X_POST_PATH.fullmatch(p.path):
            raise Invalid('X URL must identify one original post')
        if any(key not in {'s', 't'} for key, _ in parse_qsl(p.query, keep_blank_values=True)):
            raise Invalid('unsupported X URL parameters')
        # Keep /video/N for X's native copied-video link. Only identity drops it.
        return 'https://x.com' + p.path
    if kind == 'image':
        if host in PLATFORM_HOSTS or p.query or not re.fullmatch(r'/[^?#]+\.(?:png|jpe?g|webp|gif)', p.path, re.I):
            raise Invalid('image must be an original direct image URL')
        # Preserve the exact original path. No CDN rewriting, transformations or proxy.
        return urlunsplit(('https', p.netloc, p.path, '', ''))
    raise Invalid('unsupported media kind')


def validate_media(event):
    media = event.get('media', [])
    if not isinstance(media, list) or len(media) > 3:
        raise Invalid('media must contain at most one image, YouTube video and X post')
    rich = 'media' in event or 'manual_x_text' in event
    if rich and 'discord' not in event['destinations']:
        raise Invalid('media requires a Discord destination')
    sources = {canonical(source['url']): source for source in event['sources']}
    seen = set()
    for item in media:
        if not isinstance(item, dict) or item.get('kind') not in ('image', 'youtube', 'x'):
            raise Invalid('unsupported media kind')
        kind = item['kind']
        expected = {'kind', 'url', 'source_url', 'official_basis', 'event_evidence'}
        if kind == 'image':
            expected |= {'embed_permission'}
        fields(item, expected, 'media item')
        if kind in seen:
            raise Invalid('duplicate media kind')
        seen.add(kind)
        media_url(item['url'], kind)
        if canonical(item['source_url']) not in sources:
            raise Invalid('media requires a supplied official primary source')
        for field in ('official_basis', 'event_evidence'):
            text(item[field], 'media.' + field, 1000)
        if kind == 'image':
            if urlsplit(item['url']).hostname != urlsplit(item['source_url']).hostname:
                raise Invalid('image host must exactly match its official source host')
            permission = item['embed_permission']
            fields(permission, {'status', 'evidence'}, 'media.embed_permission')
            if permission['status'] != 'granted':
                raise Invalid('image embedding requires confirmed permission')
            text(permission['evidence'], 'media.embed_permission.evidence', 1000)
    if rich:
        for source in event['sources']:
            if re.search(r'[<>`\[\]{}()"\x7f]', source['url']):
                raise Invalid('unsafe rich Discord source URL syntax')
        # Rich mode relocates standalone links; prose is never silently rewritten.
        for line in event['texts']['discord'].splitlines():
            if '://' in line and (line.strip() != line or canonical(line) not in sources):
                raise Invalid('rich Discord source URLs must occupy separate lines')


def _link_identity(url):
    host = urlsplit(url).hostname
    try:
        if host in YOUTUBE_HOSTS:
            return canonical(media_url(url, 'youtube'))
        if host in X_HOSTS:
            return canonical(media_url(url, 'x'))
    except Invalid:
        pass  # A general official source may be a channel/profile rather than media.
    return canonical(url)


def _literal(value):
    return re.sub(r'([\\`*_{}\[\]()<>|~])', r'\\\1', value)


def discord_payload(event):
    """Pure, deterministic formatting of already validated editorial content."""
    content = event['texts']['discord']
    payload = {'content': content, 'allowed_mentions': {'parse': []}}
    if 'media' not in event and 'manual_x_text' not in event:
        return payload  # Existing admitted events keep their exact old payload.
    media = event.get('media', [])
    links = []
    linked = set()
    for item in media:
        if item['kind'] in ('youtube', 'x'):
            url = media_url(item['url'], item['kind'])
            identity = _link_identity(url)
            if identity not in linked:
                links.append(('公式YouTube' if item['kind'] == 'youtube' else '公式X') + '\n' + url)
                linked.add(identity)
    # All primary sources remain accessible even if an image hotlink is blocked.
    # Avoid repeating a native-preview URL inside the rich card.
    sources = []
    for source in event['sources']:
        identity = _link_identity(source['url'])
        if identity in linked:
            continue
        sources.append({'name': '出典 ' + str(len(sources) + 1),
                        'value': source['url'], 'inline': False})
        linked.add(identity)
    summary = '\n'.join(line for line in content.splitlines() if '://' not in line).strip()
    if not summary:
        raise Invalid('rich Discord post requires a reviewed summary')
    embed = {
        'title': _literal(event['work_title']),
        'description': summary,
        'color': 0x5865F2,
        'fields': [
            {'name': '発表', 'value': ' / '.join(LABELS[kind] for kind in event['announcement_kinds']), 'inline': False},
            {'name': '公式初出（JST）', 'value': timestamp(event['published_at']).astimezone(
                timezone(timedelta(hours=9))).strftime('%Y/%m/%d %H:%M:%S JST'), 'inline': False},
            *sources,
        ],
    }
    if 'manual_x_text' in event:
        embed['fields'].append({'name': 'X投稿用（手動コピー）',
                                'value': '```text\n' + event['manual_x_text'] + '\n```',
                                'inline': False})
    for item in media:
        if item['kind'] == 'image':
            embed['image'] = {'url': media_url(item['url'], 'image')}
    payload['content'] = '\n\n'.join(links)
    payload['embeds'] = [embed]
    # Discord's string limits are measured conservatively in UTF-16 code units.
    def size(value):
        return len(value.encode('utf-16-le')) // 2
    if size(payload['content']) > 2000 or size(embed['title']) > 256 or size(summary) > 4096:
        raise Invalid('Discord rendered text exceeds field limit')
    total = size(embed['title']) + size(summary)
    for field in embed['fields']:
        if size(field['name']) > 256 or size(field['value']) > 1024:
            raise Invalid('Discord rendered source or field exceeds limit')
        total += size(field['name']) + size(field['value'])
    if total > 6000:
        raise Invalid('Discord rendered embed exceeds total text limit')
    return payload
