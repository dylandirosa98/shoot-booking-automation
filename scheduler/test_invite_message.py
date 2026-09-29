from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from .models import Shoot
from .orchestrator import _invite_message


class InviteMessageTests(TestCase):
    def test_invite_keeps_team_notes_and_drops_greeting_distance_and_accept_prompt(self):
        shoot = Shoot.objects.create(
            title="Test Shoot",
            location="New England Sports Center, Marlborough, MA",
            shoot_datetime=timezone.now() + timedelta(days=3),
            notes=(
                "<strong>For:</strong> Steven Powell\n\n"
                "<strong>Player info:</strong> Klay &lt;Barker&gt; &amp; co"
            ),
        )

        message = _invite_message(shoot)

        self.assertIn("<b>Arrive 15-20 minutes before game time</b>", message)
        self.assertIn("Shoot details / notes from the team:", message)
        self.assertIn("<strong>For:</strong> Steven Powell", message)
        self.assertIn("<strong>Player info:</strong> Klay &lt;Barker&gt; &amp; co", message)
        self.assertNotIn("&amp;lt;", message)
        self.assertNotIn("Hi ", message)
        self.assertNotIn("top pick", message)
        self.assertNotIn("Distance:", message)
        self.assertNotIn("Please accept", message)
        self.assertNotIn("decline", message)
        self.assertNotIn(shoot.location, message)

    def test_invite_without_notes_is_only_the_arrival_line(self):
        shoot = Shoot.objects.create(
            location="Rink",
            shoot_datetime=timezone.now() + timedelta(days=1),
            notes="   ",
        )

        self.assertEqual(
            _invite_message(shoot),
            "<b>Arrive 15-20 minutes before game time</b>",
        )
