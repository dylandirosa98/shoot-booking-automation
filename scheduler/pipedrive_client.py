"""
Pipedrive *write* API: Person, Lead, Note.

This is independent of the incoming Pipedrive activity webhook used for shoots.
"""
import logging
import uuid
from dataclasses import dataclass, field

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class PipedriveApiError(RuntimeError):
    pass


@dataclass
class FakePipedriveClient:
    """In-memory stand-in when PIPEDRIVE_API_TOKEN is blank (local/dev)."""

    provider_name: str = "fake"
    persons: list = field(default_factory=list)
    leads: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    _next_person: int = 1
    _next_note: int = 1

    def find_person_by_email(self, email: str) -> int | None:
        email = (email or "").strip().lower()
        if not email:
            return None
        for person in self.persons:
            if (person.get("email") or "").lower() == email:
                return person["id"]
        return None

    def find_person_by_phone(self, phone: str) -> int | None:
        phone = (phone or "").strip()
        if not phone:
            return None
        for person in self.persons:
            if person.get("phone") == phone:
                return person["id"]
        return None

    def create_person(self, name: str, email: str = "", phone: str = "") -> int:
        person_id = self._next_person
        self._next_person += 1
        self.persons.append({"id": person_id, "name": name, "email": email, "phone": phone})
        logger.info("[FAKE PIPEDRIVE] CREATE person %s name=%r email=%r", person_id, name, email)
        return person_id

    def create_lead(self, title: str, person_id: int) -> str:
        lead_id = str(uuid.uuid4())
        self.leads.append({"id": lead_id, "title": title, "person_id": person_id})
        logger.info("[FAKE PIPEDRIVE] CREATE lead %s title=%r person=%s", lead_id, title, person_id)
        return lead_id

    def add_note(self, content: str, lead_id: str, person_id: int | None = None) -> int:
        note_id = self._next_note
        self._next_note += 1
        self.notes.append({
            "id": note_id,
            "content": content,
            "lead_id": lead_id,
            "person_id": person_id,
        })
        logger.info("[FAKE PIPEDRIVE] CREATE note %s lead=%s", note_id, lead_id)
        return note_id


class PipedriveClient:
    provider_name = "pipedrive"

    def __init__(self):
        token = settings.PIPEDRIVE_API_TOKEN
        if not token:
            raise PipedriveApiError("PIPEDRIVE_API_TOKEN is not set")
        domain = (settings.PIPEDRIVE_COMPANY_DOMAIN or "").strip()
        if domain:
            domain = domain.replace("https://", "").replace("http://", "").split(".")[0]
            self.base_url = f"https://{domain}.pipedrive.com"
        else:
            self.base_url = "https://api.pipedrive.com"
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-token": token,
        }

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        response = requests.request(method, url, headers=self.headers, timeout=30, **kwargs)
        if response.status_code >= 400:
            raise PipedriveApiError(
                f"Pipedrive {method} {path} failed ({response.status_code}): {response.text[:500]}"
            )
        data = response.json() if response.content else {}
        if data.get("success") is False:
            raise PipedriveApiError(f"Pipedrive {method} {path} error: {data}")
        return data

    def _search_person(self, term: str, fields: str) -> int | None:
        term = (term or "").strip()
        if len(term) < 2:
            return None
        data = self._request(
            "GET",
            "/api/v2/persons/search",
            params={"term": term, "fields": fields, "exact_match": "true", "limit": 1},
        )
        items = ((data.get("data") or {}).get("items")) or []
        if not items:
            return None
        item = items[0].get("item") or items[0]
        person_id = item.get("id")
        return int(person_id) if person_id is not None else None

    def find_person_by_email(self, email: str) -> int | None:
        return self._search_person(email, "email")

    def find_person_by_phone(self, phone: str) -> int | None:
        return self._search_person(phone, "phone")

    def create_person(self, name: str, email: str = "", phone: str = "") -> int:
        body: dict = {"name": name or "Facebook Lead"}
        if email:
            body["emails"] = [{"value": email, "primary": True, "label": "work"}]
        if phone:
            body["phones"] = [{"value": phone, "primary": True, "label": "mobile"}]
        data = self._request("POST", "/api/v2/persons", json=body)
        person = data.get("data") or {}
        person_id = person.get("id")
        if person_id is None:
            raise PipedriveApiError(f"Pipedrive create person returned no id: {data}")
        return int(person_id)

    def create_lead(self, title: str, person_id: int) -> str:
        body: dict = {"title": title, "person_id": int(person_id)}
        owner_id = (settings.PIPEDRIVE_LEAD_OWNER_ID or "").strip()
        if owner_id:
            body["owner_id"] = int(owner_id)
        label_id = (settings.PIPEDRIVE_FACEBOOK_LABEL_ID or "").strip()
        if label_id:
            body["label_ids"] = [label_id]
        data = self._request("POST", "/api/v1/leads", json=body)
        lead = data.get("data") or {}
        lead_id = lead.get("id")
        if not lead_id:
            raise PipedriveApiError(f"Pipedrive create lead returned no id: {data}")
        return str(lead_id)

    def add_note(self, content: str, lead_id: str, person_id: int | None = None) -> int:
        body: dict = {
            "content": content,
            "lead_id": lead_id,
            "pinned_to_lead_flag": 1,
        }
        if person_id:
            body["person_id"] = int(person_id)
        data = self._request("POST", "/api/v1/notes", json=body)
        note = data.get("data") or {}
        note_id = note.get("id")
        if note_id is None:
            raise PipedriveApiError(f"Pipedrive create note returned no id: {data}")
        return int(note_id)


def get_pipedrive_client():
    if settings.PIPEDRIVE_API_TOKEN:
        return PipedriveClient()
    if settings.DEBUG:
        return FakePipedriveClient()
    raise PipedriveApiError("PIPEDRIVE_API_TOKEN is not set")
