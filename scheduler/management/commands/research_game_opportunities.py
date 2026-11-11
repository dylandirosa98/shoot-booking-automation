import os
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from scheduler.game_opportunities import (
    build_game_opportunities_report,
    load_saved_report,
    mark_new_matches,
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
            "--horizon-days",
            type=int,
            default=None,
            help="Only research shoot days within this many days from today (default: all upcoming).",
        )
        parser.add_argument(
            "--use-vibe",
            action="store_true",
            help="Add a Mistral Vibe CLI web-research pass per shoot day (web tools only, no CRM data sent).",
        )
        parser.add_argument(
            "--use-muse",
            action="store_true",
            help="Add a local Muse Spark (opencode/muse-spark-1.3) web research pass per day.",
        )
        parser.add_argument(
            "--use-luna",
            action="store_true",
            help="Add Luna (gpt-6-luna) research through the local Codex/ChatGPT login and isolated browser.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Abort without saving if any schedule source fails (default: save partial results, flagged on the page).",
        )
        parser.add_argument(
            "--fail-on-partial",
            action="store_true",
            help="Exit with status 2 after saving/pushing if any schedule source failed (for schedulers/alerting).",
        )
        parser.add_argument(
            "--public-python",
            default=os.getenv("OPPORTUNITIES_PUBLIC_PYTHON") or None,
            help="Python with Playwright for the GameSheet browser worker (auto-detected if omitted).",
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
                target_date=options.get("date"),
                use_muse=options.get("use_muse", False),
                public_python=options.get("public_python"),
                use_luna=options.get("use_luna", False),
                use_vibe=options.get("use_vibe", False),
                strict=options.get("strict", False),
                horizon_days=options.get("horizon_days"),
                progress=self.stdout.write,
            )
        except ScheduleResearchError as exc:
            raise CommandError(str(exc) + " Existing local and Railway snapshots were not changed.") from exc

        new_count = mark_new_matches(report, load_saved_report())
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
                f"{summary.get('maybe_matches', 0)} maybe CRM matches, "
                f"{new_count} new since last refresh."
            )
        )
        failed = [s for s in report.get("source_status") or [] if s.get("status") != "ok"]
        for s in failed:
            self.stderr.write(self.style.WARNING(f"Source failed: {s['source']}: {s['error']}"))
        for w in report.get("source_warnings") or []:
            self.stderr.write(self.style.WARNING(w))

        if options.get("push"):
            secret = (
                os.getenv("OPPORTUNITIES_SYNC_SECRET")
                or settings.PIPEDRIVE_WEBHOOK_SECRET
                or settings.SECRET_KEY
            )
            try:
                res = push_report_to_railway(report, options["railway_url"], secret)
            except Exception as exc:  # noqa: BLE001
                raise CommandError(f"Saved locally, but the Railway push failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
            self.stdout.write(self.style.SUCCESS(f"Pushed report to {options['railway_url']}: {res}"))

        if failed and options.get("fail_on_partial"):
            sys.exit(2)
