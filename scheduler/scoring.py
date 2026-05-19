"""
Score and rank videographers for a shoot.

Formula:
    score = rating - (drive_minutes * SCORE_PENALTY_PER_MINUTE)

Filters applied:
    - Only active videographers
    - Only same-state (configurable via same_state_only kwarg)
    - Drop anyone over MAX_DRIVE_MINUTES from the shoot
"""
import logging
from dataclasses import dataclass
from .models import Videographer, SchedulingSettings
from .distance import estimate_drive
from .geocode import geocode_with_fallback

logger = logging.getLogger(__name__)


def _ensure_coords(v: Videographer) -> bool:
    """If a videographer is missing coords, try to geocode them on the spot.
    Returns True if coords are available afterward."""
    if v.lat is not None and v.lng is not None:
        return True
    candidates = []
    if v.address:
        candidates.append(v.address)
    if v.city and v.state:
        candidates.append(f"{v.city}, {v.state}")
    elif v.city:
        candidates.append(v.city)
    if not candidates:
        return False
    result = geocode_with_fallback(candidates)
    if not result:
        logger.warning("Runtime geocode failed for %s (tried %s)", v.name, candidates)
        return False
    (lat, lng), used = result
    v.lat, v.lng = lat, lng
    v.save(update_fields=["lat", "lng"])
    logger.info("Runtime-geocoded %s -> (%s, %s) via %r", v.name, lat, lng, used)
    return True


@dataclass
class ScoredVideographer:
    videographer: Videographer
    score: float
    drive_miles: float
    drive_minutes: float

    def __repr__(self):
        v = self.videographer
        return (f"{v.name:22s} {v.state} {v.rating}★  "
                f"{self.drive_miles:5.1f}mi / {self.drive_minutes:5.1f}min  "
                f"score={self.score:.3f}")


def rank_for_shoot(
    shoot_lat: float,
    shoot_lng: float,
    shoot_state: str | None = None,
    same_state_only: bool | None = None,
) -> list[ScoredVideographer]:
    """
    Returns videographers ranked best-first for a shoot.
    Anyone over MAX_DRIVE_MINUTES is filtered out entirely.
    """
    cfg = SchedulingSettings.get()
    penalty = cfg.score_penalty_per_minute
    max_min = cfg.max_drive_minutes
    if same_state_only is None:
        same_state_only = cfg.same_state_only

    qs = Videographer.objects.filter(active=True, lat__isnull=False, lng__isnull=False)
    if same_state_only and shoot_state:
        qs = qs.filter(state=shoot_state)

    scored = []
    for v in qs:
        miles, minutes = estimate_drive(shoot_lat, shoot_lng, v.lat, v.lng)
        if minutes > max_min:
            continue
        score = v.rating - (minutes * penalty)
        scored.append(ScoredVideographer(v, score, miles, minutes))

    scored.sort(key=lambda s: -s.score)
    return scored
