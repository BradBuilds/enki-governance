"""
Geocoder Worker — resolves addresses and place names to lat/lon coordinates.

Runs as a daemon thread. Finds knowledge_nodes and entities that have
address text but no coordinates, geocodes them via Nominatim (free, no key),
and updates the latitude/longitude/location columns.

Sources of addresses:
  - Investigation findings (SEC registered addresses, SAM.gov, EPA facilities)
  - Entity dossiers (identifiers with address fields)
  - Place nodes without coordinates
  - Entity nodes with place associations but no coords

Respects Nominatim rate limit: 1 request/second (their policy).

Worker pattern matches existing workers (auto_validator, feed_enrichment).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.geocoder")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "geocoded": 0,
    "failed": 0,
    "skipped": 0,
    "errors": 0,
}

CHECK_INTERVAL = 60        # seconds between batch checks
BATCH_SIZE = 10            # nodes per batch
NOMINATIM_DELAY = 1.1      # seconds between Nominatim calls (their policy: max 1/s)
CACHE_TTL_DAYS = 30        # don't re-geocode within this window


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="geocoder", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    _STATUS["phase"] = "starting"
    log.info("Geocoder worker started")
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"
    return {"status": "stopped"}


def _worker_loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = CHECK_INTERVAL

    while not _STOP_EVENT.is_set():
        try:
            processed = _process_batch(tenant_id)
            if processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = "processing"
            else:
                backoff = min(backoff + 30, 300)
                _STATUS["phase"] = "idle — no pending geocoding"
        except Exception as e:
            log.warning("Geocoder error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, 600)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _process_batch(tenant_id: str) -> int:
    """Find nodes needing geocoding and process them."""
    nodes = _find_needs_geocoding(tenant_id, BATCH_SIZE)
    if not nodes:
        return 0

    processed = 0
    for node in nodes:
        if _STOP_EVENT.is_set():
            break
        try:
            _geocode_node(node, tenant_id)
            processed += 1
        except Exception as e:
            log.debug("Geocoding failed for %s: %s", node["node_id"], e)
            _STATUS["failed"] += 1

        # Respect Nominatim's 1 req/s policy
        time.sleep(NOMINATIM_DELAY)

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    return processed


def _find_needs_geocoding(tenant_id: str, limit: int) -> list:
    """Find knowledge_nodes that have address data but no coordinates.

    Checks multiple sources of addresses:
    1. Entity nodes with address in identifiers
    2. Place/entity nodes with title that looks like a location
    3. Investigation findings with address data in envelope/metadata
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Find entity/place nodes without coordinates that might have
                # address data in their envelope or linked investigation findings
                cur.execute("""
                    SELECT kn.node_id, kn.title, kn.node_type, kn.subtype,
                           kn.envelope, kn.metadata
                    FROM knowledge_nodes kn
                    WHERE kn.tenant_id = %s
                      AND kn.latitude IS NULL
                      AND kn.node_type IN ('entity', 'place', 'assessment')
                      AND kn.title IS NOT NULL
                      AND LENGTH(kn.title) >= 3
                      AND (
                          -- Place nodes always need geocoding
                          kn.node_type = 'place'
                          -- Entity nodes with address hints
                          OR kn.subtype IN ('facility', 'country', 'city', 'region',
                                            'address', 'building', 'compound',
                                            'residence', 'facility_location')
                          -- Assessment nodes from investigation (have addresses)
                          OR (kn.node_type = 'assessment'
                              AND kn.metadata->>'finding_type' IN (
                                  'company', 'epa_facility', 'sam_registration', 'bank'
                              ))
                      )
                    ORDER BY kn.source_count DESC NULLS LAST
                    LIMIT %s
                """, (tenant_id, limit))

                return [
                    {
                        "node_id": r[0],
                        "title": r[1],
                        "node_type": r[2],
                        "subtype": r[3],
                        "envelope": r[4] or {},
                        "metadata": r[5] or {},
                    }
                    for r in cur.fetchall()
                ]
    except Exception as e:
        log.debug("Failed to find nodes for geocoding: %s", e)
        return []


