import os
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from scheduler.game_opportunities import (
    build_game_opportunities_report,
    push_report_to_railway,
    save_report_locally,
    ScheduleResearchError,
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
            help="Require a successful local Muse Spark (opencode/muse-spark-1.3) web research pass per day.",
        )
        parser.add_argument(
            "--use-luna",
            action="store_true",
            help="Require Luna (gpt-6-luna) research through the local Codex/ChatGPT login and isolated browser.",
        )
        parser.add_argument(
            "--public-python",
            default=os.getenv("OPPORTUNITIES_PUBLIC_PYTHON") or None,
            help="Optional local Python executable for public GameSheet HTTP requests only (no CRM credentials).",
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
        try:
            report = build_game_opportunities_report(
                target_date=options["date"],
                use_muse=options["use_muse"],
                public_python=options.get("public_python"),
                use_luna=options.get("use_luna", False),
                progress=self.stdout.write,
            )
        except ScheduleResearchError as exc:
            raise CommandError(str(exc) + " Existing local and Railway snapshots were not changed.") from exc
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
