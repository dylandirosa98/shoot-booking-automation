"""
Booking orchestrator: the heart of the app.

Flow:
    1. Webhook calls handle_new_shoot(payload)
    2. Geocode the location
    3. Rank videographers
    4. Save Shoot row + create first Invite
    5. Send calendar invite (via calendar_client)
    6. Schedule a 24h escalation job
    7. When 24h job fires: check acceptance, escalate if needed
"""
import logging
import re
from datetime import datetime, timedelta
from django.db import transaction
from django.utils import timezone
from .models import Videographer, Shoot, Invite, SchedulingSettings
from .geocode import geocode, geocode_with_fallback
from . import notify
from .scoring import rank_for_shoot
from .calendar_client import get_client
from .drive_client import get_drive_client
from .distance import estimate_drive

logger = logging.getLogger(__name__)


# --- helpers ---

def _state_from_location(location: str) -> str | None:
    """Best-effort 2-letter state extraction from an address string."""
    parts = [p.strip() for p in location.split(",")]
    for p in reversed(parts):
        token = p.split()[0] if p else ""
        if len(token) == 2 and token.isalpha():
            return token.upper()
    return None


def _invite_message(shoot: Shoot, videographer: Videographer, miles: float, minutes: float) -> str:
    first_name = videographer.name.split()[0] if videographer.name else "there"
    sections = [
        f"Hi {first_name},",
        "You're our top pick for an upcoming hockey shoot. Details:",
        (f"  Location: {shoot.location}\n"
         f"  Distance: ~{miles:.0f} mi (~{minutes:.0f} min drive)"),
    ]
    if shoot.notes and shoot.notes.strip():
        sections.append("Shoot details / notes from the team:\n" + shoot.notes.strip())
    sections.append(
        "Please accept or decline this calendar invite within 24 hours.\n"
        "If we don't hear back, we'll offer it to the next videographer."
    )
    return "\n\n".join(sections)


def _drive_folder_name(shoot: Shoot) -> str:
    """A human-readable folder name based on the required location and time."""
    local_start = timezone.localtime(shoot.shoot_datetime)
    return f"{shoot.location} — {local_start:%Y-%m-%d %-I:%M %p}"


def _ensure_drive_folder_shared(invite: Invite) -> bool:
    """Create, persist, and share the confirmed shoot folder exactly once."""
    try:
        with transaction.atomic():
            invite = (Invite.objects.select_for_update()
                      .select_related("shoot", "videographer")
                      .get(id=invite.id))
            shoot = Shoot.objects.select_for_update().get(id=invite.shoot_id)
            if invite.status != "accepted" or shoot.confirmed_videographer_id != invite.videographer_id:
                return False

            drive = get_drive_client()
            if not shoot.google_drive_folder_id:
                folder = drive.create_folder(name=_drive_folder_name(shoot), shoot_id=shoot.id)
                shoot.google_drive_folder_id = folder.id
                shoot.google_drive_folder_url = folder.url
                shoot.google_drive_error = ""
                shoot.save(update_fields=[
                    "google_drive_folder_id", "google_drive_folder_url", "google_drive_error",
                ])

            if not invite.google_drive_permission_id:
                invite.google_drive_permission_id = drive.share_folder(
                    folder_id=shoot.google_drive_folder_id,
                    email=invite.videographer.email,
                )
                invite.save(update_fields=["google_drive_permission_id"])
            return True
    except Exception as exc:
        logger.exception("Failed to create/share Drive folder for accepted invite %s", invite.id)
        Shoot.objects.filter(id=invite.shoot_id).update(google_drive_error=str(exc)[:2000])
        return False


def _remove_drive_access(invite: Invite) -> None:
    """Remove the direct share when an accepted videographer later declines."""
    if not invite.google_drive_permission_id or not invite.shoot.google_drive_folder_id:
        return
    try:
        get_drive_client().remove_permission(
            folder_id=invite.shoot.google_drive_folder_id,
            permission_id=invite.google_drive_permission_id,
        )
        invite.google_drive_permission_id = ""
        invite.save(update_fields=["google_drive_permission_id"])
    except Exception:
        logger.exception("Failed to remove Drive access for invite %s", invite.id)


