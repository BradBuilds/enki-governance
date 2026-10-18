"""
Place resolver — admits each place_text mention through the gateway
(same path as every other referent) and writes the spatial sidecar.

POST-UNIFICATION (migration 118): places are entities. The resolver no
longer writes a separate places table. Flow:

  1. For each place name in envelope.spatial.place_text:
       a. Gazetteer lookup → lat/lon + canonical_type (city/region/country/place)
       b. gateway.admit_entity(name, canonical_type) — this writes the
          entities row AND the base-60 substrate (form_node + concept_node
          + form_concept_binding) via the Phase 2 lockdown.
       c. UPSERT place_attributes(entity_id, coordinates, ...) — the
          spatial sidecar for this place.
  2. Stamp envelope.spatial.resolved_places with "entity:NNN" ids.

The substrate writes mean a future ambiguous "Toronto" mention will see
the form's existing bindings and route to the right concept — the same
mechanism Sam Rowan uses, applied to places.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.extraction.resolvers.base import normalize_name
from core.ldu.lineage import get_lineage_stamp
from core.extraction import gazetteer as _gz

log = logging.getLogger("enki.extraction.resolvers.place")

# Gazetteer type → canonical_type (nodevocab place subtype)
_GZ_TO_TYPE = {
    "country": "country",
    "capital": "city",
    "city":    "city",
    "region":  "region",
}


def _gazetteer_lookup(name: str) -> Optional[dict]:
    """Return {lat, lon, type} for a name, or None if the gazetteer
    doesn't know it. Exposed here so the resolver is independent of
    the place_extractor's return shape."""
    if not name:
        return None
    key = name.strip().lower()
    entry = _gz._LOCATIONS.get(key)
    if entry is None:
        return None
    lat, lon, loc_type = entry
    return {"lat": lat, "lon": lon, "type": loc_type}


def _upsert_place_attributes(
    conn,
    *,
    entity_id: int,
    lat: Optional[float],
    lon: Optional[float],
    iso_country_code: Optional[str] = None,
    iso_region_code: Optional[str] = None,
    geonames_id: Optional[str] = None,
    geocode_source: Optional[str] = None,
    geocode_confidence: Optional[float] = None,
) -> None:
    """Insert-or-update the place_attributes sidecar row for an entity.
    Idempotent — safe to call on repeated mentions of the same place."""
    coords_sql = "NULL"
    coords_params: list[Any] = []
    if lat is not None and lon is not None:
        coords_sql = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography"
        coords_params = [float(lon), float(lat)]

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO place_attributes (
                    entity_id, coordinates,
                    iso_country_code, iso_region_code,
                    geonames_id, geocode_source, geocode_confidence
                ) VALUES (
                    %s, {coords_sql},
                    %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (entity_id) DO UPDATE SET
                    coordinates        = COALESCE(EXCLUDED.coordinates,
                                                  place_attributes.coordinates),
                    iso_country_code   = COALESCE(EXCLUDED.iso_country_code,
                                                  place_attributes.iso_country_code),
                    iso_region_code    = COALESCE(EXCLUDED.iso_region_code,
                                                  place_attributes.iso_region_code),
                    geonames_id        = COALESCE(EXCLUDED.geonames_id,
                                                  place_attributes.geonames_id),
                    geocode_source     = COALESCE(EXCLUDED.geocode_source,
                                                  place_attributes.geocode_source),
                    geocode_confidence = COALESCE(EXCLUDED.geocode_confidence,
                                                  place_attributes.geocode_confidence),
                    updated_at         = NOW()
                """,
                (
                    entity_id,
                    *coords_params,
                    iso_country_code, iso_region_code,
                    geonames_id, geocode_source, geocode_confidence,
                ),
            )
        conn.commit()
    except Exception as e:
        log.warning("upsert place_attributes(entity_id=%s) failed: %s", entity_id, e)
        try:
            conn.rollback()
        except Exception:
            pass


def resolve_places(
    envelope: dict,
    conn,
    *,
    tenant_id: str = "default",
    ldu_t_id: Optional[int] = None,
    signal_rid: int = 0,
) -> dict[str, int]:
    """Resolve each place name in envelope.spatial.place_text.

    Each name is admitted through gateway.admit_entity (which triggers
    the form/concept/binding substrate writes per Phase 2 lockdown).
    Then we side-load the spatial data into place_attributes.

    Writes resolved_places[] onto the envelope in parallel to place_text.
    Returns counters {resolved, created, skipped, failed}.
    """
    stats = {"resolved": 0, "created": 0, "skipped": 0, "failed": 0}
    if conn is None:
        return stats

    place_text = (envelope.get("spatial") or {}).get("place_text") or []
    resolved: list[str] = []

    # Lazy import — gateway pulls in heavy deps.
    from core.gdu.gateway import get_gateway
    gw = get_gateway()

    for name in place_text:
        try:
            if not name or len(name) < 2:
                stats["skipped"] += 1
                continue

            gz_hit = _gazetteer_lookup(name)
            canonical_type = _GZ_TO_TYPE.get((gz_hit or {}).get("type"), "place")
            lat = (gz_hit or {}).get("lat")
            lon = (gz_hit or {}).get("lon")

            # Admit through gateway — this is the single chokepoint that
            # writes entities + base-60 substrate (form_node + concept_node
            # + form_concept_binding). Same path persons / orgs / events
            # take, so places get the same collapse semantics.
            #
            # attribute_hints lets the gateway's Layer 0 (binding
            # consumption) route ambiguous places — "Toronto" already
            # bound to concept(Toronto-ON) gets a NEW concept when this
            # signal's coords say Ohio, instead of silently collapsing.
            hints: dict = {}
            if lat is not None and lon is not None:
                hints["coords"] = {"lat": lat, "lon": lon}
            adm = gw.admit_entity(
                tenant_id, name, canonical_type,
                signal_rid=signal_rid,
                identifiers={},
                context="place_resolver",
                attribute_hints=hints or None,
            )
            if not adm or not adm.entity_id:
                stats["failed"] += 1
                continue

            # Side-load spatial data into the sidecar.
            if lat is not None and lon is not None:
                _upsert_place_attributes(
                    conn,
                    entity_id=adm.entity_id,
                    lat=lat,
                    lon=lon,
                    geocode_source="gazetteer",
                    geocode_confidence=0.80,
                )

            resolved.append(f"entity:{adm.entity_id}")
            stats["resolved"] += 1
            if adm.is_new:
                stats["created"] += 1
        except Exception as e:
            log.warning("resolve place(%r) failed: %s", name, e)
            stats["failed"] += 1

    envelope.setdefault("spatial", {})["resolved_places"] = resolved
    return stats
