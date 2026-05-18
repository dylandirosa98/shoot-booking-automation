"""
Free geocoding via Nominatim (OpenStreetMap).

No API key required. Rate limit: 1 request/second (we cache results to stay polite).
Used only for shoot locations from Pipedrive (videographer coords are baked into seed).
"""
import time
import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "videographer-scheduling-automation/1.0 (hockey media scheduler)"

_last_request_time = 0.0


def geocode(address: str) -> tuple[float, float] | None:
    """Single-query geocode. Returns (lat, lng) or None."""
    global _last_request_time
    if not address or not address.strip():
        return None

    elapsed = time.time() - _last_request_time
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)

    try:
        resp = httpx.get(
            NOMINATIM_URL,
            params={"q": address.strip(), "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        _last_request_time = time.time()
        results = resp.json()
        if not results:
            return None
        return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        return None


def geocode_with_fallback(candidates: list[str]) -> tuple[tuple[float, float], str] | None:
    """
    Try each candidate in order; return first match as ((lat, lng), query_used).
    Use this to cascade: full address -> street+city -> city+state.
    """
    seen = set()
    for q in candidates:
        if not q:
            continue
        key = q.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        coords = geocode(q)
        if coords:
            return coords, q
    return None