# --- main entry point ---

def handle_updated_shoot(
    *,
    pipedrive_deal_id: str,
    pipedrive_activity_id: str | None = None,
    title: str,
    location: str,
    shoot_datetime: datetime,
    duration_minutes: int = 120,
    notes: str = "",
    prefilled_lat: float | None = None,
    prefilled_lng: float | None = None,
    prefilled_state: str | None = None,
    location_city: str | None = None,
    location_street: str | None = None,
) -> tuple[Shoot, str]:
    """
    Pipedrive activity 'change' event. Returns (shoot, action_taken).
    action_taken is one of: 'created', 'updated_in_place', 'restarted', 'confirmed_warning', 'noop'
    """
    existing = None
    if pipedrive_activity_id:
        existing = Shoot.objects.filter(pipedrive_activity_id=pipedrive_activity_id).first()

    # Never seen → treat like create (e.g. activity type just changed to Shoot Booking)
    if not existing:
        shoot = handle_new_shoot(
            pipedrive_deal_id=pipedrive_deal_id, pipedrive_activity_id=pipedrive_activity_id,
            title=title, location=location,
            shoot_datetime=shoot_datetime, duration_minutes=duration_minutes, notes=notes,
            prefilled_lat=prefilled_lat, prefilled_lng=prefilled_lng, prefilled_state=prefilled_state,
            location_city=location_city, location_street=location_street,
        )
        return shoot, "created"

    # Detect material changes (location or datetime)
    location_changed = (existing.location or "").strip() != (location or "").strip()
    time_changed = existing.shoot_datetime != shoot_datetime

    # Always patch shallow fields
    existing.title = title or existing.title
    existing.notes = notes
    existing.duration_minutes = duration_minutes or existing.duration_minutes
    if pipedrive_activity_id and not existing.pipedrive_activity_id:
        existing.pipedrive_activity_id = pipedrive_activity_id

    if existing.status in {"confirmed", "failed", "cancelled"}:
        # Don't reshuffle; just update fields. If time/location changed, log a warning.
        if location_changed or time_changed:
            existing.location = location
            existing.shoot_datetime = shoot_datetime
            logger.warning(
                "Shoot %s is %s but location/time changed in Pipedrive. "
                "Manual follow-up may be needed (will be auto-handled once real calendar is wired).",
                existing.id, existing.status,
            )
        existing.save()
        return existing, "confirmed_warning" if (location_changed or time_changed) else "noop"

    # Still pending - if nothing material changed, just save the patch and move on
    if not (location_changed or time_changed):
        existing.save()
        return existing, "updated_in_place"

    # Material change while pending → cancel pending invites + restart the ranking
    logger.info(
        "Shoot %s changed (location_changed=%s, time_changed=%s) while pending; restarting invite chain",
        existing.id, location_changed, time_changed,
    )

    cal = get_client()
    for inv in existing.invites.all():
        if inv.status == "pending" and inv.google_event_id:
            cal.cancel_event(inv.google_event_id)
        if inv.status == "pending":
            inv.status = "cancelled"
            inv.save(update_fields=["status"])

    # Update shoot with new info, redo geocoding + ranking (with fallback cascade)
    if prefilled_lat is not None and prefilled_lng is not None:
        lat, lng = prefilled_lat, prefilled_lng
    else:
        result = geocode_with_fallback(
            _geocode_candidates(location, location_city, location_street, prefilled_state)
        )
        if not result:
            existing.status = "failed"
            existing.save()
            return existing, "noop"
        (lat, lng), _used = result
    state = prefilled_state or _state_from_location(location)

    existing.location = location
    existing.lat = lat
    existing.lng = lng
    existing.shoot_datetime = shoot_datetime
    existing.status = "pending"
    existing.save()

    ranked = rank_for_shoot(lat, lng, shoot_state=state)
    if not ranked:
        existing.status = "failed"
        existing.save(update_fields=["status"])
        return existing, "noop"

    cfg = SchedulingSettings.get()
    expires = timezone.now() + timedelta(hours=cfg.escalation_hours)

    # Wipe and recreate the invite chain so ranks reflect the new location
    existing.invites.exclude(status__in=["accepted"]).delete()
    for rank, scored in enumerate(ranked):
        Invite.objects.create(
            shoot=existing, videographer=scored.videographer, rank=rank,
            score=scored.score, drive_miles=scored.drive_miles,
            drive_minutes=scored.drive_minutes, status="pending",
            expires_at=expires if rank == 0 else timezone.now(),
        )

    _send_invite(existing, rank=0)
    return existing, "restarted"


