from datetime import datetime, timezone
from news_delivery.schema import CHECKS

NOW = datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc)


def event(event_id='example-season2-announcement', kinds=None):
    kinds = kinds or ['sequel', 'new_pv']
    url = 'https://official.example.jp/news/20261003-new/'
    return {
        'schema_version': 1, 'event_id': event_id, 'work_key': 'example-season2',
        'work_title': 'テスト用の架空作品 第2期', 'announcement_key': '20261003-production-pv',
        'announcement_kinds': kinds, 'published_at': '2026-10-03T11:30:00+09:00',
        'verified_at': '2026-10-03T11:40:00+09:00', 'queued_at': '2026-10-03T11:45:00+09:00',
        'sources': [{'url': url, 'title': '第2期制作決定・PV公開', 'publisher': '作品公式',
                     'published_at': '2026-10-03T11:30:00+09:00',
                     'timestamp_evidence': 'テスト用の日時証拠。実在するニュースではない',
                     'official_basis': 'テスト用の公式関係の証拠'}],
        'facts': [{'kind': kind, 'text': 'テスト用の事実', 'source_urls': [url]} for kind in kinds],
        'approval': {'status': 'approved', 'reviewed_by': 'unit-test-fixture',
                     'approved_at': '2026-10-03T11:42:00+09:00',
                     'checks': {check: True for check in CHECKS}},
        'destinations': ['discord', 'x'],
        'texts': {'discord': '架空作品のテスト\n' + url, 'x': '架空作品のテスト\n' + url},
        'dedupe_aliases': [],
    }


def empty_ledger():
    return {'schema_version': 1, 'events': {}, 'keys': {}, 'audit': []}


def empty_legacy():
    return {'initialized': True, 'seen': {}, 'stories': {}}
