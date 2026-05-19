"""
One-shot bootstrap for fresh deployments (Railway etc.).

Runs on every deploy via the Procfile `release` phase. All operations are
idempotent — safe to run repeatedly.

Steps:
1. Seed videographer roster (only adds new, updates existing).
2. Ensure an admin superuser exists for /admin login.
   Reads creds from env (with sensible defaults):
       BOOTSTRAP_ADMIN_USERNAME (default: info@puckpromedia.com)
       BOOTSTRAP_ADMIN_EMAIL    (default: info@puckpromedia.com)
       BOOTSTRAP_ADMIN_PASSWORD (default: Workspace_Puckpro99)
"""
import os
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Idempotent first-time bootstrap for a fresh deployment"

    def handle(self, *args, **opts):
        from scheduler.models import Videographer
        from scheduler.scoring import _ensure_coords

        # Seed videographers ONLY if the table is empty. After first deploy,
        # the user's edits in admin are the source of truth and we never touch
        # existing rows.
        if Videographer.objects.count() == 0:
            self.stdout.write("Videographer table is empty — running one-time seed...")
            call_command("seed_videographers")
        else:
            self.stdout.write(f"Videographers already exist ({Videographer.objects.count()}), "
                              "skipping seed (your edits are preserved).")

        # Backfill coords for any active videographer missing lat/lng (lat/lng only)
        missing = Videographer.objects.filter(active=True).filter(lat__isnull=True)
        if missing.exists():
            self.stdout.write(f"Backfilling coords for {missing.count()} videographer(s)...")
            for v in missing:
                ok = _ensure_coords(v)
                self.stdout.write(f"  {'OK' if ok else 'FAIL'}: {v.name} ({v.city}, {v.state})")

        # 2. Ensure superuser exists
        username = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "info@puckpromedia.com")
        email    = os.getenv("BOOTSTRAP_ADMIN_EMAIL",    "info@puckpromedia.com")
        password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "Workspace_Puckpro99")

        u, created = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "is_staff": True, "is_superuser": True, "is_active": True},
        )
        if created:
            u.set_password(password)
            u.save()
            self.stdout.write(self.style.SUCCESS(f"Created superuser: {username}"))
        else:
            self.stdout.write(f"Superuser {username} already exists, skipping")

        self.stdout.write(self.style.SUCCESS("Bootstrap complete."))
