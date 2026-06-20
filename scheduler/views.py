import base64
import json
import logging
from datetime import datetime
from django.conf import settings
from django.http import JsonResponse, HttpResponseForbidden, HttpResponseBadRequest
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .models import Editor, EditJob, Videographer, Shoot, Invite, SchedulingSettings
from .orchestrator import (
    handle_new_shoot, handle_updated_shoot, handle_deleted_shoot,
    check_and_escalate, _mark_accepted,
)
from .editing import handle_deleted_edit_job, handle_new_edit_job, handle_updated_edit_job

logger = logging.getLogger(__name__)


# Pipedrive sometimes returns "admin_area_level_1" as the full US state name.
# We store videographers by 2-letter code, so convert.
US_STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}


def _normalize_state(raw: str) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return US_STATE_NAME_TO_CODE.get(raw.lower())


def _normalize_type(raw: str) -> str:
    """Pipedrive sends 'shoot_booking' as the internal type key. Normalize for comparison."""
    return (raw or "").strip().lower().replace("_", " ").replace("-", " ")


def _extract_value(field):
    """Pipedrive v2 wraps some fields like due_time and duration as
    {'value': 'HH:MM:SS', 'timezone_id': ...}. Extract the string."""
    if isinstance(field, dict):
        return field.get("value") or ""
    return field or ""


def _strip_html(s: str) -> str:
    """Pipedrive's note/description fields often contain HTML — flatten to plain text."""
    if not s:
        return ""
    import re
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    # Decode a few common entities
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
           .replace("&lt;", "<").replace("&gt;", ">")
           .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _parse_hhmm(time_str: str) -> str:
    """Accept 'HH:MM' or 'HH:MM:SS', return 'HH:MM'."""
    if not time_str:
        return "00:00"
    parts = time_str.split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return "00:00"


def _parse_duration_minutes(duration_str: str) -> int | None:
    """'01:30:00' or '01:30' -> minutes. Return None if missing."""
    if not duration_str:
        return None
    parts = duration_str.split(":")
    try:
        h = int(parts[0]) if len(parts) > 0 else 0
        m = int(parts[1]) if len(parts) > 1 else 0
        return h * 60 + m
    except ValueError:
        return None


def dashboard(request):
    videographers = Videographer.objects.all().order_by("state", "-rating")

    # Group videographers by state
    by_state = {}
    for v in videographers:
        by_state.setdefault(v.state, []).append(v)

    # Upcoming = shoot hasn't happened yet; show regardless of status (anything
    # can still change up until the shoot starts).
    now = timezone.now()
    shoots = (Shoot.objects
              .filter(shoot_datetime__gte=now)
              .exclude(status="cancelled")
              .prefetch_related("invites__videographer")
              .order_by("shoot_datetime"))

    stats = {
        "videographers_total": Videographer.objects.count(),
        "videographers_active": Videographer.objects.filter(active=True).count(),
        "shoots_upcoming": shoots.count(),
        "shoots_pending": shoots.filter(status="pending").count(),
        "shoots_confirmed": shoots.filter(status="confirmed").count(),
        "shoots_total": Shoot.objects.count(),
    }

    # Annotate each shoot with its current invitee + prior invite history
    for s in shoots:
        sent = list(s.invites.exclude(google_event_id="").order_by("rank"))
        s.current_invitee = sent[-1] if sent else None
        s.prior_invitees = sent[:-1] if len(sent) > 1 else []

    return render(request, "dashboard.html", {
        "by_state": by_state,
        "shoots": shoots,
        "stats": stats,
    })


def edits_dashboard(request):
    editors = (Editor.objects
               .all()
               .prefetch_related("edit_jobs", "video_type_ranks")
               .order_by("name"))
    jobs = (EditJob.objects
            .select_related("assigned_editor")
            .exclude(status="cancelled")
            .order_by("due_datetime"))

    for editor in editors:
        editor.active_job_count = editor.edit_jobs.filter(status__in=EditJob.ACTIVE_STATUSES).count()

    stats = {
        "editors_total": Editor.objects.count(),
        "editors_active": Editor.objects.filter(active=True).count(),
        "edit_jobs_open": EditJob.objects.filter(status__in=EditJob.ACTIVE_STATUSES).count(),
        "edit_jobs_failed": EditJob.objects.filter(status="failed").count(),
    }
    return render(request, "edits_dashboard.html", {
        "editors": editors,
        "jobs": jobs[:50],
        "stats": stats,
    })