def handle_deleted_shoot(pipedrive_activity_id: str | None = None,
                         pipedrive_deal_id: str | None = None) -> Shoot | None:
    """
    Pipedrive activity 'delete' event. Cancel all pending invites + mark shoot cancelled.
    Look up by activity_id (primary), fall back to deal_id only if needed.
    """
    shoot = None
    if pipedrive_activity_id:
        shoot = Shoot.objects.filter(pipedrive_activity_id=pipedrive_activity_id).first()
    if not shoot and pipedrive_deal_id:
        shoot = Shoot.objects.filter(pipedrive_deal_id=pipedrive_deal_id).first()
    if not shoot:
        logger.info("Delete webhook for unknown shoot (activity=%s deal=%s), ignoring",
                    pipedrive_activity_id, pipedrive_deal_id)
        return None

    cal = get_client()
    cancelled_count = 0
    for inv in shoot.invites.filter(status="pending"):
        if inv.google_event_id:
            cal.cancel_event(inv.google_event_id)
        inv.status = "cancelled"
        inv.save(update_fields=["status"])
        cancelled_count += 1

    if shoot.status != "confirmed":
        shoot.status = "cancelled"
        shoot.save(update_fields=["status"])
    logger.info("Shoot %s deleted in Pipedrive; cancelled %d pending invites", shoot.id, cancelled_count)
    return shoot


_CITY_STATE_RE = re.compile(r",\s*([A-Za-z][A-Za-z .'\-]+?),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?(?:,\s*USA)?\s*$")


def _parse_city_state(location: str) -> tuple[str, str] | None:
    """Pull trailing 'City, ST' out of a Pipedrive-formatted address string."""
    if not location:
        return None
    m = _CITY_STATE_RE.search(location.strip())
    if not m:
        return None
    return m.group(1).strip(), m.group(2).upper()


def _geocode_candidates(location: str, city: str | None, street: str | None, state: str | None) -> list[str]:
    """Build a cascade: full address → street+city+state → city+state. Skips empties + dupes."""
    candidates = [location]
    if street and city and state:
        candidates.append(f"{street}, {city}, {state}")
    elif street and city:
        candidates.append(f"{street}, {city}")
    if city and state:
        candidates.append(f"{city}, {state}")
    elif city:
        candidates.append(city)

    # Last-resort fallback: when Pipedrive doesn't send structured city/state,
    # parse the trailing "City, ST" out of the raw location string. Prefer the
    # caller-supplied state if it disagrees — it's more authoritative.
    parsed = _parse_city_state(location)
    if parsed:
        parsed_city, parsed_state = parsed
        final_state = state or parsed_state
        candidates.append(f"{parsed_city}, {final_state}")

    seen: set[str] = set()
    deduped: list[str] = []
    for c in candidates:
        key = c.strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(c)
    return deduped


