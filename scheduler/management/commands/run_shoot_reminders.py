"""Dedicated reminder worker. Never starts invite/escalation jobs."""
import json
import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from scheduler.reminder_integration import run_reminder_cycle


class Command(BaseCommand):
    help = 'Run one reminder tick, or --loop as a dedicated worker. Live sends default off.'

    def add_arguments(self, parser):
        parser.add_argument('--status', action='store_true', help='Read-only ledger counts and enablement state; no network.')
        parser.add_argument('--loop', action='store_true', help='Poll every minute; reconcile every 15 minutes.')
        parser.add_argument('--reconcile', action='store_true', help='Repair CRM state before the one-shot worker tick.')
        parser.add_argument('--dry-run', action='store_true', help='Inspect only; no sends, reconciliation writes, or ledger claims.')

    def handle(self, *args, **options):
        if options['status']:
            from django.db.models import Count
            from scheduler.models import ShootReminder
            result = {
                'enabled':bool(getattr(settings,'SHOOT_REMINDERS_ENABLED',False)),
                'send_enabled':bool(getattr(settings,'SHOOT_REMINDERS_SEND_ENABLED',False)),
                'ledger':dict(ShootReminder.objects.values_list('status').annotate(total=Count('pk')).values_list('status','total')),
            }
            self.stdout.write(json.dumps(result,sort_keys=True))
            return
        if options['loop'] and options['dry_run']:
            raise CommandError('Use --dry-run without --loop')
        last_reconcile = None
        while True:
            tick = time.monotonic()
            reconcile = options['reconcile'] or (options['loop'] and (last_reconcile is None or tick-last_reconcile >= 900))
            try:
                result = run_reminder_cycle(reconcile=reconcile, dry_run=options['dry_run'])
                self.stdout.write(json.dumps(result, sort_keys=True))
                if reconcile:
                    last_reconcile = tick
            except Exception:
                # Never echo provider URLs, tokens, recipients or message text.
                if not options['loop']:
                    raise CommandError('Reminder cycle failed; verify credentials/configuration and inspect ledger states') from None
                self.stderr.write('Reminder cycle failed; no uncertain sends are automatically retried')
            if not options['loop'] or not getattr(settings,'SHOOT_REMINDERS_ENABLED',False):
                return
            try:
                time.sleep(max(1,60-(time.monotonic()-tick)))
            except KeyboardInterrupt:
                return
