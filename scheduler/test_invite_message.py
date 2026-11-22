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


class FootageSiteLinkTests(TestCase):
    """Today (no FOOTAGE_SITE_URL) nothing changes; once set, the invite and 48h reminder link to the shoot."""

    def setUp(self):
        self.shoot = Shoot.objects.create(location="Rink", shoot_datetime=timezone.now() + timedelta(days=3), notes="")

    def test_no_link_while_footage_goes_through_drive(self):
        from .shoot_reminders import render_reminder
        with self.settings(FOOTAGE_SITE_URL=""):
            self.assertEqual(_invite_message(self.shoot), "<b>Arrive 15-20 minutes before game time</b>")
            self.assertNotIn("http", render_reminder(self.shoot, 48, details_url=None))

    def test_invite_and_reminder_link_to_the_shoot_on_the_video_site(self):
        from .orchestrator import footage_link
        from .shoot_reminders import _footage_link, render_reminder
        url = f"https://video.puckpromedia.com/shoot/{self.shoot.id}"
        with self.settings(FOOTAGE_SITE_URL="https://video.puckpromedia.com"):
            self.assertEqual(footage_link(self.shoot), url)
            self.assertIn(f'<a href="{url}">{url}</a>', _invite_message(self.shoot))
            self.assertIn(url, render_reminder(self.shoot, 48, details_url=_footage_link(self.shoot)))
