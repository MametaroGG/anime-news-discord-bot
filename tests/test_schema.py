import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import NOW, event
from news_delivery.schema import Invalid, canonical, event_keys, read_json, read_queue, validate


class SchemaTests(unittest.TestCase):
    def test_complete_jst_event(self):
        self.assertEqual(validate(event(), NOW)['work_key'], 'example-season2')

    def test_reject_unapproved_excluded_unknown_and_missing_fields(self):
        mutations = [lambda e: e['approval'].update(status='draft'),
                     lambda e: e['approval']['checks'].update(official_primary=False),
                     lambda e: e.update(announcement_kinds=['goods']),
                     lambda e: e.update(announcement_kinds=['rumor']),
                     lambda e: e.update(announcement_kinds=[{}]),
                     lambda e: e.update(discovery_time='2026-10-03'),
                     lambda e: e['sources'][0].pop('timestamp_evidence'),
                     lambda e: e.update(schema_version=True),
                     lambda e: e.update(destinations=['x', 'x'])]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                candidate = event()
                mutate(candidate)
                with self.assertRaises(Invalid):
                    validate(candidate, NOW)

    def test_no_fuzzy_dates_or_old_announcements_revived_by_verification(self):
        for value in ('2026-10-03', '2026-10-03T02:30:00', 'yesterday',
                      '2026-10-02T02:30:00Z', '2026-10-03T04:30:00Z'):
            with self.subTest(value=value):
                candidate = event()
                candidate['published_at'] = value
                candidate['sources'][0]['published_at'] = value
                with self.assertRaises(Invalid):
                    validate(candidate, NOW)

    def test_exact_hour_boundary_and_future_queue(self):
        candidate = event()
        candidate['published_at'] = candidate['sources'][0]['published_at'] = '2026-10-03T01:45:00Z'
        validate(candidate, NOW)
        candidate['published_at'] = candidate['sources'][0]['published_at'] = '2026-10-03T01:44:59Z'
        with self.assertRaises(Invalid):
            validate(candidate, NOW)
        with self.assertRaises(Invalid):
            validate(event(), NOW - timedelta(hours=1))

    def test_primary_sources_and_fact_links_required(self):
        mutations = [lambda e: e.update(sources=[]),
                     lambda e: e['facts'][0].update(source_urls=['https://news.example.net/article']),
                     lambda e: e['sources'][0].update(official_basis=''),
                     lambda e: e['facts'].pop(),
                     lambda e: e['texts'].update(x='根拠リンクなし')]
        for mutate in mutations:
            candidate = event()
            mutate(candidate)
            with self.assertRaises(Invalid):
                validate(candidate, NOW)

    def test_other_uri_schemes_and_uppercase_urls_are_rejected(self):
        for extra in ('http://evil.example/a', 'HTTPS://evil.example/a',
                      'ftp://evil.example/a', 'mailto:private@example.jp', 'javascript:alert(1)'):
            candidate = event()
            candidate['texts']['discord'] += '\n' + extra
            with self.subTest(extra=extra), self.assertRaises(Invalid):
                validate(candidate, NOW)

    def test_unicode_x_weight_and_mentions_are_never_silently_trimmed(self):
        candidate = event()
        candidate['texts']['x'] = 'あ' * 140 + '\n' + candidate['sources'][0]['url']
        with self.assertRaises(Invalid):
            validate(candidate, NOW)
        candidate['texts']['x'] = '@someone テスト\n' + candidate['sources'][0]['url']
        with self.assertRaises(Invalid):
            validate(candidate, NOW)

    def test_canonical_tracking_and_https_safety(self):
        self.assertEqual(canonical('https://Official.example.jp/a/?utm_source=x&b=2#top'),
                         'https://official.example.jp/a?b=2')
        for url in ('http://example.jp/a', 'https://a:b@example.jp/a', 'https://127.0.0.1/a',
                    'https://localhost/a', 'https://example.jp:444/a', 'https://example.jp/\\evil'):
            with self.subTest(url=url), self.assertRaises(Invalid):
                canonical(url)

    def test_x_aliases_and_work_seasons(self):
        a, b = event(), event('example-season3-announcement')
        b['work_key'] = 'example-season3'
        b['sources'][0]['url'] = 'https://official.example.jp/season3/news/1'
        self.assertNotEqual(event_keys(a)[0], event_keys(b)[0])
        self.assertEqual(canonical('https://twitter.com/Official/status/123?s=20'),
                         canonical('https://x.com/Official/status/123?t=xxx'))

    def test_duplicate_json_keys_symlink_filename_and_size(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'bad.json'
            path.write_text('{"status":"draft","status":"approved"}')
            with self.assertRaises(Invalid):
                read_json(path)
            path.write_text(json.dumps(event()))
            with self.assertRaises(Invalid):
                read_queue(root, NOW)
            with self.assertRaises(Invalid):
                read_json(path, 4)
            link = root / 'linked.json'
            link.symlink_to(path)
            with self.assertRaises(Invalid):
                read_json(link)
            path.write_text('{"x":NaN}')
            with self.assertRaises(Invalid):
                read_json(path)


if __name__ == '__main__':
    unittest.main()
