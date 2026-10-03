"""Delivery state machine with at-most-once retries after ambiguous outcomes."""
from __future__ import annotations

from datetime import timedelta

from .schema import Invalid, digest, event_keys, iso, timestamp, validate
from .state import StateError, legacy_match
from .transport import ConfigurationError

MAX_ATTEMPTS = 3
DELIVERY_TTL = timedelta(hours=1)


def _components(events, ledger):
    """Resolve all transitive aliases before sending, including skipped duplicates."""
    parents = {}
    def find(node):
        parents.setdefault(node, node)
        if parents[node] != node:
            parents[node] = find(parents[node])
        return parents[node]
    def union(left, right):
        parents[find(right)] = find(left)
    keys = dict(ledger['keys'])
    for event in events:
        event_id = event['event_id']
        find(event_id)
        for key in event_keys(event):
            if key in keys:
                union(event_id, keys[key])
            keys[key] = event_id
    groups = {}
    for node in list(parents):
        groups.setdefault(find(node), set()).add(node)
    owners, merged_keys = {}, {}
    event_by_id = {event['event_id']: event for event in events}
    for nodes in groups.values():
        existing = nodes.intersection(ledger['events'])
        if len(existing) > 1:
            raise StateError('duplicate aliases bridge multiple existing events; manual review required')
        owner = next(iter(existing)) if existing else min(
            nodes, key=lambda key: (timestamp(event_by_id[key]['queued_at']), key))
        merged_keys[owner] = set(ledger['events'].get(owner, {}).get('keys', []))
        for node in nodes:
            owners[node] = owner
            if node in event_by_id:
                merged_keys[owner].update(event_keys(event_by_id[node]))
    return owners, merged_keys