def handle_new_shoot(
    *,
    pipedrive_deal_id: str,
    pipedrive_activity_id: str | None = None,
    title: str,
    location: str,
    shoot_datetime: datetime,
    duration_minutes: int = 120,
    notes: str = "",
    prefilled_lat: float | None = None,
    prefilled_lng: float | None = None,
    prefilled_state: str | None = None,
    location_city: str | None = None,
    location_street: str | None = None,
) -> Shoot:
    """
    Called by the Pipedrive webhook. Idempotent on pipedrive_activity_id
    (each Pipedrive activity = one shoot; a single deal can have multiple shoots).
    """
    # Dedupe by activity_id — the actual unique key
    if pipedrive_activity_id:
        existing = Shoot.objects.filter(pipedrive_activity_id=pipedrive_activity_id).first()
        if existing:
            logger.info("Shoot for activity %s already exists (id=%s), skipping",
                        pipedrive_activity_id, existing.id)
            return existing

    # Coords: prefer Pipedrive's lat/lng, then cascade geocode (full -> street+city -> city+state)
    if prefilled_lat is not None and prefilled_lng is not None:
        lat, lng = prefilled_lat, prefilled_lng
        logger.info("Using Pipedrive-provided coords for %r: (%s, %s)", location, lat, lng)
    else:
        candidates = _geocode_candidates(location, location_city, location_street, prefilled_state)
        result = geocode_with_fallback(candidates)
        if not result:
            logger.error("Could not geocode any candidate for shoot: %s (tried %s)", location, candidates)
            shoot = Shoot.objects.create(
                pipedrive_deal_id=pipedrive_deal_id, pipedrive_activity_id=pipedrive_activity_id,
                title=title, location=location,
                shoot_datetime=shoot_datetime, duration_minutes=duration_minutes,
                notes=notes, status="failed",
            )
            return shoot
        (lat, lng), used = result
        if used != location:
            logger.warning("Couldn't geocode %r directly; fell back to %r", location, used)

    # State: prefer Pipedrive's structured admin_area_level_1, fall back to text parsing
    state = prefilled_state or _state_from_location(location)

    shoot = Shoot.objects.create(
        pipedrive_deal_id=pipedrive_deal_id, pipedrive_activity_id=pipedrive_activity_id,
        title=title, location=location,
        lat=lat, lng=lng, shoot_datetime=shoot_datetime, duration_minutes=duration_minutes,
        notes=notes, status="pending",
    )

    # Rank
    ranked = rank_for_shoot(lat, lng, shoot_state=state)
    if not ranked:
        logger.warning("No eligible videographers for shoot %s", shoot.id)
        shoot.status = "failed"
        shoot.save(update_fields=["status"])
        notify.shoot_failed(shoot)
        return shoot

    # Save all invites as records (rank 0..N) but only send to #0
    cfg = SchedulingSettings.get()
    expires = timezone.now() + timedelta(hours=cfg.escalation_hours)
    for rank, scored in enumerate(ranked):
        Invite.objects.create(
            shoot=shoot, videographer=scored.videographer, rank=rank,
            score=scored.score, drive_miles=scored.drive_miles,
            drive_minutes=scored.drive_minutes, status="pending",
            expires_at=expires if rank == 0 else timezone.now(),
        )

    _send_invite(shoot, rank=0)
    return shoot


def _send_invite(shoot: Shoot, rank: int, reuse_event_id: str | None = None) -> Invite | None:
    """Send the calendar invite to the videographer at this rank."""
    invite = shoot.invites.filter(rank=rank).first()
    if not invite:
        return None

    description = _invite_message(shoot, invite.videographer, invite.drive_miles or 0, invite.drive_minutes or 0)
    try:
        cal = get_client()
        if reuse_event_id:
            event_id = cal.replace_event_attendee(
                event_id=reuse_event_id,
                description=description,
                attendee_email=invite.videographer.email,
            )
        else:
            end = shoot.shoot_datetime + timedelta(minutes=shoot.duration_minutes)
            event_id = cal.create_event(
                summary=f"Hockey shoot - {shoot.title or shoot.location}",
                description=description,
                location=shoot.location,
                start=shoot.shoot_datetime,
                end=end,
                attendee_email=invite.videographer.email,
            )
    except Exception as exc:
        invite.calendar_error = str(exc)[:2000]
        invite.calendar_last_attempt_at = timezone.now()
        invite.save(update_fields=["calendar_error", "calendar_last_attempt_at"])
        logger.exception("Calendar invite send failed for invite %s", invite.id)
        return None

    cfg = SchedulingSettings.get()
    invite.google_event_id = event_id
    invite.expires_at = timezone.now() + timedelta(hours=cfg.escalation_hours)
    invite.calendar_error = ""
    invite.calendar_last_attempt_at = timezone.now()
    invite.save(update_fields=[
        "google_event_id", "expires_at", "calendar_error", "calendar_last_attempt_at",
    ])

    # Schedule the escalation check
    from .jobs import schedule_escalation_check
    schedule_escalation_check(invite.id)

    logger.info("Invite sent to %s for shoot %s (rank %d, event %s)",
                invite.videographer.name, shoot.id, rank, event_id)
    return invite


