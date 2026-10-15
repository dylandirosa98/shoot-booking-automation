import os
from django.conf import settings
from django.core.management.base import BaseCommand
from scheduler.game_opportunities import (
    build_game_opportunities_report,
    push_report_to_railway,
    save_report_locally,
)


class Command(BaseCommand):
    help = "Run day-by-day game opportunity research across AYHL, GameSheet, AHF, THF, and FedHockey + Pipedrive CRM."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            default=None,
            help="Optional YYYY-MM-DD date to research a single shoot day.",
        )
        parser.add_argument(
            "--use-muse",
            action="store_true",
            help="Also launch local Muse Spark (opencode/muse-spark-1.3) web/browser agent per day.",
        )
        parser.add_argument(
            "--push",
            action="store_true",
            help="Push the generated report directly to the deployed Railway app.",
        )
        parser.add_argument(
            "--railway-url",
            default=os.getenv("RAILWAY_SYNC_URL", "https://web-production-09290.up.railway.app"),
            help="Base URL of the deployed Railway app for --push.",
        )

    def handle(self, *args, **options):
        self.stdout.write("Running 5-site schedule + full Pipedrive CRM research...")
        report = build_game_opportunities_report(
            target_date=options["date"],
            use_muse=options["use_muse"],
        )
        path = save_report_locally(report)
        summary = report.get("summary") or {}
        self.stdout.write(
            self.style.SUCCESS(
                f"Saved report to {path} — "
                f"{summary.get('shoot_days', 0)} shoot days, "
                f"{summary.get('upcoming_shoots', 0)} shoots, "
                f"{summary.get('total_games', 0)} games, "
                f"{summary.get('unique_teams', 0)} teams, "
                f"{summary.get('direct_matches', 0)} direct CRM matches, "
                f"{summary.get('maybe_matches', 0)} maybe CRM matches."
            )
        )
        if options["push"]:
            secret = (
                os.getenv("OPPORTUNITIES_SYNC_SECRET")
                or settings.PIPEDRIVE_WEBHOOK_SECRET
                or settings.SECRET_KEY
            )
            res = push_report_to_railway(report, options["railway_url"], secret)
            self.stdout.write(
                self.style.SUCCESS(f"Pushed report to {options['railway_url']}: {res}")
            )
