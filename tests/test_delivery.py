import copy
import hashlib
import unittest
from datetime import timedelta

from helpers import NOW, empty_ledger, empty_legacy, event
from news_delivery.publisher import publish, reconcile
from news_delivery.schema import Invalid, digest, event_keys, iso
from news_delivery.state import StateError
from news_delivery.transport import ConfigurationError, Outcome


class FakeClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def send(self, content, now):
        self.calls.append(content)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.ledger = empty_ledger()
        self.legacy = empty_legacy()
        self.checkpoints = []
        self.discord = FakeClient(Outcome('sent', 'confirmed', '123'))
        self.x = FakeClient(Outcome('sent', 'confirmed', '456'))
        self.clients = {'discord': lambda: self.discord, 'x': lambda: self.x}

    def checkpoint(self, value):
        self.checkpoints.append(copy.deepcopy(value))

    def run_live(self, events=None, now=NOW, clients=None, checkpoint=None):
        return publish(events or [event()], self.ledger, self.legacy,
                       checkpoint or self.checkpoint, self.clients if clients is None else clients,
                       now, dry_run=False)

    def test_dry_run_does_not_call_client_or_write_any_state(self):
        original = copy.deepcopy(self.ledger)
        def forbidden(*args):
            self.fail('dry run performed a side effect')
        result = publish([event()], self.ledger, self.legacy, forbidden,
                         {'discord': forbidden, 'x': forbidden}, NOW)
        self.assertEqual(result['ready'], 2)
        self.assertEqual(self.ledger, original)

    def test_each_destination_has_durable_reservation_before_post(self):
        class CheckClient:
            def send(inner, content, now):
                latest = self.checkpoints[-1]['events'][event()['event_id']]['deliveries']
                self.assertTrue(any(row['status'] == 'pending' for row in latest.values()))
                return Outcome('sent', 'confirmed', '123')
        self.discord = CheckClient()
        self.run_live()
        self.assertEqual(len(self.checkpoints), 5)  # admission, reservation+result per destination
        self.assertEqual(self.checkpoints[-1]['events'][event()['event_id']]['deliveries']['x']['remote_id'], '456')

    def test_partial_success_does_not_resend_discord(self):
        self.x.outcome = Outcome('ready', 'rate_limited', next_attempt_at=iso(NOW + timedelta(minutes=15)))
        self.run_live()
        self.x.outcome = Outcome('sent', 'confirmed', '456')
        early = self.run_live(now=NOW + timedelta(minutes=5))
        self.assertEqual(early['waiting'], 1)
        self.run_live(now=NOW + timedelta(minutes=20))
        self.assertEqual(len(self.discord.calls), 1)
        self.assertEqual(len(self.x.calls), 2)

    def test_crash_pending_becomes_uncertain_without_retry(self):
        self.discord.outcome = RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError):
            self.run_live()
        self.ledger = self.checkpoints[-1]
        self.discord.outcome = Outcome('sent', 'confirmed', '123')
        result = self.run_live()
        self.assertEqual(len(self.discord.calls), 1)
        self.assertEqual(result['uncertain'], 1)
        self.assertEqual(len(self.x.calls), 1)

    def test_checkpoint_failure_before_post_stops_every_send(self):
        def fail(value):
            if len(self.checkpoints) == 1:
                raise StateError('cannot persist reservation')
            self.checkpoint(value)
        with self.assertRaises(StateError):
            self.run_live(checkpoint=fail)
        self.assertEqual(self.discord.calls, [])
        self.assertEqual(self.x.calls, [])

    def test_checkpoint_failure_after_post_keeps_durable_pending(self):
        def fail(value):
            if len(self.checkpoints) == 2:
                raise StateError('cannot persist result')
            self.checkpoint(value)
        with self.assertRaises(StateError):
            self.run_live(checkpoint=fail)
        self.ledger = self.checkpoints[-1]
        self.run_live()
        self.assertEqual(len(self.discord.calls), 1)
        self.assertEqual(self.ledger['events'][event()['event_id']]['deliveries']['discord']['status'], 'uncertain')

    def test_timeout_and_5xx_uncertainty_never_retry(self):
        self.discord.outcome = Outcome('uncertain', 'timeout')
        self.run_live()
        self.run_live()
        self.assertEqual(len(self.discord.calls), 1)

    def test_canonical_duplicate_different_urls_and_titles(self):
        self.run_live()
        duplicate = event('second-source-same-announcement')
        old_url = duplicate['sources'][0]['url']
        duplicate['work_title'] = '表記ゆれのある作品名'
        duplicate['sources'][0]['url'] = 'https://publisher.example.jp/announcements/2'
        for fact in duplicate['facts']:
            fact['source_urls'] = [duplicate['sources'][0]['url']]
        for target in duplicate['texts']:
            duplicate['texts'][target] = duplicate['texts'][target].replace(old_url, duplicate['sources'][0]['url'])
        result = self.run_live([duplicate])
        self.assertEqual(result['duplicate'], 1)
        self.assertEqual(len(self.discord.calls), 1)

    def test_duplicate_inside_batch_and_dry_run(self):
        a, b = event(), event('different-id-same-announcement')
        result = self.run_live([a, b])
        self.assertEqual(result['duplicate'], 1)
        self.assertEqual(len(self.discord.calls), 1)
        result = publish([a, b], empty_ledger(), self.legacy, None, self.clients, NOW)
        self.assertEqual(result['duplicate'], 1)

    def test_mutation_after_admission_fails_before_any_new_send(self):
        self.run_live()
        candidate = event()
        candidate['texts']['discord'] = '訂正\n' + candidate['sources'][0]['url']
        with self.assertRaises(Invalid):
            self.run_live([candidate])
        self.assertEqual(len(self.discord.calls), 1)

    def test_legacy_observed_is_hold_not_sent_and_not_migrated_to_x(self):
        url = event()['sources'][0]['url'].rstrip('/')
        self.legacy['seen'][hashlib.sha256(url.encode()).hexdigest()] = iso(NOW)
        result = self.run_live()
        self.assertEqual(result['held'], 2)
        self.assertEqual(self.discord.calls, [])
        deliveries = self.ledger['events'][event()['event_id']]['deliveries']
        self.assertTrue(all(row['status'] == 'legacy_hold' for row in deliveries.values()))

    def test_discord_only_does_not_read_x_credentials_or_backfill_later(self):
        self.run_live(clients={'discord': lambda: self.discord})
        self.assertEqual(len(self.discord.calls), 1)
        self.assertEqual(self.x.calls, [])
        result = self.run_live(now=NOW + timedelta(hours=3))
        self.assertEqual(result['expired'], 1)
        self.assertEqual(self.x.calls, [])

    def test_configuration_error_allows_other_destination(self):
        def fail():
            raise ConfigurationError('missing X credentials')
        result = self.run_live(clients={'discord': lambda: self.discord, 'x': fail})
        self.assertEqual(result['blocked'], 1)
        self.assertEqual(len(self.discord.calls), 1)
        self.assertEqual(self.ledger['events'][event()['event_id']]['deliveries']['x']['attempts'], 0)

    def test_expired_queue_and_attempt_cap(self):
        result = self.run_live(now=NOW + timedelta(hours=3))
        self.assertEqual(result['expired'], 2)
        self.assertEqual(self.discord.calls, [])
        self.ledger = empty_ledger()
        self.discord.outcome = Outcome('ready', 'rate_limited')
        for _ in range(5):
            self.run_live()
        self.assertEqual(len(self.discord.calls), 3)

    def test_manual_reconciliation_requires_evidence_and_id(self):
        self.discord.outcome = Outcome('uncertain', 'timeout')
        self.run_live()
        with self.assertRaises(Invalid):
            reconcile(self.ledger, event()['event_id'], 'discord', 'sent', 'operator', 'checked history', NOW)
        reconcile(self.ledger, event()['event_id'], 'discord', 'sent', 'operator', 'confirmed original message', NOW, '789')
        self.run_live()
        self.assertEqual(len(self.discord.calls), 1)
        self.assertEqual(len(self.ledger['audit']), 1)

    def test_alias_matches_canonical_identity(self):
        a, b = event(), event('alias-of-original-event')
        b['announcement_key'] = 'another-announcement-name'
        b['dedupe_aliases'] = [a['announcement_key']]
        url = b['sources'][0]['url']
        b['sources'][0]['url'] = 'https://publisher.example.jp/news/alternate'
        for fact in b['facts']:
            fact['source_urls'] = [b['sources'][0]['url']]
        for destination in b['texts']:
            b['texts'][destination] = b['texts'][destination].replace(url, b['sources'][0]['url'])
        self.run_live([a, b])
        self.assertEqual(len(self.discord.calls), 1)

    def test_transitive_source_bridge_precomputed_and_persisted(self):
        a, b, c = event(), event('middle-bridge-event'), event('third-source-event')
        url = a['sources'][0]['url']
        c['announcement_key'] = 'different-canonical-key'
        c['sources'][0]['url'] = 'https://publisher.example.jp/news/second'
        for fact in c['facts']:
            fact['source_urls'] = [c['sources'][0]['url']]
        for destination in c['texts']:
            c['texts'][destination] = c['texts'][destination].replace(url, c['sources'][0]['url'])
        b['announcement_key'] = 'bridge-canonical-key'
        b['sources'].append(copy.deepcopy(c['sources'][0]))
        self.run_live([a, c, b])
        self.assertEqual(len(self.discord.calls), 1)
        self.assertIn('source:https://publisher.example.jp/news/second', self.ledger['keys'])
        self.run_live([c])
        self.assertEqual(len(self.discord.calls), 1)

    def test_bridge_between_existing_owners_fails_before_any_post(self):
        a, c = event(), event('distinct-other-announcement')
        c['announcement_key'] = 'distinct-other-key'
        url = c['sources'][0]['url']
        c['sources'][0]['url'] = 'https://publisher.example.jp/news/other'
        for fact in c['facts']:
            fact['source_urls'] = [c['sources'][0]['url']]
        for destination in c['texts']:
            c['texts'][destination] = c['texts'][destination].replace(url, c['sources'][0]['url'])
        self.run_live([a, c])
        bridge = event('new-bridge-between-owners')
        bridge['sources'].append(copy.deepcopy(c['sources'][0]))
        with self.assertRaises(StateError):
            self.run_live([bridge])
        self.assertEqual(len(self.discord.calls), 2)

    def test_duplicate_group_uses_earliest_publication(self):
        a, b = event(), event('duplicate-earlier-publication')
        b['published_at'] = b['sources'][0]['published_at'] = '2026-10-03T01:50:00Z'
        result = self.run_live([a, b])
        self.assertEqual(result['expired'], 2)
        self.assertEqual(self.discord.calls, [])

    def test_legacy_match_in_skipped_source_holds_entire_group(self):
        a, b = event(), event('duplicate-legacy-source')
        source = copy.deepcopy(b['sources'][0])
        source['url'] = 'https://publisher.example.jp/legacy-news'
        b['sources'].append(source)
        self.legacy['seen'][hashlib.sha256(source['url'].encode()).hexdigest()] = iso(NOW)
        result = self.run_live([a, b])
        self.assertEqual(result['held'], 2)
        self.assertEqual(self.discord.calls, [])

    def test_strict_original_one_hour_expiry_not_queue_age(self):
        result = self.run_live(now=NOW + timedelta(minutes=30, seconds=1))
        self.assertEqual(result['expired'], 2)
        self.assertEqual(self.discord.calls, [])

    def test_clock_crosses_cutoff_during_checkpoint_no_post(self):
        times = iter([NOW, NOW, NOW + timedelta(minutes=31), NOW + timedelta(minutes=31)])
        result = publish([event()], self.ledger, self.legacy, self.checkpoint,
                         self.clients, NOW, dry_run=False,
                         clock=lambda: next(times, NOW + timedelta(minutes=31)))
        self.assertEqual(result['expired'], 2)
        self.assertEqual(self.discord.calls, [])
        self.assertEqual(self.x.calls, [])

    def test_entire_batch_validated_before_any_send(self):
        bad = event('bad-new-announcement')
        bad['approval']['checks']['in_scope'] = False
        with self.assertRaises(Invalid):
            self.run_live([event(), bad])
        self.assertEqual(self.checkpoints, [])
        self.assertEqual(self.discord.calls, [])


if __name__ == '__main__':
    unittest.main()
