import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import empty_ledger, empty_legacy
from news_delivery import cli
from news_delivery.schema import Invalid, read_json, validate

ROOT = Path(__file__).resolve().parent.parent


class CliWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / 'data').mkdir()
        (self.root / 'queue/events').mkdir(parents=True)
        (self.root / 'data/delivery-state.json').write_text(json.dumps(empty_ledger()))
        (self.root / 'data/seen.json').write_text(json.dumps(empty_legacy()))

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, args, env=None):
        out = io.StringIO()
        with patch.object(cli, 'ROOT', self.root), patch.dict(os.environ, env or {}, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = cli.main(args)
        return result, out.getvalue()

    def test_default_dry_run_has_no_network_state_or_secret_access(self):
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with patch.object(cli, 'DiscordClient', side_effect=AssertionError('client constructed')), \
                patch.object(cli, 'XClient', side_effect=AssertionError('client constructed')), \
                patch.object(cli, 'GitCheckpoint', side_effect=AssertionError('state writer constructed')):
            code, output = self.run_cli(['--mode', 'both'])
        self.assertEqual(code, 0)
        self.assertIn('DRY RUN', output)
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_live_gate_actions_and_ref_enforced_before_clients(self):
        options = [({}, 'disabled'),
                   ({'DELIVERY_LIVE_ENABLED': 'true'}, 'GitHub Actions'),
                   ({'DELIVERY_LIVE_ENABLED': 'true', 'GITHUB_ACTIONS': 'true',
                     'DELIVERY_BRANCH': 'main', 'GITHUB_REF': 'refs/pull/1/merge'}, 'default branch'),
                   ({'DELIVERY_LIVE_ENABLED': 'true', 'GITHUB_ACTIONS': 'true',
                     'DELIVERY_BRANCH': 'main', 'GITHUB_REF': 'refs/heads/main',
                     'GITHUB_EVENT_NAME': 'pull_request_target'}, 'event type')]
        for env, expected in options:
            with self.subTest(expected=expected), patch.object(cli, 'DiscordClient', side_effect=AssertionError):
                code, output = self.run_cli(['--live'], env)
            self.assertEqual(code, 2)
            self.assertIn(expected, output)

    def test_contradictory_flags_and_template_fail_closed(self):
        code, output = self.run_cli(['--live', '--dry-run'])
        self.assertEqual(code, 2)
        with self.assertRaises(Invalid):
            validate(read_json(ROOT / 'docs/event-template.json'))

    def test_ci_no_secrets_or_privileged_pr_trigger(self):
        ci = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('pull_request:', ci)
        self.assertNotIn('pull_request_target:', ci)
        self.assertNotIn('secrets.', ci)
        self.assertNotIn('--live', ci)
        self.assertIn('persist-credentials: false', ci)
        self.assertIn('contents: read', ci)

    def test_delivery_workflow_gates_and_unchanged_discord_secret(self):
        workflow = (ROOT / '.github/workflows/monitor.yml').read_text()
        self.assertIn('default: true', workflow)
        self.assertIn("vars.DELIVERY_LIVE_ENABLED == 'true'", workflow)
        self.assertIn('github.event.repository.default_branch', workflow)
        self.assertIn('DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}', workflow)
        self.assertIn('group: anime-news-monitor', workflow)
        self.assertIn('cancel-in-progress: false', workflow)
        self.assertNotIn('pull_request', workflow)
        self.assertIn("paths: ['queue/events/**']", workflow)
        self.assertNotIn('--test-post', workflow)
        self.assertIn('--live --mode discord', workflow)
        self.assertNotIn('secrets.X_', workflow)
        self.assertNotIn('vars.X_ENABLED', workflow)
        self.assertNotIn('--live --mode both', workflow)


if __name__ == '__main__':
    unittest.main()
