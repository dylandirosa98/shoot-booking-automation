"""
Meta Graph API helpers for Instant Form (leadgen) webhooks.

The webhook payload is only IDs. Field answers come from a second Graph call.
"""
import hashlib
import hmac
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GRAPH_FIELDS = "created_time,ad_id,adgroup_id,form_id,field_data,campaign_id"


class FacebookLeadFetchError(RuntimeError):
    pass


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    """Validate X-Hub-Signature-256. If no app secret is configured, allow (dev)."""
    secret = (settings.FACEBOOK_APP_SECRET or "").encode("utf-8")
    if not secret:
        logger.warning("FACEBOOK_APP_SECRET is empty; skipping webhook signature check")
        return True
    if not header:
        return False
    sent = header.split("=", 1)[1] if header.startswith("sha256=") else header
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent)


def fetch_lead(leadgen_id: str) -> dict:
    token = settings.FACEBOOK_PAGE_ACCESS_TOKEN
    if not token:
        raise FacebookLeadFetchError("FACEBOOK_PAGE_ACCESS_TOKEN is not set")

    version = settings.FACEBOOK_GRAPH_API_VERSION
    url = f"https://graph.facebook.com/{version}/{leadgen_id}"
    response = requests.get(
        url,
        params={"fields": GRAPH_FIELDS, "access_token": token},
        timeout=30,
    )
    if response.status_code >= 400:
        raise FacebookLeadFetchError(
            f"Graph lead fetch failed ({response.status_code}): {response.text[:500]}"
        )
    data = response.json()
    if data.get("error"):
        raise FacebookLeadFetchError(f"Graph lead fetch error: {data['error']}")
    return data


def parse_field_data(field_data) -> list[tuple[str, str]]:
    """Turn Graph field_data into ordered (name, joined values) pairs."""
    rows: list[tuple[str, str]] = []
    for item in field_data or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        values = item.get("values") or []
        joined = ", ".join(str(v) for v in values if v is not None and str(v) != "")
        rows.append((name, joined))
    return rows


def fields_lookup(rows: list[tuple[str, str]]) -> dict[str, str]:
    return {name: value for name, value in rows}
