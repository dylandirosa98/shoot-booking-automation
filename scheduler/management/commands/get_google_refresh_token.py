"""
One-time OAuth flow to obtain a Google Calendar refresh token.

Run once. Opens a browser. After you click Allow, the refresh token gets
written into your .env file automatically.

Usage:
    python manage.py get_google_refresh_token
"""
import re
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send",
]


class Command(BaseCommand):
    help = "Run OAuth flow once and save refresh token to .env"

    def handle(self, *args, **opts):
        client_id = settings.GOOGLE_OAUTH_CLIENT_ID
        client_secret = settings.GOOGLE_OAUTH_CLIENT_SECRET
        owner_email = settings.GOOGLE_CALENDAR_OWNER_EMAIL

        if not (client_id and client_secret):
            raise CommandError("GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET must be set in .env")

        self.stdout.write(self.style.NOTICE(
            f"\nA browser will open. Sign in as: {owner_email or '(any Google account you want to grant access)'}\n"
            f"Then click ALLOW on the consent screen.\n"
        ))

        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",
            open_browser=True,
        )

        if not creds.refresh_token:
            raise CommandError("No refresh token returned. Try revoking access at "
                               "https://myaccount.google.com/permissions and re-running this command.")

        self._write_refresh_token_to_env(creds.refresh_token)
        self.stdout.write(self.style.SUCCESS(
            f"\nSuccess! Refresh token saved to .env.\n"
            f"You can now run: python manage.py send_test_invite\n"
        ))

    def _write_refresh_token_to_env(self, token: str):
        env_path = Path(settings.BASE_DIR) / ".env"
        if not env_path.exists():
            raise CommandError(f"Could not find {env_path}")
        text = env_path.read_text()
        new_line = f"GOOGLE_OAUTH_REFRESH_TOKEN={token}"
        if re.search(r"^GOOGLE_OAUTH_REFRESH_TOKEN=.*$", text, flags=re.M):
            text = re.sub(r"^GOOGLE_OAUTH_REFRESH_TOKEN=.*$", new_line, text, flags=re.M)
        else:
            text = text.rstrip() + "\n" + new_line + "\n"
        env_path.write_text(text)
