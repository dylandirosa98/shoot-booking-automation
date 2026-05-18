"""
Send emails via Gmail API using the same OAuth refresh token we use for Calendar.
"""
import base64
import logging
from email.mime.text import MIMEText
from django.conf import settings

logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str) -> bool:
    """Send a plaintext email FROM the OAuth account TO the given address.
    Returns True on success, False on failure. Never raises."""
    if not settings.GOOGLE_OAUTH_REFRESH_TOKEN:
        logger.warning("Skipping email send: no Google refresh token configured")
        return False
    if not to:
        logger.warning("Skipping email send: no recipient")
        return False

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=settings.GOOGLE_OAUTH_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/gmail.send"],
        )
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        msg = MIMEText(body)
        msg["to"] = to
        msg["from"] = settings.GOOGLE_CALENDAR_OWNER_EMAIL or "me"
        msg["subject"] = subject

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as e:
        logger.exception("Failed to send email to %s: %s", to, e)
        return False
