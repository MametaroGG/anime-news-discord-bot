from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .publisher import publish, reconcile
from .schema import Invalid, read_queue
from .state import GitCheckpoint, StateError, atomic_write, load, load_legacy, locked
from .transport import ConfigurationError, DiscordClient, XClient

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser(description='Publish only externally reviewed official anime announcements')
    parser.add_argument('--queue', type=Path, default=ROOT / 'queue/events')
    parser.add_argument('--dry-run', action='store_true', help='default; never reads secrets, calls APIs or writes state')
    parser.add_argument('--live', action='store_true', help='also requires DELIVERY_LIVE_ENABLED=true')
    parser.add_argument('--mode', choices=('discord', 'both'), default='discord')
    parser.add_argument('--limit', type=int, default=10, help='maximum external POSTs in this run (1..20)')
    sub = parser.add_subparsers(dest='command')
    repair = sub.add_parser('reconcile', help='create local ledger repair for review; never posts/pushes')
    repair.add_argument('event_id')
    repair.add_argument('destination', choices=('discord', 'x'))
    repair.add_argument('decision', choices=('sent', 'not-sent'))
    repair.add_argument('--reviewer', required=True)
    repair.add_argument('--evidence', required=True)
    repair.add_argument('--remote-id')
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        if args.live and args.dry_run:
            raise Invalid('choose dry-run or live')
        if args.live and args.command:
            raise Invalid('reconciliation never accepts --live')
        ledger_path = ROOT / 'data/delivery-state.json'
        with locked(ROOT / '.delivery.lock') if args.live or args.command else _no_lock():
            ledger = load(ledger_path)
            if args.command == 'reconcile':
                reconcile(ledger, args.event_id, args.destination, args.decision,
                          args.reviewer, args.evidence, now, args.remote_id)
                atomic_write(ledger_path, ledger)
                print('Local reconciliation saved. Review the diff before committing; no API calls or push performed.')
                return 0
            events = read_queue(args.queue, now)
            legacy = load_legacy(ROOT / 'data/seen.json')
            clients = {'discord': lambda: DiscordClient(os.environ.get('DISCORD_WEBHOOK_URL', ''))}
            if args.mode == 'both':
                def x_factory():
                    client = XClient(os.environ)
                    client.verify_identity()
                    return client
                clients['x'] = x_factory
            checkpoint = None
            if args.live:
                if os.environ.get('DELIVERY_LIVE_ENABLED') != 'true':
                    raise ConfigurationError('live delivery disabled; requires explicit enablement')
                if os.environ.get('GITHUB_ACTIONS') != 'true':
                    raise ConfigurationError('live mode is supported only in the serialized GitHub Actions workflow')
                branch = os.environ.get('DELIVERY_BRANCH', '')
                if os.environ.get('GITHUB_REF') != 'refs/heads/' + branch:
                    raise ConfigurationError('live mode requires the configured default branch')
                if os.environ.get('GITHUB_EVENT_NAME') not in ('push', 'schedule', 'workflow_dispatch'):
                    raise ConfigurationError('live mode is not allowed for this event type')
                if args.queue.resolve() != (ROOT / 'queue/events').resolve():
                    raise ConfigurationError('live mode requires the committed queue')
                checkpoint = GitCheckpoint(ROOT, branch)
            summary = publish(events, ledger, legacy, checkpoint, clients, now,
                              dry_run=not args.live, limit=args.limit,
                              clock=lambda: datetime.now(timezone.utc))
            print(('LIVE ' if args.live else 'DRY RUN ') + json.dumps(summary, sort_keys=True))
            return 1 if args.live and (summary['uncertain'] or summary['blocked'] or summary['held']) else 0
    except (Invalid, StateError, ConfigurationError) as exc:
        print('STOP: ' + str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Never leak raw requests/URL/OAuth exceptions. The durable reservation
        # prevents automatic retries if an unexpected error followed a POST.
        print('STOP: unexpected failure; inspect ledger and reconcile pending delivery before retrying', file=sys.stderr)
        return 2


class _no_lock:
    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False
