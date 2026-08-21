from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0014_invite_calendar_error_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="shoot",
            name="queue_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Automatically progress a manually created queue after each member's deadline.",
            ),
        ),
        migrations.AddField(
            model_name="invite",
            name="queue_wait_hours",
            field=models.PositiveIntegerField(default=24),
        ),
        migrations.AddField(
            model_name="invite",
            name="deadline_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
