"""Standalone reminder adapters; no Django, env lookup, registration or live setup.

The injected HTTP client must not retry POST requests (requests.Session defaults
are suitable). tempGuid is a stable correlation key, not a claim of provider
exactly-once semantics: callers must durably guard attempts and quarantine
DeliveryUncertain rather than automatically retrying.
"""
import re
from datetime import date
from urllib.parse import quote, urlsplit

import requests


class ConfigurationError(ValueError):
    """Missing or unsafe explicit configuration."""


class SourceError(RuntimeError):
    """CRM read failed; message contains no provider response or credentials."""



class PipedriveReminderSource:
    def __init__(self, base_url, api_token, *, http=None, timeout=15):
        """base_url is the API root BEFORE /v1 (e.g. https://company.pipedrive.com/api)."""
        valid = False
        try:
            url = urlsplit(base_url)
            valid = (url.scheme in ("http", "https") and bool(url.hostname)
                     and not url.username and not url.password and not url.query and not url.fragment
                     and isinstance(api_token, str) and bool(api_token.strip()))
        except (TypeError, ValueError):
            pass
        if not valid:
            raise ConfigurationError("Explicit CRM API root and credentials required")
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.http = http if http is not None else requests.Session()
        self.timeout = timeout

    def _get(self, path, **params):
        try:
            result = self.http.request(
                "GET", f"{self.base_url}{path}",
                params={"api_token": self.api_token, **params}, timeout=self.timeout,
                allow_redirects=False)
            if result.status_code == 404:
                return None
            if result.status_code != 200:
                raise SourceError("CRM read failed")
            payload = result.json()
            if not isinstance(payload, dict) or payload.get("success") is not True:
                raise SourceError("Invalid CRM response")
            return payload
        except (requests.RequestException, ValueError):
            pass
        raise SourceError("CRM read failed")

    def get_activity(self, activity_id):
        if (isinstance(activity_id, bool) or not isinstance(activity_id, (int, str))
                or not re.fullmatch(r"[1-9][0-9]*", str(activity_id))):
            raise ValueError("A positive numeric activity ID is required")
        payload = self._get(f"/v1/activities/{activity_id}")
        if payload is None:
            return None
        if not isinstance(payload.get("data"), dict):
            raise SourceError("Invalid CRM activity")
        return payload["data"]

    def list_activities(self, start_date, end_date):
        """Fully page incomplete activities; caller filters shoot_booking type.

        Materialize before returning so a later-page failure cannot look complete.
        """
        valid_dates = False
        try:
            valid_dates = (isinstance(start_date, str) and isinstance(end_date, str)
                           and bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", start_date))
                           and bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", end_date))
                           and date.fromisoformat(start_date) <= date.fromisoformat(end_date))
        except ValueError:
            pass
        if not valid_dates:
            raise ValueError("Ordered ISO start_date and end_date required")
        start, activities = 0, []
        while True:
            payload = self._get("/v1/activities", start_date=start_date,
                                end_date=end_date, done=0, user_id=0,
                                type="shoot_booking", start=start, limit=100)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise SourceError("Invalid CRM activity page")
            page = payload["data"]
            if not all(isinstance(item, dict) for item in page):
                raise SourceError("Invalid CRM activity page")
            additional = payload.get("additional_data")
            pagination = additional.get("pagination") if isinstance(additional, dict) else None
            if not isinstance(pagination, dict) or type(pagination.get("more_items_in_collection")) is not bool:
                raise SourceError("Invalid CRM pagination")
            activities.extend(page)
            if not pagination["more_items_in_collection"]:
                return activities
            next_start = pagination.get("next_start")
            if type(next_start) is not int or next_start <= start:
                raise SourceError("Invalid CRM pagination")
            start = next_start


def _phone(value):
    if not isinstance(value, str) or not re.fullmatch(r"[+0-9 ().-]+", value):
        raise ValueError("Valid recipient phone required")
    compact = re.sub(r"[ ().-]", "", value)
    if re.fullmatch(r"[0-9]{10}", compact):
        compact = "+1" + compact
    elif re.fullmatch(r"1[0-9]{10}", compact):
        compact = "+" + compact
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", compact):
        raise ValueError("Valid recipient phone required")
    return compact


class TransportError(RuntimeError):
    """Pre-send bridge failure; no message POST was attempted."""


class DeliveryUncertain(RuntimeError):
    """A POST was attempted; do not resend automatically, reconcile manually."""


