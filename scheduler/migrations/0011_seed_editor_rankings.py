# Generated manually to seed initial editor rankings once on deployment.

from django.db import migrations


EDITORS = {
    "nick@puckpromedia.com": ("Nick Potero", 95284598),
    "pcrider117@gmail.com": ("Paige Rider", 101212611),
    "lora.jonathan3@gmail.com": ("John", 95296551),
    "frankiehartman15@gmail.com": ("Frankie Hartman", 95296552),
    "jaxfolvik44chc@gmail.com": ("jackson Folvik", 95296553),
    "miked55211@gmail.com": ("Michael Donaghy", 101211981),
    "stephenwil92@gmail.com": ("Stephen Wilchensky", 95296625),
    "dshea177@gmail.com": ("Dylan Shea", 95296544),
    "maherchristopher93@gmail.com": ("Christopher Maher", 95355889),
    "jaylinkelly342@gmail.com": ("Dubsie", 101212612),
    "ekrause2929@gmail.com": ("Eric Krause", 101163479),
    "48media.official@gmail.com": ("liam dizazzo", 95357659),
    "jonathanmtheodore@gmail.com": ("Jonathan Theodore", 101218770),
    "kaedenmurphy7706@gmail.com": ("Kaeden Murphy", 95354070),
}

RANKINGS = {
    "Recruiting": [
        "nick@puckpromedia.com",
        "pcrider117@gmail.com",
        "lora.jonathan3@gmail.com",
        "frankiehartman15@gmail.com",
        "jaxfolvik44chc@gmail.com",
        "miked55211@gmail.com",
        "stephenwil92@gmail.com",
        "dshea177@gmail.com",
        "maherchristopher93@gmail.com",
        "jaylinkelly342@gmail.com",
    ],
    "Hype": [
        "ekrause2929@gmail.com",
        "jaxfolvik44chc@gmail.com",
        "frankiehartman15@gmail.com",
        "lora.jonathan3@gmail.com",
        "48media.official@gmail.com",
        "stephenwil92@gmail.com",
        "dshea177@gmail.com",
        "jonathanmtheodore@gmail.com",
        "kaedenmurphy7706@gmail.com",
        "jaylinkelly342@gmail.com",
    ],
    "Highlight": [
        "nick@puckpromedia.com",
        "lora.jonathan3@gmail.com",
        "48media.official@gmail.com",
        "frankiehartman15@gmail.com",
        "jaxfolvik44chc@gmail.com",
        "dshea177@gmail.com",
        "maherchristopher93@gmail.com",
    ],
}


def seed_editor_rankings(apps, schema_editor):
    Editor = apps.get_model("scheduler", "Editor")
    EditorVideoTypeRank = apps.get_model("scheduler", "EditorVideoTypeRank")

    editors_by_email = {}
    for email, (name, clickup_user_id) in EDITORS.items():
        editor, created = Editor.objects.get_or_create(
            email=email,
            defaults={
                "name": name,
                "clickup_user_id": clickup_user_id,
                "max_active_jobs": 5,
                "active": True,
            },
        )
        if not created:
            changed_fields = []
            if not editor.clickup_user_id:
                editor.clickup_user_id = clickup_user_id
                changed_fields.append("clickup_user_id")
            if not editor.name:
                editor.name = name
                changed_fields.append("name")
            if changed_fields:
                editor.save(update_fields=changed_fields)
        editors_by_email[email] = editor

    for video_type, emails in RANKINGS.items():
        for rank, email in enumerate(emails, start=1):
            editor = editors_by_email[email]
            EditorVideoTypeRank.objects.get_or_create(
                video_type=video_type,
                rank=rank,
                defaults={"editor": editor, "active": True},
            )


def noop_reverse(apps, schema_editor):
    # Keep production data if this migration is ever rolled back.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0010_editjob_clickup_error_editjob_clickup_synced_at_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_editor_rankings, noop_reverse),
    ]
