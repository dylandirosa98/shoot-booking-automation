"""Read-only CRM reconciliation for reminders; never sends invites or writes CRM."""
from datetime import datetime, timedelta, timezone as dt_timezone
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .models import Shoot


def get_reminder_source():
    from .reminder_adapters import PipedriveReminderSource
    token = getattr(settings, 'SHOOT_REMINDERS_PIPEDRIVE_API_TOKEN', '') or settings.PIPEDRIVE_API_TOKEN
    domain = (settings.PIPEDRIVE_COMPANY_DOMAIN or '').removeprefix('https://').removeprefix('http://').split('.')[0]
    base_url = f'https://{domain}.pipedrive.com/api' if domain else 'https://api.pipedrive.com/api'
    return PipedriveReminderSource(base_url=base_url, api_token=token)


def canonical_activity_payload(payload, *, source=None):
    """Webhook is a wake-up signal, not authority. Resolve exact activity ID.

    A delayed delete event must not cancel an activity that currently exists.
    Never fall back to deal ID: one deal may have several shoot activities.
    """
    raw = payload.get('data') or payload.get('current') or payload.get('previous') or {}
    activity_id = raw.get('id') or (payload.get('meta') or {}).get('entity_id') or (payload.get('meta') or {}).get('id')
    if not str(activity_id or '').isdigit():
        raise ValueError('Webhook needs an exact numeric activity ID')
    source = source or get_reminder_source()
    current = source.get_activity(str(activity_id))
    if current is not None and str(current.get('id')) != str(activity_id):
        raise ValueError('CRM activity identity mismatch')
    return {'meta': {'entity':'activity', 'action':'change' if current is not None else 'delete'},
            'data':current if current is not None else {'id':int(activity_id)}}


def _activity_fields(activity):
    if not activity or activity.get('done') in (True, 1, '1', 'true'):
        return None
    if str(activity.get('type') or '').strip().lower().replace(' ', '_') != 'shoot_booking':
        return None
    time = activity.get('due_time')
    if isinstance(time, dict):
        time = time.get('value')
    # Missing time is not an instruction to send midnight reminders. A real
    # 00:00 UTC, however, is valid (often 8 PM Eastern on the previous day).
    if not time or not activity.get('due_date'):
        return None
    try:
        start = datetime.fromisoformat(f"{activity['due_date']}T{time}").replace(tzinfo=dt_timezone.utc)
    except (TypeError, ValueError):
        return None
    location = activity.get('location') or ''
    if isinstance(location, dict):
        location = location.get('value') or location.get('formatted_address') or ''
    if not isinstance(location, str) or not location.strip():
        return None
    return {'shoot_datetime': start, 'location': location.strip(),
            'title': activity.get('subject') or '',
            'notes': activity.get('public_description') or activity.get('note') or activity.get('description') or ''}


def refresh_shoot(shoot, *, source):
    """Fetch authoritative activity before each send. Errors propagate, fail closed.

    Assignments are owned by the scheduler, never guessed from CRM owner/person.
    Cancellation affects reminder eligibility, not calendar/invite side effects.
    """
    if not shoot.pipedrive_activity_id:
        return False
    activity = source.get_activity(shoot.pipedrive_activity_id)
    return apply_activity_to_shoot(shoot, activity)


def apply_activity_to_shoot(shoot, activity):
    if activity and str(activity.get('id')) != str(shoot.pipedrive_activity_id):
        raise ValueError('CRM activity identity mismatch')
    fields = _activity_fields(activity)
    if fields is not None:
        # Manual Calendar-invite notes are owned by the scheduler. A freshness
        # read must not erase or replace them with a CRM activity's note.
        fields.pop('notes', None)
    with transaction.atomic():
        current = Shoot.objects.select_for_update().filter(pk=shoot.pk).first()
        if current is None:
            return False
        if fields is None:
            current.status = 'cancelled'
            current.save(update_fields=['status', 'updated_at'])
            return False
        for key, value in fields.items():
            setattr(current, key, value)
        current.save(update_fields=[*fields, 'updated_at'])
    shoot.refresh_from_db()
    return True


