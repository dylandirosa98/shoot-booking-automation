"""Read upcoming shoot-booking contacts from Pipedrive on this machine and push
them to the Railway scheduler, for when Railway can't read Pipedrive itself."""
import os
from datetime import date, timedelta

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from scheduler.client_names import client_name_from_activity
from scheduler.game_opportunities import get_pipedrive_credentials, pipedrive_get_all


class Command(BaseCommand):
    help = "Push Pipedrive client names for upcoming shoots to the Railway dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=365, help="How far ahead to look (default 365).")
        parser.add_argument("--past-days", type=int, default=7, help="Also include shoots this many days back.")
        parser.add_argument("--railway-url", default=os.getenv("RAILWAY_SYNC_URL", "https://web-production-09290.up.railway.app"))

    def handle(self, *args, **opts):
        token, domain = get_pipedrive_credentials()
        start = date.today() - timedelta(days=opts["past_days"])
        end = date.today() + timedelta(days=opts["days"])
        activities = pipedrive_get_all(
            token, domain, "activities",
            type="shoot_booking", start_date=start.isoformat(), end_date=end.isoformat(), user_id=0,
        )
        names = {str(a["id"]): n for a in activities if a.get("id") and (n := client_name_from_activity(a))}
        self.stdout.write(f"Pipedrive: {len(activities)} shoot activities, {len(names)} with a client name.")

        secret = os.getenv("OPPORTUNITIES_SYNC_SECRET") or settings.PIPEDRIVE_WEBHOOK_SECRET
        if not secret:
            raise CommandError("OPPORTUNITIES_SYNC_SECRET is not set.")
        url = opts["railway_url"].rstrip("/") + "/api/shoots/client-names/sync/"
        resp = requests.post(url, json={"names": names}, headers={"X-Sync-Secret": secret}, timeout=60)
        if resp.status_code != 200:
            raise CommandError(f"Railway rejected the sync ({resp.status_code}): {resp.text[:200]}")
        self.stdout.write(self.style.SUCCESS(f"Railway: {resp.json()}"))
