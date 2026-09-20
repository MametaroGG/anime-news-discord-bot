import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import bot

class BotTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(bot.classify('「作品」TVアニメ化決定！'), 'TVアニメ化')
        self.assertEqual(bot.classify('「作品」第2期制作決定'), '続編制作')
        self.assertEqual(bot.classify('「作品」劇場版制作決定'), '劇場版制作')
        self.assertIsNone(bot.classify('実写映画化決定'))
        self.assertIsNone(bot.classify('アニメ化してほしい作品'))
        self.assertIsNone(bot.classify('アニメ第2期PV公開'))
    def test_url_normalization(self):
        self.assertEqual(bot.canonical('https://example.com/a/?utm_source=x'), 'https://example.com/a')
    def test_first_run_no_posts(self):
        entry = {'title': '作品 TVアニメ化決定', 'url': 'https://example.com/a', 'source': 'test', 'published': datetime.now(timezone.utc)}
        with patch.object(bot, 'load_state', return_value={'initialized': False, 'seen': {}}), patch.object(bot, 'fetch_entries', return_value=[entry]), patch.object(bot, 'post') as post, patch.object(bot, 'save_state') as save:
            with patch.dict('os.environ', {'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/1/test'}):
                bot.run()
            post.assert_not_called()
            save.assert_called_once()

if __name__ == '__main__':
    unittest.main()
