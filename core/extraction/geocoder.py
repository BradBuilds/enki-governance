"""
Entity Geocoder — resolves location entity names to coordinates.

Two-tier approach:
  1. Gazetteer (instant, 200+ locations) — countries, capitals, major cities
  2. Nominatim API (slower, any location) — fallback for unknown locations

Called after entity creation to populate lat/lon on watch_topic_entities.
Also provides a batch function to backfill coordinates on existing entities.
"""

from __future__ import annotations

import logging
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.extraction.geocoder")

# Location entity types that should have coordinates
GEOCODABLE_TYPES = {"location", "country", "city", "region", "facility", "address"}


def geocode_entity(name: str, entity_type: str = "location") -> Optional[dict]:
    """Get coordinates for a location name. Returns {lat, lon, source} or None."""
    if entity_type not in GEOCODABLE_TYPES:
        return None

    # Tier 1: Gazetteer (instant)
    result = _gazetteer_lookup(name)
    if result:
        return result

    # Tier 2: Nominatim (API call)
    result = _nominatim_lookup(name)
    if result:
        return result

    return None


def geocode_and_update_entity(
    entity_id: str,
    name: str,
    entity_type: str,
    topic_id: str,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
):
    """Geocode an entity and update its coordinates in watch_topic_entities."""
    coords = geocode_entity(name, entity_type)
    if not coords:
        return

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE watch_topic_entities SET
                        latitude = %s, longitude = %s,
                        location = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                        updated_at = NOW()
                    WHERE entity_id = %s AND topic_id = %s
                """, (
                    coords["lat"], coords["lon"],
                    coords["lon"], coords["lat"],
                    entity_id, topic_id,
                ))
            conn.commit()
    except Exception as e:
        log.debug("Geocode update failed for %s: %s", name, e)


def backfill_coordinates(topic_id: str, tenant_id: str = "default",
                          dsn: Optional[str] = None) -> int:
    """Backfill coordinates on all location entities in a topic that lack lat/lon."""
    updated = 0
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT entity_id, name, entity_type
                    FROM watch_topic_entities
                    WHERE topic_id = %s AND tenant_id = %s
                      AND entity_type IN ('location', 'country', 'city', 'region', 'facility', 'address')
                      AND latitude IS NULL
                """, (topic_id, tenant_id))

                for entity_id, name, entity_type in cur.fetchall():
                    coords = geocode_entity(name, entity_type)
                    if coords:
                        cur.execute("""
                            UPDATE watch_topic_entities SET
                                latitude = %s, longitude = %s,
                                location = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                                updated_at = NOW()
                            WHERE entity_id = %s AND topic_id = %s
                        """, (
                            coords["lat"], coords["lon"],
                            coords["lon"], coords["lat"],
                            entity_id, topic_id,
                        ))
                        updated += 1

                # Update location_count
                cur.execute("""
                    UPDATE watch_topics SET
                        location_count = (
                            SELECT count(*) FROM watch_topic_entities
                            WHERE topic_id = %s AND latitude IS NOT NULL
                        ),
                        updated_at = NOW()
                    WHERE topic_id = %s
                """, (topic_id, topic_id))

            conn.commit()
    except Exception as e:
        log.debug("Geocode backfill failed for topic %s: %s", topic_id, e)

    return updated


def _gazetteer_lookup(name: str) -> Optional[dict]:
    """Fast lookup against the built-in gazetteer."""
    try:
        from core.extraction.gazetteer import _LOCATIONS
        key = name.lower().strip()
        if key in _LOCATIONS:
            lat, lon, loc_type = _LOCATIONS[key]
            return {"lat": lat, "lon": lon, "source": "gazetteer", "type": loc_type}

        # Try without common prefixes
        for prefix in ["the ", "republic of ", "state of "]:
            if key.startswith(prefix):
                stripped = key[len(prefix):]
                if stripped in _LOCATIONS:
                    lat, lon, loc_type = _LOCATIONS[stripped]
                    return {"lat": lat, "lon": lon, "source": "gazetteer", "type": loc_type}
    except ImportError:
        pass

    return None


def _nominatim_lookup(name: str) -> Optional[dict]:
    """Geocode via Nominatim/OpenStreetMap API."""
    try:
        from core.research.sources import nominatim_details
        result = nominatim_details(name)
        if result and result.get("latitude"):
            return {
                "lat": result["latitude"],
                "lon": result["longitude"],
                "source": "nominatim",
                "display_name": result.get("display_name"),
            }
    except Exception as e:
        log.debug("Nominatim geocode failed for %s: %s", name, e)

    return None