def _geocode_node(node: dict, tenant_id: str):
    """Geocode a single node. Tries multiple strategies to find an address."""

    node_id = node["node_id"]
    title = node["title"]
    metadata = node.get("metadata", {})
    envelope = node.get("envelope", {})

    # Strategy 1: If this is an investigation finding, extract address from description/data
    address = _extract_address_from_metadata(metadata, title)

    # Strategy 2: If entity, check envelope for address in profile
    if not address:
        profile = envelope.get("profile", {})
        for key in ("address", "headquarters", "location", "registered_address"):
            if profile.get(key):
                address = str(profile[key])
                break

    # Strategy 3: For place nodes, use the title itself
    if not address and node["node_type"] == "place":
        address = title

    # Strategy 4: For entity nodes with location subtypes, use title
    if not address and node.get("subtype") in ("facility", "country", "city", "region"):
        address = title

    if not address:
        _STATUS["skipped"] += 1
        return

    # Geocode via Nominatim
    coords = _nominatim_geocode(address)
    if not coords:
        # Try with just city, state if full address fails
        parts = address.split(",")
        if len(parts) >= 2:
            short_addr = ",".join(parts[-2:]).strip()
            coords = _nominatim_geocode(short_addr)

    if not coords:
        _STATUS["failed"] += 1
        return

    # Update the knowledge_node with coordinates
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE knowledge_nodes
                    SET latitude = %s, longitude = %s, updated_at = NOW()
                    WHERE node_id = %s AND tenant_id = %s AND latitude IS NULL
                """, (coords["lat"], coords["lon"], node_id, tenant_id))

                # Also update the entity if this is an entity node
                entity_id_str = node_id.replace("entity:", "")
                if entity_id_str.isdigit():
                    eid = int(entity_id_str)
                    cur.execute("""
                        UPDATE entities
                        SET identifiers = COALESCE(identifiers, '{}'::jsonb)
                            || %s::jsonb,
                            updated_at = NOW()
                        WHERE entity_id = %s AND tenant_id = %s
                    """, (
                        json.dumps({
                            "latitude": coords["lat"],
                            "longitude": coords["lon"],
                            "geocoded_address": coords.get("display_name", address),
                        }),
                        eid, tenant_id,
                    ))
                    # Phase 8b.6.3: also write to the structured entity_addresses
                    # table so the dossier + map can query addresses without
                    # parsing the JSONB blob.
                    addr_components = (coords.get("address") or {}) if isinstance(coords.get("address"), dict) else {}
                    _upsert_entity_address(
                        cur, tenant_id=tenant_id, entity_id=eid,
                        address_type=("hq" if node["node_type"] == "entity" else "asset_location"),
                        raw_address=coords.get("display_name", address),
                        parsed={
                            "street": addr_components.get("road"),
                            "city": addr_components.get("city") or addr_components.get("town") or addr_components.get("village"),
                            "region": addr_components.get("state"),
                            "postal_code": addr_components.get("postcode"),
                            "country": addr_components.get("country"),
                        },
                        lat=coords["lat"], lon=coords["lon"],
                        source="geocoder", source_record={"display_name": coords.get("display_name")},
                        confidence=0.85,
                    )

            conn.commit()

        _STATUS["geocoded"] += 1
        log.info("Geocoded %s → %.4f, %.4f (%s)",
                 node_id, coords["lat"], coords["lon"], address[:50])

    except Exception as e:
        log.debug("Failed to store geocode for %s: %s", node_id, e)
        _STATUS["errors"] += 1


def _extract_address_from_metadata(metadata: dict, title: str) -> Optional[str]:
    """Extract an address string from investigation finding metadata."""
    # Investigation findings store data as JSON in the description field
    # but also in metadata for assessment nodes
    finding_type = metadata.get("finding_type", "")

    if finding_type in ("company", "sam_registration"):
        # These have address in the node description (JSON data)
        # Try to parse address from the title
        # e.g., "SEC Company: Example Holdings Corp."
        # The address is in the finding data, not the title
        return None  # will be handled by description parsing

    if finding_type == "epa_facility":
        # EPA facilities have addresses in the data
        return None

    return None


# ─── Phase 8b.6.3: entity_addresses upserts ──────────────────────────────────

def _upsert_entity_address(
    cur, *, tenant_id: str, entity_id: int, address_type: str,
    raw_address: str, parsed: Optional[dict] = None,
    lat: Optional[float] = None, lon: Optional[float] = None,
    source: str = "geocoder", source_record: Optional[dict] = None,
    confidence: float = 0.85,
) -> None:
    """Upsert one row into entity_addresses (migration 051).

    Idempotent on (tenant_id, entity_id, address_type, raw_address).
    The trigger trg_entity_addresses_set_coords populates the GEOGRAPHY
    point from lat/lon automatically.
    """
    if not raw_address or not entity_id:
        return
    parsed = parsed or {}
    cur.execute("""
        INSERT INTO entity_addresses
            (tenant_id, entity_id, address_type, raw_address,
             street, city, region, postal_code, country,
             lat, lon, geocoded_at, geocode_source,
             source, source_record, confidence)
        VALUES (%s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, CASE WHEN %s IS NOT NULL THEN NOW() ELSE NULL END, %s,
                %s, %s, %s)
        ON CONFLICT (tenant_id, entity_id, address_type, raw_address)
        DO UPDATE SET
            street       = COALESCE(EXCLUDED.street,       entity_addresses.street),
            city         = COALESCE(EXCLUDED.city,         entity_addresses.city),
            region       = COALESCE(EXCLUDED.region,       entity_addresses.region),
            postal_code  = COALESCE(EXCLUDED.postal_code,  entity_addresses.postal_code),
            country      = COALESCE(EXCLUDED.country,      entity_addresses.country),
            lat          = COALESCE(EXCLUDED.lat,          entity_addresses.lat),
            lon          = COALESCE(EXCLUDED.lon,          entity_addresses.lon),
            geocoded_at  = COALESCE(EXCLUDED.geocoded_at,  entity_addresses.geocoded_at),
            geocode_source = COALESCE(EXCLUDED.geocode_source, entity_addresses.geocode_source),
            source       = COALESCE(EXCLUDED.source,       entity_addresses.source),
            source_record = COALESCE(EXCLUDED.source_record, entity_addresses.source_record),
            confidence   = GREATEST(EXCLUDED.confidence,   entity_addresses.confidence),
            updated_at   = NOW()
    """, (
        tenant_id, entity_id, address_type, raw_address,
        parsed.get("street"), parsed.get("city"), parsed.get("region"),
        parsed.get("postal_code"), parsed.get("country"),
        lat, lon,
        lat,  # for the CASE WHEN check on geocoded_at
        ("nominatim" if lat else None) if source == "geocoder" else source,
        source,
        json.dumps(source_record) if source_record else None,
        confidence,
    ))


def _entity_id_from_node_id(node_id: str) -> Optional[int]:
    """node_id 'entity:1234' → 1234. Returns None for non-entity nodes."""
    if not node_id or not node_id.startswith("entity:"):
        return None
    rest = node_id.split(":", 1)[1]
    return int(rest) if rest.isdigit() else None


def _nominatim_geocode(query: str) -> Optional[dict]:
    """Geocode a query string via Nominatim. Returns {lat, lon, display_name}."""
    import urllib.request
    import urllib.parse
    import ssl

    try:
        url = (
            f"https://nominatim.openstreetmap.org/search"
            f"?q={urllib.parse.quote_plus(query)}"
            f"&format=json&limit=1&addressdetails=1"
        )
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Enki/1.0 research@example.com")
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            results = json.loads(resp.read())
            if results:
                r = results[0]
                return {
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "display_name": r.get("display_name", ""),
                    "type": r.get("type", ""),
                    "importance": r.get("importance"),
                    "country_code": r.get("address", {}).get("country_code"),
                    "address": r.get("address", {}),
                }
    except Exception as e:
        log.debug("Nominatim geocode failed for '%s': %s", query[:50], e)

    return None


# ─── One-shot geocoder for investigation findings ────────────────────────────

def geocode_investigation_findings(investigation_id: int, tenant_id: str):
    """Geocode addresses found in investigation findings.

    Called after an investigation completes. Extracts addresses from
    findings (SEC addresses, EPA facilities, SAM registrations) and
    updates the corresponding knowledge_nodes with coordinates.

    Also geocodes newly discovered entities that have address data
    in their relationship_detail.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Find findings with address data
            cur.execute("""
                SELECT finding_id, source, finding_type, data
                FROM investigation_findings
                WHERE investigation_id = %s AND tenant_id = %s
            """, (investigation_id, tenant_id))

            for finding_id, source, ftype, data in cur.fetchall():
                if not isinstance(data, dict):
                    try:
                        data = json.loads(data) if isinstance(data, str) else {}
                    except Exception:
                        continue

                # Extract address from finding data
                address = None
                lat = data.get("latitude")
                lon = data.get("longitude")

                if lat and lon:
                    # Already has coordinates (EPA facilities)
                    continue

                # Build address from data fields
                addr_parts = []
                for key in ("address", "street", "addressLine1"):
                    if data.get(key):
                        addr_parts.append(str(data[key]))
                        break
                for key in ("city",):
                    if data.get(key):
                        addr_parts.append(str(data[key]))
                for key in ("state", "stateOrProvinceCode"):
                    if data.get(key):
                        addr_parts.append(str(data[key]))
                for key in ("zip", "zipCode"):
                    if data.get(key):
                        addr_parts.append(str(data[key]))

                if addr_parts:
                    address = ", ".join(addr_parts)

                if not address:
                    continue

                # Geocode it
                time.sleep(NOMINATIM_DELAY)
                coords = _nominatim_geocode(address)
                if coords:
                    # Update the finding data with coordinates
                    data["latitude"] = coords["lat"]
                    data["longitude"] = coords["lon"]
                    data["geocoded_address"] = coords.get("display_name", "")
                    cur.execute("""
                        UPDATE investigation_findings
                        SET data = %s
                        WHERE finding_id = %s
                    """, (json.dumps(data), finding_id))

                    log.info("Geocoded finding %d: %s → %.4f, %.4f",
                             finding_id, address[:40], coords["lat"], coords["lon"])

                    # Phase 8b.6.3: also persist the structured address row.
                    # Pull the parent investigation's entity_id so the row
                    # attaches to the right entity.
                    cur.execute("""
                        SELECT entity_id FROM entity_investigations
                         WHERE investigation_id = %s
                    """, (investigation_id,))
                    erow = cur.fetchone()
                    if erow and erow[0]:
                        addr = coords.get("address") or {}
                        addr_type_map = {
                            "sec_edgar": "registered",
                            "sam_gov": "principal_office",
                            "opencorporates": "registered",
                            "faa_aircraft": "registered",
                            "fdic": "branch",
                            "fda": "facility",
                            "epa_echo": "facility",
                        }
                        _upsert_entity_address(
                            cur, tenant_id=tenant_id, entity_id=erow[0],
                            address_type=addr_type_map.get(source, "branch"),
                            raw_address=coords.get("display_name", address),
                            parsed={
                                "street": addr.get("road"),
                                "city": addr.get("city") or addr.get("town") or addr.get("village"),
                                "region": addr.get("state"),
                                "postal_code": addr.get("postcode"),
                                "country": addr.get("country"),
                            },
                            lat=coords["lat"], lon=coords["lon"],
                            source=source or "investigation_finding",
                            source_record={"finding_id": finding_id, "finding_type": ftype},
                            confidence=0.88,
                        )

            # Also geocode discovered entities with addresses
            cur.execute("""
                SELECT discovered_id, name, relationship_detail, entity_id
                FROM investigation_discovered
                WHERE investigation_id = %s AND tenant_id = %s
                  AND entity_id IS NOT NULL
            """, (investigation_id, tenant_id))

            for disc_id, name, detail, entity_id in cur.fetchall():
                if not isinstance(detail, dict):
                    try:
                        detail = json.loads(detail) if isinstance(detail, str) else {}
                    except Exception:
                        continue

                address = detail.get("address", "")
                if not address or len(address) < 5:
                    continue

                # Check if entity already has coords
                cur.execute("""
                    SELECT 1 FROM entities
                    WHERE entity_id = %s AND identifiers->>'latitude' IS NOT NULL
                """, (entity_id,))
                if cur.fetchone():
                    continue

                time.sleep(NOMINATIM_DELAY)
                coords = _nominatim_geocode(address)
                if coords:
                    # Update entity with coordinates
                    cur.execute("""
                        UPDATE entities
                        SET identifiers = COALESCE(identifiers, '{}'::jsonb)
                            || %s::jsonb,
                            updated_at = NOW()
                        WHERE entity_id = %s
                    """, (
                        json.dumps({
                            "latitude": coords["lat"],
                            "longitude": coords["lon"],
                            "geocoded_address": coords.get("display_name", ""),
                        }),
                        entity_id,
                    ))

                    # Update knowledge_node too
                    cur.execute("""
                        UPDATE knowledge_nodes
                        SET latitude = %s, longitude = %s, updated_at = NOW()
                        WHERE node_id = %s AND latitude IS NULL
                    """, (coords["lat"], coords["lon"], f"entity:{entity_id}"))

                    log.info("Geocoded entity %s (%s) → %.4f, %.4f",
                             name[:30], address[:30], coords["lat"], coords["lon"])

        conn.commit()
