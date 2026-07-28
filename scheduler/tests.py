import json
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from .clickup_client import ClickUpTaskResult
from .drive_client import DriveFolder
from .editing import rank_editors
from .models import Editor, EditorVideoTypeRank, EditJob, Invite, SchedulingSettings, Shoot, Videographer, VideographerServiceState
from .orchestrator import _mark_accepted, _send_invite, check_and_escalate, poll_all_pending_invites
from .scoring import rank_for_shoot


def activity_payload(*, action="create", activity_id="act-1", deal_id="deal-1", type_name="Highlight Recap", subject="Test highlight edit"):
    return {
        "meta": {"action": action, "entity": "activity"},
        "data": {
            "id": activity_id,
            "deal_id": deal_id,
            "type": type_name,
            "subject": subject,
            "due_date": "2026-07-01",
            "due_time": "15:30:00",
            "duration": "01:00:00",
            "note": "<p>Fallback note</p>",
            "description": "<p>Fallback description</p>",
            "public_description": "<p>Edit description</p>",
            "done": False,
        },
    }


class EditorSelectionTests(TestCase):
    def setUp(self):
        EditJob.objects.all().delete()
        EditorVideoTypeRank.objects.all().delete()
        Editor.objects.all().delete()
        cfg = SchedulingSettings.get()
        cfg.activity_type_filter = "Shoot Booking"
        cfg.edit_activity_type_filter = "Recruiting Highlight Video,Hype Video,Highlight Recap"
        cfg.edit_subject_filter = ""
        cfg.save()

    def test_rank_editors_uses_video_type_rank_and_skips_capacity(self):
        first_choice = Editor.objects.create(name="First Choice", email="a@example.com", max_active_jobs=2)
        second_choice = Editor.objects.create(name="Second Choice", email="b@example.com", max_active_jobs=2)
        third_choice = Editor.objects.create(name="Third Choice", email="c@example.com", max_active_jobs=5)
        EditorVideoTypeRank.objects.create(editor=first_choice, video_type="Highlight", rank=1)
        EditorVideoTypeRank.objects.create(editor=second_choice, video_type="Highlight", rank=2)
        EditorVideoTypeRank.objects.create(editor=third_choice, video_type="Highlight", rank=3)
        EditorVideoTypeRank.objects.create(editor=third_choice, video_type="Hype", rank=1)
        for editor in [first_choice, second_choice]:
            for idx in range(2):
                EditJob.objects.create(
                    pipedrive_activity_id=f"existing-{editor.id}-{idx}",
                    title="Existing",
                    due_datetime=timezone.now() + timedelta(days=idx + 1),
                    assigned_editor=editor,
                    status="created",
                )

        ranked = rank_editors("Highlight")

        self.assertEqual(ranked[0].editor, third_choice)
        self.assertEqual(ranked[0].rank, 3)

    def test_edit_activity_webhook_creates_local_edit_job(self):
        editor = Editor.objects.create(name="Editor One", email="editor@example.com", max_active_jobs=5, clickup_user_id=12345)
        EditorVideoTypeRank.objects.create(editor=editor, video_type="Highlight", rank=1)

        class FakeClickUp:
            def create_edit_task(self, job):
                self.job = job
                return ClickUpTaskResult(task_id="clickup-123")

        fake_clickup = FakeClickUp()
        with patch("scheduler.editing.get_clickup_client", return_value=fake_clickup):
            response = Client().post(
                "/webhook/pipedrive/",
                data=json.dumps(activity_payload()),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["action"], "created")
        job = EditJob.objects.get(pipedrive_activity_id="act-1")
        self.assertEqual(job.assigned_editor, editor)
        self.assertEqual(job.status, "created")
        self.assertEqual(job.duration_minutes, 60)
        self.assertEqual(job.clickup_task_id, "clickup-123")
        self.assertEqual(fake_clickup.job.due_datetime, job.due_datetime)
        self.assertEqual(fake_clickup.job.title, "Test highlight edit")
        self.assertEqual(fake_clickup.job.notes, "Edit description")
        self.assertEqual(fake_clickup.job.video_type, "Highlight")

    def test_date_only_activity_keeps_due_date_on_local_day(self):
        editor = Editor.objects.create(name="Editor One", email="editor@example.com", max_active_jobs=5, clickup_user_id=12345)
        EditorVideoTypeRank.objects.create(editor=editor, video_type="Highlight", rank=1)

        payload = activity_payload()
        payload["data"]["due_date"] = "2026-06-22"
        payload["data"]["due_time"] = None

        class FakeClickUp:
            def create_edit_task(self, job):
                self.job = job
                return ClickUpTaskResult(task_id="clickup-123")

        fake_clickup = FakeClickUp()
        with patch("scheduler.editing.get_clickup_client", return_value=fake_clickup):
            response = Client().post(
                "/webhook/pipedrive/",
                data=json.dumps(payload),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        job = EditJob.objects.get(pipedrive_activity_id="act-1")
        self.assertEqual(timezone.localtime(job.due_datetime).date().isoformat(), "2026-06-22")
        self.assertEqual(timezone.localtime(fake_clickup.job.due_datetime).date().isoformat(), "2026-06-22")

    def test_edit_activity_type_maps_video_types(self):
        cases = [
            ("Highlight Recap", "Highlight"),
            ("Hype Video", "Hype"),
            ("Recruiting Highlight Video", "Recruiting"),
        ]
        for idx, (activity_type, video_type) in enumerate(cases):
            editor = Editor.objects.create(
                name=f"Editor {idx}",
                email=f"editor{idx}@example.com",
                max_active_jobs=5,
                clickup_user_id=1000 + idx,
            )
            EditorVideoTypeRank.objects.create(editor=editor, video_type=video_type, rank=1)

            class FakeClickUp:
                def create_edit_task(self, job):
                    return ClickUpTaskResult(task_id=f"clickup-{idx}")

            with patch("scheduler.editing.get_clickup_client", return_value=FakeClickUp()):
                response = Client().post(
                    "/webhook/pipedrive/",
                    data=json.dumps(activity_payload(activity_id=f"act-{idx}", type_name=activity_type)),
                    content_type="application/json",
                )

            self.assertEqual(response.status_code, 200)
            job = EditJob.objects.get(pipedrive_activity_id=f"act-{idx}")
            self.assertEqual(job.video_type, video_type)
            self.assertEqual(job.assigned_editor, editor)

    def test_editor_dashboard_renders(self):
        Editor.objects.create(name="Editor One", email="editor@example.com", max_active_jobs=5)

        response = Client().get("/edits/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Editor Dashboard")

    def test_non_edit_email_change_does_not_cancel_shoot_on_same_deal(self):
        shoot = Shoot.objects.create(
            pipedrive_deal_id="deal-1",
            pipedrive_activity_id="shoot-1",
            title="Existing shoot",
            location="Boston, MA",
            shoot_datetime=timezone.now() + timedelta(days=10),
            status="pending",
        )

        response = Client().post(
            "/webhook/pipedrive/",
            data=json.dumps(activity_payload(action="change", activity_id="email-1", type_name="Email", subject="Normal email")),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        shoot.refresh_from_db()
        self.assertEqual(shoot.status, "pending")
        self.assertEqual(EditJob.objects.count(), 0)


class VideographerServiceStateTests(TestCase):
    def setUp(self):
        cfg = SchedulingSettings.get()
        cfg.same_state_only = True
        cfg.max_drive_minutes = 10000
        cfg.score_penalty_per_minute = 0.01
        cfg.save()

    def test_service_state_makes_home_state_videographer_eligible_without_changing_address(self):
        videographer = Videographer.objects.create(
            name="Multi State Video",
            email="multi@example.com",
            state="MA",
            city="Boston",
            address="Boston, MA",
            lat=42.3601,
            lng=-71.0589,
            rating=5.0,
            active=True,
        )
        VideographerServiceState.objects.create(videographer=videographer, state="RI")

        ranked = rank_for_shoot(41.8240, -71.4128, shoot_state="RI")

        self.assertEqual([item.videographer for item in ranked], [videographer])
        self.assertEqual(videographer.state, "MA")
        self.assertEqual(videographer.address, "Boston, MA")
        self.assertGreater(ranked[0].drive_minutes, 0)

    def test_same_state_filter_excludes_videographer_without_matching_service_state(self):
        videographer = Videographer.objects.create(
            name="Home State Only",
            email="home@example.com",
            state="MA",
            city="Boston",
            address="Boston, MA",
            lat=42.3601,
            lng=-71.0589,
            rating=5.0,
            active=True,
        )
        VideographerServiceState.objects.create(videographer=videographer, state="MA")

        ranked = rank_for_shoot(41.8240, -71.4128, shoot_state="RI")

        self.assertEqual(ranked, [])

    def test_dashboard_lists_videographer_under_each_service_state_and_empty_states(self):
        videographer = Videographer.objects.create(
            name="Multi State Video",
            email="multi@example.com",
            state="MA",
            city="Boston",
            address="Boston, MA",
            lat=42.3601,
            lng=-71.0589,
            rating=5.0,
            active=True,
        )
        VideographerServiceState.objects.create(videographer=videographer, state="MA")
        VideographerServiceState.objects.create(videographer=videographer, state="CT")

        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Multi State Video", count=2)
        self.assertContains(response, "No videographers currently serve DE.")


class VideographerEscalationTests(TestCase):
    def setUp(self):
        cfg = SchedulingSettings.get()
        cfg.escalation_hours = 24
        cfg.save()

    def _shoot_with_invites(self):
        first = Videographer.objects.create(
            name="First Video", email="first@example.com", state="MI", rating=5.0, active=True,
        )
        second = Videographer.objects.create(
            name="Second Video", email="second@example.com", state="MI", rating=4.8, active=True,
        )
        shoot = Shoot.objects.create(
            pipedrive_deal_id="deal-shoot",
            pipedrive_activity_id="shoot-activity",
            title="Test Shoot",
            location="Detroit, MI",
            shoot_datetime=timezone.now() + timedelta(days=5),
            duration_minutes=120,
            status="pending",
        )
        first_invite = Invite.objects.create(
            shoot=shoot,
            videographer=first,
            rank=0,
            score=10,
            drive_miles=5,
            drive_minutes=10,
            status="pending",
            google_event_id="event-123",
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        second_invite = Invite.objects.create(
            shoot=shoot,
            videographer=second,
            rank=1,
            score=9,
            drive_miles=6,
            drive_minutes=12,
            status="pending",
            expires_at=timezone.now(),
        )
        return shoot, first_invite, second_invite

    def test_escalation_skips_invite_that_is_already_accepted(self):
        _, first_invite, second_invite = self._shoot_with_invites()
        first_invite.status = "accepted"
        first_invite.save(update_fields=["status"])

        class FakeCalendar:
            def __init__(self):
                self.status_checked = False

            def get_attendee_status(self, event_id, attendee_email):
                self.status_checked = True
                return "needsAction"

        fake_calendar = FakeCalendar()
        with patch("scheduler.orchestrator.get_client", return_value=fake_calendar):
            check_and_escalate(first_invite.id)

        first_invite.refresh_from_db()
        second_invite.refresh_from_db()
        self.assertEqual(first_invite.status, "accepted")
        self.assertEqual(second_invite.google_event_id, "")
        self.assertFalse(fake_calendar.status_checked)

    def test_shoot_detail_renders_manual_controls(self):
        shoot, _first_invite, _second_invite = self._shoot_with_invites()

        response = Client().get(f"/shoots/{shoot.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manual Send")
        self.assertContains(response, "Queued Order")

    def test_manual_send_moves_selected_invite_next_and_reuses_event(self):
        shoot, first_invite, second_invite = self._shoot_with_invites()
        third = Videographer.objects.create(
            name="Third Video", email="third@example.com", state="MI", rating=4.6, active=True,
        )
        third_invite = Invite.objects.create(
            shoot=shoot,
            videographer=third,
            rank=2,
            score=8,
            drive_miles=7,
            drive_minutes=14,
            status="pending",
            expires_at=timezone.now(),
        )

        class FakeCalendar:
            def __init__(self):
                self.replacements = []

            def replace_event_attendee(self, *, event_id, description, attendee_email):
                self.replacements.append((event_id, attendee_email))
                return event_id

        fake_calendar = FakeCalendar()
        with patch("scheduler.orchestrator.get_client", return_value=fake_calendar):
            with patch("scheduler.jobs.schedule_escalation_check"):
                response = Client().post(
                    f"/shoots/{shoot.id}/manual-send/",
                    data={"videographer_id": str(third.id)},
                )

        self.assertEqual(response.status_code, 302)
        first_invite.refresh_from_db()
        second_invite.refresh_from_db()
        third_invite.refresh_from_db()
        self.assertEqual(first_invite.status, "declined")
        self.assertEqual(first_invite.rank, 0)
        self.assertEqual(third_invite.status, "pending")
        self.assertEqual(third_invite.rank, 1)
        self.assertEqual(third_invite.google_event_id, "event-123")
        self.assertEqual(second_invite.rank, 2)
        self.assertEqual(fake_calendar.replacements, [("event-123", "third@example.com")])

    def test_manual_send_existing_queued_invite_does_not_duplicate_and_rewrites_chain(self):
        shoot, first_invite, second_invite = self._shoot_with_invites()
        third = Videographer.objects.create(
            name="Third Video", email="third@example.com", state="MI", rating=4.6, active=True,
        )
        fourth = Videographer.objects.create(
            name="Fourth Video", email="fourth@example.com", state="MI", rating=4.4, active=True,
        )
        third_invite = Invite.objects.create(
            shoot=shoot, videographer=third, rank=2, score=8,
            drive_miles=7, drive_minutes=14, status="pending", expires_at=timezone.now(),
        )
        fourth_invite = Invite.objects.create(
            shoot=shoot, videographer=fourth, rank=3, score=7,
            drive_miles=8, drive_minutes=16, status="pending", expires_at=timezone.now(),
        )

        class FakeCalendar:
            def __init__(self):
                self.replacements = []

            def replace_event_attendee(self, *, event_id, description, attendee_email):
                self.replacements.append((event_id, attendee_email, "Third" in description))
                return event_id

        fake_calendar = FakeCalendar()
        with patch("scheduler.orchestrator.get_client", return_value=fake_calendar):
            with patch("scheduler.jobs.schedule_escalation_check") as schedule_check:
                response = Client().post(
                    f"/shoots/{shoot.id}/manual-send/",
                    data={"videographer_id": str(third.id)},
                )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Invite.objects.filter(shoot=shoot).count(), 4)
        first_invite.refresh_from_db()
        second_invite.refresh_from_db()
        third_invite.refresh_from_db()
        fourth_invite.refresh_from_db()
        shoot.refresh_from_db()

        self.assertEqual(shoot.status, "pending")
        self.assertIsNone(shoot.confirmed_videographer)
        self.assertEqual(first_invite.status, "declined")
        self.assertIsNotNone(first_invite.responded_at)
        self.assertEqual(first_invite.google_event_id, "event-123")
        self.assertEqual(third_invite.status, "pending")
        self.assertEqual(third_invite.google_event_id, "event-123")
        self.assertGreater(third_invite.expires_at, timezone.now())
        self.assertEqual(second_invite.status, "pending")
        self.assertEqual(second_invite.google_event_id, "")
        self.assertEqual(fourth_invite.status, "pending")
        self.assertEqual(fourth_invite.google_event_id, "")
        self.assertEqual(
            list(shoot.invites.order_by("rank").values_list("videographer__email", "status", "google_event_id")),
            [
                ("first@example.com", "declined", "event-123"),
                ("third@example.com", "pending", "event-123"),
                ("second@example.com", "pending", ""),
                ("fourth@example.com", "pending", ""),
            ],
        )
        self.assertEqual(fake_calendar.replacements, [("event-123", "third@example.com", True)])
        schedule_check.assert_called_once_with(third_invite.id)

    def test_manual_send_existing_queued_then_decline_continues_with_original_next_person(self):
        shoot, first_invite, second_invite = self._shoot_with_invites()
        third = Videographer.objects.create(
            name="Third Video", email="third@example.com", state="MI", rating=4.6, active=True,
        )
        fourth = Videographer.objects.create(
            name="Fourth Video", email="fourth@example.com", state="MI", rating=4.4, active=True,
        )
        third_invite = Invite.objects.create(
            shoot=shoot, videographer=third, rank=2, score=8,
            drive_miles=7, drive_minutes=14, status="pending", expires_at=timezone.now(),
        )
        fourth_invite = Invite.objects.create(
            shoot=shoot, videographer=fourth, rank=3, score=7,
            drive_miles=8, drive_minutes=16, status="pending", expires_at=timezone.now(),
        )

        class FakeCalendar:
            def __init__(self):
                self.replacements = []
                self.status_checks = []

            def replace_event_attendee(self, *, event_id, description, attendee_email):
                self.replacements.append((event_id, attendee_email))
                return event_id

            def get_attendee_status(self, event_id, attendee_email):
                self.status_checks.append((event_id, attendee_email))
                return "declined"

            def cancel_event(self, event_id):
                raise AssertionError("Manual decline should move the event to the next queued videographer")

        fake_calendar = FakeCalendar()
        with patch("scheduler.orchestrator.get_client", return_value=fake_calendar):
            with patch("scheduler.jobs.schedule_escalation_check"):
                Client().post(
                    f"/shoots/{shoot.id}/manual-send/",
                    data={"videographer_id": str(third.id)},
                )
                poll_all_pending_invites()

        first_invite.refresh_from_db()
        second_invite.refresh_from_db()
        third_invite.refresh_from_db()
        fourth_invite.refresh_from_db()
        self.assertEqual(first_invite.status, "declined")
        self.assertEqual(third_invite.status, "declined")
        self.assertEqual(second_invite.status, "pending")
        self.assertEqual(second_invite.google_event_id, "event-123")
        self.assertEqual(fourth_invite.status, "pending")
        self.assertEqual(fourth_invite.google_event_id, "")
        self.assertEqual(
            list(shoot.invites.order_by("rank").values_list("videographer__email", "status")),
            [
                ("first@example.com", "declined"),
                ("third@example.com", "declined"),
                ("second@example.com", "pending"),
                ("fourth@example.com", "pending"),
            ],
        )
        self.assertEqual(fake_calendar.status_checks, [("event-123", "third@example.com")])
        self.assertEqual(
            fake_calendar.replacements,
            [("event-123", "third@example.com"), ("event-123", "second@example.com")],
        )

    def test_reorder_invites_changes_only_unsent_queue(self):
        shoot, first_invite, second_invite = self._shoot_with_invites()
        third = Videographer.objects.create(
            name="Third Video", email="third@example.com", state="MI", rating=4.6, active=True,
        )
        third_invite = Invite.objects.create(
            shoot=shoot,
            videographer=third,
            rank=2,
            score=8,
            drive_miles=7,
            drive_minutes=14,
            status="pending",
            expires_at=timezone.now(),
        )

        response = Client().post(
            f"/shoots/{shoot.id}/reorder-invites/",
            data={"invite_ids": [str(third_invite.id), str(second_invite.id)]},
        )

        self.assertEqual(response.status_code, 302)
        first_invite.refresh_from_db()
        second_invite.refresh_from_db()
        third_invite.refresh_from_db()
        self.assertEqual(first_invite.rank, 0)
        self.assertEqual(third_invite.rank, 1)
        self.assertEqual(second_invite.rank, 2)
        self.assertEqual(first_invite.google_event_id, "event-123")

    def test_escalation_reuses_calendar_event_for_next_videographer(self):
        _, first_invite, second_invite = self._shoot_with_invites()

        class FakeCalendar:
            def __init__(self):
                self.replacements = []
                self.cancelled = []

            def get_attendee_status(self, event_id, attendee_email):
                return "needsAction"

            def replace_event_attendee(self, *, event_id, description, attendee_email):
                self.replacements.append((event_id, attendee_email, description))
                return event_id

            def cancel_event(self, event_id):
                self.cancelled.append(event_id)

        fake_calendar = FakeCalendar()
        with patch("scheduler.orchestrator.get_client", return_value=fake_calendar):
            with patch("scheduler.jobs.schedule_escalation_check"):
                check_and_escalate(first_invite.id)

        first_invite.refresh_from_db()
        second_invite.refresh_from_db()
        self.assertEqual(first_invite.status, "expired")
        self.assertEqual(second_invite.google_event_id, "event-123")
        self.assertEqual(fake_calendar.replacements[0][0], "event-123")
        self.assertEqual(fake_calendar.replacements[0][1], "second@example.com")
        self.assertEqual(fake_calendar.cancelled, [])

    def test_decline_poll_reuses_calendar_event_for_next_videographer(self):
        _, first_invite, second_invite = self._shoot_with_invites()

        class FakeCalendar:
            def __init__(self):
                self.replacements = []

            def get_attendee_status(self, event_id, attendee_email):
                return "declined"

            def replace_event_attendee(self, *, event_id, description, attendee_email):
                self.replacements.append((event_id, attendee_email))
                return event_id

            def cancel_event(self, event_id):
                raise AssertionError("Decline should reuse the event when another videographer is available")

        fake_calendar = FakeCalendar()
        with patch("scheduler.orchestrator.get_client", return_value=fake_calendar):
            with patch("scheduler.jobs.schedule_escalation_check"):
                poll_all_pending_invites()

        first_invite.refresh_from_db()
        second_invite.refresh_from_db()
        self.assertEqual(first_invite.status, "declined")
        self.assertEqual(second_invite.google_event_id, "event-123")
        self.assertEqual(fake_calendar.replacements, [("event-123", "second@example.com")])

    def test_accepted_invite_creates_and_shares_drive_folder_once(self):
        shoot, first_invite, _ = self._shoot_with_invites()

        class FakeDrive:
            def __init__(self):
                self.created = []
                self.shared = []

            def create_folder(self, *, name, shoot_id):
                self.created.append((name, shoot_id))
                return DriveFolder(id="folder-123", url="https://drive.example/folder-123")

            def share_folder(self, *, folder_id, email):
                self.shared.append((folder_id, email))
                return "permission-123"

        fake_drive = FakeDrive()
        with patch("scheduler.orchestrator.get_drive_client", return_value=fake_drive):
            _mark_accepted(first_invite)
            _mark_accepted(first_invite)

        shoot.refresh_from_db()
        first_invite.refresh_from_db()
        self.assertEqual(shoot.status, "confirmed")
        self.assertEqual(shoot.google_drive_folder_id, "folder-123")
        self.assertEqual(shoot.google_drive_folder_url, "https://drive.example/folder-123")
        self.assertEqual(first_invite.google_drive_permission_id, "permission-123")
        self.assertEqual(fake_drive.created[0][1], shoot.id)
        self.assertTrue(fake_drive.created[0][0].startswith("Detroit, MI — "))
        self.assertEqual(fake_drive.shared, [("folder-123", "first@example.com")])

    def test_failed_calendar_send_records_error_without_event(self):
        shoot, _first_invite, second_invite = self._shoot_with_invites()

        class FailingCalendar:
            def create_event(self, **kwargs):
                raise RuntimeError("Calendar credentials rejected")

        with patch("scheduler.orchestrator.get_client", return_value=FailingCalendar()):
            result = _send_invite(shoot, rank=second_invite.rank)

        second_invite.refresh_from_db()
        self.assertIsNone(result)
        self.assertEqual(second_invite.google_event_id, "")
        self.assertIn("Calendar credentials rejected", second_invite.calendar_error)
        self.assertIsNotNone(second_invite.calendar_last_attempt_at)

    def test_poll_retries_one_unsent_calendar_invite(self):
        shoot, first_invite, second_invite = self._shoot_with_invites()
        first_invite.google_event_id = ""
        first_invite.calendar_error = "Previous Calendar failure"
        first_invite.save(update_fields=["google_event_id", "calendar_error"])

        class FakeCalendar:
            def __init__(self):
                self.created_for = []

            def create_event(self, **kwargs):
                self.created_for.append(kwargs["attendee_email"])
                return "retry-event-123"

            def get_attendee_status(self, event_id, attendee_email):
                return "needsAction"

        fake_calendar = FakeCalendar()
        with patch("scheduler.orchestrator.get_client", return_value=fake_calendar):
            with patch("scheduler.jobs.schedule_escalation_check"):
                poll_all_pending_invites()

        first_invite.refresh_from_db()
        second_invite.refresh_from_db()
        self.assertEqual(first_invite.google_event_id, "retry-event-123")
        self.assertEqual(first_invite.calendar_error, "")
        self.assertEqual(second_invite.google_event_id, "")
        self.assertEqual(fake_calendar.created_for, ["first@example.com"])

    def test_manual_send_allows_an_active_out_of_state_videographer(self):
        shoot, first_invite, _second_invite = self._shoot_with_invites()
        out_of_state = Videographer.objects.create(
            name="Out of State Video", email="outofstate@example.com", state="MA",
            city="Boston", rating=4.7, active=True,
        )

        class FakeCalendar:
            def __init__(self):
                self.replacements = []

            def replace_event_attendee(self, *, event_id, description, attendee_email):
                self.replacements.append((event_id, attendee_email, description))
                return event_id

        fake_calendar = FakeCalendar()
        with patch("scheduler.orchestrator.get_client", return_value=fake_calendar):
            with patch("scheduler.jobs.schedule_escalation_check"):
                response = Client().post(
                    f"/shoots/{shoot.id}/manual-send/",
                    data={
                        "videographer_id": str(out_of_state.id),
                        "notes": "Updated instructions for the out-of-state videographer.",
                        "action": "send",
                    },
                )

        shoot.refresh_from_db()
        first_invite.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(shoot.notes, "Updated instructions for the out-of-state videographer.")
        self.assertEqual(first_invite.status, "declined")
        self.assertEqual(fake_calendar.replacements[0][1], "outofstate@example.com")

    def test_manual_send_can_save_notes_without_sending(self):
        shoot, first_invite, _second_invite = self._shoot_with_invites()

        response = Client().post(
            f"/shoots/{shoot.id}/manual-send/",
            data={"notes": "Bring a second camera body.", "action": "save_notes"},
        )

        shoot.refresh_from_db()
        first_invite.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(shoot.notes, "Bring a second camera body.")
        self.assertEqual(first_invite.status, "pending")
        self.assertEqual(first_invite.google_event_id, "event-123")

    def test_manual_send_search_displays_all_active_videographers(self):
        shoot, _first_invite, _second_invite = self._shoot_with_invites()
        out_of_state = Videographer.objects.create(
            name="Boston Search Result", email="boston@example.com", state="MA",
            city="Boston", rating=4.7, active=True,
        )

        response = Client().get(f"/shoots/{shoot.id}/")

        self.assertContains(response, "videographer-search")
        self.assertContains(response, "Boston Search Result")
        self.assertContains(response, out_of_state.email)
