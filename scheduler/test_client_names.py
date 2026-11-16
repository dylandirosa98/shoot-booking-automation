import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from .client_names import backfill_client_names
from .models import SchedulingSettings, Shoot


class FakeSource:
    def __init__(self, activities):
        self.activities = activities
        self.calls = []

    def get_activity(self, activity_id):
        self.calls.append(activity_id)
        return self.activities.get(str(activity_id))


def _webhook_payload(activity_id, **extra):
    due = (timezone.now() + timedelta(days=5)).date().isoformat()
    data = {
        "id": activity_id, "type": "shoot_booking", "subject": "Shoot booking",
        "deal_id": 42, "due_date": due, "due_time": "18:00:00",
        "location": {"value": "Ice House, Hackensack, NJ", "lat": 40.88, "long": -74.04,
                     "admin_area_level_1": "New Jersey"},
        **extra,
    }
    return {"meta": {"action": "create", "entity": "activity"}, "data": data}


@override_settings(PIPEDRIVE_WEBHOOK_SECRET="", SHOOT_REMINDERS_ENABLED=False)
class ClientNameWebhookTests(TestCase):
    def setUp(self):
        cfg = SchedulingSettings.get()
        cfg.activity_type_filter = "shoot booking"
        cfg.save()

    def _post(self, payload):
        return self.client.post("/webhook/pipedrive/", data=json.dumps(payload), content_type="application/json")

    def test_name_in_payload_is_stored_without_lookup(self):
        source = FakeSource({})
        with patch("scheduler.reminder_integration.get_reminder_source", return_value=source):
            self._post(_webhook_payload(901, person_name="Salvatore Trazzera"))
        self.assertEqual(Shoot.objects.get(pipedrive_activity_id="901").client_name, "Salvatore Trazzera")
        self.assertEqual(source.calls, [])

    def test_v2_payload_without_name_is_looked_up(self):
        source = FakeSource({"902": {"id": 902, "person_name": "Jamie Rivera"}})
        with patch("scheduler.reminder_integration.get_reminder_source", return_value=source):
            self._post(_webhook_payload(902, person_id=7))
        self.assertEqual(Shoot.objects.get(pipedrive_activity_id="902").client_name, "Jamie Rivera")

    def test_lookup_failure_still_books_the_shoot(self):
        with patch("scheduler.reminder_integration.get_reminder_source", side_effect=RuntimeError("down")):
            response = self._post(_webhook_payload(903))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Shoot.objects.get(pipedrive_activity_id="903").client_name, "")

    def test_dashboard_shows_client_name(self):
        Shoot.objects.create(
            pipedrive_activity_id="904", title="Shoot booking", client_name="Salvatore Trazzera",
            location="Ice House", shoot_datetime=timezone.now() + timedelta(days=2),
        )
        User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.login(username="admin", password="pw")
        response = self.client.get("/")
        self.assertContains(response, "Salvatore Trazzera")


class ClientNameBackfillTests(TestCase):
    def test_backfills_upcoming_shoots_only(self):
        soon = timezone.now() + timedelta(days=3)
        past = timezone.now() - timedelta(days=3)
        Shoot.objects.create(pipedrive_activity_id="1", title="Shoot booking", location="A", shoot_datetime=soon)
        Shoot.objects.create(pipedrive_activity_id="2", title="Shoot booking", location="B", shoot_datetime=past)
        Shoot.objects.create(pipedrive_activity_id="3", title="Shoot booking", location="C",
                             shoot_datetime=soon, client_name="Already Set")
        source = FakeSource({"1": {"person_name": "Upcoming Client"}, "2": {"person_name": "Past Client"}})

        checked, filled = backfill_client_names(source=source)

        self.assertEqual((checked, filled), (1, 1))
        self.assertEqual(Shoot.objects.get(pipedrive_activity_id="1").client_name, "Upcoming Client")
        self.assertEqual(Shoot.objects.get(pipedrive_activity_id="2").client_name, "")
        self.assertEqual(Shoot.objects.get(pipedrive_activity_id="3").client_name, "Already Set")


class ClientNameSyncEndpointTests(TestCase):
    URL = "/api/shoots/client-names/sync/"

    def _post(self, body, secret="sync-secret"):
        with patch.dict("os.environ", {"OPPORTUNITIES_SYNC_SECRET": "sync-secret"}):
            return self.client.post(self.URL, data=json.dumps(body), content_type="application/json",
                                    HTTP_X_SYNC_SECRET=secret)

    def test_updates_matching_shoots(self):
        Shoot.objects.create(pipedrive_activity_id="18120", title="Shoot booking", location="A",
                             shoot_datetime=timezone.now() + timedelta(days=4))
        response = self._post({"names": {"18120": "Salvatore Trazzera", "99999": "Nobody"}})
        self.assertEqual(response.json()["updated"], 1)
        self.assertEqual(Shoot.objects.get(pipedrive_activity_id="18120").client_name, "Salvatore Trazzera")

    def test_rejects_wrong_secret(self):
        self.assertEqual(self._post({"names": {}}, secret="nope").status_code, 403)
