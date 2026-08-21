"""Compose + send notifications about shoot outcomes."""
import logging
from django.conf import settings
from .models import Shoot
from .email_client import send_email

logger = logging.getLogger(__name__)


def booking_needed(shoot: Shoot, reason: str) -> bool:
    """Tell the scheduler owner that a shoot needs a manual booking action."""
    to = settings.NOTIFY_EMAIL or settings.GOOGLE_CALENDAR_OWNER_EMAIL
    if not to:
        logger.warning("No NOTIFY_EMAIL or owner email configured; skipping booking notification")
        return False
    import os
    base = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    dashboard_url = f"https://{base}/shoots/{shoot.id}/" if base else f"http://127.0.0.1:8000/shoots/{shoot.id}/"
    body = (
        f"{reason}\n\n"
        f"Shoot: {shoot.title or 'Untitled'}\n"
        f"When: {shoot.shoot_datetime:%A, %B %-d at %-I:%M %p}\n"
        f"Location: {shoot.location}\n\n"
        f"Open the shoot: {dashboard_url}\n"
    )
    return send_email(to, f"[Shoot Scheduler] Booking action needed: {shoot.title or shoot.location}", body)


def shoot_failed(shoot: Shoot) -> bool:
    """Sent when every videographer has declined / expired and there's no one left."""
    to = settings.NOTIFY_EMAIL or settings.GOOGLE_CALENDAR_OWNER_EMAIL
    if not to:
        logger.warning("No NOTIFY_EMAIL or owner email configured; skipping failure email")
        return False

    invites = shoot.invites.all().order_by("rank")
    chain = "\n".join(
        f"  #{i.rank + 1}: {i.videographer.name:25s}  {i.status}"
        + (f"  (responded {i.responded_at:%b %-d %-I:%M %p})" if i.responded_at else "")
        for i in invites
    ) or "  (no invites were sent)"

    import os
    base = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    dashboard_url = f"https://{base}/shoots/{shoot.id}/" if base else f"http://127.0.0.1:8000/shoots/{shoot.id}/"

    body = (
        f"Heads up: nobody accepted the shoot below. You'll need to find a videographer manually.\n\n"
        f"  Shoot:    {shoot.title or 'Untitled'}\n"
        f"  When:     {shoot.shoot_datetime:%A, %B %-d at %-I:%M %p}\n"
        f"  Location: {shoot.location}\n"
        f"  Pipedrive activity: {shoot.pipedrive_activity_id or 'n/a'}\n\n"
        f"Invite chain results:\n{chain}\n\n"
        f"Dashboard: {dashboard_url}\n"
    )
    subject = f"[Shoot Scheduler] FAILED — manual booking needed: {shoot.title or shoot.location}"
    return send_email(to, subject, body)
