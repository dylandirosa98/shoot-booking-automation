"""
Distance + drive-time estimation, free, no API keys.

- Haversine for straight-line distance between two lat/lng pairs.
- Estimate driving distance ~= straight-line * 1.3 (typical road detour).
- Estimate drive time using an average speed of 45 mph (mix of city/highway).

Accuracy: within ~15-20% of Google Maps for typical regional drives.
Good enough for "is this person 30 min or 2 hours away" decisions.
"""
from math import radians, sin, cos, asin, sqrt

# Tunable constants
ROAD_FACTOR = 1.3        # roads aren't straight lines
AVG_SPEED_MPH = 45.0     # mix of highway + local driving


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in miles between two lat/lng points."""
    EARTH_RADIUS_MI = 3958.8
    lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_MI * asin(sqrt(a))


def estimate_drive(lat1: float, lng1: float, lat2: float, lng2: float) -> tuple[float, float]:
    """
    Returns (estimated_drive_miles, estimated_drive_minutes).
    """
    straight = haversine_miles(lat1, lng1, lat2, lng2)
    miles = straight * ROAD_FACTOR
    minutes = (miles / AVG_SPEED_MPH) * 60
    return miles, minutes
