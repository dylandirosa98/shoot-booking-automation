import os
import sys
from django.apps import AppConfig


class SchedulerConfig(AppConfig):
    name = "scheduler"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Wire up signals (auto-geocode videographers on save)
        from . import signals  # noqa: F401

        # Don't start the scheduler during migrations, makemigrations, tests, shell, etc.
        skip_commands = {"migrate", "makemigrations", "collectstatic", "shell",
                         "seed_videographers", "score_demo", "test", "createsuperuser",
                         "simulate_pipedrive"}
        if any(cmd in sys.argv for cmd in skip_commands):
            return
        # Avoid double-start under the autoreloader (only relevant if --noreload not set)
        is_runserver = "runserver" in sys.argv
        autoreload_off = "--noreload" in sys.argv
        if is_runserver and not autoreload_off and os.environ.get("RUN_MAIN") != "true":
            return
        from . import jobs
        try:
            jobs.start()
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("Failed to start APScheduler: %s", e)
