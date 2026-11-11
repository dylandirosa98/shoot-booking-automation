from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import Mock

from django.test import TransactionTestCase

from .models import Shoot, Videographer


class ShootReminderTests(TransactionTestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 23, 12, tzinfo=dt_timezone.utc)
        self.person = Videographer.objects.create(name='Camera', email='a@example.test',
            phone='+16175550123', state='MA', rating=5)
        self.shoot = Shoot.objects.create(location='Rink', status='confirmed',
            confirmed_videographer=self.person, pipedrive_activity_id='activity-1',
            shoot_datetime=self.now + timedelta(hours=60))

    def sync(self):
        from .shoot_reminders import sync_shoot_reminders
        return sync_shoot_reminders(self.shoot, now=self.now)

    def rows(self):
        from .models import ShootReminder
        return ShootReminder.objects.order_by('-offset_hours')

    def run_worker(self, **kwargs):
        from .shoot_reminders import run_due_reminders
        return run_due_reminders(sender=kwargs.pop('sender', Mock(return_value='guid-1')),
            validator=kwargs.pop('validator', lambda shoot: True),
            now=kwargs.pop('now', self.now + timedelta(hours=12)), **kwargs)

    def test_confirmation_db_lock_retries_only_database_not_provider(self):
        from unittest.mock import patch
        from django.db import OperationalError
        from django.db.models.query import QuerySet
        self.sync()
        original = QuerySet.update
        locked = False
        def update(queryset, **kwargs):
            nonlocal locked
            if kwargs.get('status') == 'sent' and not locked:
                locked = True
                raise OperationalError('database table is locked')
            return original(queryset, **kwargs)
        sender = Mock(return_value='guid-after-lock')
        with patch.object(QuerySet, 'update', update):
            result = self.run_worker(enabled=True, sender=sender)
        self.assertEqual(result['sent'], 1)
        sender.assert_called_once()

    def test_validation_and_resync_precede_claim(self):
        self.sync()
        def validator(shoot):
            self.assertEqual(self.rows().get(offset_hours=48).status, 'pending')
            return True
        result = self.run_worker(enabled=True, validator=validator)
        self.assertEqual(result['sent'], 1)

    def test_live_clock_advances_during_validation(self):
        from unittest.mock import patch
        from .shoot_reminders import run_due_reminders
        self.sync()
        sender = Mock()
        with patch('scheduler.shoot_reminders.timezone.now', side_effect=[
                self.now + timedelta(hours=12), self.now + timedelta(hours=61)]):
            result = run_due_reminders(enabled=True, sender=sender, validator=lambda shoot: True)
        self.assertEqual(result['skipped'], 1)
        sender.assert_not_called()

    def test_provider_confirmation_after_stale_recovery_stays_manual_but_keeps_guid(self):
        self.sync()
        def sender(**kwargs):
            self.run_worker(enabled=True, now=self.now + timedelta(hours=13))
            return 'confirmed-but-late'
        result = self.run_worker(enabled=True, sender=sender)
        row = self.rows().get(offset_hours=48)
        self.assertEqual(row.status, 'manual_review')
        self.assertEqual(row.provider_guid, 'confirmed-but-late')
        self.assertEqual(result['sent'], 0)

    def test_concurrent_sqlite_claim_contention_is_safe(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, local
        from unittest.mock import patch
        from django.db import connections
        from django.db.models.query import QuerySet
        from .models import ShootReminder
        self.sync()
        barrier, state = Barrier(2), local()
        original = QuerySet.get
        sender = Mock(return_value='one-guid')
        def racing_get(queryset, *args, **kwargs):
            row = original(queryset, *args, **kwargs)
            if queryset.model is ShootReminder and not getattr(state, 'waited', False):
                state.waited = True
                barrier.wait(timeout=5)
            return row
        def worker():
            try:
                return self.run_worker(enabled=True, sender=sender)
            finally:
                connections.close_all()
        with patch.object(QuerySet, 'get', racing_get), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)
        self.run_worker(enabled=True, sender=sender)
        sender.assert_called_once()
        self.assertEqual(self.rows().get(offset_hours=48).status, 'sent')

    def test_live_gate_requires_boolean_true_and_limit_is_validated(self):
        self.sync()
        sender = Mock()
        for gate in ['false', 'true', 1, None]:
            self.run_worker(enabled=gate, sender=sender)
        sender.assert_not_called()
        for limit in [-1, 1.5, True]:
            with self.assertRaises(ValueError):
                self.run_worker(enabled=True, limit=limit)

    def test_validator_cancellation_false_result_cancels_not_retries(self):
        self.sync()
        def validator(shoot):
            Shoot.objects.filter(pk=shoot.pk).update(status='cancelled')
            return False
        result = self.run_worker(enabled=True, validator=validator)
        self.assertEqual(result['cancelled'], 1)
        self.assertEqual(self.rows().filter(status='pending').count(), 0)

    def test_source_deleted_between_claim_and_read_is_cancelled(self):
        from unittest.mock import patch
        self.sync()
        sender = Mock()
        with patch.object(Shoot.objects, 'get', side_effect=Shoot.DoesNotExist):
            self.run_worker(enabled=True, sender=sender)
        sender.assert_not_called()
        self.assertEqual(self.rows().get(offset_hours=48).status, 'cancelled')

    def test_same_recipient_time_change_during_validator_requeues_new_schedule(self):
        self.sync()
        def validator(shoot):
            Shoot.objects.filter(pk=shoot.pk).update(shoot_datetime=shoot.shoot_datetime + timedelta(days=1))
            return True
        sender = Mock()
        self.run_worker(enabled=True, sender=sender, validator=validator)
        sender.assert_not_called()
        self.shoot.refresh_from_db()
        row = self.rows().get(offset_hours=48)
        self.assertEqual(row.status, 'pending')
        self.assertEqual(row.due_at, self.shoot.shoot_datetime - timedelta(hours=48))
        self.assertEqual(row.schedule_version, 2)

    def test_candidate_rescheduled_before_claim_is_not_sent_early(self):
        from django.db.models.query import QuerySet
        from unittest.mock import patch
        from .models import ShootReminder
        self.sync()
        original = QuerySet.get
        changed = False
        def racing_get(queryset, *args, **kwargs):
            nonlocal changed
            if queryset.model is ShootReminder and not changed:
                changed = True
                self.shoot.shoot_datetime += timedelta(days=2)
                self.shoot.save()
                self.sync()
            return original(queryset, *args, **kwargs)
        sender = Mock(return_value='never')
        with patch.object(QuerySet, 'get', racing_get):
            self.run_worker(enabled=True, sender=sender)
        sender.assert_not_called()
        self.assertEqual(self.rows().get(offset_hours=48).status, 'pending')

    def test_quarantined_candidate_cannot_send_when_validator_returns(self):
        self.sync()
        sender = Mock(return_value='never')
        def validator(shoot):
            self.rows().filter(offset_hours=48).update(status='manual_review')
            return True
        self.run_worker(enabled=True, sender=sender, validator=validator)
        sender.assert_not_called()
        self.assertEqual(self.rows().get(offset_hours=48).status, 'manual_review')

    def test_worker_refuses_outer_transaction(self):
        from django.db import transaction
        self.sync()
        with transaction.atomic(), self.assertRaises(RuntimeError):
            self.run_worker(enabled=True)
        self.assertEqual(self.rows().get(offset_hours=48).status, 'pending')

    def test_worker_skips_obsolete_milestones_after_outage(self):
        self.sync()
        sender = Mock(return_value='only-3h')
        result = self.run_worker(enabled=True, sender=sender, now=self.now + timedelta(hours=58))
        self.assertEqual(result['skipped'], 2)
        self.assertEqual(result['sent'], 1)
        sender.assert_called_once()
        self.assertTrue(sender.call_args.kwargs['body'].startswith('Almost game time!'))

    def test_exact_templates_and_dst(self):
        from .shoot_reminders import render_reminder
        self.shoot.shoot_datetime = datetime(2026, 11, 1, 6, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(render_reminder(self.shoot, 24),
            '24h til puck drop, 1:30 AM EST at Rink. Please arrive minimum 15m early, ensure you’re clear on details, and reach out to Jack or Declan with any questions.')
        self.assertEqual(render_reminder(self.shoot, 3),
            'Almost game time! Any questions, please address them with Jack or Declan asap. Remember to check in post game with a quick summary so we can follow up with the client. Thanks!')
        self.assertEqual(render_reminder(self.shoot, 48, details_url='https://example.test/shoot/1'),
            'Your PuckPro shoot is in 48h. The game is at Rink and begins at 1:30 AM EST. Please review shoot details here https://example.test/shoot/1 and contact Jack or Declan if you have any questions')
        self.shoot.save()
        self.sync()
        for row in self.rows():
            self.assertEqual(row.shoot_datetime - row.due_at, timedelta(hours=row.offset_hours))

    def test_racing_worker_does_not_claim_same_milestone(self):
        self.sync()
        nested_sender = Mock(return_value='nested-guid')
        def validate(shoot):
            nested = self.run_worker(enabled=True, sender=nested_sender)
            self.assertEqual(nested['claimed'], 1)
            return True
        sender = Mock(return_value='one-guid')
        self.run_worker(enabled=True, sender=sender, validator=validate)
        sender.assert_not_called()
        nested_sender.assert_called_once()

    def test_restart_after_crash_requires_manual_review(self):
        self.sync()
        sender = Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_worker(enabled=True, sender=sender)
        self.assertEqual(self.rows().get(offset_hours=48).status, 'sending')
        result = self.run_worker(enabled=True, sender=sender, now=self.now + timedelta(hours=13))
        self.assertEqual(result['manual_review'], 1)
        self.assertEqual(self.rows().get(offset_hours=48).status, 'manual_review')
        sender.assert_called_once()

    def test_unconfirmed_provider_response_requires_review(self):
        self.sync()
        sender = Mock(return_value=None)
        result = self.run_worker(enabled=True, sender=sender)
        self.assertEqual(result['manual_review'], 1)
        self.assertEqual(self.rows().get(offset_hours=48).provider_guid, '')
        self.run_worker(enabled=True, sender=sender)
        sender.assert_called_once()

    def test_ambiguous_provider_failure_never_retries(self):
        self.sync()
        sender = Mock(side_effect=TimeoutError('private token and body'))
        result = self.run_worker(enabled=True, sender=sender)
        self.assertEqual(result['manual_review'], 1)
        row = self.rows().get(offset_hours=48)
        self.assertEqual(row.status, 'manual_review')
        self.assertEqual(row.provider_guid, '')
        self.assertNotIn('private', row.reason)
        self.run_worker(enabled=True, sender=sender)
        self.shoot.shoot_datetime += timedelta(days=2)
        self.shoot.save()
        self.sync()
        self.assertEqual(self.rows().get(offset_hours=48).status, 'manual_review')
        sender.assert_called_once()

    def test_deleted_source_preserves_audit_and_never_sends(self):
        self.sync()
        source_id = self.shoot.pk
        self.shoot.delete()
        sender, validator = Mock(), Mock(return_value=True)
        result = self.run_worker(enabled=True, sender=sender, validator=validator)
        sender.assert_not_called()
        validator.assert_not_called()
        self.assertEqual(self.rows().count(), 3)
        self.assertEqual(self.rows().filter(source_shoot_id=source_id, status='cancelled').count(), 3)

    def test_validator_recipient_and_time_changes_cancel_old_job(self):
        self.sync()
        sender = Mock(return_value='never')
        other = Videographer.objects.create(name='Other', email='b@example.test',
            phone='+16175550124', state='MA', rating=4)
        def validate(shoot):
            Shoot.objects.filter(pk=shoot.pk).update(confirmed_videographer=other,
                shoot_datetime=shoot.shoot_datetime + timedelta(days=1))
            return True
        self.run_worker(enabled=True, sender=sender, validator=validate)
        sender.assert_not_called()
        self.assertEqual(self.rows().get(offset_hours=48, recipient_id=self.person.pk).status, 'cancelled')

    def test_source_rechecked_after_validator_changes(self):
        self.sync()
        sender = Mock(return_value='never')
        def validate(shoot):
            Shoot.objects.filter(pk=shoot.pk).update(status='cancelled')
            return True
        result = self.run_worker(enabled=True, sender=sender, validator=validate)
        sender.assert_not_called()
        self.assertEqual(result['cancelled'], 1)
        self.assertEqual(self.rows().get(offset_hours=48).status, 'cancelled')

    def test_validator_failure_fails_closed(self):
        self.sync()
        sender = Mock()
        for validator in [lambda shoot: False, lambda shoot: None, Mock(side_effect=TimeoutError('secret'))]:
            result = self.run_worker(enabled=True, sender=sender, validator=validator)
            self.assertEqual(result['sent'], 0)
            self.assertEqual(self.rows().get(offset_hours=48).status, 'pending')
        sender.assert_not_called()

    def test_confirmed_send_claims_committed_before_network_and_dedupes(self):
        from django.db import connection
        self.sync()
        def send(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(self.rows().get(offset_hours=48).status, 'sending')
            self.assertEqual(kwargs['phone'], self.person.phone)
            self.assertEqual(kwargs['body'], 'Your PuckPro shoot is in 48h. The game is at Rink and begins at 8:00 PM EDT. Please contact Jack or Declan if you have any questions')
            self.assertTrue(kwargs['idempotency_key'])
            return 'provider-123'
        sender = Mock(side_effect=send)
        result = self.run_worker(sender=sender, enabled=True)
        self.assertEqual(result['sent'], 1)
        row = self.rows().get(offset_hours=48)
        self.assertEqual(row.status, 'sent')
        self.assertEqual(row.provider_guid, 'provider-123')
        self.run_worker(sender=sender, enabled=True)
        sender.assert_called_once()

    def test_disabled_and_dry_run_are_side_effect_free(self):
        self.sync()
        before = list(self.rows().values())
        sender, validator = Mock(), Mock()
        result = self.run_worker(sender=sender, validator=validator)
        self.assertEqual(result['sent'], 0)
        result = self.run_worker(sender=sender, validator=validator, enabled=True, dry_run=True)
        self.assertEqual(result['due'], 1)
        self.assertEqual(before, list(self.rows().values()))
        sender.assert_not_called()
        validator.assert_not_called()

    def test_late_booking_skips_elapsed_then_reschedule_rearms_unsent(self):
        self.shoot.shoot_datetime = self.now + timedelta(hours=20)
        self.shoot.save()
        self.sync()
        self.assertEqual(list(self.rows().values_list('status', flat=True)), ['skipped', 'skipped', 'pending'])
        self.shoot.shoot_datetime += timedelta(days=3)
        self.shoot.save()
        self.sync()
        self.assertEqual(self.rows().filter(status='pending').count(), 3)

    def test_ineligible_sources_cancel_or_create_nothing(self):
        for field, value in [('status', 'cancelled'), ('status', 'pending'), ('status', 'failed'),
                             ('confirmed_videographer', None)]:
            with self.subTest(field=field, value=value):
                self.shoot.status = 'confirmed'
                self.shoot.confirmed_videographer = self.person
                setattr(self.shoot, field, value)
                self.shoot.save()
                self.sync()
                self.assertEqual(self.rows().filter(status='pending').count(), 0)
        self.shoot.status = 'confirmed'
        self.shoot.confirmed_videographer = self.person
        self.shoot.save()
        self.sync()
        self.person.active = False
        self.person.save()
        self.sync()
        self.assertEqual(self.rows().filter(status='pending').count(), 0)
        self.person.active = True
        for phone in ['', 'garbage', '12345']:
            self.person.phone = phone
            self.person.save()
            self.sync()
            self.assertEqual(self.rows().filter(status='pending').count(), 0)

    def test_reassignment_cancels_old_recipient(self):
        self.sync()
        other = Videographer.objects.create(name='Other', email='b@example.test',
            phone='+16175550124', state='MA', rating=4)
        self.shoot.confirmed_videographer = other
        self.shoot.save()
        self.sync()
        self.assertEqual(self.rows().filter(recipient_id=self.person.pk, status='cancelled').count(), 3)
        self.assertEqual(self.rows().filter(recipient_id=other.pk, status='pending').count(), 3)

    def test_reschedule_preserves_sent_milestone(self):
        self.sync()
        row = self.rows().get(offset_hours=48)
        row.status = 'sent'
        row.save()
        self.shoot.shoot_datetime += timedelta(days=1)
        self.shoot.save()
        self.sync()
        self.assertEqual(self.rows().get(offset_hours=48).status, 'sent')
        row = self.rows().get(offset_hours=24)
        self.assertEqual(row.due_at, self.shoot.shoot_datetime - timedelta(hours=24))
        self.assertEqual(row.schedule_version, 2)

    def test_create_idempotent_source_ledger(self):
        self.sync()
        self.sync()
        self.assertEqual(self.rows().count(), 3)
        self.assertEqual(list(self.rows().values_list('offset_hours', flat=True)), [48,24,3])
        for row in self.rows():
            self.assertEqual(row.source_shoot_id, self.shoot.pk)
            self.assertEqual(row.source_activity_id, 'activity-1')
            self.assertEqual(row.due_at, self.shoot.shoot_datetime - timedelta(hours=row.offset_hours))
            self.assertEqual(row.status, 'pending')
