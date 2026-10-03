"""Full plain-news rendering contracts. All publication and HTTP calls are mocked."""
import copy
import unittest
from unittest.mock import Mock

from helpers import NOW, empty_ledger, empty_legacy, event
from news_delivery.discord_media import discord_payload
from news_delivery.publisher import publish
from news_delivery.schema import Invalid, digest, validate
from news_delivery.transport import Outcome
from test_discord_media import media_event


def utf16_units(value):
    return len(value.encode('utf-16-le')) // 2


def boundary_event(units, *, rich=False, emoji=False, empty_media=False):
    """Build an independently specified complete message, not a clipped renderer output."""
    candidate = media_event() if rich else event()
    candidate['destinations'] = ['discord']
    x_text = candidate['texts'].pop('x')
    candidate['work_title'] = '題' * 100
    source = candidate['sources'][0]['url']
    if empty_media:
        candidate['media'] = []
    if rich:
        candidate['manual_x_text'] = x_text
    raw_prefix = '*確認* ' + ('😀' * 100 if emoji else '')
    head = candidate['work_title'] + '\n\n\\*確認\\* ' + ('😀' * 100 if emoji else '')
    tail = '\n\n発表：続編決定 / 新PV\n\n公式初出：2026/10/03 11:30:00 JST'
    if rich:
        tail += ('\n\n公式YouTube\nhttps://www.youtube.com/watch?v=TEST_ONLY01'
                 '\n\n公式X\nhttps://x.com/OfficialTest/status/1234567890'
                 '\n\n公式画像\nhttps://official.example.jp/assets/visual.jpg')
    tail += '\n\n出典 1\n' + source
    if rich:
        tail += '\n\nX投稿用（手動コピー）\n```text\n' + x_text + '\n```'
    filler = 'あ' * (units - utf16_units(head + tail))
    candidate['texts']['discord'] = raw_prefix + filler + '\n' + source
    return candidate, head + filler + tail


