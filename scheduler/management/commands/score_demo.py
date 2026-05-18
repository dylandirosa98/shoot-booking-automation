"""
Preview how the scoring formula ranks videographers for hypothetical shoots.

Usage:
    python manage.py score_demo                # run the built-in scenarios
    python manage.py score_demo --penalty 0.02 # try a different penalty value
"""
from django.core.management.base import BaseCommand
from scheduler.scoring import rank_for_shoot
from scheduler.models import SchedulingSettings

# (label, lat, lng, state)
SCENARIOS = [
    ("Newark, NJ (close to most NJ folks)",            40.7357, -74.1724, "NJ"),
    ("Princeton, NJ (central NJ)",                     40.3573, -74.6672, "NJ"),
    ("Manhattan, NY (NYC hockey rink)",                40.7831, -73.9712, "NY"),
    ("Albany, NY (upstate — likely no one close)",     42.6526, -73.7562, "NY"),
    ("New Haven, CT (central CT)",                     41.3083, -72.9279, "CT"),
    ("Boston, MA (only 2 MA folks)",                   42.3601, -71.0589, "MA"),
    ("Philadelphia, PA (lots of PA people nearby)",    39.9526, -75.1652, "PA"),
    ("Pittsburgh, PA (only Declan is close)",          40.4406, -79.9959, "PA"),
]


class Command(BaseCommand):
    help = "Demo the scoring formula on a few hypothetical shoot locations"

    def add_arguments(self, parser):
        parser.add_argument("--penalty", type=float, default=None,
                            help="Temporarily override penalty for this run (not saved)")

    def handle(self, *args, **opts):
        cfg = SchedulingSettings.get()
        if opts["penalty"] is not None:
            cfg.score_penalty_per_minute = opts["penalty"]  # in-memory only

        self.stdout.write(self.style.NOTICE(
            f"\nUsing penalty = {cfg.score_penalty_per_minute} rating-points per drive-minute"
        ))
        self.stdout.write(self.style.NOTICE(
            f"Max drive cap = {cfg.max_drive_minutes} minutes (anyone further is dropped)\n"
        ))
        self.stdout.write("=" * 78)

        for label, lat, lng, state in SCENARIOS:
            self.stdout.write(self.style.SUCCESS(f"\n>>> {label}"))
            ranked = rank_for_shoot(lat, lng, shoot_state=state, same_state_only=True)
            if not ranked:
                self.stdout.write("    (no eligible videographers in this state)")
                continue
            for i, r in enumerate(ranked):
                marker = "WINNER" if i == 0 else f"  #{i+1}  "
                self.stdout.write(f"  [{marker}] {r}")
