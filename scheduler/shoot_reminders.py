"""Independent, opt-in SMS milestone ledger; importing this module sends nothing.

No scheduler, provider client, signals, secrets or production migrations run here.
Call sync after source/assignment changes; the worker also quarantines deleted
sources and stale claims. Phones must already be E.164; ambiguous local numbers
are rejected rather than guessed. Sent/uncertain milestones survive reschedules
and reassignment away and back to the same recipient.

Source reads cannot be atomic with an external SMS system: a change after the
final source read can race delivery. Provider ambiguity and crashed claims are
therefore quarantined, never retried automatically. An operator must reconcile
manual_review against provider records before any manual intervention.
"""
from datetime import timedelta
from zoneinfo import ZoneInfo
import re
import time

from django.db import OperationalError, connection, models, transaction
from django.utils import timezone

from .models import Shoot, ShootReminder

OFFSETS = (48, 24, 3)
PROTECTED = ('sent', 'sending', 'manual_review')


def _now(value):
    value = value or timezone.now()
    if timezone.is_naive(value):
        raise ValueError('An aware datetime is required')
    return value


def _eligible(source):
    if source.status != 'confirmed' or not source.confirmed_videographer_id:
        return False
    person = source.confirmed_videographer
    return person.active and bool(re.fullmatch(r'\+[1-9][0-9]{7,14}', person.phone or ''))


def _matches(row, source):
    return (source is not None and _eligible(source)
            and source.confirmed_videographer_id == row.recipient_id
            and source.shoot_datetime == row.shoot_datetime
            and (source.pipedrive_activity_id or '') == row.source_activity_id)


def sync_shoot_reminders(shoot, now=None):
    """Reconcile from persisted Shoot, never from a possibly stale caller object."""
    now = _now(now)
    with transaction.atomic():
        source = Shoot.objects.select_for_update().filter(pk=shoot.pk).first()
        if source is None or not _eligible(source):
            ShootReminder.objects.filter(source_shoot_id=shoot.pk, status='pending').update(
                status='cancelled', reason='ineligible')
            return {'created': 0}
        ShootReminder.objects.filter(source_shoot_id=source.pk, status='pending').exclude(
            recipient_id=source.confirmed_videographer_id).update(status='cancelled', reason='reassigned')
        created = 0
        for offset in OFFSETS:
            row, new = ShootReminder.objects.get_or_create(
                source_shoot_id=source.pk, recipient_id=source.confirmed_videographer_id,
                offset_hours=offset, defaults=dict(shoot=source,
                    source_activity_id=source.pipedrive_activity_id or '',
                    shoot_datetime=source.shoot_datetime,
                    due_at=source.shoot_datetime - timedelta(hours=offset)))
            created += new
            if row.status not in PROTECTED and (new or row.shoot_datetime != source.shoot_datetime or row.status == 'cancelled'):
                if not new and row.shoot_datetime != source.shoot_datetime:
                    row.schedule_version += 1
                row.shoot_datetime = source.shoot_datetime
                row.due_at = source.shoot_datetime - timedelta(hours=offset)
                row.status = 'skipped' if row.due_at < now else 'pending'
                row.reason = 'elapsed' if row.status == 'skipped' else ''
                row.source_activity_id = source.pipedrive_activity_id or ''
                row.save()
        return {'created': created}


def render_reminder(shoot, offset_hours, *, details_url=None):
    _now(shoot.shoot_datetime)
    start_time = shoot.shoot_datetime.astimezone(ZoneInfo('America/New_York')).strftime('%I:%M %p %Z').lstrip('0')
    if offset_hours == 48:
        clause = f'review shoot details here {details_url} and contact' if details_url else 'contact'
        return (f'Your PuckPro shoot is in 48h. The game is at {shoot.location} and begins at {start_time}. '
                f'Please {clause} Jack or Declan if you have any questions')
    if offset_hours == 24:
        return (f'24h til puck drop, {start_time} at {shoot.location}. Please arrive minimum 15m early, '
                'ensure you’re clear on details, and reach out to Jack or Declan with any questions.')
    if offset_hours == 3:
        return ('Almost game time! Any questions, please address them with Jack or Declan asap. '
                'Remember to check in post game with a quick summary so we can follow up with the client. Thanks!')
    raise ValueError('Unsupported reminder milestone')


def _persist_confirmation(queryset, **values):
    """Retry only idempotent DB evidence writes, never replay a provider call."""
    for attempt in range(6):
        try:
            return queryset.update(**values)
        except OperationalError as exc:
            if (connection.vendor != 'sqlite' or connection.in_atomic_block
                    or 'locked' not in str(exc).lower() or attempt == 5):
                raise
            time.sleep(0.01 * (2 ** attempt))


