"""
Send a real Google Calendar invite to confirm the integration works.

Usage:
    python manage.py send_test_invite                      # invites dylandirosa980@gmail.com
    python manage.py send_test_invite --to other@email.com
"""
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from scheduler.calendar_client import get_client, GoogleCalendarClient


class Command(BaseCommand):
    help = "Send a real Google Calendar invite to verify the integration"

    def add_arguments(self, parser):
        parser.add_argument("--to", default="dylandirosa980@gmail.com")

    def handle(self, *args, **opts):
        cal = get_client()
        if not isinstance(cal, GoogleCalendarClient):
            raise CommandError(
                "Refresh token not set yet. Run `python manage.py get_google_refresh_token` first."
            )

        when = timezone.now() + timedelta(hours=2)
        event_id = cal.create_event(
            summary="🏒 TEST: Shoot Scheduler integration check",
            description=(
                "This is an automated test invite from the Puck Pro Media shoot scheduler.\n\n"
                "If you're seeing this, the Google Calendar integration is working end-to-end.\n"
                "You can ignore or decline this event."
            ),
            location="Test Location, Newark NJ",
            start=when,
            end=when + timedelta(hours=1),
            attendee_email=opts["to"],
        )
        self.stdout.write(self.style.SUCCESS(
            f"\nSent! Event ID: {event_id}\n"
            f"Check {opts['to']} inbox for the invite (and the spam folder just in case).\n"
        ))
