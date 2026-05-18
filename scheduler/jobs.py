"""
Background job scheduling via APScheduler.

Started by scheduler/apps.py on Django startup.
Jobs are persisted in the DB (via django-apscheduler) so they survive restarts.
"""
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from django_apscheduler.jobstores import DjangoJobStore
from django.utils import timezone
from .models import Invite

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="America/New_York")
        _scheduler.add_jobstore(DjangoJobStore(), "default")
    return _scheduler


POLL_JOB_ID = "poll-pending-invites"
POLL_MINUTES = 5


def start():
    """Called once at Django startup. Idempotent."""
    sched = get_scheduler()
    if sched.running:
        return
    sched.start()
    # Register the recurring poller (idempotent — replace_existing handles re-registration)
    sched.add_job(
        _run_poll,
        trigger="interval",
        minutes=POLL_MINUTES,
        id=POLL_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    logger.info("APScheduler started; polling every %d min", POLL_MINUTES)


def _run_poll():
    """Top-level wrapper so the scheduler can pickle it."""
    from .orchestrator import poll_all_pending_invites
    try:
        poll_all_pending_invites()
    except Exception:
        logger.exception("Poll job failed")


def schedule_escalation_check(invite_id: int) -> None:
    """Schedule check_and_escalate to run when this invite expires."""
    invite = Invite.objects.get(id=invite_id)
    sched = get_scheduler()
    job_id = f"escalate-invite-{invite_id}"
    sched.add_job(
        _run_escalation,
        trigger="date",
        run_date=invite.expires_at,
        args=[invite_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=60 * 60,  # if we miss by up to an hour (e.g., restart), still fire
    )
    logger.info("Scheduled escalation job %s for %s", job_id, invite.expires_at)


def _run_escalation(invite_id: int):
    # Import inside to avoid circular import on startup
    from .orchestrator import check_and_escalate
    check_and_escalate(invite_id)