def check_and_escalate(invite_id: int) -> None:
    """
    Runs when an invite's 24h window expires. If still pending, mark
    expired and send to the next rank.
    """
    try:
        invite = Invite.objects.select_related("shoot", "videographer").get(id=invite_id)
    except Invite.DoesNotExist:
        return

    if invite.status != "pending":
        logger.info("Escalation check: invite %s is %s, skipping", invite.id, invite.status)
        return

    cal = get_client()
    status = cal.get_attendee_status(invite.google_event_id, invite.videographer.email)
    logger.info("Escalation check: invite %s, attendee status=%s", invite.id, status)

    if status == "accepted":
        _mark_accepted(invite)
        return

    # Otherwise expire + move the same calendar event to the next videographer.
    _move_event_to_next_invite(invite, cal, "declined" if status == "declined" else "expired")


def poll_all_pending_invites() -> None:
    """
    Runs every few minutes. Looks at:
      - PENDING invites (videographer hasn't responded yet)
      - ACCEPTED invites for shoots that haven't started yet (in case they flip to decline)

    For pending invites:
      accepted   -> confirm the shoot, cancel other invites
      declined   -> mark declined, escalate to next rank immediately
      needsAction-> nothing (wait for 24h timer)

    For accepted invites (already-confirmed shoots in the future):
      accepted   -> nothing (still good)
      declined   -> reset shoot to pending, recover the next available videographer
      needsAction-> nothing (treat as still accepted until they explicitly decline)
    """
    now = timezone.now()
    # A failed Calendar create has no event ID, so it is invisible to the
    # response-status poll below. Retry only the first unsent pending invite
    # per shoot; later ranks remain queued until it is resolved.
    unsent_invites = (Invite.objects
                      .filter(status="pending", google_event_id="", shoot__status="pending",
                              shoot__shoot_datetime__gte=now)
                      .select_related("shoot")
                      .order_by("shoot_id", "rank"))
    shoots_with_active_event = set(
        Invite.objects.filter(status="pending", google_event_id__gt="", shoot__shoot_datetime__gte=now)
        .values_list("shoot_id", flat=True)
    )
    retried_shoot_ids: set[int] = set()
    for invite in unsent_invites:
        if invite.shoot_id in shoots_with_active_event or invite.shoot_id in retried_shoot_ids:
            continue
        logger.warning("Retrying Calendar invite %s after prior failure: %s", invite.id, invite.calendar_error)
        _send_invite(invite.shoot, rank=invite.rank)
        retried_shoot_ids.add(invite.shoot_id)

    # Anything we still care about: pending OR accepted, and shoot hasn't started
    invites = (Invite.objects
               .filter(status__in=["pending", "accepted"],
                       shoot__shoot_datetime__gte=now)
               .exclude(google_event_id="")
               .select_related("shoot", "videographer"))
    if not invites.exists():
        return

    cal = get_client()
    for invite in invites:
        try:
            status = cal.get_attendee_status(invite.google_event_id, invite.videographer.email)
        except Exception as e:
            logger.exception("poll: failed to read status for invite %s: %s", invite.id, e)
            continue

        # --- Pending invite paths ---
        if invite.status == "pending":
            if status == "accepted":
                logger.info("poll: invite %s ACCEPTED by %s", invite.id, invite.videographer.email)
                _mark_accepted(invite)
            elif status == "declined":
                logger.info("poll: invite %s DECLINED by %s, escalating", invite.id, invite.videographer.email)
                _decline_and_escalate(invite, cal)

        # --- Accepted invite paths (watch for reversals) ---
        elif invite.status == "accepted":
            if status == "declined":
                logger.warning(
                    "poll: previously-accepted invite %s now DECLINED by %s; reopening shoot",
                    invite.id, invite.videographer.email,
                )
                _handle_post_acceptance_decline(invite, cal)
            else:
                # Retry a transient Drive error without sending a duplicate share.
                _ensure_drive_folder_shared(invite)


