import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class PipedriveClient:
    def __init__(self):
        self.api_token = settings.PIPEDRIVE_API_TOKEN

    def get_activity_note(self, activity_id: str) -> str:
        if not self.api_token or not activity_id:
            return ""

        for base_url in (
            "https://api.pipedrive.com/api/v2",
            "https://api.pipedrive.com/api/v1",
        ):
            try:
                response = requests.get(
                    f"{base_url}/activities/{activity_id}",
                    params={"api_token": self.api_token},
                    timeout=20,
                )
            except requests.RequestException:
                logger.exception("Pipedrive activity note fetch failed for activity_id=%s", activity_id)
                return ""

            if response.status_code == 404:
                continue
            if response.status_code >= 400:
                logger.warning(
                    "Pipedrive activity note fetch failed for activity_id=%s status=%s body=%s",
                    activity_id,
                    response.status_code,
                    response.text[:300],
                )
                continue

            payload = response.json()
            data = payload.get("data") or {}
            if isinstance(data, dict):
                return data.get("note") or ""

        return ""


def get_activity_note(activity_id: str) -> str:
    return PipedriveClient().get_activity_note(activity_id)
