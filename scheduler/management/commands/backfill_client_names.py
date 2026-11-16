from django.core.management.base import BaseCommand

from scheduler.client_names import backfill_client_names


class Command(BaseCommand):
    help = "Fill in the Pipedrive client name on shoots that don't have one yet (read-only on Pipedrive)."

    def add_arguments(self, parser):
        parser.add_argument("--include-past", action="store_true", help="Also backfill shoots that already happened.")

    def handle(self, *args, **opts):
        checked, filled = backfill_client_names(include_past=opts["include_past"])
        self.stdout.write(f"Client names: checked {checked} shoot(s), filled {filled}.")