class DiscordPlainNewsTests(unittest.TestCase):
    def assert_rejected_before_side_effects(self, candidate, reason):
        ledger, legacy = empty_ledger(), empty_legacy()
        before_event, before_ledger, before_legacy = map(copy.deepcopy, (candidate, ledger, legacy))
        checkpoint, discord_factory, x_factory = Mock(), Mock(), Mock()
        with self.assertRaisesRegex(Invalid, reason):
            publish([candidate], ledger, legacy, checkpoint,
                    {'discord': discord_factory, 'x': x_factory}, NOW, dry_run=False)
        checkpoint.assert_not_called()
        discord_factory.assert_not_called()
        x_factory.assert_not_called()
        self.assertEqual(candidate, before_event)
        self.assertEqual(ledger, before_ledger)
        self.assertEqual(legacy, before_legacy)

    def test_exactly_2000_utf16_units_is_valid_and_sent_intact_once(self):
        for options in ({}, {'empty_media': True}, {'rich': True}, {'rich': True, 'emoji': True}):
            candidate, expected = boundary_event(2000, **options)
            with self.subTest(options=options):
                self.assertEqual(utf16_units(expected), 2000)
                self.assertLessEqual(len(candidate['texts']['discord']), 1900)
                before = copy.deepcopy(candidate)
                validate(candidate, NOW)
                expected_payload = {'content': expected, 'allowed_mentions': {'parse': []}}
                self.assertEqual(discord_payload(candidate), expected_payload)
                client, checkpoint = Mock(), Mock()
                client.send.return_value = Outcome('sent', 'confirmed', '123')
                ledger = empty_ledger()
                summary = publish([candidate], ledger, empty_legacy(), checkpoint,
                                  {'discord': lambda: client}, NOW, dry_run=False)
                client.send.assert_called_once_with(expected_payload, NOW)
                self.assertEqual(summary['sent'], 1)
                self.assertEqual(checkpoint.call_count, 3)
                self.assertEqual(candidate, before)
                self.assertEqual(ledger['events'][candidate['event_id']]['fingerprint'], digest(before))

    def test_2001_utf16_units_rejected_before_checkpoint_client_or_mutation(self):
        for options in ({}, {'empty_media': True}, {'rich': True}, {'rich': True, 'emoji': True}):
            candidate, expected = boundary_event(2001, **options)
            with self.subTest(options=options):
                self.assertEqual(utf16_units(expected), 2001)
                self.assertLessEqual(len(candidate['texts']['discord']), 1900)
                with self.assertRaisesRegex(Invalid, 'rendered content exceeds 2000 UTF-16 units'):
                    discord_payload(candidate)
                self.assert_rejected_before_side_effects(candidate, 'rendered content exceeds 2000 UTF-16 units')

    def test_non_bmp_character_counts_as_two_units_not_one_character(self):
        candidate, expected = boundary_event(2000, emoji=True)
        self.assertEqual(utf16_units(expected), 2000)
        self.assertEqual(len(expected), 1900)
        validate(candidate, NOW)
        self.assertEqual(discord_payload(candidate)['content'], expected)
        # Same number of Unicode scalars, one extra UTF-16 unit.
        candidate['texts']['discord'] = candidate['texts']['discord'].replace('あ', '😀', 1)
        self.assert_rejected_before_side_effects(candidate, 'rendered content exceeds 2000 UTF-16 units')

    def test_summary_escaping_and_copy_block_overhead_count_toward_limit(self):
        for mutation in ('title', 'escape', 'copy'):
            candidate, expected = boundary_event(2000)
            with self.subTest(mutation=mutation):
                self.assertEqual(discord_payload(candidate)['content'], expected)
                if mutation == 'title':
                    candidate['work_title'] += '題'
                elif mutation == 'escape':
                    # The source text's length is unchanged, but rendering adds one backslash.
                    candidate['texts']['discord'] = candidate['texts']['discord'].replace('あ', '*', 1)
                else:
                    candidate['manual_x_text'] = '手動コピー\n' + candidate['sources'][0]['url']
                self.assert_rejected_before_side_effects(candidate, 'rendered content exceeds 2000 UTF-16 units')

    def test_titles_are_plain_content_without_old_embed_title_limit(self):
        candidate = event()
        candidate['work_title'] = '😀' * 200  # 400 units, within the existing 200-character schema.
        validate(candidate, NOW)
        payload = discord_payload(candidate)
        self.assertTrue(payload['content'].startswith(candidate['work_title'] + '\n\n'))
        self.assertEqual(set(payload), {'content', 'allowed_mentions'})

    def test_title_summary_markdown_and_mentions_are_inert_and_reviewed_text_unchanged(self):
        candidate = event()
        candidate['destinations'] = ['discord']
        candidate['texts'].pop('x')
        candidate['work_title'] = '**題名** _続編_ [公式] <@123> @everyone'
        summary = ('`コード` ~~取消~~ ||伏字|| {補足} (説明) \\素材\n'
                   '> 引用 <@&456> <#789> @here @someone')
        escaped_title = r'\*\*題名\*\* \_続編\_ \[公式\] \<@123\> @everyone'
        escaped_summary = (r'\`コード\` \~\~取消\~\~ \|\|伏字\|\| \{補足\} \(説明\) \\素材'
                           '\n' + r'\> 引用 \<@&456\> \<\#789\> @here @someone')
        source = candidate['sources'][0]['url']
        candidate['texts']['discord'] = summary + '\n' + source
        before = copy.deepcopy(candidate)
        validate(candidate, NOW)
        payload = discord_payload(candidate)
        self.assertEqual(payload, {
            'content': escaped_title + '\n\n' + escaped_summary + '\n\n'
                       '発表：続編決定 / 新PV\n\n公式初出：2026/10/03 11:30:00 JST\n\n'
                       '出典 1\n' + source,
            'allowed_mentions': {'parse': []},
        })
        self.assertEqual(candidate, before)

    def test_heading_subtext_and_list_markers_are_literal_even_when_indented(self):
        cases = [('# 大見出し', r'\# 大見出し'),
                 ('## 中見出し', r'\#\# 中見出し'),
                 ('### 小見出し', r'\#\#\# 小見出し'),
                 ('-# 注釈', r'\-\# 注釈'),
                 ('- 項目', r'\- 項目'),
                 ('+ 項目', r'\+ 項目'),
                 ('* 項目', r'\* 項目'),
                 ('1. 項目', r'1\. 項目'),
                 ('2) 項目', r'2\) 項目')]
        for text, escaped in cases:
            for indent in ('', '  ', '\t'):
                candidate = event()
                candidate['work_title'] = indent + text
                source = candidate['sources'][0]['url']
                # Leading indentation is meaningful inside the reviewed summary.
                candidate['texts']['discord'] = '第一段落\n' + indent + text + '\n' + source
                with self.subTest(text=text, indent=indent):
                    validate(candidate, NOW)
                    content = discord_payload(candidate)['content']
                    self.assertTrue(content.startswith(indent + escaped + '\n\n'))
                    self.assertIn('第一段落\n' + indent + escaped + '\n\n発表：', content)

    def test_title_cannot_introduce_external_or_supplied_source_urls(self):
        official = event()['sources'][0]['url']
        for title in ('https://unreviewed.example/story',
                      '日本語https://unreviewed.example/story',
                      'HTTPS://unreviewed.example/story',
                      '題名 ' + official,
                      '日本語' + official,
                      '題名 mailto:reader@example.jp',
                      '日本語mailto:reader@example.jp',
                      '題名 javascript:alert', '題名 data:text/plain,hello',
                      '題名 file:/private', '題名 tel:123'):
            candidate = event()
            candidate['work_title'] = title
            with self.subTest(title=title):
                self.assert_rejected_before_side_effects(candidate, 'title')

    def test_all_announcement_labels_follow_reviewed_order(self):
        kinds = ['delay', 'title', 'studio', 'theme_song', 'staff', 'cast', 'release_date',
                 'new_visual', 'new_pv', 'film_production', 'sequel', 'new_adaptation']
        candidate = event(kinds=kinds)
        validate(candidate, NOW)
        self.assertIn('発表：延期 / 正式タイトル / 制作会社 / 主題歌 / スタッフ / 追加キャスト / '
                      '放送・公開日 / 新ビジュアル / 新PV / 映画制作決定 / 続編決定 / アニメ化決定',
                      discord_payload(candidate)['content'])

    def test_publication_time_is_converted_to_jst_across_date_boundary(self):
        candidate = event()
        candidate['published_at'] = candidate['sources'][0]['published_at'] = '2026-10-02T23:59:59Z'
        candidate['verified_at'] = '2026-10-03T00:10:00Z'
        candidate['approval']['approved_at'] = '2026-10-03T00:12:00Z'
        candidate['queued_at'] = '2026-10-03T00:15:00Z'
        validate(candidate, NOW)
        content = discord_payload(candidate)['content']
        self.assertIn('公式初出：2026/10/03 08:59:59 JST', content)
        self.assertNotIn('09:10:00', content)
        self.assertNotIn('09:15:00', content)

    def test_absent_media_requires_reviewed_summary_and_separate_source_lines(self):
        source = event()['sources'][0]['url']
        for text in (source, '\n\n' + source + '\n\n', '本文 ' + source,
                     '本文\n ' + source, '本文\n' + source + ' ', '[出典](' + source + ')'):
            candidate = event()
            candidate['texts']['discord'] = text
            with self.subTest(text=text):
                self.assert_rejected_before_side_effects(
                    candidate, 'reviewed summary|separate lines|URL must be public HTTPS|'
                               'post links must use supplied official primary sources')

    def test_summary_keeps_reviewed_paragraphs_and_relocates_duplicate_source_lines(self):
        candidate = event()
        source = candidate['sources'][0]['url']
        candidate['texts']['discord'] = '\n第一段落\n\n第二段落\n' + source + '\n' + source + '\n'
        validate(candidate, NOW)
        content = discord_payload(candidate)['content']
        self.assertIn('\n\n第一段落\n\n第二段落\n\n発表：', content)
        self.assertEqual(content.count(source), 1)
        self.assertTrue(content.endswith('出典 1\n' + source))

    def test_manual_copy_is_verbatim_while_its_duplicate_url_is_only_active_once(self):
        candidate = event()
        candidate['destinations'] = ['discord']
        candidate['texts'].pop('x')
        source = candidate['sources'][0]['url']
        candidate['manual_x_text'] = '  **題名** _続編_\n\n[公式] 😀\n' + source + '\n'
        validate(candidate, NOW)
        payload = discord_payload(candidate)
        active, copy_block = payload['content'].split('```text\n')
        self.assertEqual(copy_block, candidate['manual_x_text'] + '\n```')
        self.assertEqual(active.count(source), 1)
        self.assertEqual(payload['content'].count(source), 2)
        self.assertEqual([line for line in active.splitlines() if line.startswith('https://')], [source])
        self.assertEqual(payload['allowed_mentions'], {'parse': []})

    def test_approved_image_is_original_direct_url_and_dedupes_identical_source(self):
        candidate = media_event()
        original = 'https://official.example.jp/Assets/Original%20Visual.JPEG'
        candidate['sources'][0]['url'] = original
        for fact in candidate['facts']:
            fact['source_urls'] = [original]
        for destination in candidate['texts']:
            candidate['texts'][destination] = '架空作品のテスト\n' + original
        for item in candidate['media']:
            item['source_url'] = original
        candidate['media'][0]['url'] = original
        validate(candidate, NOW)
        payload = discord_payload(candidate)
        self.assertEqual(payload['content'].count(original), 1)
        self.assertIn('公式画像\n' + original, payload['content'])
        self.assertNotIn('出典 ', payload['content'])
        self.assertEqual(set(payload), {'content', 'allowed_mentions'})

    def test_source_only_youtube_aliases_dedupe_without_media(self):
        candidate = event()
        short = 'https://youtu.be/TEST_ONLY01?si=tracking'
        watch = 'https://www.youtube.com/watch?v=TEST_ONLY01'
        candidate['sources'][0]['url'] = short
        second = copy.deepcopy(candidate['sources'][0])
        second['url'] = watch
        candidate['sources'].append(second)
        for fact in candidate['facts']:
            fact['source_urls'] = [short, watch]
        for destination in candidate['texts']:
            candidate['texts'][destination] = '架空作品のテスト\n' + short + '\n' + watch
        validate(candidate, NOW)
        content = discord_payload(candidate)['content']
        self.assertEqual([line for line in content.splitlines() if line.startswith('https://')], [short])
        self.assertNotIn('出典 2', content)


if __name__ == '__main__':
    unittest.main()