def get_reminder_sender():
    from .reminder_adapters import BlueBubblesReminderSender
    return BlueBubblesReminderSender(
        settings.SHOOT_REMINDERS_BLUEBUBBLES_URL,
        settings.SHOOT_REMINDERS_BLUEBUBBLES_PASSWORD,
        expected_sender_number=settings.SHOOT_REMINDERS_EXPECTED_SENDER_NUMBER,
        verified_sender_number=settings.SHOOT_REMINDERS_VERIFIED_SENDER_NUMBER,
        sender_verified=settings.SHOOT_REMINDERS_SENDER_VERIFIED,
    )


def run_reminder_cycle(*, reconcile=False, dry_run=False, now=None):
    """One bounded worker tick, with independent feature and live-send switches."""
    if not getattr(settings, 'SHOOT_REMINDERS_ENABLED', False):
        return {'status':'disabled'}
    from .shoot_reminders import run_due_reminders
    now = now or timezone.now()
    source = get_reminder_source()
    result: dict = {'status':'dry-run' if dry_run else 'ok'}
    if reconcile and not dry_run:
        result['reconciliation'] = reconcile_reminders(source=source, now=now)
    enabled = getattr(settings, 'SHOOT_REMINDERS_SEND_ENABLED', False) and not dry_run
    adapter = get_reminder_sender() if enabled else None
    def send(*, phone, body, idempotency_key):
        if adapter is None:
            raise RuntimeError('Live reminder sender is disabled')
        return adapter.send(phone, body, idempotency_key)
    result['worker'] = run_due_reminders(
        sender=send if enabled else None, validator=lambda shoot: refresh_shoot(shoot, source=source),
        now=now, enabled=enabled, dry_run=dry_run,
    )
    return result


def reconcile_reminders(*, source=None, now=None):
    """Repair missed changes/deletes plus surface missed creates without auto-invites."""
    from .shoot_reminders import sync_shoot_reminders
    source = source or get_reminder_source()
    now = now or timezone.now()
    result = {'checked': 0, 'discovered': 0, 'cancelled': 0, 'errors': 0}
    for shoot in Shoot.objects.filter(shoot_datetime__gte=now-timedelta(hours=1)).exclude(pipedrive_activity_id__isnull=True).exclude(pipedrive_activity_id='').iterator():
        try:
            valid = refresh_shoot(shoot, source=source)
            shoot.refresh_from_db()
            sync_shoot_reminders(shoot, now=now)
            result['checked'] += 1
            result['cancelled'] += int(not valid)
        except Exception:
            # Counts only: never leak tokens, contact names or message text.
            result['errors'] += 1
    activities = source.list_activities(start_date=(now-timedelta(days=1)).date().isoformat(), end_date=(now+timedelta(days=30)).date().isoformat())
    for activity in activities:
        fields = _activity_fields(activity)
        activity_id = str(activity.get('id') or '')
        if fields is None or not activity_id.isdigit() or fields['shoot_datetime'] <= now:
            continue
        existing = Shoot.objects.filter(pipedrive_activity_id=activity_id).first()
        if existing:
            # An old local time might lie outside the initial reconciliation
            # window after a missed move-to-future webhook. Refresh it too.
            apply_activity_to_shoot(existing, activity)
            existing.refresh_from_db()
            sync_shoot_reminders(existing, now=now)
            continue
        # Discovery is intentionally pending: a CRM activity does not identify
        # a confirmed videographer. Do not send invites or infer an assignee.
        _, created = Shoot.objects.get_or_create(pipedrive_activity_id=activity_id, defaults={**fields, 'pipedrive_deal_id':str(activity.get('deal_id') or '') or None, 'status':'pending'})
        result['discovered'] += int(created)
    return result
