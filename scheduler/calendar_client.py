"""
Calendar client abstraction.

- FakeCalendarClient: logs what it would send. Used when no Google refresh token is configured.
- GoogleCalendarClient: real Google Calendar API.

get_client() picks based on env: if GOOGLE_OAUTH_REFRESH_TOKEN is set, uses real Google.
"""
import logging
import uuid
from datetime import datetime, timedelta
from django.conf import settings

logger = logging.getLogger(__name__)


class FakeCalendarClient:
    """Logs everything and returns synthetic event IDs. Safe for local testing."""
    provider_name = "fake"

    def create_event(self, *, summary: str, description: str, location: str,
                     start: datetime, end: datetime, attendee_email: str) -> str:
        event_id = f"fake-{uuid.uuid4().hex[:12]}"
        logger.info(
            "[FAKE CAL] CREATE event %s\n"
            "  summary:   %s\n"
            "  location:  %s\n"
            "  when:      %s -> %s\n"
            "  attendee:  %s\n"
            "  desc:      %s",
            event_id, summary, location, start, end, attendee_email, description[:120],
        )
        return event_id

    def cancel_event(self, event_id: str) -> None:
        logger.info("[FAKE CAL] CANCEL event %s", event_id)

    def get_attendee_status(self, event_id: str, attendee_email: str) -> str:
        """Always 'needsAction' so escalation fires. Monkey-patch in tests."""
        return "needsAction"


class GoogleCalendarClient:
    """Real Google Calendar API client using a saved refresh token."""
    provider_name = "google"

    def __init__(self):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=settings.GOOGLE_OAUTH_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/calendar.events"],
        )
        self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.owner_email = settings.GOOGLE_CALENDAR_OWNER_EMAIL

    def create_event(self, *, summary: str, description: str, location: str,
                     start: datetime, end: datetime, attendee_email: str) -> str:
        body = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {"dateTime": start.isoformat(), "timeZone": settings.TIME_ZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": settings.TIME_ZONE},
            "attendees": [{"email": attendee_email}],
            "guestsCanModify": False,
            "reminders": {"useDefault": True},
        }
        event = self.service.events().insert(
            calendarId="primary",
            body=body,
            sendUpdates="all",  # actually email the invite
        ).execute()
        event_id = event["id"]
        logger.info("[GOOGLE CAL] created event %s for %s", event_id, attendee_email)
        return event_id

    def cancel_event(self, event_id: str) -> None:
        try:
            self.service.events().delete(
                calendarId="primary",
                eventId=event_id,
                sendUpdates="all",
            ).execute()
            logger.info("[GOOGLE CAL] cancelled event %s", event_id)
        except Exception as e:
            logger.exception("[GOOGLE CAL] failed to cancel event %s: %s", event_id, e)

    def get_attendee_status(self, event_id: str, attendee_email: str) -> str:
        """Returns 'accepted' | 'declined' | 'tentative' | 'needsAction'."""
        try:
            event = self.service.events().get(calendarId="primary", eventId=event_id).execute()
            for att in event.get("attendees", []):
                if att.get("email", "").lower() == attendee_email.lower():
                    return att.get("responseStatus", "needsAction")
            return "needsAction"
        except Exception as e:
            logger.exception("[GOOGLE CAL] failed to read event %s: %s", event_id, e)
            return "needsAction"


def get_client():
    """Use real Google if a refresh token is configured, else fake."""
    if settings.GOOGLE_OAUTH_REFRESH_TOKEN:
        return GoogleCalendarClient()
    return FakeCalendarClient()
