from datetime import timedelta
from unittest.mock import Mock, patch
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from scheduler.models import Shoot, Videographer


@override_settings(SHOOT_REMINDERS_ENABLED=True, SHOOT_REMINDERS_SEND_ENABLED=True)
class ReminderWholePathTests(TransactionTestCase):
    def setUp(self):
        from scheduler.reminder_adapters import BlueBubblesReminderSender, PipedriveReminderSource
        self.now = timezone.now().replace(second=0,microsecond=0)+timedelta(minutes=10)
        self.start = self.now+timedelta(hours=48)
        self.person = Videographer.objects.create(name='Synthetic Camera', email='test@example.invalid',phone='+15555550101',state='NJ',rating=4)
        self.shoot = Shoot.objects.create(pipedrive_activity_id='123',title='Synthetic',location='Synthetic rink',shoot_datetime=self.start,status='confirmed',confirmed_videographer=self.person)
        self.activity = {'id':123,'type':'shoot_booking','done':False,'due_date':self.start.strftime('%Y-%m-%d'),'due_time':self.start.strftime('%H:%M'),'location':'Synthetic rink','subject':'Synthetic'}
        self.crm_http = Mock()
        self.crm_http.request.return_value = self.response(self.activity)
        self.source = PipedriveReminderSource('https://crm.example.invalid/api','synthetic-token',http=self.crm_http)
        self.bb_http = Mock()
        self.sender = BlueBubblesReminderSender('https://bridge.example.invalid','synthetic-secret',expected_sender_number='+15555550199',verified_sender_number='+15555550199',sender_verified=True,http=self.bb_http)

    def response(self, data):
        value = Mock(status_code=200)
        value.json.return_value = {'success':True,'data':data}
        return value

    def test_whole_path_reads_source_sends_reads_back_and_deduplicates(self):
        from scheduler.reminder_integration import run_reminder_cycle
        from scheduler.shoot_reminders import render_reminder
        from scheduler.models import ShootReminder
        guid = 'synthetic-message-guid'
        chat = 'iMessage;-;+15555550101'
        self.bb_http.request.side_effect = [
            self.response({'guid':chat,'participants':[{'address':self.person.phone}]}),
            self.response({'guid':guid}),
            self.response({'guid':guid,'text':render_reminder(self.shoot,48),'isFromMe':True,'chats':[{'guid':chat}],'error':0}),
        ]
        with patch('scheduler.reminder_integration.get_reminder_source',return_value=self.source), patch('scheduler.reminder_integration.get_reminder_sender',return_value=self.sender):
            first = run_reminder_cycle(now=self.now)
            second = run_reminder_cycle(now=self.now)
        self.assertEqual(first['worker']['sent'],1)
        self.assertEqual(second['worker']['sent'],0)
        self.assertEqual(self.bb_http.request.call_count,3)
        self.assertEqual(self.crm_http.request.call_count,1)
        row = ShootReminder.objects.get(source_shoot_id=self.shoot.pk,offset_hours=48)
        self.assertEqual((row.status,row.provider_guid),('sent',guid))

    def test_dry_run_makes_no_network_calls_or_ledger_changes(self):
        from scheduler.reminder_integration import run_reminder_cycle
        from scheduler.models import ShootReminder
        before = list(ShootReminder.objects.values())
        with patch('scheduler.reminder_integration.get_reminder_source',return_value=self.source), patch('scheduler.reminder_integration.get_reminder_sender') as factory:
            result = run_reminder_cycle(now=self.now,dry_run=True,reconcile=True)
        self.assertEqual(result['worker']['sent'],0)
        self.assertEqual(before,list(ShootReminder.objects.values()))
        self.crm_http.request.assert_not_called()
        self.bb_http.request.assert_not_called()
        factory.assert_not_called()