def publish(events, ledger, legacy, checkpoint, clients, now, dry_run=True, limit=10, clock=None):
    """Clients are lazy factories. Dry runs call neither factories nor checkpoint.

    ready: definitely not delivered. pending: durable pre-POST reservation.
    pending found in a fresh run becomes uncertain, including crashes before POST.
    This sacrifices delivery in ambiguous cases to prevent duplicate posts.
    """
    clock = clock or (lambda: now)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise Invalid('delivery limit must be 1..20')
    all_ids = set()
    for event in events:
        validate(event, now)
        event_id = event['event_id']
        if event_id in all_ids:
            raise Invalid('duplicate event ID in queue')
        all_ids.add(event_id)
        old = ledger['events'].get(event_id)
        if old and old['fingerprint'] != digest(event):
            raise Invalid('queued event changed after admission; manual review required')
    owners, merged_keys = _components(events, ledger)
    group_published, group_legacy = {}, {}
    for event in events:
        owner = owners[event['event_id']]
        original = timestamp(event['published_at'])
        previous = ledger['events'].get(owner, {}).get('published_at')
        if previous:
            original = min(original, timestamp(previous))
        group_published[owner] = min(original, group_published.get(owner, original))
        group_legacy[owner] = group_legacy.get(owner, False) or legacy_match(event, legacy)
    summary = {'ready': 0, 'sent': 0, 'duplicate': 0, 'held': 0, 'uncertain': 0,
               'blocked': 0, 'expired': 0, 'waiting': 0, 'disabled': 0, 'attempted': 0}
    if not dry_run:
        recovered = False
        for row in ledger['events'].values():
            for delivery in row['deliveries'].values():
                if delivery['status'] == 'pending':
                    delivery.update(status='uncertain', reason='interrupted_after_durable_reservation', updated_at=iso(now))
                    recovered = True
        for owner, keys in merged_keys.items():
            if owner in ledger['events']:
                row = ledger['events'][owner]
                earliest = iso(group_published[owner])
                if timestamp(row['published_at']) != group_published[owner]:
                    row['published_at'] = earliest
                    recovered = True
                if group_legacy[owner]:
                    for delivery in row['deliveries'].values():
                        if delivery['status'] == 'ready' and delivery.get('reason') != 'manual_reconciliation':
                            delivery.update(status='legacy_hold', reason='duplicate_source_matches_legacy', updated_at=iso(now))
                            recovered = True
            if owner in ledger['events'] and set(ledger['events'][owner]['keys']) != keys:
                ledger['events'][owner]['keys'] = sorted(keys)
                for key in keys:
                    ledger['keys'][key] = owner
                recovered = True
        if recovered:
            checkpoint(ledger)
    active_clients = {}
    unavailable = set()
    for event in events:
        event_id = event['event_id']
        row = ledger['events'].get(event_id)
        keys = sorted(merged_keys[owners[event_id]])
        if owners[event_id] != event_id:
            summary['duplicate'] += 1
            continue
        published_at = group_published[event_id]
        if not row:
            held = group_legacy[event_id]
            expired = not timedelta(0) <= now - published_at <= DELIVERY_TTL
            status = 'legacy_hold' if held else 'expired' if expired else 'ready'
            row = {'fingerprint': digest(event), 'keys': keys, 'queued_at': event['queued_at'],
                   'published_at': iso(published_at),
                   'deliveries': {destination: {'status': status, 'attempts': 0,
                                                'updated_at': iso(now), 'reason': status}
                                  for destination in event['destinations']}}
            if not dry_run:
                ledger['events'][event_id] = row
                for key in keys:
                    ledger['keys'][key] = event_id
                checkpoint(ledger)
        for destination in event['destinations']:
            now = clock()
            delivery = row['deliveries'][destination]
            status = delivery['status']
            if status != 'ready':
                summary[{'legacy_hold': 'held', 'pending': 'uncertain'}.get(status, status)] += 1
                continue
            if destination not in clients:
                summary['disabled'] += 1
                continue
            if not timedelta(0) <= now - published_at <= DELIVERY_TTL:
                summary['expired'] += 1
                if not dry_run:
                    delivery.update(status='expired', reason='delivery_window_expired', updated_at=iso(now))
                    checkpoint(ledger)
                continue
            if delivery.get('next_attempt_at') and timestamp(delivery['next_attempt_at']) > now:
                summary['waiting'] += 1
                continue
            if delivery['attempts'] >= MAX_ATTEMPTS:
                summary['blocked'] += 1
                if not dry_run:
                    delivery.update(status='blocked', reason='attempt_limit', updated_at=iso(now))
                    checkpoint(ledger)
                continue
            if dry_run:
                summary['ready'] += 1
                continue
            if summary['attempted'] >= limit:
                summary['waiting'] += 1
                continue
            if destination in unavailable:
                summary['blocked'] += 1
                continue
            if destination not in active_clients:
                try:
                    active_clients[destination] = clients[destination]()
                except ConfigurationError as exc:
                    # Nothing sent, no reservation needed. Repair configuration then rerun.
                    print(destination + ': ' + str(exc))
                    unavailable.add(destination)
                    summary['blocked'] += 1
                    continue
            now = clock()
            if not timedelta(0) <= now - published_at <= DELIVERY_TTL:
                delivery.update(status='expired', reason='delivery_window_expired', updated_at=iso(now))
                checkpoint(ledger)
                summary['expired'] += 1
                continue
            delivery.update(status='pending', reason='durable_pre_send_reservation',
                            attempts=delivery['attempts'] + 1, updated_at=iso(now))
            delivery.pop('next_attempt_at', None)
            checkpoint(ledger)  # If this fails or is uncertain, STOP before the network POST.
            now = clock()
            if not timedelta(0) <= now - published_at <= DELIVERY_TTL:
                delivery.update(status='expired', reason='expired_during_checkpoint', updated_at=iso(now))
                checkpoint(ledger)
                summary['expired'] += 1
                continue
            summary['attempted'] += 1
            outcome = active_clients[destination].send(event['texts'][destination], now)
            delivery.update(status=outcome.status, reason=outcome.reason, updated_at=iso(clock()))
            if outcome.remote_id:
                delivery['remote_id'] = outcome.remote_id
            if outcome.next_attempt_at:
                delivery['next_attempt_at'] = outcome.next_attempt_at
            checkpoint(ledger)  # If this fails, remote reservation remains uncertain.
            summary[outcome.status] += 1
    return summary


def reconcile(ledger, event_id, destination, decision, reviewer, evidence, now, remote_id=None):
    """Only a separately reviewed, explicit local operation. It never calls an API."""
    from .schema import text
    import re
    text(reviewer, 'reviewer', 100)
    text(evidence, 'reconciliation evidence', 2000)
    if event_id not in ledger['events'] or destination not in ledger['events'][event_id]['deliveries']:
        raise Invalid('unknown reconciliation target')
    delivery = ledger['events'][event_id]['deliveries'][destination]
    if delivery['status'] not in {'pending', 'uncertain', 'blocked', 'legacy_hold'}:
        raise Invalid('only held or uncertain deliveries can be reconciled')
    if decision == 'sent':
        if not isinstance(remote_id, str) or not re.fullmatch(r'\d+', remote_id):
            raise Invalid('confirmed remote message ID required')
        delivery.update(status='sent', remote_id=remote_id)
    elif decision == 'not-sent':
        delivery.update(status='ready', attempts=0)
        delivery.pop('remote_id', None)
        delivery.pop('next_attempt_at', None)
    else:
        raise Invalid('reconciliation decision must be sent or not-sent')
    delivery.update(reason='manual_reconciliation', updated_at=iso(now))
    ledger['audit'].append({'event_id': event_id, 'destination': destination,
                            'decision': decision, 'reviewer': reviewer,
                            'evidence': evidence, 'at': iso(now), 'remote_id': remote_id})
