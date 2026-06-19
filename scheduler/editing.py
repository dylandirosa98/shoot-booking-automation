import logging
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count, Q
from django.utils import timezone

from .models import Editor, EditorVideoTypeRank, EditJob
from .clickup_client import get_client as get_clickup_client

logger = logging.getLogger(__name__)


@dataclass
class RankedEditor:
    editor: Editor
    rank: int
    active_jobs: int


def rank_editors(video_type: str) -> list[RankedEditor]:
    active_filter = Q(editor__edit_jobs__status__in=EditJob.ACTIVE_STATUSES)
    ranks = (
        EditorVideoTypeRank.objects
        .filter(active=True, video_type=video_type, editor__active=True)
        .select_related("editor")
        .annotate(active_jobs=Count("editor__edit_jobs", filter=active_filter))
    )

    ranked: list[RankedEditor] = []
    for ranking in ranks:
        editor = ranking.editor
        active_jobs = ranking.active_jobs or 0
        if active_jobs >= editor.max_active_jobs:
            continue
        ranked.append(RankedEditor(editor=editor, rank=ranking.rank, active_jobs=active_jobs))

    ranked.sort(key=lambda item: (item.rank, item.editor.name.lower()))
    return ranked


def describe_editor_selection(video_type: str, selected: RankedEditor | None) -> str:
    ranks = (
        EditorVideoTypeRank.objects
        .filter(active=True, video_type=video_type, editor__active=True)
        .select_related("editor")
        .annotate(active_jobs=Count("editor__edit_jobs", filter=Q(editor__edit_jobs__status__in=EditJob.ACTIVE_STATUSES)))
        .order_by("rank", "editor__name")
    )
    lines = [f"{video_type} editor rankings are checked in rank order."]
    for ranking in ranks:
        editor = ranking.editor
        active_jobs = ranking.active_jobs or 0
        if active_jobs >= editor.max_active_jobs:
            lines.append(f"Skipped #{ranking.rank} {editor.name}: {active_jobs}/{editor.max_active_jobs} active jobs.")
            continue
        prefix = "Selected" if selected and editor.id == selected.editor.id else "Available"
        lines.append(f"{prefix} #{ranking.rank} {editor.name}: {active_jobs}/{editor.max_active_jobs} active jobs.")
    if not ranks:
        lines.append(f"No active editor rankings exist for {video_type}.")
    if not selected:
        lines.append("No editor was selected because every ranked editor is at capacity or unavailable.")
    return "\n".join(lines)


def handle_new_edit_job(
    *,
    pipedrive_deal_id: str | None,
    pipedrive_activity_id: str | None = None,
    title: str,
    video_type: str = "",
    due_datetime: datetime = None,
    duration_minutes: int = 0,
    notes: str = "",
) -> EditJob:
    if pipedrive_activity_id:
        existing = EditJob.objects.filter(pipedrive_activity_id=pipedrive_activity_id).first()
        if existing:
            logger.info("Edit job for activity %s already exists (id=%s), skipping", pipedrive_activity_id, existing.id)
            return existing

    ranked = rank_editors(video_type)
    selected = ranked[0] if ranked else None
    if not selected:
        logger.warning("No eligible editors for edit activity %s", pipedrive_activity_id)
        return EditJob.objects.create(
            pipedrive_deal_id=pipedrive_deal_id,
            pipedrive_activity_id=pipedrive_activity_id,
            title=title,
            video_type=video_type,
            due_datetime=due_datetime,
            duration_minutes=duration_minutes,
            notes=notes,
            status="failed",
            selection_reason=describe_editor_selection(video_type, None),
        )

    job = EditJob.objects.create(
        pipedrive_deal_id=pipedrive_deal_id,
        pipedrive_activity_id=pipedrive_activity_id,
        title=title,
        video_type=video_type,
        due_datetime=due_datetime,
        duration_minutes=duration_minutes,
        notes=notes,
        status="created",
        assigned_editor=selected.editor,
        active_job_count_at_assignment=selected.active_jobs,
        selection_reason=describe_editor_selection(video_type, selected),
    )
    _create_clickup_task(job)
    logger.info(
        "Edit job %s assigned to %s (rank=%s active_jobs=%s clickup_task=%s)",
        job.id, selected.editor.name, selected.rank, selected.active_jobs, job.clickup_task_id or "-",
    )
    return job


def _create_clickup_task(job: EditJob) -> None:
    try:
        result = get_clickup_client().create_edit_task(job)
    except Exception as exc:
        logger.exception("Failed to create ClickUp task for edit job %s", job.id)
        job.status = "clickup_failed"
        job.clickup_error = str(exc)
        job.save(update_fields=["status", "clickup_error", "updated_at"])
        return

    job.clickup_task_id = result.task_id
    job.clickup_error = ""
    job.clickup_synced_at = timezone.now()
    job.save(update_fields=["clickup_task_id", "clickup_error", "clickup_synced_at", "updated_at"])


def handle_updated_edit_job(
    *,
    pipedrive_deal_id: str | None,
    pipedrive_activity_id: str | None = None,
    title: str,
    video_type: str = "",
    due_datetime: datetime = None,
    duration_minutes: int = 0,
    notes: str = "",
) -> tuple[EditJob, str]:
    existing = None
    if pipedrive_activity_id:
        existing = EditJob.objects.filter(pipedrive_activity_id=pipedrive_activity_id).first()
    if not existing:
        return handle_new_edit_job(
            pipedrive_deal_id=pipedrive_deal_id,
            pipedrive_activity_id=pipedrive_activity_id,
            title=title,
            video_type=video_type,
            due_datetime=due_datetime,
            duration_minutes=duration_minutes,
            notes=notes,
        ), "created"

    existing.pipedrive_deal_id = pipedrive_deal_id or existing.pipedrive_deal_id
    existing.title = title or existing.title
    existing.video_type = video_type or existing.video_type
    existing.due_datetime = due_datetime
    existing.duration_minutes = duration_minutes or existing.duration_minutes
    existing.notes = notes
    if existing.assigned_editor and not existing.clickup_task_id and existing.status != "clickup_failed":
        _create_clickup_task(existing)
    if existing.status == "created":
        existing.status = "updated"
    existing.save()
    return existing, "updated"


def handle_deleted_edit_job(pipedrive_activity_id: str | None = None, pipedrive_deal_id: str | None = None) -> EditJob | None:
    job = None
    if pipedrive_activity_id:
        job = EditJob.objects.filter(pipedrive_activity_id=pipedrive_activity_id).first()
    if not job and pipedrive_deal_id:
        job = EditJob.objects.filter(pipedrive_deal_id=pipedrive_deal_id).first()
    if not job:
        return None
    job.status = "cancelled"
    job.save(update_fields=["status", "updated_at"])
    logger.info("Edit job %s cancelled from Pipedrive delete/type change", job.id)
    return job