def _decline_and_escalate(invite: Invite, cal) -> None:
    """Mark invite declined and move the same calendar event to the next available person."""
    _move_event_to_next_invite(invite, cal, "declined")


def manual_send_to_videographer(shoot: Shoot, videographer: Videographer) -> Invite:
    """
    Move the currently-active invite out of the way and send the shoot to the
    selected videographer next. The rest of the queued chain keeps its order.
    """
    with transaction.atomic():
        shoot = Shoot.objects.select_for_update().get(id=shoot.id)
        invites = list(
            Invite.objects.select_for_update()
            .filter(shoot=shoot)
            .select_related("videographer")
            .order_by("rank", "id")
        )

        current = next(
            (invite for invite in invites if invite.status == "pending" and invite.google_event_id),
            None,
        )
        event_id = current.google_event_id if current else None

        existing = next((invite for invite in invites if invite.videographer_id == videographer.id), None)
        if existing and existing.status in {"accepted", "expired"}:
            raise ValueError("That videographer already has a completed invite for this shoot.")
        if existing and existing == current:
            raise ValueError("That videographer is already the active invitee.")

        if current:
            current.status = "declined"
            current.responded_at = timezone.now()
            current.save(update_fields=["status", "responded_at"])

        if existing:
            selected = existing
            if selected.status == "declined":
                selected.status = "pending"
                selected.responded_at = None
                selected.save(update_fields=["status", "responded_at"])
        else:
            cfg = SchedulingSettings.get()
            drive_miles = None
            drive_minutes = None
            score = videographer.rating
            if shoot.lat is not None and shoot.lng is not None and videographer.lat is not None and videographer.lng is not None:
                drive_miles, drive_minutes = estimate_drive(shoot.lat, shoot.lng, videographer.lat, videographer.lng)
                score = videographer.rating - (drive_minutes * cfg.score_penalty_per_minute)
            selected = Invite.objects.create(
                shoot=shoot,
                videographer=videographer,
                rank=len(invites),
                score=score,
                drive_miles=drive_miles,
                drive_minutes=drive_minutes,
                status="pending",
                expires_at=timezone.now(),
            )
            invites.append(selected)

        completed_before = [
            invite for invite in invites
            if invite.id != selected.id and invite.status in {"declined", "expired", "accepted"}
        ]
        active_position = len(completed_before)
        queued_after = [
            invite for invite in invites
            if invite.id != selected.id and invite.status in {"pending", "cancelled"}
        ]
        ordered = completed_before + [selected] + queued_after
        for rank, invite in enumerate(ordered):
            if invite.rank != rank:
                invite.rank = rank
                invite.save(update_fields=["rank"])

        shoot.status = "pending"
        shoot.confirmed_videographer = None
        shoot.save(update_fields=["status", "confirmed_videographer"])

    return _send_invite(shoot, rank=active_position, reuse_event_id=event_id)


