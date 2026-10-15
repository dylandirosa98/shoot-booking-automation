"""
Turn a Facebook Instant Form submission into a Pipedrive Person + Lead + Note.

Does not touch the incoming Pipedrive activity webhook.
"""
import logging
from datetime import datetime, timezone as dt_timezone
from html import escape

from django.db import IntegrityError
from django.utils.dateparse import parse_datetime

from .facebook_client import fetch_lead, fields_lookup, parse_field_data
from .models import FacebookLead
from .pipedrive_client import get_pipedrive_client

logger = logging.getLogger(__name__)

FIELD_LABELS = {
    "full_name": "Full name",
    "first_name": "First name",
    "last_name": "Last name",
    "email": "Email",
    "work_email": "Work email",
    "phone_number": "Phone",
    "phone": "Phone",
    "work_phone_number": "Work phone",
    "company_name": "Company",
    "job_title": "Job title",
    "city": "City",
    "state": "State",
    "zip_code": "Zip code",
    "post_code": "Postal code",
    "country": "Country",
    "street_address": "Street address",
    "date_of_birth": "Date of birth",
    "gender": "Gender",
    "relationship_status": "Relationship status",
    "military_status": "Military status",
}


def _parse_fb_time(raw) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(int(raw), tz=dt_timezone.utc)
    text = str(raw)
    if text.endswith("+0000"):
        text = text[:-5] + "+00:00"
    parsed = parse_datetime(text)
    return parsed


def _humanize_field(name: str) -> str:
    if name in FIELD_LABELS:
        return FIELD_LABELS[name]
    return name.replace("_", " ").strip().capitalize()


def _contact_from_fields(lookup: dict[str, str]) -> tuple[str, str, str]:
    email = (lookup.get("email") or lookup.get("work_email") or "").strip()
    phone = (
        lookup.get("phone_number")
        or lookup.get("phone")
        or lookup.get("work_phone_number")
        or ""
    ).strip()
    name = (lookup.get("full_name") or "").strip()
    if not name:
        name = f"{lookup.get('first_name', '')} {lookup.get('last_name', '')}".strip()
    if not name and email:
        name = email.split("@")[0]
    if not name:
        name = "Facebook Lead"
    return name, email, phone


def _lead_title(name: str, lookup: dict[str, str]) -> str:
    company = (lookup.get("company_name") or "").strip()
    if company:
        return f"Facebook Lead — {name} ({company})"
    return f"Facebook Lead — {name}"


def format_note_html(
    rows: list[tuple[str, str]],
    *,
    leadgen_id: str = "",
    form_id: str = "",
    ad_id: str = "",
    created_time: str = "",
) -> str:
    items = []
    for name, value in rows:
        items.append(
            f"<li><strong>{escape(_humanize_field(name))}:</strong> {escape(value or '—')}</li>"
        )
    answers = "".join(items) or "<li>No form answers returned.</li>"
    meta_bits = []
    if created_time:
        meta_bits.append(f"Submitted: {escape(str(created_time))}")
    if form_id:
        meta_bits.append(f"Form ID: {escape(str(form_id))}")
    if ad_id:
        meta_bits.append(f"Ad ID: {escape(str(ad_id))}")
    if leadgen_id:
        meta_bits.append(f"Facebook lead ID: {escape(str(leadgen_id))}")
    meta_html = f"<p>{'<br/>'.join(meta_bits)}</p>" if meta_bits else ""
    return (
        "<p><strong>Facebook Instant Form answers</strong></p>"
        f"<ul>{answers}</ul>"
        f"{meta_html}"
    )


def _get_or_create_record(leadgen_id: str, **defaults) -> FacebookLead:
    try:
        record, _created = FacebookLead.objects.get_or_create(
            leadgen_id=leadgen_id,
            defaults=defaults,
        )
        return record
    except IntegrityError:
        return FacebookLead.objects.get(leadgen_id=leadgen_id)