def shoots_list(request):
    """All shoots, with optional ?filter=upcoming|past|cancelled|failed|confirmed|all"""
    filter_key = request.GET.get("filter", "all")
    qs = Shoot.objects.all().prefetch_related("invites__videographer")
    now = timezone.now()

    if filter_key == "upcoming":
        qs = qs.filter(shoot_datetime__gte=now).exclude(status="cancelled").order_by("shoot_datetime")
    elif filter_key == "past":
        qs = qs.filter(shoot_datetime__lt=now).order_by("-shoot_datetime")
    elif filter_key in {"pending", "confirmed", "failed", "cancelled"}:
        qs = qs.filter(status=filter_key).order_by("-shoot_datetime")
    else:
        filter_key = "all"
        qs = qs.order_by("-shoot_datetime")

    for s in qs:
        sent = list(s.invites.exclude(google_event_id="").order_by("rank"))
        s.current_invitee = sent[-1] if sent else None
        s.prior_invitees = sent[:-1] if len(sent) > 1 else []

    tabs = [
        ("all",       "All",       Shoot.objects.count()),
        ("upcoming",  "Upcoming",  Shoot.objects.filter(shoot_datetime__gte=now).exclude(status="cancelled").count()),
        ("past",      "Past",      Shoot.objects.filter(shoot_datetime__lt=now).count()),
        ("pending",   "Pending",   Shoot.objects.filter(status="pending").count()),
        ("confirmed", "Confirmed", Shoot.objects.filter(status="confirmed").count()),
        ("failed",    "Failed",    Shoot.objects.filter(status="failed").count()),
        ("cancelled", "Cancelled", Shoot.objects.filter(status="cancelled").count()),
    ]
    return render(request, "shoots_list.html", {
        "shoots": qs,
        "filter_key": filter_key,
        "tabs": tabs,
    })


def shoot_detail(request, shoot_id):
    shoot = get_object_or_404(
        Shoot.objects.prefetch_related("invites__videographer"),
        id=shoot_id,
    )
    invites = shoot.invites.all().order_by("rank")
    return render(request, "shoot_detail.html", {"shoot": shoot, "invites": invites})


@require_POST
def simulate_accept(request, invite_id):
    """Test helper: manually mark an invite accepted to simulate calendar acceptance."""
    invite = get_object_or_404(Invite, id=invite_id)
    if invite.status == "pending":
        _mark_accepted(invite)
    return redirect("shoot_detail", shoot_id=invite.shoot_id)


@require_POST
def simulate_escalate(request, invite_id):
    """Test helper: fire the escalation immediately instead of waiting for the 24h job."""
    invite = get_object_or_404(Invite, id=invite_id)
    if invite.status == "pending":
        check_and_escalate(invite.id)
    return redirect("shoot_detail", shoot_id=invite.shoot_id)


# ----------------------------------------------------------------------------
# Pipedrive webhook
# ----------------------------------------------------------------------------

def _check_pipedrive_auth(request) -> bool:
    """If PIPEDRIVE_WEBHOOK_SECRET is set, verify HTTP Basic auth matches it."""
    secret = settings.PIPEDRIVE_WEBHOOK_SECRET
    if not secret:
        return True  # auth not configured; allow (dev mode)
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    except Exception:
        return False
    return decoded == secret


def _detect_action(payload: dict) -> str:
    """Return one of 'create', 'change', 'delete', or '' if not recognized."""
    meta = payload.get("meta") or {}
    if meta.get("action"):
        return meta["action"]  # v2: 'create' | 'change' | 'delete'
    event_str = payload.get("event") or ""  # v1: 'added.activity' | 'updated.activity' | 'deleted.activity'
    if event_str.startswith("added"):
        return "create"
    if event_str.startswith("updated"):
        return "change"
    if event_str.startswith("deleted"):
        return "delete"
    return ""