class BlueBubblesReminderSender:
    """Explicit bridge only. sender_verified is an operator attestation, not discovery.

    Configure Jack's actual Mac/number, never Dylan's existing bridge. Before enable,
    perform a native Messages and actual-recipient proof of the sender identity.
    Matching configuration and GET readback prove neither identity nor delivery.
    """

    def __init__(self, base_url, password, *, expected_sender_number,
                 verified_sender_number, sender_verified, http=None, timeout=15):
        valid = False
        try:
            url = urlsplit(base_url)
            valid = (url.scheme in ("http", "https") and bool(url.hostname)
                     and not url.username and not url.password and not url.query and not url.fragment
                     and isinstance(password, str) and bool(password.strip())
                     and sender_verified is True
                     and _phone(expected_sender_number) == _phone(verified_sender_number))
        except (ValueError, TypeError):
            pass
        if not valid:
            raise ConfigurationError("Explicit verified sender and bridge configuration required")
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.http = http if http is not None else requests.Session()
        self.timeout = timeout

    def _request(self, method, path, **kwargs):
        params = {"password": self.password, **kwargs.pop("params", {})}
        return self.http.request(method, self.base_url + "/api/v1" + path,
                                 params=params, timeout=self.timeout,
                                 allow_redirects=False, **kwargs)

    def _existing_chat(self, address):
        for service in ("iMessage", "SMS"):
            guid = service + ";-;" + address
            try:
                result = self._request("GET", "/chat/" + quote(guid, safe=""))
                if result.status_code == 404:
                    continue
                if result.status_code != 200:
                    raise TransportError("Bridge chat lookup failed")
                payload = result.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if (not isinstance(data, dict) or data.get("guid") != guid
                        or payload.get("status", 200) != 200 or payload.get("success", True) is not True):
                    raise TransportError("Invalid bridge chat")
                participants = data.get("participants")
                if (not isinstance(participants, list) or len(participants) != 1
                        or not isinstance(participants[0], dict)
                        or _phone(participants[0].get("address")) != address):
                    raise TransportError("Bridge chat is not exact one-to-one recipient")
                return guid
            except (requests.RequestException, ValueError):
                pass
            raise TransportError("Bridge chat lookup failed")
        return None

    def _service(self, address):
        try:
            result = self._request("GET", "/handle/availability/imessage", params={"address": address})
            if result.status_code == 200:
                payload = result.json()
                data = (payload.get("data") if isinstance(payload, dict)
                        and payload.get("status", 200) == 200
                        and payload.get("success", True) is True else None)
                if data is True or (isinstance(data, dict) and data.get("available") is True):
                    return "iMessage"
        except (requests.RequestException, ValueError):
            pass
        return "SMS"

    def send(self, phone, text, idempotency_key):
        """Return independently persisted GUID, NOT recipient delivery confirmation."""
        address = _phone(phone)
        if (not isinstance(text, str) or not text.strip()
                or not isinstance(idempotency_key, str) or not idempotency_key.strip()):
            raise ValueError("Nonempty message and stable idempotency key required")
        chat_guid = self._existing_chat(address)
        payload: dict[str, object] = {"message": text, "method": "private-api", "tempGuid": idempotency_key}
        if chat_guid:
            path = "/message/text"
            payload["chatGuid"] = chat_guid
        else:
            path = "/chat/new"
            service = self._service(address)
            payload.update(addresses=[address], service=service)
            chat_guid = service + ";-;" + address
        try:
            # No retry loop: even a failed HTTP response can follow an accepted send.
            result = self._request("POST", path, json=payload)
            posted = self._message_data(result)
            guid = posted.get("guid")
            if not isinstance(guid, str) or not guid.strip():
                raise DeliveryUncertain("Send persistence uncertain; do not automatically resend")
            readback = self._message_data(self._request("GET", "/message/" + quote(guid, safe="")))
            handle = readback.get("handle")
            handle_matches = (isinstance(handle, dict) and handle.get("address") is not None
                              and _phone(handle["address"]) == address)
            chats = readback.get("chats")
            chat_matches = (bool(chat_guid) and isinstance(chats, list) and len(chats) == 1
                            and isinstance(chats[0], dict) and chats[0].get("guid") == chat_guid)
            if (readback.get("guid") != guid or readback.get("text") != text
                    or readback.get("isFromMe") is not True or readback.get("error") not in (None, 0)
                    or (handle is not None and not handle_matches)
                    or (chats is not None and not chat_matches)
                    or not (handle_matches or chat_matches)):
                raise DeliveryUncertain("Send persistence uncertain; do not automatically resend")
            return guid
        except (requests.RequestException, ValueError):
            pass
        # Raised outside except: no leaking credential-bearing exception context.
        raise DeliveryUncertain("Send persistence uncertain; do not automatically resend")

    @staticmethod
    def _message_data(result):
        if result.status_code != 200:
            raise DeliveryUncertain("Send persistence uncertain; do not automatically resend")
        payload = result.json()
        if (not isinstance(payload, dict) or not isinstance(payload.get("data"), dict)
                or payload.get("status", 200) != 200 or payload.get("success", True) is not True):
            raise DeliveryUncertain("Send persistence uncertain; do not automatically resend")
        return payload["data"]
