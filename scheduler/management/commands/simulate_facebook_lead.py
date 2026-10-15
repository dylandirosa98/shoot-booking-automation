"""
Push a fake Instant Form submission through the Facebook -> Pipedrive ingest path.

Does not call Meta. Uses the Pipedrive API token if set, otherwise the fake client.

Usage:
    python manage.py simulate_facebook_lead
    python manage.py simulate_facebook_lead --name "Jane Doe" --email jane@example.com
    python manage.py simulate_facebook_lead --answer "event_date=June 12" --answer "budget=under 5k"
"""
import uuid

from django.core.management.base import BaseCommand

from scheduler.facebook_leads import ingest_facebook_lead


class Command(BaseCommand):
    help = "Ingest a fake Facebook Instant Form lead into Pipedrive"

    def add_arguments(self, parser):
        parser.add_argument("--name", default="Jane Example")
        parser.add_argument("--email", default="jane.example@example.com")
        parser.add_argument("--phone", default="+15555550123")
        parser.add_argument("--leadgen-id", default="", help="Reuse an id to test dedupe")
        parser.add_argument(
            "--answer",
            action="append",
            default=[],
            help="Extra form field as name=value. Repeatable.",
        )

    def handle(self, *args, **opts):
        field_data = [
            {"name": "full_name", "values": [opts["name"]]},
            {"name": "email", "values": [opts["email"]]},
            {"name": "phone_number", "values": [opts["phone"]]},
        ]
        for raw in opts["answer"]:
            if "=" not in raw:
                self.stderr.write(self.style.ERROR(f"Bad --answer {raw!r}; expected name=value"))
                return
            name, value = raw.split("=", 1)
            field_data.append({"name": name.strip(), "values": [value.strip()]})

        leadgen_id = opts["leadgen_id"] or f"test-{uuid.uuid4().hex[:12]}"
        result = ingest_facebook_lead(
            leadgen_id=leadgen_id,
            page_id="test-page",
            form_id="test-form",
            ad_id="test-ad",
            created_time="2026-08-23T12:00:00+00:00",
            details={"field_data": field_data, "form_id": "test-form", "ad_id": "test-ad"},
        )
        if result.get("status") == "ok":
            self.stdout.write(self.style.SUCCESS(str(result)))
        else:
            self.stdout.write(self.style.WARNING(str(result)))