def run_due_reminders(*, sender, validator, now=None, enabled=False, limit=100, dry_run=False):
    """Claim and process at most limit pending milestones, returning count summary.

    validator(shoot) must return literal True only after authoritative freshness
    verification, and may persist source updates. We reread DB state afterwards.
    False/exception blocks delivery safely and can retry validation, not SMS.
    sender(*, phone, body, idempotency_key) returns the confirmed provider GUID
    string, not an HTTP response or an unverified acknowledgement. Every sender
    exception/missing GUID is ambiguous and permanently requires manual review.
    Claims commit before SMS network I/O (freshness validation precedes claiming).
    Do not call inside atomic/ATOMIC_REQUESTS.
    enabled must be literal True. Disabled/dry-run performs no writes or callbacks.
    now is an aware fixed clock for deterministic tests; production omits it.
    Pending milestones expire at the next milestone (3h expires at game start).
    Stale sending (15m) never re-enters pending. Summary counts are per invocation,
    except due which is the initial backlog before cancellation/limit filtering.
    """
    fixed_clock = now is not None
    now = _now(now)
    if type(limit) is not int or limit < 0:
        raise ValueError('limit must be a nonnegative integer')
    result = dict(due=0, claimed=0, sent=0, cancelled=0, skipped=0, manual_review=0,
                  blocked=0, enabled=enabled is True, dry_run=bool(dry_run))
    due = ShootReminder.objects.filter(status='pending', due_at__lte=now).order_by('due_at', 'pk')
    result['due'] = due.count()
    if enabled is not True or dry_run:
        return result
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError('Reminder worker requires autocommit outside any transaction')
    stale = ShootReminder.objects.filter(status='sending').filter(
        models.Q(claimed_at__lte=now - timedelta(minutes=15)) | models.Q(claimed_at__isnull=True))
    orphaned = ShootReminder.objects.filter(status='pending', shoot__isnull=True)
    # Avoid empty UPDATEs: even a no-op takes SQLite's database-wide writer lock.
    try:
        if stale.exists():
            result['manual_review'] += stale.update(status='manual_review', reason='stale_claim')
        if orphaned.exists():
            result['cancelled'] += orphaned.update(status='cancelled', reason='source_deleted')
    except OperationalError:
        result['blocked'] += 1
        return result
    for pk in list(due.values_list('pk', flat=True)[:limit]):
        try:
            row = ShootReminder.objects.get(pk=pk)
            if row.status != 'pending' or row.due_at > now:
                continue
            source = Shoot.objects.get(pk=row.source_shoot_id)
        except ShootReminder.DoesNotExist:
            continue
        except Shoot.DoesNotExist:
            result['cancelled'] += ShootReminder.objects.filter(pk=pk, status='pending').update(
                status='cancelled', reason='source_deleted')
            continue
        try:
            valid = validator(source) is True
        except Exception:
            valid = False
        source = Shoot.objects.filter(pk=row.source_shoot_id).first()
        if not _matches(row, source):
            result['cancelled'] += ShootReminder.objects.filter(pk=pk, status='pending').update(
                status='cancelled', reason='source_changed')
            if source is not None:
                sync_shoot_reminders(source, now=now)
            continue
        if not valid:
            ShootReminder.objects.filter(pk=pk, status='pending').update(reason='freshness_unavailable')
            result['blocked'] += 1
            continue
        try:
            with transaction.atomic():
                # Lock in the same source->ledger order as reconciliation.
                source = Shoot.objects.select_for_update().filter(pk=row.source_shoot_id).first()
                current = ShootReminder.objects.select_for_update().get(pk=pk)
                if (not _matches(row, source) or current.status != 'pending'
                        or current.due_at > now or current.schedule_version != row.schedule_version):
                    continue
                claimed = ShootReminder.objects.filter(pk=pk, status='pending', due_at__lte=now,
                                                        schedule_version=row.schedule_version).update(
                    status='sending', claimed_at=now)
        except (OperationalError, ShootReminder.DoesNotExist):
            # SQLite has no row locks. Defer a writer race after full rollback.
            result['blocked'] += 1
            continue
        if not claimed:
            continue
        result['claimed'] += 1
        # Fresh DB read after the claim as well; no cached source/recipient.
        source = Shoot.objects.filter(pk=row.source_shoot_id).first()
        if not _matches(row, source):
            result['cancelled'] += ShootReminder.objects.filter(pk=pk, status='sending').update(
                status='cancelled', reason='source_changed')
            if source is not None:
                sync_shoot_reminders(source, now=now)
            continue
        send_now = now if fixed_clock else timezone.now()
        remaining = {48: 24, 24: 3, 3: 0}[row.offset_hours]
        if source.shoot_datetime - timedelta(hours=remaining) <= send_now:
            ShootReminder.objects.filter(pk=pk, status='sending').update(status='skipped', reason='obsolete')
            result['skipped'] += 1
            continue
        if not ShootReminder.objects.filter(pk=pk, status='sending', claimed_at=now).exists():
            result['blocked'] += 1
            continue
        try:
            guid = sender(phone=source.confirmed_videographer.phone,
                          body=render_reminder(source, row.offset_hours),
                          idempotency_key=f'shoot-reminder-{row.pk}')
            if not isinstance(guid, str) or not guid.strip() or len(guid) > 255:
                raise ValueError('Unconfirmed delivery')
        except Exception:
            ShootReminder.objects.filter(pk=pk, status='sending').update(
                status='manual_review', reason='provider_ambiguous')
            result['manual_review'] += 1
            continue
        delivered = _persist_confirmation(
            ShootReminder.objects.filter(pk=pk, status='sending', claimed_at=now),
            status='sent', sent_at=now, provider_guid=guid, reason='')
        result['sent'] += delivered
        if not delivered:
            # A stale-claim sweep may have quarantined an in-flight request.
            # Keep its confirmed evidence without automatically clearing review.
            ShootReminder.objects.filter(pk=pk, status='manual_review', claimed_at=now).update(provider_guid=guid)
    return result
