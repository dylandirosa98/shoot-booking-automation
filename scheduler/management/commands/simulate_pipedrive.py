"""
Simulate a Pipedrive activity-added webhook hitting our endpoint.
No real Pipedrive needed.

Usage:
    python manage.py simulate_pipedrive                       # default fake shoot in Newark NJ
    python manage.py simulate_pipedrive --location "Boston, MA"
    python manage.py simulate_pipedrive --location "Princeton, NJ" --type "Shoot Booking"
"""
import json
import uuid
from datetime import datetime, timedelta
import httpx
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "POST a fake Pipedrive activity payload to our webhook"

    def add_arguments(self, parser):
        parser.add_argument("--url", default="http://127.0.0.1:8000/webhook/pipedrive/")
        parser.add_argument("--location", default="Newark, NJ")
        parser.add_argument("--subject", default="Hockey Shoot - Newark Rink")
        parser.add_argument("--type", default="Shoot Booking", help="Pipedrive activity type")
        parser.add_argument("--notes", default="Bring drone, 2-cam setup, ice time starts sharp.")
        parser.add_argument("--days-out", type=int, default=10, help="Days from today")
        parser.add_argument("--auth", default="", help="Basic auth string user:pass (matches PIPEDRIVE_WEBHOOK_SECRET)")
        parser.add_argument("--v2", action="store_true",
                            help="Send v2-shaped payload with structured location object (default = v1 string)")
        parser.add_argument("--lat", type=float, default=None, help="(v2) lat for structured location")
        parser.add_argument("--lng", type=float, default=None, help="(v2) lng for structured location")
        parser.add_argument("--state", default=None, help="(v2) 2-letter state for structured location")

    def handle(self, *args, **opts):
        when = datetime.now() + timedelta(days=opts["days_out"])
        deal_id = uuid.uuid4().hex[:8]
        activity_id = int(uuid.uuid4().int % 100000)

        if opts["v2"]:
            # Pipedrive v2 webhook shape with structured location
            loc_obj = {"value": opts["location"]}
            if opts["lat"] is not None and opts["lng"] is not None:
                loc_obj["lat"] = opts["lat"]
                loc_obj["long"] = opts["lng"]
            if opts["state"]:
                loc_obj["admin_area_level_1"] = opts["state"]
            payload = {
                "meta": {"action": "create", "entity": "activity", "version": "2.0"},
                "data": {
                    "id": activity_id,
                    "deal_id": deal_id,
                    "type": opts["type"],
                    "subject": opts["subject"],
                    "location": loc_obj,
                    "due_date": when.strftime("%Y-%m-%d"),
                    "due_time": when.strftime("%H:%M"),
                    "note": opts["notes"],
                },
                "previous": None,
            }
        else:
            # Pipedrive v1 webhook shape with plain string location
            payload = {
                "v": 1,
                "event": "added.activity",
                "current": {
                    "id": activity_id,
                    "deal_id": deal_id,
                    "type": opts["type"],
                    "subject": opts["subject"],
                    "location": opts["location"],
                    "due_date": when.strftime("%Y-%m-%d"),
                    "due_time": when.strftime("%H:%M"),
                    "note": opts["notes"],
                },
            }

        self.stdout.write(self.style.NOTICE(f"Posting to {opts['url']}"))
        self.stdout.write(json.dumps(payload, indent=2))

        headers = {}
        if opts["auth"]:
            import base64
            headers["Authorization"] = "Basic " + base64.b64encode(opts["auth"].encode()).decode()

        try:
            r = httpx.post(opts["url"], json=payload, headers=headers, timeout=30)
            self.stdout.write(self.style.SUCCESS(f"\nResponse {r.status_code}: {r.text}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Request failed: {e}"))
