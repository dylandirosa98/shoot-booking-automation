import json
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from .clickup_client import ClickUpTaskResult
from .editing import rank_editors
from .models import Editor, EditorVideoTypeRank, EditJob, SchedulingSettings, Shoot


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
