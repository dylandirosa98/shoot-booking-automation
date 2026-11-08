from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from .models import Shoot, Videographer


class FootageFeedTests(TestCase):
    def setUp(self):
        self.vid = Videographer.objects.create(name="Vic Video", email="vic@example.com", phone="555-0100", state="NJ", rating=4.5)
        now = timezone.now()
        self.confirmed = Shoot.objects.create(title="Shoot Booking", location="Hollydell Ice Arena, 601 Hollydell Dr, Sewell, NJ 08080",
                                              shoot_datetime=now + timedelta(days=2), status="confirmed", confirmed_videographer=self.vid,
                                              pipedrive_activity_id="101", notes="<b>Team</b> wears white")
        self.pending = Shoot.objects.create(title="Shoot Booking", location="Rink B", shoot_datetime=now + timedelta(days=5), status="pending")
        self.old = Shoot.objects.create(title="Old", location="Rink C", shoot_datetime=now - timedelta(days=90), status="confirmed")

    def get(self, auth):
        with mock.patch.dict("os.environ", {"FOOTAGE_FEED_SECRET": "s3cret"}):
            return self.client.get("/api/footage/shoots/", **({"HTTP_AUTHORIZATION": auth} if auth else {}))

    def test_requires_the_secret(self):
        self.assertEqual(self.get(None).status_code, 401)
        self.assertEqual(self.get("Bearer wrong").status_code, 401)
        with mock.patch.dict("os.environ", {"FOOTAGE_FEED_SECRET": ""}):
            self.assertEqual(self.client.get("/api/footage/shoots/", HTTP_AUTHORIZATION="Bearer ").status_code, 401)

    def test_lists_recent_and_upcoming_shoots_with_the_confirmed_videographer(self):
        r = self.get("Bearer s3cret")
        self.assertEqual(r.status_code, 200)
        shoots = r.json()["shoots"]
        self.assertEqual([s["id"] for s in shoots], [self.confirmed.id, self.pending.id])  # the 90-day-old one is outside the window
        first = shoots[0]
        self.assertEqual(first["videographer"], {"id": self.vid.id, "name": "Vic Video", "email": "vic@example.com", "phone": "555-0100"})
        self.assertEqual(first["notes"], "Team wears white")
        self.assertEqual(first["pipedriveActivityId"], "101")
        self.assertIsNone(shoots[1]["videographer"])

    def test_read_only(self):
        self.assertEqual(self.client.post("/api/footage/shoots/").status_code, 405)
