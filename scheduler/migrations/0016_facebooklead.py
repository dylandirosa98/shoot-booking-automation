from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0015_manual_queue_controls"),
    ]

    operations = [
        migrations.CreateModel(
            name="FacebookLead",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("leadgen_id", models.CharField(max_length=64, unique=True)),
                ("page_id", models.CharField(blank=True, max_length=64)),
                ("form_id", models.CharField(blank=True, max_length=64)),
                ("ad_id", models.CharField(blank=True, max_length=64)),
                ("adgroup_id", models.CharField(blank=True, max_length=64)),
                ("facebook_created_time", models.DateTimeField(blank=True, null=True)),
                ("field_data", models.JSONField(blank=True, default=list)),
                ("full_name", models.CharField(blank=True, max_length=255)),
                ("email", models.CharField(blank=True, max_length=255)),
                ("phone", models.CharField(blank=True, max_length=50)),
                ("pipedrive_person_id", models.CharField(blank=True, max_length=50)),
                ("pipedrive_lead_id", models.CharField(blank=True, max_length=64)),
                ("pipedrive_note_id", models.CharField(blank=True, max_length=50)),
                ("status", models.CharField(
                    choices=[
                        ("received", "Received"),
                        ("synced", "Synced to Pipedrive"),
                        ("failed", "Failed"),
                    ],
                    default="received",
                    max_length=20,
                )),
                ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