def _sync_to_pipedrive(record: FacebookLead, rows: list[tuple[str, str]], lookup: dict[str, str]) -> dict:
    name, email, phone = _contact_from_fields(lookup)
    record.full_name = name
    record.email = email
    record.phone = phone
    record.field_data = [{"name": n, "values": [v] if v else []} for n, v in rows]
    record.save(update_fields=["full_name", "email", "phone", "field_data", "updated_at"])

    client = get_pipedrive_client()
    person_id = None
    if email:
        person_id = client.find_person_by_email(email)
    if person_id is None and phone:
        person_id = client.find_person_by_phone(phone)
    if person_id is None:
        person_id = client.create_person(name, email=email, phone=phone)

    lead_id = client.create_lead(_lead_title(name, lookup), person_id)
    note_html = format_note_html(
        rows,
        leadgen_id=record.leadgen_id,
        form_id=record.form_id,
        ad_id=record.ad_id,
        created_time=(
            record.facebook_created_time.isoformat() if record.facebook_created_time else ""
        ),
    )
    note_id = client.add_note(note_html, lead_id, person_id=person_id)

    record.pipedrive_person_id = str(person_id)
    record.pipedrive_lead_id = str(lead_id)
    record.pipedrive_note_id = str(note_id)
    record.status = "synced"
    record.error = ""
    record.save()

    logger.info(
        "Synced Facebook lead %s to Pipedrive person=%s lead=%s note=%s",
        record.leadgen_id, person_id, lead_id, note_id,
    )
    return {
        "status": "ok",
        "leadgen_id": record.leadgen_id,
        "pipedrive_person_id": str(person_id),
        "pipedrive_lead_id": str(lead_id),
        "pipedrive_note_id": str(note_id),
        "pipedrive_provider": getattr(client, "provider_name", ""),
    }


def ingest_facebook_lead(
    *,
    leadgen_id: str,
    page_id: str = "",
    form_id: str = "",
    ad_id: str = "",
    adgroup_id: str = "",
    created_time=None,
    details: dict | None = None,
) -> dict:
    """Fetch (unless details provided) and write the lead to Pipedrive. Idempotent on leadgen_id."""
    leadgen_id = str(leadgen_id or "").strip()
    if not leadgen_id:
        return {"status": "ignored", "reason": "missing leadgen_id"}

    record = _get_or_create_record(
        leadgen_id,
        page_id=str(page_id or ""),
        form_id=str(form_id or ""),
        ad_id=str(ad_id or ""),
        adgroup_id=str(adgroup_id or ""),
        facebook_created_time=_parse_fb_time(created_time),
        status="received",
    )
    if record.status == "synced" and record.pipedrive_lead_id:
        return {
            "status": "skipped",
            "reason": "already synced",
            "leadgen_id": leadgen_id,
            "pipedrive_lead_id": record.pipedrive_lead_id,
        }

    # Fill ids from this delivery if the row was created earlier with blanks.
    dirty = False
    for attr, value in (
        ("page_id", page_id),
        ("form_id", form_id),
        ("ad_id", ad_id),
        ("adgroup_id", adgroup_id),
    ):
        value = str(value or "")
        if value and not getattr(record, attr):
            setattr(record, attr, value)
            dirty = True
    parsed_time = _parse_fb_time(created_time)
    if parsed_time and not record.facebook_created_time:
        record.facebook_created_time = parsed_time
        dirty = True
    if dirty:
        record.save()

    try:
        if details is None:
            details = fetch_lead(leadgen_id)
        raw_fields = details.get("field_data") or []
        rows = parse_field_data(raw_fields)
        lookup = fields_lookup(rows)
        if details.get("form_id") and not record.form_id:
            record.form_id = str(details["form_id"])
        if details.get("ad_id") and not record.ad_id:
            record.ad_id = str(details["ad_id"])
        if details.get("created_time") and not record.facebook_created_time:
            record.facebook_created_time = _parse_fb_time(details["created_time"])
        return _sync_to_pipedrive(record, rows, lookup)
    except Exception as exc:
        logger.exception("Failed to ingest Facebook lead %s", leadgen_id)
        record.status = "failed"
        record.error = str(exc)
        record.save(update_fields=["status", "error", "updated_at"])
        return {"status": "failed", "leadgen_id": leadgen_id, "reason": str(exc)}


def ingest_leadgen_notification(value: dict) -> dict:
    value = value or {}
    return ingest_facebook_lead(
        leadgen_id=str(value.get("leadgen_id") or ""),
        page_id=str(value.get("page_id") or ""),
        form_id=str(value.get("form_id") or ""),
        ad_id=str(value.get("ad_id") or ""),
        adgroup_id=str(value.get("adgroup_id") or ""),
        created_time=value.get("created_time"),
    )
