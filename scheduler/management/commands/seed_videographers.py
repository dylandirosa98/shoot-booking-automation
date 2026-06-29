"""
One-shot seed: load the initial videographer roster into the DB.

Usage:
    python manage.py seed_videographers          # add only new, skip existing
    python manage.py seed_videographers --reset  # wipe + reload everything

Geocoding (lat/lng) happens in a separate command so this can run without
a Google Maps API key configured yet:
    python manage.py geocode_videographers
"""
from django.core.management.base import BaseCommand
from scheduler.models import Videographer, VideographerServiceState


# Format: (name, city, state, rating, phone, email, lat, lng)
# lat/lng are approximate city-center coordinates (close enough for distance estimation).
ROSTER = [
    # --- New Jersey ---
    ("Stephen Wilchensky", "Glassboro",    "NJ", 5.0, "856-563-4917", "wilche19@rowan.edu",          39.7026, -75.1118),
    ("Selma Mehmedagic",   "New Brunswick","NJ", 4.0, "848-248-7090", "selma@puckpromedia.com",      40.4862, -74.4518),
    ("Jackson Folvik",     "Cranford",     "NJ", 4.1, "732-259-5750", "jaxfolvik44chc@gmail.com",    40.6582, -74.2996),
    ("Anthony Grillo",     "Wayne",        "NJ", 3.7, "551-262-2321", "anthonygrillo33@yahoo.com",   40.9251, -74.2767),
    ("Mel Calhoun",        "Deptford",     "NJ", 3.0, "",             "sportzshots22@gmail.com",     39.8295, -75.1057),
    ("Evangeline Avila",   "Kearny",       "NJ", 3.0, "310-569-0147", "avila.evangeline@gmail.com",  40.7684, -74.1454),
    ("Carson Noll",        "Wayne",        "NJ", 3.2, "",             "carsonjoes2028@gmail.com",    40.9251, -74.2767),

    # --- New York ---
    ("John Testa",         "West Islip",        "NY", 4.5, "631-358-0538", "testajohn@icloud.com",   40.7048, -73.2956),
    ("Lawton Meyer",       "Dover Plains",      "NY", 4.5, "845-249-6161", "lawtonmeyer@gmail.com",  41.7409, -73.5765),
    ("Chris Magliario",    "Long Island",       "NY", 4.0, "516-457-4963", "prodbychid@outlook.com", 40.7891, -73.1350),
    ("Luis Dejesus",       "Harlem, NYC",       "NY", 3.0, "646-510-0405", "dpsalm13@gmail.com",     40.8116, -73.9465),

    # --- Connecticut ---
    ("John Dufor",         "Willimantic",  "CT", 4.5, "860-209-0451", "john@johndufour.com",          41.7106, -72.2087),
    ("Romaine Rookwood",   "Shelton",      "CT", 4.0, "475-455-2657", "romainevisuals@gmail.com",     41.3164, -73.0931),
    ("Logan Winslow",      "Hamden",       "CT", 4.5, "973-943-7000", "winslowsportsmedia@gmail.com", 41.3960, -72.8967),
    ("Michael Negron",     "Stamford",     "CT", 4.2, "",             "michael.r.negron@gmail.com",   41.0534, -73.5387),
    ("Otto Lamnayra",      "Hartford",     "CT", 3.0, "860-790-9811", "otmanfit@gmail.com",           41.7658, -72.6734),

    # --- Massachusetts ---
    ("Dylan Shea",         "Ipswich",      "MA", 5.0, "978-471-2027", "DShea177@gmail.com",         42.6792, -70.8412),
    ("Silas Morris",       "Framingham",   "MA", 4.8, "508-745-3861", "morris.aerials@gmail.com",   42.2793, -71.4162),

    # --- Pennsylvania ---
    ("Declan Moffatt",     "Pittsburgh",   "PA", 5.0, "412-529-9470", "dmoffatt@puckpromedia.com",  40.4406, -79.9959),
    ("Eric Krause",        "Hatfield",     "PA", 4.0, "484-238-3121", "ekrause2929@gmail.com",      40.2776, -75.2999),
    ("Noah Youngbluth",    "Hershey",      "PA", 3.7, "717-736-6711", "vision.photos.27@gmail.com", 40.2859, -76.6502),
    ("Maya Polss",         "West Chester", "PA", 3.0, "302-743-0148", "mp.media.29@gmail.com",      39.9601, -75.6055),
    ("Paige Rider",        "Philadelphia", "PA", 3.5, "484-935-2227", "pcrider117@gmail.com",       39.9526, -75.1652),
    ("Frankie Hartman",    "West Chester", "PA", 4.5, "484-901-9727", "frankiehartman15@gmail.com", 39.9601, -75.6055),
]


class Command(BaseCommand):
    help = "Seed the Videographer table with the initial roster"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete all videographers first")

    def handle(self, *args, **opts):
        if opts["reset"]:
            count = Videographer.objects.count()
            Videographer.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {count} existing videographers"))

        added, updated = 0, 0
        for name, city, state, rating, phone, email, lat, lng in ROSTER:
            address = f"{city}, {state}"
            obj, created = Videographer.objects.update_or_create(
                email=email,
                defaults={
                    "name": name,
                    "city": city,
                    "state": state,
                    "address": address,
                    "rating": rating,
                    "phone": phone,
                    "lat": lat,
                    "lng": lng,
                    "active": True,
                },
            )
            VideographerServiceState.objects.get_or_create(videographer=obj, state=state)
            if created:
                added += 1
                self.stdout.write(f"  + {name} ({state})")
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Added {added}, updated {updated}. "
            f"Total in DB: {Videographer.objects.count()}"
        ))
