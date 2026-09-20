import argparse
import hashlib
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'data' / 'seen.json'
FEEDS = ROOT / 'feeds.json'
JST = timezone(timedelta(hours=9))
HEADERS = {'User-Agent': 'AnimeNewsDiscordBot/1.0 (RSS monitor; contact repository owner)'}
PATTERNS = [
    ('TVアニメ化', re.compile(r'(?:TV|テレビ)?アニメ化(?:が|を|は|決定|発表|へ|！|!|、|\s|$)|アニメ化決定')),
    ('続編制作', re.compile(r'(?:第[2-9二三四五六七八九]期|[2-9]期|続編|新シリーズ|Season\s*[2-9]|シーズン[2-9]).{0,18}(?:制作決定|製作決定|放送決定|アニメ化決定)|(?:制作決定|製作決定).{0,12}(?:続編|第[2-9二三四五六七八九]期)')),
    ('劇場版制作', re.compile(r'(?:劇場版|アニメ映画|映画化).{0,18}(?:制作決定|製作決定|アニメ化決定|公開決定)|(?:制作決定|製作決定).{0,12}(?:劇場版|アニメ映画)')),
]
EXCLUDE = re.compile(r'(?:実写(?:映画|ドラマ)化|舞台化|ミュージカル化|アニメ化してほしい|アニメ化希望|アニメ化するなら|アニメ化を予想|アニメ化の噂|アニメ化のうわさ|アニメ化は未定)')


def canonical(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return ''
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/') or '/', query, ''))


def key_for(entry):
    return hashlib.sha256(canonical(entry['url']).encode('utf-8')).hexdigest()


def classify(title):
    title = html.unescape(re.sub('<[^>]+>', '', title))
    if EXCLUDE.search(title):
        return None
    for label, pattern in PATTERNS:
        if pattern.search(title):
            return label
    return None


def load_state():
    if not STATE.exists():
        return {'initialized': False, 'seen': {}}
    raw = json.loads(STATE.read_text(encoding='utf-8'))
    return {'initialized': bool(raw.get('initialized')), 'seen': dict(raw.get('seen', {}))}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(STATE)


def fetch_entries():
    sources = json.loads(FEEDS.read_text(encoding='utf-8'))
    if not sources:
        raise RuntimeError('feeds.json にRSSを登録してください')
    results, successes = {}, 0
    for source in sources:
        try:
            resp = requests.get(source['url'], headers=HEADERS, timeout=20)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if not parsed.entries and parsed.bozo:
                raise ValueError(f'RSS解析エラー: {parsed.bozo_exception}')
            successes += 1
            for item in parsed.entries:
                url = canonical(item.get('link', ''))
                title = html.unescape(re.sub('<[^>]+>', '', item.get('title', ''))).strip()
                if not url or not title:
                    continue
                stamp = item.get('published_parsed') or item.get('updated_parsed')
                published = datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else None
                results[url] = {'url': url, 'title': title, 'source': source['name'], 'published': published}
        except (requests.RequestException, ValueError, KeyError) as exc:
            logging.warning('RSS取得失敗 [%s]: %s', source.get('name'), exc)
    if successes == 0:
        raise RuntimeError('全RSSの取得に失敗。既読状態は更新しません')
    return list(results.values())


def post(webhook, entry, category):
    if not webhook.startswith('https://discord.com/api/webhooks/') and not webhook.startswith('https://discordapp.com/api/webhooks/'):
        raise ValueError('Discord Webhook URLの形式が正しくありません')
    payload = {
        'username': 'アニメ化速報Bot',
        'allowed_mentions': {'parse': []},
        'embeds': [{
            'title': ('🚨【' + category + '】 ' + entry['title'])[:256],
            'url': entry['url'],
            'description': 'RSSで検出した候補です。公式発表かどうかはリンク先で確認してください。',
            'color': 0x16A34A,
            'fields': [{'name': '情報源', 'value': entry['source'][:1024], 'inline': True}],
            'footer': {'text': '自動収集 / 公式確認は未実施'},
        }],
    }
    if entry['published']:
        payload['embeds'][0]['timestamp'] = entry['published'].isoformat()
    for attempt in range(3):
        response = requests.post(webhook, json=payload, params={'wait': 'true'}, timeout=20)
        if response.status_code == 429 and attempt < 2:
            delay = min(float(response.json().get('retry_after', 2)), 30)
            time.sleep(delay)
            continue
        response.raise_for_status()
        if not response.json().get('id'):
            raise RuntimeError('Discordの投稿IDを確認できませんでした')
        return


def run(dry_run=False, test_post=False):
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '')
    if not dry_run and not webhook:
        raise RuntimeError('DISCORD_WEBHOOK_URL をGitHub Secretsに登録してください')
    if test_post:
        example = {'title': 'テスト作品 TVアニメ化決定（架空の情報）', 'url': 'https://example.com/', 'source': '動作確認', 'published': datetime.now(timezone.utc)}
        if dry_run:
            print('DRY RUN: Discordテスト投稿')
        else:
            post(webhook, example, 'テスト')
            print('Discordテスト投稿成功')
        return
    state = load_state()
    entries = fetch_entries()
    now = datetime.now(timezone.utc)
    # First run seeds all existing articles, preventing a flood of historical news.
    if not state['initialized']:
        for entry in entries:
            state['seen'][key_for(entry)] = now.isoformat()
        state['initialized'] = True
        if not dry_run:
            save_state(state)
        print(f'初回起動: {len(entries)}件を既読登録。投稿はしません。')
        return
    candidates = []
    for entry in entries:
        key = key_for(entry)
        if key in state['seen']:
            continue
        # Entries without timestamps are skipped for safety, but marked seen.
        if not entry['published'] or entry['published'] < now - timedelta(hours=6) or entry['published'] > now + timedelta(minutes=10):
            state['seen'][key] = now.isoformat()
            continue
        category = classify(entry['title'])
        if category:
            candidates.append((entry, category, key))
        else:
            state['seen'][key] = now.isoformat()
    candidates.sort(key=lambda row: row[0]['published'])
    for entry, category, key in candidates[:10]:
        if dry_run:
            print(f'DRY RUN [{category}] {entry["title"]} {entry["url"]}')
        else:
            post(webhook, entry, category)
            state['seen'][key] = now.isoformat()
            save_state(state)  # Save after each successful post.
            print(f'投稿成功 [{category}] {entry["title"]}')
    # Keep recent history for 90 days.
    cutoff = now - timedelta(days=90)
    state['seen'] = {k: v for k, v in state['seen'].items() if datetime.fromisoformat(v) >= cutoff}
    if not dry_run:
        save_state(state)
    print(f'取得 {len(entries)}件 / 投稿候補 {len(candidates)}件 / 今回処理 {min(len(candidates), 10)}件')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--test-post', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    run(args.dry_run, args.test_post)