def _parse_activity(payload: dict) -> dict | None:
    """
    Pull the activity fields we need out of a Pipedrive webhook payload.

    Pipedrive v1 shape: { "v": 1, "event": "added.activity", "current": {...activity...} }
    Pipedrive v2 shape: { "meta": {"action": "create", "entity": "activity"}, "data": {...activity...}, "previous": null }

    Returns dict with: type, pipedrive_deal_id, title, location, location_lat, location_lng,
                       location_state, shoot_datetime, notes
    """
    # --- pick out the activity record (handle both v1 and v2) ---
    data = payload.get("data") or payload.get("current") or {}
    if not isinstance(data, dict) or not data:
        return None

    type_name = data.get("type") or data.get("type_name") or ""
    subject   = data.get("subject") or ""
    # Pipedrive v2 sends the activity Description field as public_description.
    # Keep note/description fallbacks for older webhook shapes.
    note = _strip_html(
        data.get("public_description") or data.get("note") or data.get("description") or ""
    ).strip()
    deal_id   = data.get("deal_id")
    activity_id = data.get("id")

    # --- location: string (v1 legacy) OR structured object (v2) ---
    raw_loc = data.get("location")
    location_str = ""
    location_lat = None
    location_lng = None
    location_state = None
    location_city = None
    location_street = None
    if isinstance(raw_loc, dict):
        # Pipedrive v2: prefer human-friendly 'value', fall back to 'formatted_address'
        location_str = raw_loc.get("value") or raw_loc.get("formatted_address") or ""
        if raw_loc.get("lat") is not None and raw_loc.get("long") is not None:
            location_lat = float(raw_loc["lat"])
            location_lng = float(raw_loc["long"])
        location_state = _normalize_state(raw_loc.get("admin_area_level_1") or "")
        location_city = raw_loc.get("locality") or None
        # Build a clean "street_number route" string (e.g. "56 South Oakland Avenue")
        street_num = raw_loc.get("street_number") or ""
        route = raw_loc.get("route") or ""
        location_street = f"{street_num} {route}".strip() or None
    elif isinstance(raw_loc, str):
        location_str = raw_loc

    # --- datetime: due_date + due_time. Time can be string OR {'value': 'HH:MM:SS'} ---
    # When Pipedrive sends a real due_time, it is UTC. Date-only activities either
    # omit due_time or send 00:00:00, so keep those on the configured local date.
    due_date = data.get("due_date") or ""
    raw_due_time = _extract_value(data.get("due_time"))
    due_time_str = _parse_hhmm(raw_due_time)
    is_date_only = not raw_due_time or due_time_str == "00:00"
    shoot_dt = None
    if due_date:
        if is_date_only:
            shoot_dt = parse_datetime(f"{due_date}T00:00:00")
            if shoot_dt and timezone.is_naive(shoot_dt):
                shoot_dt = timezone.make_aware(shoot_dt, timezone.get_current_timezone())
        else:
            from datetime import timezone as _tz
            shoot_dt = parse_datetime(f"{due_date}T{due_time_str}:00")
            if shoot_dt and timezone.is_naive(shoot_dt):
                shoot_dt = timezone.make_aware(shoot_dt, _tz.utc)

    # --- duration: also may be {'value': 'HH:MM:SS'} ---
    duration_minutes = _parse_duration_minutes(_extract_value(data.get("duration")))

    if not shoot_dt:
        return None

    # Skip already-completed activities (e.g., webhook fires on backfill/edit)
    if data.get("done") is True:
        return None

    if not activity_id:
        # No activity_id = nothing to dedupe on. Skip rather than risk duplicates.
        return None

    return {
        "type": type_name,
        "pipedrive_deal_id": str(deal_id) if deal_id else None,
        "pipedrive_activity_id": str(activity_id),
        "title": subject or (f"Shoot @ {location_str}" if location_str else f"Activity {activity_id}"),
        "location": location_str,
        "location_lat": location_lat,
        "location_lng": location_lng,
        "location_state": location_state,
        "location_city": location_city,
        "location_street": location_street,
        "shoot_datetime": shoot_dt,
        "duration_minutes": duration_minutes,
        "notes": note,
    }


EDIT_ACTIVITY_TYPE_TO_VIDEO_TYPE = {
    "recruiting highlight video": "Recruiting",
    "hype video": "Hype",
    "highlight recap": "Highlight",
}


def _detect_edit_video_type(parsed: dict) -> str:
    return EDIT_ACTIVITY_TYPE_TO_VIDEO_TYPE.get(_normalize_type(parsed.get("type") or ""), "Unspecified")


def _activity_type_matches(raw_filter: str, activity_type: str) -> bool:
    filters = [_normalize_type(item) for item in (raw_filter or "").split(",") if item.strip()]
    if not filters:
        return False
    normalized_activity_type = _normalize_type(activity_type)
    return normalized_activity_type in filters


def _activity_matches_edit(parsed: dict, cfg: SchedulingSettings) -> bool:
    if not _activity_type_matches(cfg.edit_activity_type_filter, parsed["type"]):
        return False
    subject_filter = (cfg.edit_subject_filter or "").strip().lower()
    if subject_filter and subject_filter not in (parsed.get("title") or "").lower():
        return False
    return True


def _edit_kwargs(parsed: dict) -> dict:
    return {
        "pipedrive_deal_id": parsed["pipedrive_deal_id"],
        "pipedrive_activity_id": parsed.get("pipedrive_activity_id"),
        "title": parsed["title"],
        "video_type": _detect_edit_video_type(parsed),
        "due_datetime": parsed["shoot_datetime"],
        "duration_minutes": parsed.get("duration_minutes") or 0,
        "notes": parsed["notes"],
    }


def _ids_from_delete_payload(payload: dict) -> tuple[str | None, str | None]:
    """Returns (deal_id, activity_id) from a delete payload.
    Pipedrive's DELETE often omits deal_id; we get activity_id from data.id or meta.entity_id."""
    data = payload.get("data") or payload.get("current") or {}
    meta = payload.get("meta") or {}
    deal_id = None
    activity_id = None
    if isinstance(data, dict):
        if data.get("deal_id"):
            deal_id = str(data["deal_id"])
        if data.get("id"):
            activity_id = str(data["id"])
    if not activity_id and meta.get("entity_id"):
        activity_id = str(meta["entity_id"])
    return deal_id, activity_id


