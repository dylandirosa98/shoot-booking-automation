"""Reminder lifecycle hooks. No geocoding, invites or provider I/O here.

Videographer coordinates remain manually entered; no auto-geocoding is restored.
"""
from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import Shoot, Videographer


def _sync_source(pk):
    if not getattr(settings, 'SHOOT_REMINDERS_ENABLED', False):
        return
    from .models import ShootReminder
    from .shoot_reminders import sync_shoot_reminders
    source = Shoot.objects.filter(pk=pk).first()
    if source is not None:
        sync_shoot_reminders(source)
    else:
        ShootReminder.objects.filter(source_shoot_id=pk, status='pending').update(
            status='cancelled',reason='source_deleted')


@receiver(post_save, sender=Shoot, dispatch_uid='shoot_reminder_after_save')
@receiver(post_delete, sender=Shoot, dispatch_uid='shoot_reminder_after_delete')
def shoot_reminder_changed(sender, instance, **kwargs):
    if kwargs.get('raw') or not getattr(settings, 'SHOOT_REMINDERS_ENABLED', False):
        return
    source_id = instance.pk
    transaction.on_commit(lambda: _sync_source(source_id))


@receiver(post_save, sender=Videographer, dispatch_uid='shoot_reminder_roster_changed')
def reminder_recipient_changed(sender, instance, **kwargs):
    if kwargs.get('raw') or not getattr(settings, 'SHOOT_REMINDERS_ENABLED', False):
        return
    person_id = instance.pk
    def reconcile_assignments():
        for pk in Shoot.objects.filter(confirmed_videographer_id=person_id).values_list('pk',flat=True):
            _sync_source(pk)
    transaction.on_commit(reconcile_assignments)
