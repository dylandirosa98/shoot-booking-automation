"""Reminder integration tests use a test database and synthetic activities only."""
from datetime import timedelta
from unittest.mock import Mock, patch
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from scheduler.models import Shoot, Videographer


@override_settings(SHOOT_REMINDERS_ENABLED=True)
class ReminderIntegrationTests(TestCase):
    def setUp(self):
        self.person = Videographer.objects.create(name='Synthetic Camera', email='camera@example.invalid', phone='+15555550101', state='NJ', rating=4)
        self.shoot = Shoot.objects.create(pipedrive_activity_id='123', location='Synthetic rink', shoot_datetime=timezone.now()+timedelta(days=4), status='confirmed', confirmed_videographer=self.person)

    def test_webhook_uses_current_record_not_stale_payload(self):
        from scheduler.reminder_integration import canonical_activity_payload
        current = {'id':123, 'subject':'Current'}
        source = type('Source', (), {'get_activity':lambda _, pk: current})()
        result = canonical_activity_payload({'meta':{'action':'delete'},'previous':{'id':123}}, source=source)
        self.assertEqual(result['meta']['action'],'change')
        self.assertEqual(result['data'],current)

    def test_missing_activity_webhook_becomes_delete(self):
        from scheduler.reminder_integration import canonical_activity_payload
        source = type('Source', (), {'get_activity':lambda _, pk: None})()
        result = canonical_activity_payload({'meta':{'action':'change'},'data':{'id':123}},source=source)
        self.assertEqual(result['meta']['action'],'delete')
        self.assertEqual(result['data']['id'],123)

    def test_webhook_without_exact_activity_id_is_rejected(self):
        from scheduler.reminder_integration import canonical_activity_payload
        source = type('Source', (), {'get_activity':lambda _, pk: self.fail('must not query')})()
        with self.assertRaises(ValueError):
            canonical_activity_payload({'meta':{'action':'delete'},'previous':{'deal_id':456}},source=source)

    @override_settings(PIPEDRIVE_WEBHOOK_SECRET='')
    def test_webhook_delete_cancels_confirmed_reminders_source(self):
        with patch('scheduler.reminder_integration.get_reminder_source') as factory, patch('scheduler.views.handle_deleted_shoot', return_value=self.shoot):
            factory.return_value.get_activity.return_value = None
            response = self.client.post('/webhook/pipedrive/', {'meta':{'action':'delete'},'data':{'id':123}}, content_type='application/json')
        self.assertEqual(response.status_code,200)
        self.shoot.refresh_from_db()
        self.assertEqual(self.shoot.status,'cancelled')

    @override_settings(PIPEDRIVE_WEBHOOK_SECRET='')
    def test_webhook_crm_outage_retries_without_using_payload(self):
        with patch('scheduler.reminder_integration.get_reminder_source') as factory:
            factory.return_value.get_activity.side_effect = RuntimeError('offline')
            response = self.client.post('/webhook/pipedrive/', {'meta':{'action':'delete'},'data':{'id':123}}, content_type='application/json')
        self.assertEqual(response.status_code,503)
        self.shoot.refresh_from_db()
        self.assertEqual(self.shoot.status,'confirmed')

    def test_disabled_cycle_does_not_construct_any_network_adapter(self):
        from scheduler.reminder_integration import run_reminder_cycle
        with override_settings(SHOOT_REMINDERS_ENABLED=False), patch('scheduler.reminder_integration.get_reminder_source') as source:
            self.assertEqual(run_reminder_cycle()['status'],'disabled')
            source.assert_not_called()

    def test_assignment_save_schedules_reminders_after_commit(self):
        from scheduler.models import ShootReminder
        with self.captureOnCommitCallbacks(execute=True):
            self.shoot.save()
        self.assertEqual(ShootReminder.objects.filter(source_shoot_id=self.shoot.pk,status='pending').count(),3)

    def test_roster_phone_removed_cancels_pending_after_commit(self):
        from scheduler.models import ShootReminder
        from scheduler.shoot_reminders import sync_shoot_reminders
        sync_shoot_reminders(self.shoot)
        with self.captureOnCommitCallbacks(execute=True):
            self.person.phone = ''
            self.person.save()
        self.assertEqual(ShootReminder.objects.filter(source_shoot_id=self.shoot.pk,status='pending').count(),0)

    def test_local_delete_cancels_ledger_and_preserves_audit(self):
        from scheduler.models import ShootReminder
        from scheduler.shoot_reminders import sync_shoot_reminders
        sync_shoot_reminders(self.shoot)
        pk = self.shoot.pk
        with self.captureOnCommitCallbacks(execute=True):
            self.shoot.delete()
        self.assertEqual(ShootReminder.objects.filter(source_shoot_id=pk,status='cancelled').count(),3)

    def test_refresh_preserves_scheduler_authored_invite_notes(self):
        from scheduler.reminder_integration import refresh_shoot
        self.shoot.notes = 'Operator-authored player and gear details'
        self.shoot.save()
        activity = {'id':123,'type':'shoot_booking','done':False,'due_date':'2028-02-01','due_time':'12:00','location':'Rink','note':'Unrelated CRM note'}
        source = type('Source', (), {'get_activity':lambda _, pk: activity})()
        refresh_shoot(self.shoot,source=source)
        self.shoot.refresh_from_db()
        self.assertEqual(self.shoot.notes,'Operator-authored player and gear details')

    def test_reconciliation_updates_existing_shoot_moved_from_past_to_future(self):
        from scheduler.reminder_integration import reconcile_reminders
        old = timezone.now()-timedelta(days=4)
        self.shoot.shoot_datetime = old
        self.shoot.save()
        future = timezone.now().replace(second=0,microsecond=0)+timedelta(days=4)
        activity = {'id':123,'type':'shoot_booking','done':False,'due_date':future.strftime('%Y-%m-%d'),'due_time':future.strftime('%H:%M'),'location':'Rink'}
        class Source:
            def get_activity(inner,pk):
                return activity
            def list_activities(inner,**kwargs):
                return [activity]
        reconcile_reminders(source=Source())
        self.shoot.refresh_from_db()
        self.assertEqual(self.shoot.shoot_datetime,future)

    def test_status_command_reports_ledger_without_network(self):
        import json
        from io import StringIO
        from django.core.management import call_command
        from scheduler.models import ShootReminder
        from scheduler.shoot_reminders import sync_shoot_reminders
        sync_shoot_reminders(self.shoot)
        row = ShootReminder.objects.first()
        row.status = 'manual_review'
        row.save()
        output = StringIO()
        with patch('scheduler.reminder_integration.get_reminder_source') as source:
            call_command('run_shoot_reminders',status=True,stdout=output)
        source.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['ledger']['manual_review'],1)

    def test_activity_refresh_moves_confirmed_shoot_without_sending_invites(self):
        from scheduler.reminder_integration import refresh_shoot
        when = timezone.now()+timedelta(days=5)
        activity = {'id':123,'type':'shoot_booking','done':False,'due_date':when.strftime('%Y-%m-%d'),'due_time':when.strftime('%H:%M'),'location':'Updated synthetic rink','subject':'Synthetic shoot'}
        source = type('Source', (), {'get_activity':lambda _, pk: activity})()
        with patch('scheduler.orchestrator.handle_new_shoot') as create:
            self.assertTrue(refresh_shoot(self.shoot, source=source))
        self.shoot.refresh_from_db()
        self.assertEqual(self.shoot.location,'Updated synthetic rink')
        self.assertEqual(self.shoot.status,'confirmed')
        self.assertEqual(self.shoot.confirmed_videographer_id,self.person.pk)
        create.assert_not_called()

    def test_deleted_source_cancels_confirmed_shoot(self):
        from scheduler.reminder_integration import refresh_shoot
        source = type('Source', (), {'get_activity':lambda _, pk: None})()
        self.assertFalse(refresh_shoot(self.shoot,source=source))
        self.shoot.refresh_from_db()
        self.assertEqual(self.shoot.status,'cancelled')

    def test_unknown_crm_failure_does_not_cancel_shoot(self):
        from scheduler.reminder_integration import refresh_shoot
        class Source:
            def get_activity(self,pk):
                raise RuntimeError('unavailable')
        with self.assertRaises(RuntimeError):
            refresh_shoot(self.shoot,source=Source())
        self.shoot.refresh_from_db()
        self.assertEqual(self.shoot.status,'confirmed')

    def test_date_only_activity_never_becomes_a_midnight_reminder(self):
        from scheduler.reminder_integration import refresh_shoot
        source = type('Source', (), {'get_activity':lambda _, pk: {'id':123,'type':'shoot_booking','due_date':'2028-02-01','due_time':'','location':'Rink'}})()
        self.assertFalse(refresh_shoot(self.shoot,source=source))

    def test_real_midnight_utc_is_not_treated_as_date_only(self):
        from scheduler.reminder_integration import refresh_shoot
        source = type('Source', (), {'get_activity':lambda _, pk: {'id':123,'type':'shoot_booking','due_date':'2028-02-01','due_time':'00:00','location':'Rink'}})()
        self.assertTrue(refresh_shoot(self.shoot,source=source))
        self.shoot.refresh_from_db()
        self.assertEqual(self.shoot.shoot_datetime.isoformat(),'2028-02-01T00:00:00+00:00')