@csrf_exempt
@require_POST
def pipedrive_webhook(request):
    if not _check_pipedrive_auth(request):
        return HttpResponseForbidden("Bad auth")

    try:
        payload = json.loads(request.body.decode())
    except Exception:
        return HttpResponseBadRequest("Invalid JSON")

    action = _detect_action(payload)
    logger.info("Pipedrive webhook: action=%r", action)

    # --- DELETE ---
    if action == "delete":
        deal_id, activity_id = _ids_from_delete_payload(payload)
        if not (deal_id or activity_id):
            return JsonResponse({"status": "ignored", "reason": "delete payload had no usable ids"})
        shoot = handle_deleted_shoot(pipedrive_activity_id=activity_id, pipedrive_deal_id=deal_id)
        if shoot:
            return JsonResponse({"status": "ok", "action": "deleted", "shoot_id": shoot.id})
        edit_job = handle_deleted_edit_job(pipedrive_activity_id=activity_id, pipedrive_deal_id=deal_id)
        if edit_job:
            return JsonResponse({"status": "ok", "action": "deleted", "edit_job_id": edit_job.id})
        return JsonResponse({"status": "ignored", "reason": "no matching shoot or edit job to delete"})

    # --- CREATE / CHANGE need full parsed payload ---
    if action not in {"create", "change"}:
        return JsonResponse({"status": "ignored", "reason": f"action {action!r} not handled"})

    parsed = _parse_activity(payload)
    if not parsed:
        return JsonResponse({"status": "ignored", "reason": "no usable activity data"})

    cfg = SchedulingSettings.get()
    type_filter_normalized = _normalize_type(cfg.activity_type_filter)
    activity_type_normalized = _normalize_type(parsed["type"])
    type_matches = (not type_filter_normalized) or (type_filter_normalized in activity_type_normalized)
    edit_matches = _activity_matches_edit(parsed, cfg)

    if edit_matches:
        kwargs = _edit_kwargs(parsed)
        if action == "create":
            edit_job = handle_new_edit_job(**kwargs)
            return JsonResponse({"status": "ok", "action": "created", "edit_job_id": edit_job.id, "edit_job_status": edit_job.status})
        edit_job, taken = handle_updated_edit_job(**kwargs)
        return JsonResponse({"status": "ok", "action": taken, "edit_job_id": edit_job.id, "edit_job_status": edit_job.status})

    # Edge case: a 'change' event might be reporting that the type was changed AWAY
    # from a tracked workflow. Cancel only records that already exist.
    if not type_matches:
        if action == "change":
            existing_shoot = handle_deleted_shoot(
                pipedrive_activity_id=parsed.get("pipedrive_activity_id"),
            )
            if existing_shoot:
                return JsonResponse({"status": "ok", "action": "cancelled_due_to_type_change", "shoot_id": existing_shoot.id})
            existing_edit = handle_deleted_edit_job(
                pipedrive_activity_id=parsed.get("pipedrive_activity_id"),
            )
            if existing_edit:
                return JsonResponse({"status": "ok", "action": "cancelled_due_to_type_change", "edit_job_id": existing_edit.id})
        logger.info("Pipedrive webhook: activity type %r doesn't match filters, ignoring", parsed["type"])
        return JsonResponse({"status": "ignored", "reason": f"type {parsed['type']!r} doesn't match filters"})

    if not parsed["location"]:
        return JsonResponse({"status": "ignored", "reason": "shoot activity had no location"})

    kwargs = dict(
        pipedrive_deal_id=parsed["pipedrive_deal_id"],
        pipedrive_activity_id=parsed.get("pipedrive_activity_id"),
        title=parsed["title"],
        location=parsed["location"],
        shoot_datetime=parsed["shoot_datetime"],
        notes=parsed["notes"],
        prefilled_lat=parsed["location_lat"],
        prefilled_lng=parsed["location_lng"],
        prefilled_state=parsed["location_state"],
        location_city=parsed.get("location_city"),
        location_street=parsed.get("location_street"),
    )
    if parsed.get("duration_minutes"):
        kwargs["duration_minutes"] = parsed["duration_minutes"]

    if action == "create":
        shoot = handle_new_shoot(**kwargs)
        return JsonResponse({"status": "ok", "action": "created", "shoot_id": shoot.id, "shoot_status": shoot.status})

    # action == "change"
    shoot, taken = handle_updated_shoot(**kwargs)
    return JsonResponse({"status": "ok", "action": taken, "shoot_id": shoot.id, "shoot_status": shoot.status})
