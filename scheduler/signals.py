"""
Auto-geocode a Videographer's city when lat/lng aren't set.

Runs whenever a Videographer is saved (including from the admin form).
Uses the same Nominatim fallback chain we use for shoot locations.
"""
import logging
from django.db.models.signals import pre_save
from django.dispatch import receiver
from .models import Videographer
from .geocode import geocode_with_fallback

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Videographer)
def auto_geocode_videographer(sender, instance: Videographer, **kwargs):
    # Skip if coords already set (manual override always wins)
    if instance.lat is not None and instance.lng is not None:
        return

    # Build a cascade of address candidates
    candidates = []
    if instance.address:
        candidates.append(instance.address)
    if instance.city and instance.state:
        candidates.append(f"{instance.city}, {instance.state}")
    elif instance.city:
        candidates.append(instance.city)
    if not candidates:
        logger.warning("Videographer %r has no address/city to geocode", instance.name)
        return

    result = geocode_with_fallback(candidates)
    if result:
        (lat, lng), used = result
        instance.lat = lat
        instance.lng = lng
        logger.info("Auto-geocoded %s -> (%s, %s) using %r", instance.name, lat, lng, used)
    else:
        logger.warning("Could not auto-geocode %s (tried %s)", instance.name, candidates)