def reorder_queued_invites(shoot: Shoot, invite_ids: list[int]) -> None:
    """
    Reorder only unsent queued invites. Sent history and the active invite stay
    fixed so the audit trail remains readable.
    """
    with transaction.atomic():
        invites = list(
            Invite.objects.select_for_update()
            .filter(shoot=shoot)
            .order_by("rank", "id")
        )
        queued = [invite for invite in invites if invite.status in {"pending", "cancelled"} and not invite.google_event_id]
        queued_ids = [invite.id for invite in queued]
        if sorted(queued_ids) != sorted(invite_ids):
            raise ValueError("Queued invite list does not match the shoot's reorderable invites.")

        by_id = {invite.id: invite for invite in queued}
        ordered_queued = [by_id[invite_id] for invite_id in invite_ids]
        ordered = []
        queue_iter = iter(ordered_queued)
        for invite in invites:
            if invite.id in by_id:
                ordered.append(next(queue_iter))
            else:
                ordered.append(invite)

        for rank, invite in enumerate(ordered):
            if invite.rank != rank:
                invite.rank = rank
                invite.save(update_fields=["rank"])


def _move_event_to_next_invite(invite: Invite, cal, final_status: str) -> Invite | None:
    invite.status = final_status
    invite.responded_at = timezone.now()
    invite.save(update_fields=["status", "responded_at"])

    next_invite = _next_available_invite(invite.shoot, after_rank=invite.rank)
    if next_invite:
        return _send_invite(invite.shoot, rank=next_invite.rank, reuse_event_id=invite.google_event_id)

    if invite.google_event_id:
        cal.cancel_event(invite.google_event_id)
    invite.shoot.status = "failed"
    invite.shoot.save(update_fields=["status"])
    logger.warning("exhausted all videographers for shoot %s", invite.shoot.id)
    notify.shoot_failed(invite.shoot)
    return None


def _handle_post_acceptance_decline(invite: Invite, cal) -> None:
    """
    Someone accepted then changed their mind. Roll back the shoot to 'pending'
    and find the next available person. Previously-cancelled people are
    eligible again because their cancellation only happened because we thought
    we had someone.
    """
    _remove_drive_access(invite)
    invite.status = "declined"
    invite.responded_at = timezone.now()
    invite.save(update_fields=["status", "responded_at"])

    shoot = invite.shoot
    shoot.status = "pending"
    shoot.confirmed_videographer = None
    shoot.save(update_fields=["status", "confirmed_videographer"])

    # Find the next person to ask and move the same calendar event forward.
    next_invite = _next_available_invite(shoot, after_rank=invite.rank)
    if next_invite:
        _send_invite(shoot, rank=next_invite.rank, reuse_event_id=invite.google_event_id)
    else:
        if invite.google_event_id:
            cal.cancel_event(invite.google_event_id)
        shoot.status = "failed"
        shoot.save(update_fields=["status"])
        logger.warning("post-acceptance decline: no fallback available for shoot %s", shoot.id)
        notify.shoot_failed(shoot)


def _next_available_invite(shoot: Shoot, after_rank: int) -> Invite | None:
    """
    Find the lowest-rank invite that hasn't already declined / accepted-and-bailed.
    Looks at ranks > after_rank first, then wraps to cancelled invites at any rank
    that weren't already explicitly declined.
    """
    # First: try the next rank in the chain
    n = shoot.invites.filter(rank__gt=after_rank).order_by("rank").first()
    if n and n.status in {"pending", "cancelled"}:
        return n
    # Otherwise: re-offer to anyone we previously cancelled (they never got asked)
    return (shoot.invites
            .filter(status="cancelled")
            .exclude(rank=after_rank)
            .order_by("rank")
            .first())


def _mark_accepted(invite: Invite) -> None:
    invite.status = "accepted"
    invite.responded_at = timezone.now()
    invite.save(update_fields=["status", "responded_at"])

    invite.shoot.status = "confirmed"
    invite.shoot.confirmed_videographer = invite.videographer
    invite.shoot.save(update_fields=["status", "confirmed_videographer"])

    # Cancel any other pending invites for this shoot
    other_pending = invite.shoot.invites.filter(status="pending").exclude(id=invite.id)
    cal = get_client()
    for other in other_pending:
        if other.google_event_id:
            cal.cancel_event(other.google_event_id)
        other.status = "cancelled"
        other.save(update_fields=["status"])

    _ensure_drive_folder_shared(invite)

    logger.info("Shoot %s confirmed with %s", invite.shoot.id, invite.videographer.name)
    # TODO: notify owner
