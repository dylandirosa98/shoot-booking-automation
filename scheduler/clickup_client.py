import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

CLICKUP_EDITING_LIST_ID = "901709629903"
CLICKUP_EDITING_DEFAULT_STATUS = "waiting on footage"


@dataclass
class ClickUpTaskResult:
    task_id: str
    url: str = ""


def _datetime_to_clickup_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _task_description(job) -> str:
    return job.notes.strip() if job.notes else ""


class FakeClickUpClient:
    provider_name = "fake"

    def create_edit_task(self, job) -> ClickUpTaskResult:
        task_id = f"fake-clickup-{uuid.uuid4().hex[:12]}"
        logger.info(
            "[FAKE CLICKUP] CREATE task %s title=%r assignee=%s due=%s video_type=%s",
            task_id, job.title, getattr(job.assigned_editor, "clickup_user_id", None), job.due_datetime, job.video_type,
        )
        return ClickUpTaskResult(task_id=task_id)


class ClickUpClient:
    provider_name = "clickup"

    def __init__(self):
        self.base_url = "https://api.clickup.com/api/v2"
        self.headers = {
            "Authorization": settings.CLICKUP_API_TOKEN,
            "Content-Type": "application/json",
        }

    def create_edit_task(self, job) -> ClickUpTaskResult:
        if not job.assigned_editor or not job.assigned_editor.clickup_user_id:
            raise ValueError("Selected editor is missing a ClickUp user ID")

        body = {
            "name": job.title or f"{job.video_type or 'Edit'} job",
            "description": _task_description(job),
            "assignees": [int(job.assigned_editor.clickup_user_id)],
            "status": CLICKUP_EDITING_DEFAULT_STATUS,
            "due_date": _datetime_to_clickup_ms(job.due_datetime),
            "due_date_time": False,
        }

        response = requests.post(
            f"{self.base_url}/list/{CLICKUP_EDITING_LIST_ID}/task",
            headers=self.headers,
            json=body,
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp create task failed ({response.status_code}): {response.text[:500]}")

        data = response.json()
        return ClickUpTaskResult(task_id=data.get("id", ""), url=data.get("url", ""))


def get_client():
    if settings.CLICKUP_API_TOKEN:
        return ClickUpClient()
    return FakeClickUpClient()
