"""Native X link formatting only; no source lookup, downloads or live posting."""
import copy
import unittest
from unittest.mock import Mock

from helpers import NOW, empty_ledger, empty_legacy, event
from news_delivery.discord_media import discord_payload, media_url
from news_delivery.publisher import publish
from news_delivery.schema import Invalid, canonical, digest, event_keys, validate
from news_delivery.transport import Outcome


# User-supplied URL is a format regression case, not a verified news fixture.
POST = 'https://x.com/hirayasumi0426/status/2104707978460803552'
VIDEO = POST + '/video/1'


def video_event(index=1):
    candidate = event()
    candidate['destinations'] = ['discord']
    candidate['sources'][0]['url'] = POST
    for fact in candidate['facts']:
        fact['source_urls'] = [POST]
    candidate['texts'] = {'discord': '架空作品のテスト\n' + POST}
    candidate['manual_x_text'] = '架空作品のテスト\n' + POST + '/video/' + str(index)
    candidate['media'] = [{
        'kind': 'x', 'url': POST + '/video/' + str(index), 'source_url': POST,
        'official_basis': '形式検証のみの架空の根拠。実際の投稿は未取得',
        'event_evidence': '形式検証のみの架空の根拠。実際のニュースではない',
    }]
    return candidate


class XVideoLinkTests(unittest.TestCase):
    def test_exact_user_video_link_is_preserved(self):
        self.assertEqual(media_url(VIDEO, 'x'), VIDEO)

    def test_supported_hosts_tracking_and_positive_indexes(self):
        for host in ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'):
            for index in (1, 2, 12):
                path = '/Official/status/123/video/' + str(index)
                with self.subTest(host=host, index=index):
                    self.assertEqual(media_url('https://' + host + path + '?s=20&t=share', 'x'),
                                     'https://x.com' + path)

    def test_malformed_video_paths_are_not_media(self):
        for suffix in ('/video', '/video/', '/video/0', '/video/01', '/video/-1',
                       '/video/+1', '/video/1.0', '/video/１', '/video/1/',
                       '/video/1/extra', '/video/1/video/2', '/photo/1',
                       '/video/%31', '/video/1%2fextra', '/video/1%0a'):
            with self.subTest(suffix=suffix), self.assertRaises(Invalid):
                media_url(POST + suffix, 'x')

    def test_video_links_keep_existing_host_and_url_safety(self):
        for url in (VIDEO.replace('https:', 'http:'),
                    VIDEO.replace('x.com', 'x.com.evil.example'),
                    VIDEO.replace('x.com', 'evil.x.com'),
                    VIDEO.replace('x.com', 'fxtwitter.com'),
                    VIDEO.replace('x.com', 'vxtwitter.com'),
                    VIDEO.replace('x.com', 'video.twimg.com'),
                    VIDEO.replace('x.com', '127.0.0.1'),
                    VIDEO.replace('x.com', 'user:password@x.com'),
                    VIDEO.replace('x.com', 'x.com:443'),
                    VIDEO + '?url=https://evil.example', VIDEO + '#fragment',
                    VIDEO + '\n', VIDEO + '/<script>'):
            with self.subTest(url=url), self.assertRaises(Invalid):
                media_url(url, 'x')

    def test_video_indexes_and_host_aliases_share_original_post_identity(self):
        expected = 'https://x.com/i/status/2104707978460803552'
        for url in (POST, VIDEO, POST + '/video/2', VIDEO + '?s=20&t=share',
                    VIDEO.replace('x.com', 'www.twitter.com'),
                    VIDEO.replace('hirayasumi0426', 'ChangedName')):
            with self.subTest(url=url):
                self.assertEqual(canonical(url), expected)

    def test_general_sources_stay_broader_than_media(self):
        for url in ('https://x.com/Official?lang=ja',
                    'https://official.example.jp/Official/status/123/video/1',
                    POST + '/video/0', POST + '/photo/1', POST + '/video/1/extra'):
            with self.subTest(url=url):
                self.assertEqual(canonical(url), url)
                with self.assertRaises(Invalid):
                    media_url(url, 'x')
        # Preserve legacy general-source alias behavior outside strict media syntax.
        self.assertEqual(canonical('https://x.com/a-longer-general-source/status/123'),
                         'https://x.com/i/status/123')

    def test_video_link_outside_copy_block_and_manual_copy_are_preserved(self):
        candidate = video_event()
        before = copy.deepcopy(candidate)
        validate(candidate, NOW)
        payload = discord_payload(candidate)
        self.assertEqual(candidate, before)
        expected = ('テスト用の架空作品 第2期\n\n架空作品のテスト\n\n'
                    '発表：続編決定 / 新PV\n\n公式初出：2026/10/03 11:30:00 JST\n\n'
                    '公式X\n' + VIDEO + '\n\nX投稿用（手動コピー）\n```text\n'
                    + candidate['manual_x_text'] + '\n```')
        self.assertEqual(payload['content'], expected)
        active, manual = payload['content'].split('```text\n')
        self.assertEqual([line for line in active.splitlines() if line.startswith('https://')],
                         [VIDEO])
        self.assertNotIn('出典 ', active)
        self.assertEqual(manual, candidate['manual_x_text'] + '\n```')
        self.assertEqual(set(payload), {'content', 'allowed_mentions'})
        self.assertEqual(payload['allowed_mentions'], {'parse': []})
        self.assertNotIn('embeds', payload)
        self.assertNotIn('flags', payload)

    def test_video_source_and_base_media_are_not_repeated_in_active_urls(self):
        candidate = video_event()
        candidate['sources'][0]['url'] = VIDEO
        candidate['texts']['discord'] = '架空作品のテスト\n' + VIDEO
        candidate['media'][0]['url'] = POST
        validate(candidate, NOW)
        payload = discord_payload(candidate)
        active, manual = payload['content'].split('```text\n')
        self.assertEqual([line for line in active.splitlines() if line.startswith('https://')],
                         [POST])
        self.assertNotIn('出典 ', active)
        self.assertEqual(manual, candidate['manual_x_text'] + '\n```')

    def test_video_media_dedupes_tracked_host_and_username_source_aliases(self):
        for source_url in (POST, VIDEO, POST + '/video/2',
                           VIDEO.replace('x.com', 'www.twitter.com') + '?s=20&t=share',
                           POST.replace('hirayasumi0426', 'ChangedName')):
            candidate = video_event()
            candidate['sources'][0]['url'] = source_url
            candidate['texts']['discord'] = '架空作品のテスト\n' + source_url
            candidate['manual_x_text'] = '架空作品のテスト\n' + source_url
            with self.subTest(source_url=source_url):
                validate(candidate, NOW)
                content = discord_payload(candidate)['content']
                active, manual = content.split('```text\n')
                self.assertEqual([line for line in active.splitlines() if line.startswith('https://')],
                                 [VIDEO])
                self.assertEqual(active.count(VIDEO), 1)
                self.assertNotIn('出典 ', active)
                self.assertEqual(manual, candidate['manual_x_text'] + '\n```')

    def test_video_and_post_cannot_count_as_distinct_primary_sources(self):
        candidate = video_event()
        source = copy.deepcopy(candidate['sources'][0])
        source['url'] = VIDEO
        candidate['sources'].append(source)
        with self.assertRaisesRegex(Invalid, 'sources must be distinct'):
            validate(candidate, NOW)

    def test_changing_video_index_changes_fingerprint_not_identity(self):
        first, second = video_event(1), video_event(2)
        self.assertEqual(event_keys(first), event_keys(second))
        self.assertNotEqual(digest(first), digest(second))
        ledger, client = empty_ledger(), Mock()
        client.send.return_value = Outcome('sent', 'confirmed', '123')
        publish([first], ledger, empty_legacy(), lambda _: None,
                {'discord': lambda: client}, NOW, dry_run=False)
        with self.assertRaisesRegex(Invalid, 'queued event changed after admission'):
            publish([second], ledger, empty_legacy(), lambda _: None,
                    {'discord': lambda: client}, NOW, dry_run=False)
        self.assertEqual(client.send.call_count, 1)

    def test_different_events_for_base_and_video_share_one_delivery(self):
        first, second = video_event(1), video_event(2)
        second['event_id'] = 'other-video-announcement'
        second['announcement_key'] = 'another-description-of-same-post'
        second['sources'][0]['url'] = POST + '/video/2'
        second['texts']['discord'] = '架空作品のテスト\n' + POST + '/video/2'
        client = Mock()
        client.send.return_value = Outcome('sent', 'confirmed', '123')
        summary = publish([first, second], empty_ledger(), empty_legacy(), lambda _: None,
                          {'discord': lambda: client}, NOW, dry_run=False)
        self.assertEqual(summary['duplicate'], 1)
        self.assertEqual(client.send.call_count, 1)


if __name__ == '__main__':
    unittest.main()
