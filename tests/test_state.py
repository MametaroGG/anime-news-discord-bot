import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import NOW, empty_ledger, event
from news_delivery.schema import Invalid, digest, event_keys, iso
from news_delivery.state import GitCheckpoint, StateError, atomic_write, load, locked


def git(path, *args):
    return subprocess.run(['git', *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


class StateTests(unittest.TestCase):
    def test_corrupt_or_missing_state_never_resets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            with self.assertRaises(Invalid):
                load(path)
            path.write_text('{}')
            with self.assertRaises(StateError):
                load(path)
            atomic_write(path, empty_ledger())
            self.assertEqual(load(path), empty_ledger())

    def test_orphan_index_and_sent_without_id_rejected(self):
        value = empty_ledger()
        value['events']['test-event'] = {'fingerprint': digest(event()), 'keys': ['test-key'],
                                         'queued_at': iso(NOW), 'published_at': iso(NOW), 'deliveries': {'discord': {
                                             'status': 'sent', 'attempts': 1, 'updated_at': iso(NOW)}}}
        value['keys']['test-key'] = 'test-event'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            atomic_write(path, value)
            with self.assertRaises(StateError):
                load(path)
            value['events']['test-event']['deliveries']['discord']['remote_id'] = '123'
            value['keys']['test-key'] = 'other'
            atomic_write(path, value)
            with self.assertRaises(StateError):
                load(path)

    def test_local_concurrent_publisher_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with locked(Path(folder) / 'lock'):
                with self.assertRaises(StateError):
                    with locked(Path(folder) / 'lock'):
                        self.fail('second lock acquired')

    def test_git_remote_checkpoint_and_stale_runner_rejected(self):
        # A real local bare Git remote, no GitHub credentials/network/API calls.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote, one, two = root / 'remote.git', root / 'one', root / 'two'
            git(root, 'init', '--bare', '--initial-branch=main', str(remote))
            git(root, 'clone', str(remote), str(one))
            git(one, 'config', 'user.name', 'Unit Test')
            git(one, 'config', 'user.email', 'test@example.invalid')
            (one / 'data').mkdir()
            atomic_write(one / 'data/delivery-state.json', empty_ledger())
            git(one, 'add', '.')
            git(one, 'commit', '-m', 'initial test fixture')
            git(one, 'push', 'origin', 'main')
            git(root, 'clone', str(remote), str(two))
            first, stale = GitCheckpoint(one, 'main'), GitCheckpoint(two, 'main')
            value = empty_ledger()
            value['audit'].append({'test': 'durable'})
            first(value)
            remote_state = json.loads(git(remote, 'show', 'main:data/delivery-state.json'))
            self.assertEqual(remote_state['audit'], [{'test': 'durable'}])
            with self.assertRaises(StateError):
                stale(value)
            self.assertEqual(load(two / 'data/delivery-state.json'), empty_ledger())

    def test_git_checkpoint_failure_is_sanitized(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(StateError) as caught:
                GitCheckpoint(folder, 'main')
            self.assertNotIn(folder, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
