"""Client (Pipedrive contact) names for shoots, so the dashboard can show who
each shoot is for without opening Pipedrive. Read-only; every lookup fails soft."""
import logging

from django.utils import timezone

from .models import Shoot

logger = logging.getLogger(__name__)


def client_name_from_activity(activity: dict | None) -> str:
    """Pipedrive v1 activities carry person_name (and deal_title). v2 webhook
    payloads only carry ids, so they return "" here and need a lookup."""
    if not isinstance(activity, dict):
        return ""
    name = activity.get("person_name") or activity.get("deal_title") or ""
    return name.strip() if isinstance(name, str) else ""


def lookup_client_name(activity_id: str | None, *, source=None) -> str:
    """Fetch the activity from Pipedrive and return its contact name, or ""."""
    if not activity_id:
        return ""
    try:
        if source is None:
            from .reminder_integration import get_reminder_source
            source = get_reminder_source()
        return client_name_from_activity(source.get_activity(str(activity_id)))
    except Exception:  # noqa: BLE001 — a missing name must never block a booking
        logger.warning("Could not look up client name for activity %s", activity_id)
        return ""


def apply_client_name(shoot: Shoot, payload_activity: dict | None, *, source=None) -> None:
    """Set shoot.client_name from the webhook payload, falling back to an API lookup."""
    name = client_name_from_activity(payload_activity) or lookup_client_name(
        shoot.pipedrive_activity_id, source=source
    )
    if name and name != shoot.client_name:
        shoot.client_name = name
        Shoot.objects.filter(pk=shoot.pk).update(client_name=name)


def backfill_client_names(*, source=None, include_past: bool = False) -> tuple[int, int]:
    """Fill client_name on shoots that don't have one. Returns (checked, filled)."""
    qs = Shoot.objects.filter(client_name="").exclude(pipedrive_activity_id__isnull=True).exclude(pipedrive_activity_id="")
    if not include_past:
        qs = qs.filter(shoot_datetime__gte=timezone.now())
    if source is None:
        try:
            from .reminder_integration import get_reminder_source
            source = get_reminder_source()
        except Exception:  # noqa: BLE001
            logger.warning("Pipedrive is not configured; skipping client name backfill")
            return 0, 0
    checked = filled = 0
    for shoot in qs.order_by("shoot_datetime"):
        checked += 1
        name = lookup_client_name(shoot.pipedrive_activity_id, source=source)
        if name:
            Shoot.objects.filter(pk=shoot.pk).update(client_name=name)
            filled += 1
    return checked, filled
