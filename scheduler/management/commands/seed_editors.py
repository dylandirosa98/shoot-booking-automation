from django.core.management.base import BaseCommand
from scheduler.models import Editor, EditorVideoTypeRank


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


class Command(BaseCommand):
    help = "Seed editor ClickUp IDs and per-video-type rankings"

    def add_arguments(self, parser):
        parser.add_argument("--max-active-jobs", type=int, default=5)
        parser.add_argument("--clear-rankings", action="store_true", help="Delete existing editor rankings first")

    def handle(self, *args, **opts):
        if opts["clear_rankings"]:
            deleted, _ = EditorVideoTypeRank.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {deleted} existing ranking rows"))

        editors_by_email = {}
        added = updated = 0
        for email, (name, clickup_user_id) in EDITORS.items():
            editor, created = Editor.objects.update_or_create(
                email=email,
                defaults={
                    "name": name,
                    "clickup_user_id": clickup_user_id,
                    "max_active_jobs": opts["max_active_jobs"],
                    "active": True,
                },
            )
            editors_by_email[email] = editor
            added += int(created)
            updated += int(not created)

        ranking_count = 0
        for video_type, emails in RANKINGS.items():
            for rank, email in enumerate(emails, start=1):
                EditorVideoTypeRank.objects.update_or_create(
                    editor=editors_by_email[email],
                    video_type=video_type,
                    defaults={"rank": rank, "active": True},
                )
                ranking_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Seeded editors. Added {added}, updated {updated}, rankings upserted {ranking_count}."
        ))
