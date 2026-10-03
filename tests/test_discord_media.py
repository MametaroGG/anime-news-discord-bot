import copy
import json
import unittest
from unittest.mock import Mock, patch

from helpers import NOW, empty_ledger, empty_legacy, event
from news_delivery.discord_media import discord_payload, media_url
from news_delivery.publisher import publish
from news_delivery.schema import Invalid, digest, event_keys, validate
from news_delivery.transport import DiscordClient, Outcome


# Deliberately fictional fixture URLs. Tests never fetch any media or post messages.
def media_event():
    candidate = event()
    source = candidate['sources'][0]['url']
    common = {'source_url': source, 'official_basis': '架空の公式サイトから公式投稿者を確認したテスト証拠',
              'event_evidence': '架空の同一発表ページに掲載された素材というテスト証拠'}
    candidate['media'] = [
        dict(common, kind='image', url='https://official.example.jp/assets/visual.jpg',
             embed_permission={'status': 'granted', 'evidence': '架空の権利者による埋め込み許諾のテスト証拠'}),
        dict(common, kind='youtube', url='https://www.youtube.com/watch?v=TEST_ONLY01'),
        dict(common, kind='x', url='https://x.com/OfficialTest/status/1234567890'),
    ]
    return candidate


class DiscordMediaTests(unittest.TestCase):
    def test_legacy_event_retains_exact_payload_and_fingerprint(self):
        candidate = event()
        before = copy.deepcopy(candidate)
        validate(candidate, NOW)
        self.assertEqual(discord_payload(candidate), {
            'content': candidate['texts']['discord'], 'allowed_mentions': {'parse': []}})
        self.assertEqual(candidate, before)
        self.assertEqual(digest(candidate), digest(before))

    def test_rich_card_with_official_image_video_links_and_jst(self):
        candidate = media_event()
        validate(candidate, NOW)
        payload = discord_payload(candidate)
        self.assertEqual(payload['allowed_mentions'], {'parse': []})
        self.assertEqual(payload['content'], '公式YouTube\nhttps://www.youtube.com/watch?v=TEST_ONLY01\n\n公式X\nhttps://x.com/OfficialTest/status/1234567890')
        card = payload['embeds'][0]
        self.assertEqual(card['title'], candidate['work_title'])
        self.assertEqual(card['description'], '架空作品のテスト')
        self.assertEqual(card['image'], {'url': candidate['media'][0]['url']})
        self.assertEqual(card['fields'][0]['value'], '続編決定 / 新PV')
        self.assertEqual(card['fields'][1]['value'], '2026/10/03 11:30:00 JST')
        self.assertEqual(card['fields'][2]['value'], candidate['sources'][0]['url'])
        self.assertNotIn('video', card)
        self.assertNotIn('type', card)
        self.assertNotIn('flags', payload)
        self.assertNotIn('username', payload)
        self.assertNotIn('avatar_url', payload)

    def test_empty_media_explicitly_enables_image_free_card(self):
        candidate = event()
        candidate['media'] = []
        validate(candidate, NOW)
        payload = discord_payload(candidate)
        self.assertEqual(payload['content'], '')
        self.assertNotIn('image', payload['embeds'][0])

    def test_missing_image_permission_does_not_silently_embed(self):
        for permission in ({'status': 'unknown', 'evidence': '公式だから'},
                           {'status': 'granted', 'evidence': ''},
                           {'status': 'granted', 'evidence': 'ok', 'extra': True}):
            candidate = media_event()
            candidate['media'][0]['embed_permission'] = permission
            with self.subTest(permission=permission), self.assertRaises(Invalid):
                validate(candidate, NOW)

    def test_image_requires_same_official_source_host_and_direct_file(self):
        for url in ('https://mirror.example.jp/visual.jpg',
                    'https://cdn.official.example.jp/visual.jpg',
                    'https://pbs.twimg.com/media/picture.jpg',
                    'https://official.example.jp/visual.svg',
                    'https://official.example.jp/visual.mp4',
                    'https://official.example.jp/visual.jpg?redirect=evil',
                    'https://official.example.jp/visual.jpg#fragment'):
            candidate = media_event()
            candidate['media'][0]['url'] = url
            with self.subTest(url=url), self.assertRaises(Invalid):
                validate(candidate, NOW)

    def test_media_https_public_host_and_syntax_fail_closed(self):
        for url in ('http://official.example.jp/a.jpg', 'https://127.0.0.1/a.jpg',
                    'https://localhost/a.jpg', 'https://name:pass@official.example.jp/a.jpg',
                    'https://official.example.jp:443/a.jpg', 'https://official.example.jp/a.jpg\n',
                    'https://official.example.jp/\\a.jpg', 'https://official.example.jp/a%0a.jpg',
                    'https://official.example.jp/a%2f.jpg', 'https://official.example.jp/<a>.jpg'):
            with self.subTest(url=url), self.assertRaises(Invalid):
                media_url(url, 'image')

    def test_media_schema_unknown_missing_and_duplicate_rejected(self):
        mutations = [lambda e: e.update(media={}), lambda e: e.update(media=None),
                     lambda e: e['media'].append(copy.deepcopy(e['media'][0])),
                     lambda e: e['media'][1].update(kind='vimeo'),
                     lambda e: e['media'][1].update(kind=[]),
                     lambda e: e['media'][1].update(official_basis=''),
                     lambda e: e['media'][1].pop('event_evidence'),
                     lambda e: e['media'][1].update(event_evidence=''),
                     lambda e: e['media'][1].update(extra='unreviewed'),
                     lambda e: e['media'][1].update(source_url='https://other.example.jp/news'),
                     lambda e: e['media'][1].update(url='https://www.youtube.com/watch?v=' + 'a' * 1001)]
        for mutation in mutations:
            candidate = media_event()
            mutation(candidate)
            with self.subTest(mutation=mutation), self.assertRaises(Invalid):
                validate(candidate, NOW)

    def test_youtube_and_x_only_allow_original_single_resource_urls(self):
        for kind, url in [('youtube', 'https://youtube.com.evil.example/watch?v=TEST_ONLY01'),
                          ('youtube', 'https://youtube.com/redirect?q=evil'),
                          ('youtube', 'https://youtube.com/playlist?list=abc'),
                          ('youtube', 'https://www.youtube.com/embed/TEST_ONLY01'),
                          ('youtube', 'https://youtu.be/TEST_ONLY01?v=DIFFERENT01'),
                          ('youtube', 'https://www.youtube.com/watch?v=TEST_ONLY01&v=DIFFERENT01'),
                          ('youtube', 'https://www.youtube.com/watch?v=TEST_ONLY01&list=abc'),
                          ('x', 'https://fxtwitter.com/Official/status/123'),
                          ('x', 'https://vxtwitter.com/Official/status/123'),
                          ('x', 'https://x.com.evil.example/Official/status/123'),
                          ('x', 'https://x.com/Official'),
                          ('x', 'https://video.twimg.com/file.mp4'),
                          ('x', 'https://x.com/Official/status/123?url=evil')]:
            with self.subTest(kind=kind, url=url), self.assertRaises(Invalid):
                media_url(url, kind)

    def test_native_video_tracking_is_removed_without_proxy(self):
        for url in ('https://youtu.be/TEST_ONLY01?si=tracking',
                    'https://m.youtube.com/shorts/TEST_ONLY01',
                    'https://www.youtube.com/live/TEST_ONLY01',
                    'https://www.youtube.com/watch?v=TEST_ONLY01&t=30s'):
            self.assertEqual(media_url(url, 'youtube'), 'https://www.youtube.com/watch?v=TEST_ONLY01')
        self.assertEqual(media_url('https://twitter.com/Official/status/123?s=20', 'x'),
                         'https://x.com/Official/status/123')

    def test_extra_source_cannot_inject_markdown_links_into_card(self):
        for ending in ('>[details](https://unreviewed.example/story)',
                       '[details](https://unreviewed.example/story)', 'bad\x7f'):
            candidate = media_event()
            source = copy.deepcopy(candidate['sources'][0])
            source['url'] = 'https://official.example.jp/news/' + ending
            candidate['sources'].append(source)
            with self.subTest(ending=ending), self.assertRaises(Invalid):
                validate(candidate, NOW)

    def test_templates_are_unapproved_and_not_sendable(self):
        from pathlib import Path
        for filename in ('event-template.json', 'event-media-template.json'):
            candidate = json.loads((Path(__file__).resolve().parents[1] / 'docs' / filename).read_text())
            with self.subTest(filename=filename), self.assertRaises(Invalid):
                validate(candidate, NOW)

    def test_same_x_link_not_repeated_in_card_sources(self):
        candidate = media_event()
        source = copy.deepcopy(candidate['sources'][0])
        source['url'] = 'https://twitter.com/OfficialTest/status/1234567890?s=20'
        candidate['sources'].append(source)
        validate(candidate, NOW)
        payload = discord_payload(candidate)
        self.assertNotIn(source['url'], json.dumps(payload))
        self.assertEqual(json.dumps(payload).count('https://x.com/OfficialTest/status/1234567890'), 1)

    def test_same_youtube_short_link_not_repeated_in_card_sources(self):
        candidate = media_event()
        source = copy.deepcopy(candidate['sources'][0])
        source['url'] = 'https://youtu.be/TEST_ONLY01?si=tracking'
        candidate['sources'].append(source)
        validate(candidate, NOW)
        payload = discord_payload(candidate)
        self.assertNotIn(source['url'], json.dumps(payload))
        self.assertEqual(json.dumps(payload).count('https://www.youtube.com/watch?v=TEST_ONLY01'), 1)

    def test_media_does_not_change_deduplication_identity_but_is_fingerprinted(self):
        plain, rich = event(), media_event()
        self.assertEqual(event_keys(plain), event_keys(rich))
        self.assertNotEqual(digest(plain), digest(rich))
        changed = copy.deepcopy(rich)
        changed['media'][0]['url'] = 'https://official.example.jp/assets/new.jpg'
        self.assertEqual(event_keys(rich), event_keys(changed))
        self.assertNotEqual(digest(rich), digest(changed))

    def test_rich_links_must_be_separate_from_approved_prose(self):
        for summary in ('本文 ' + event()['sources'][0]['url'],
                        '[' + '出典' + '](' + event()['sources'][0]['url'] + ')',
                        event()['sources'][0]['url']):
            candidate = media_event()
            candidate['texts']['discord'] = summary
            with self.subTest(summary=summary), self.assertRaises(Invalid):
                validate(candidate, NOW)

    def test_discord_only_media_does_not_change_x_text_or_account_guard(self):
        candidate = media_event()
        discord, x = Mock(), Mock()
        for client in (discord, x):
            client.send.return_value = Outcome('sent', 'confirmed', '123')
        publish([candidate], empty_ledger(), empty_legacy(), lambda _: None,
                {'discord': lambda: discord, 'x': lambda: x}, NOW, dry_run=False)
        self.assertEqual(discord.send.call_args.args[0], discord_payload(candidate))
        self.assertEqual(x.send.call_args.args[0], candidate['texts']['x'])

    def test_mutation_after_durable_admission_stops_before_any_send(self):
        ledger, candidate = empty_ledger(), media_event()
        client = Mock()
        client.send.return_value = Outcome('sent', 'confirmed', '123')
        publish([candidate], ledger, empty_legacy(), lambda _: None,
                {'discord': lambda: client}, NOW, dry_run=False)
        candidate['media'][0]['url'] = 'https://official.example.jp/assets/other.jpg'
        with self.assertRaises(Invalid):
            publish([candidate], ledger, empty_legacy(), lambda _: None,
                    {'discord': lambda: client}, NOW, dry_run=False)
        self.assertEqual(client.send.call_count, 1)

    def test_rich_limits_checked_before_any_checkpoint_or_client(self):
        candidate = media_event()
        # Legacy allows 200 Unicode characters, but Discord titles allow 256 UTF-16 units.
        candidate['work_title'] = '😀' * 129
        checkpoint, factory = Mock(), Mock()
        with self.assertRaises(Invalid):
            publish([candidate], empty_ledger(), empty_legacy(), checkpoint,
                    {'discord': factory}, NOW, dry_run=False)
        checkpoint.assert_not_called()
        factory.assert_not_called()

    def test_rich_source_field_limit_and_total_limit(self):
        for count, path_size in ((1, 1050), (8, 850)):
            candidate = media_event()
            candidate['sources'] = []
            for i in range(count):
                source = copy.deepcopy(event()['sources'][0])
                source['url'] = 'https://official.example.jp/' + str(i) + 'a' * path_size
                candidate['sources'].append(source)
            first = candidate['sources'][0]['url']
            for fact in candidate['facts']:
                fact['source_urls'] = [first]
            candidate['texts']['discord'] = 'テスト\n' + first
            candidate['texts']['x'] = 'テスト\n' + first
            for item in candidate['media']:
                item['source_url'] = first
            with self.subTest(count=count), self.assertRaises(Invalid):
                validate(candidate, NOW)

    def test_media_cannot_be_added_to_x_only_event(self):
        candidate = media_event()
        candidate['destinations'] = ['x']
        del candidate['texts']['discord']
        with self.assertRaises(Invalid):
            validate(candidate, NOW)

    def test_mock_http_payload_and_no_media_fetch_or_reupload(self):
        candidate = media_event()
        validate(candidate, NOW)
        session = Mock()
        session.request.return_value.status_code = 200
        session.request.return_value.json.return_value = {'id': '123'}
        session.request.return_value.headers = {}
        with patch('requests.get', side_effect=AssertionError('no downloads')):
            result = DiscordClient('https://discord.com/api/webhooks/123/fake-token', session).send(
                discord_payload(candidate), NOW)
        self.assertEqual(result.status, 'sent')
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(session.request.call_args.args[0], 'POST')
        kwargs = session.request.call_args.kwargs
        self.assertEqual(kwargs['json'], discord_payload(candidate))
        self.assertNotIn('files', kwargs)
        self.assertFalse(kwargs['allow_redirects'])

    def test_manual_x_copy_is_one_discord_message_and_no_x_client(self):
        candidate = media_event()
        candidate['destinations'] = ['discord']
        candidate['manual_x_text'] = candidate['texts'].pop('x')
        validate(candidate, NOW)
        payload = discord_payload(candidate)
        copy_field = payload['embeds'][0]['fields'][-1]
        self.assertEqual(copy_field['name'], 'X投稿用（手動コピー）')
        self.assertEqual(copy_field['value'], '```text\n' + candidate['manual_x_text'] + '\n```')
        client = Mock()
        client.send.return_value = Outcome('sent', 'confirmed', '123')
        forbidden = Mock(side_effect=AssertionError('no X API or credentials'))
        ledger = empty_ledger()
        publish([candidate], ledger, empty_legacy(), lambda _: None,
                {'discord': lambda: client, 'x': forbidden}, NOW, dry_run=False)
        self.assertEqual(client.send.call_count, 1)
        forbidden.assert_not_called()
        self.assertEqual(set(ledger['events'][candidate['event_id']]['deliveries']), {'discord'})

    def test_manual_copy_enables_card_without_media_field(self):
        candidate = event()
        candidate['destinations'] = ['discord']
        candidate['manual_x_text'] = candidate['texts'].pop('x')
        validate(candidate, NOW)
        self.assertIn('embeds', discord_payload(candidate))
        self.assertNotIn('image', discord_payload(candidate)['embeds'][0])

    def test_manual_x_copy_uses_same_length_sources_and_mention_safety(self):
        for content in ('リンクなし', 'あ' * 140 + '\n' + event()['sources'][0]['url'],
                        '@someone テスト\n' + event()['sources'][0]['url'],
                        '```突破\n' + event()['sources'][0]['url'],
                        'テスト\nhttps://unreviewed.example/story'):
            candidate = media_event()
            candidate['destinations'] = ['discord']
            candidate['texts'].pop('x')
            candidate['manual_x_text'] = content
            with self.subTest(content=content), self.assertRaises(Invalid):
                validate(candidate, NOW)

    def test_mentions_after_japanese_and_fullwidth_at_are_rejected(self):
        for prefix in ('日本語@someone', '＠someone', '連結＠someone'):
            for manual in (True, False):
                candidate = event()
                content = prefix + '\n' + candidate['sources'][0]['url']
                if manual:
                    candidate['destinations'] = ['discord']
                    candidate['texts'].pop('x')
                    candidate['manual_x_text'] = content
                else:
                    candidate['texts']['x'] = content
                with self.subTest(prefix=prefix, manual=manual), self.assertRaises(Invalid):
                    validate(candidate, NOW)

    def test_manual_copy_cannot_also_target_x_api(self):
        candidate = media_event()
        candidate['manual_x_text'] = candidate['texts']['x']
        with self.assertRaises(Invalid):
            validate(candidate, NOW)

    def test_manual_copy_is_fingerprinted_and_cannot_be_silently_changed(self):
        candidate = media_event()
        candidate['destinations'] = ['discord']
        candidate['manual_x_text'] = candidate['texts'].pop('x')
        before = digest(candidate)
        candidate['manual_x_text'] = '変更\n' + candidate['sources'][0]['url']
        self.assertNotEqual(before, digest(candidate))

    def test_media_dry_run_is_side_effect_free(self):
        ledger = empty_ledger()
        original = copy.deepcopy(ledger)
        forbidden = Mock(side_effect=AssertionError('unexpected side effect'))
        result = publish([media_event()], ledger, empty_legacy(), forbidden,
                         {'discord': forbidden, 'x': forbidden}, NOW, dry_run=True)
        self.assertEqual(result['ready'], 2)
        self.assertEqual(ledger, original)
        forbidden.assert_not_called()


if __name__ == '__main__':
    unittest.main()
