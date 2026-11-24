from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from .models import Invite, Shoot, Videographer, VideographerServiceState


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

    def test_assign_and_unassign_videographer(self):
        other = Videographer.objects.create(name="Sam Shoot", email="sam@example.com", phone="555-0101", state="NJ", rating=4.8)
        with mock.patch.dict("os.environ", {"FOOTAGE_FEED_SECRET": "s3cret"}):
            # Unauthorized rejected
            r = self.client.post(f"/api/footage/shoots/{self.pending.id}/assign/", data='{"email":"sam@example.com"}', content_type="application/json")
            self.assertEqual(r.status_code, 401)

            # Assign pending shoot -> confirmed with videographer
            r = self.client.post(
                f"/api/footage/shoots/{self.pending.id}/assign/",
                data='{"email":"SAM@example.com"}',
                content_type="application/json",
                HTTP_AUTHORIZATION="Bearer s3cret",
            )
            self.assertEqual(r.status_code, 200)
            self.pending.refresh_from_db()
            self.assertEqual(self.pending.status, "confirmed")
            self.assertEqual(self.pending.confirmed_videographer_id, other.id)

            # Unassign -> pending with no videographer
            r = self.client.post(
                f"/api/footage/shoots/{self.pending.id}/assign/",
                data='{"email":null}',
                content_type="application/json",
                HTTP_AUTHORIZATION="Bearer s3cret",
            )
            self.assertEqual(r.status_code, 200)
            self.pending.refresh_from_db()
            self.assertEqual(self.pending.status, "pending")
            self.assertIsNone(self.pending.confirmed_videographer_id)

            # Unknown videographer -> 404
            r = self.client.post(
                f"/api/footage/shoots/{self.pending.id}/assign/",
                data='{"email":"nobody@example.com"}',
                content_type="application/json",
                HTTP_AUTHORIZATION="Bearer s3cret",
            )
            self.assertEqual(r.status_code, 404)

    def test_feed_includes_active_videographer_roster_and_recommendations_scores(self):
        self.vid.lat = 39.75
        self.vid.lng = -75.15
        self.vid.city = "Sewell"
        self.vid.save()
        VideographerServiceState.objects.create(videographer=self.vid, state="NJ")
        VideographerServiceState.objects.create(videographer=self.vid, state="PA")
        far = Videographer.objects.create(
            name="Far Vid", email="far@example.com", phone="555-0200",
            state="MA", city="Boston", lat=42.36, lng=-71.06, rating=4.9, active=True,
        )
        Videographer.objects.create(
            name="Inactive Vid", email="old@example.com", state="NJ", rating=5.0, active=False,
        )
        self.pending.lat = 39.96
        self.pending.lng = -75.60
        self.pending.save()
        Invite.objects.create(
            shoot=self.pending, videographer=far, rank=0, score=3.1,
            drive_minutes=200, drive_miles=150, status="declined",
            expires_at=timezone.now() + timedelta(hours=24),
        )

        # Feed returns all active videographers (not inactive) with state, serviceStates, rating
        r = self.get("Bearer s3cret")
        self.assertEqual(r.status_code, 200)
        roster = r.json()["videographers"]
        self.assertEqual({v["email"] for v in roster}, {"vic@example.com", "far@example.com"})
        vic_row = next(v for v in roster if v["email"] == "vic@example.com")
        self.assertEqual(vic_row["serviceStates"], ["NJ", "PA"])
        self.assertEqual(vic_row["rating"], 4.5)

        # Recommendations endpoint requires auth and returns drive-time scores + invites
        with mock.patch.dict("os.environ", {"FOOTAGE_FEED_SECRET": "s3cret"}):
            self.assertEqual(self.client.get("/api/footage/recommendations/").status_code, 401)
            rec = self.client.get(
                f"/api/footage/recommendations/?shootIds={self.pending.id}",
                HTTP_AUTHORIZATION="Bearer s3cret",
            )
            self.assertEqual(rec.status_code, 200)
            body = rec.json()
            self.assertEqual({v["email"] for v in body["videographers"]}, {"vic@example.com", "far@example.com"})
            shoot_data = body["shoots"][str(self.pending.id)]
            self.assertEqual(shoot_data["ranked"][0]["email"], "vic@example.com")
            self.assertTrue(shoot_data["ranked"][0]["withinMaxDrive"])
            self.assertGreater(shoot_data["ranked"][0]["score"], shoot_data["ranked"][1]["score"])
            self.assertEqual(shoot_data["invites"][0], {
                "email": "far@example.com",
                "name": "Far Vid",
                "status": "declined",
                "rank": 0,
                "score": 3.1,
                "driveMinutes": 200,
                "driveMiles": 150.0,
            })
