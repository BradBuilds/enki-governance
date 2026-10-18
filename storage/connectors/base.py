"""
Connector base class and framework.

Connectors bridge external data sources into the ingestion pipeline.
Every connector is a device with its own grant and audit trail.

Types:
  pull:   Enki fetches on a schedule (IMAP, RSS, HTTP API, browser history)
  stream: Enki maintains persistent connection (TAK, syslog, MQTT)
  watch:  Enki monitors filesystem (log files, drop folders)
  push:   External source POSTs to Enki (phones, sensors — handled by API, not here)
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from storage.ingest.signal_ingestion import ingest_signal_batch

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Result of a connector fetch cycle."""
    items_fetched: int
    items_ingested: int
    errors: list[str]
    new_watermark: Optional[dict] = None  # updated cursor position


class BaseConnector(ABC):
    """Abstract base for all pull/stream/watch connectors."""

    connector_type: str = "pull"
    source_type: str = "unknown"

    def __init__(
        self,
        connector_id: str,
        device_id: str,
        tenant_id: str,
        source_config: dict,
        watermark: Optional[dict] = None,
    ):
        self.connector_id = connector_id
        self.device_id = device_id
        self.tenant_id = tenant_id
        self.source_config = source_config
        self.watermark = watermark or {}

    @abstractmethod
    def fetch(self) -> list[dict]:
        """
        Fetch new data from the external source.

        Returns list of dicts, each with:
            raw_bytes: bytes
            content_type: str (optional)
            source_type: str (optional)

        The connector is responsible for:
          - Connecting to the external source
          - Fetching only NEW data (using watermark)
          - Converting to raw bytes
          - NOT interpreting the data (that's the pipeline's job)
        """
        ...

    @abstractmethod
    def update_watermark(self, items: list[dict]) -> dict:
        """
        Compute the new watermark after a successful fetch.
        The watermark is a cursor that tracks what was last fetched
        so the next fetch only gets new data.
        """
        ...

    def run(self) -> FetchResult:
        """
        Execute a complete fetch cycle:
        1. Fetch new data from source
        2. Filter gate: check case watchlists for high-volume sources
        3. Feed matches into ingestion pipeline, cache non-matches
        4. Update watermark
        5. Record stats
        """
        errors = []

        try:
            items = self.fetch()
        except Exception as e:
            logger.error(f"Connector {self.connector_id} fetch failed: {e}")
            return FetchResult(items_fetched=0, items_ingested=0, errors=[str(e)])

        if not items:
            return FetchResult(items_fetched=0, items_ingested=0, errors=[])

        config = self.source_config if isinstance(self.source_config, dict) else {}
        type_hint = config.get("signal_type_hint")
        filter_mode = config.get("filter_mode", "all")  # "all", "watchlist", "threshold"

        # ── FILTER GATE ──────────────────────────────────────────────────
        # For "watchlist" mode: only ingest items whose observables match
        # a case watchlist. Cache everything else in live_cache for browse.
        if filter_mode == "watchlist":
            ingest_items, cache_items = self._apply_watchlist_filter(items, config)
        else:
            ingest_items = items
            cache_items = []

        # ── WRITE HOT CACHE (non-matching items for browse queries) ──────
        if cache_items:
            try:
                self._write_hot_cache(cache_items, config)
            except Exception as e:
                logger.warning(f"Hot cache write failed for {self.connector_id}: {e}")

        # ── INGEST MATCHING ITEMS ────────────────────────────────────────
        # Read the optional _topic_id from source_config ONCE. Connectors
        # created by watch_topics._auto_create_connectors stamp a _topic_id
        # so the research fanout hook in the extraction runner can attribute
        # the resulting signal to its originating topic without a keyword
        # match. Default RSS feeds (BBC, Al Jazeera, etc.) have no topic
        # attached — their signals will be fanned out via keyword matching
        # against active topics' keyword lists instead.
        topic_id = config.get("_topic_id") if isinstance(config, dict) else None

        pipeline_items = []
        for item in ingest_items:
            pi = {
                "raw_bytes": item.get("raw_bytes", b""),
                "device_id": self.device_id,
                "content_type": item.get("content_type"),
                "source_type": item.get("source_type", self.source_type),
                "identity_id": item.get("identity_id"),
            }
            if type_hint:
                pi["signal_type_hint"] = type_hint
            if topic_id:
                pi["_topic_id"] = topic_id
            pipeline_items.append(pi)

        ingested = 0
        if pipeline_items:
            try:
                result = ingest_signal_batch(pipeline_items, self.tenant_id)
                ingested = result.accepted + result.provisioned
            except Exception as e:
                logger.error(f"Connector {self.connector_id} ingestion failed: {e}")
                errors.append(f"Ingestion error: {e}")

        # Also cache the matched items so they show in live view too
        if ingest_items and filter_mode == "watchlist":
            try:
                self._write_hot_cache(ingest_items, config)
            except Exception:
                pass

        # Update watermark
        try:
            new_watermark = self.update_watermark(items)
            self.watermark = new_watermark
        except Exception as e:
            errors.append(f"Watermark update error: {e}")
            new_watermark = self.watermark

        return FetchResult(
            items_fetched=len(items),
            items_ingested=ingested,
            errors=errors,
            new_watermark=new_watermark,
        )

    def _apply_watchlist_filter(
        self, items: list[dict], config: dict,
    ) -> tuple[list[dict], list[dict]]:
        """Split items into watchlist matches (ingest) and non-matches (cache).

        Parses each item's raw_bytes as JSON, extracts observable fields,
        and checks against active case_observables in the database.
        """
        from storage.postgres.db import get_connection

        obs_fields = config.get("observable_fields", [])
        if not obs_fields:
            # No fields configured — ingest everything
            return items, []

        # Load all active case watchlist values (type → set of normalized values)
        watchlist: dict[str, set] = {}
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT co.observable_type, co.normalized_value
                        FROM case_observables co
                        JOIN cases c ON c.case_id = co.case_id AND c.tenant_id = co.tenant_id
                        WHERE co.tenant_id = %s AND co.active = TRUE
                          AND c.status IN ('open', 'investigating', 'awaiting_input')
                    """, (self.tenant_id,))
                    for row in cur.fetchall():
                        watchlist.setdefault(row[0], set()).add(row[1])
        except Exception as e:
            logger.warning(f"Watchlist load failed: {e}, ingesting all")
            return items, []

        if not watchlist:
            # No active watchlists — cache everything, ingest nothing
            return [], items

        ingest = []
        cache = []

        for item in items:
            matched = False
            try:
                raw = item.get("raw_bytes", b"")
                if isinstance(raw, memoryview):
                    raw = bytes(raw)
                data = json.loads(raw) if raw else {}

                for field in obs_fields:
                    val = data.get(field)
                    if not val or not isinstance(val, str):
                        continue
                    val_upper = val.strip().upper()
                    val_lower = val.strip().lower()

                    # Check against all watchlist types that could match this field
                    from storage.ingest.parsers.json_parser import _FIELD_TO_TYPE
                    obs_type = _FIELD_TO_TYPE.get(field.lower())
                    if obs_type and obs_type in watchlist:
                        if val_upper in watchlist[obs_type] or val_lower in watchlist[obs_type]:
                            matched = True
                            break
                    # Also check all watchlist types (in case field→type mapping is missing)
                    if not matched:
                        for wl_type, wl_values in watchlist.items():
                            if val_upper in wl_values or val_lower in wl_values:
                                matched = True
                                break
                    if matched:
                        break
            except Exception:
                pass  # parse error — cache it

            if matched:
                ingest.append(item)
            else:
                cache.append(item)

        logger.info(
            f"Filter gate [{self.connector_id}]: {len(ingest)} matches, "
            f"{len(cache)} cached of {len(items)} fetched"
        )
        return ingest, cache

    def _write_hot_cache(self, items: list[dict], config: dict) -> None:
        """Write items to the live_cache table for browse queries."""
        from storage.postgres.db import get_connection

        source = config.get("cache_source", self.source_type)
        ttl_hours = config.get("cache_ttl_hours", 4)
        key_field = config.get("id_field") or config.get("cache_key_field", "id")
        lat_field = config.get("cache_lat_field", "lat")
        lon_field = config.get("cache_lon_field", "lon")
        label_field = config.get("cache_label_field")

        rows = []
        for item in items:
            try:
                raw = item.get("raw_bytes", b"")
                if isinstance(raw, memoryview):
                    raw = bytes(raw)
                data = json.loads(raw) if raw else {}

                key = str(data.get(key_field, ""))[:255]
                if not key:
                    continue

                lat = data.get(lat_field)
                lon = data.get(lon_field)

                # GeoJSON support
                if lat is None and lon is None:
                    geom = data.get("geometry", {})
                    if isinstance(geom, dict) and geom.get("coordinates"):
                        coords = geom["coordinates"]
                        if len(coords) >= 2:
                            lon, lat = coords[0], coords[1]

                label = str(data.get(label_field, ""))[:500] if label_field else key

                def _safe_float(v):
                    if v is None:
                        return None
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        return None

                rows.append((
                    self.tenant_id, source, key, label,
                    json.dumps(data, default=str),
                    _safe_float(lat),
                    _safe_float(lon),
                    _safe_float(data.get("alt_baro") or data.get("altitude")),
                    _safe_float(data.get("track") or data.get("heading")),
                    _safe_float(data.get("gs") or data.get("speed")),
                    ttl_hours,
                ))
            except Exception:
                continue

        if not rows:
            return

        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    for r in rows:
                        # Upsert current position in live_cache
                        cur.execute("""
                            INSERT INTO live_cache
                                (tenant_id, source, key, display_label, payload,
                                 latitude, longitude, altitude, heading, speed,
                                 cached_at, expires_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    NOW(), NOW() + interval '%s hours')
                            ON CONFLICT (tenant_id, source, key)
                            DO UPDATE SET
                                payload = EXCLUDED.payload,
                                latitude = EXCLUDED.latitude,
                                longitude = EXCLUDED.longitude,
                                altitude = EXCLUDED.altitude,
                                heading = EXCLUDED.heading,
                                speed = EXCLUDED.speed,
                                display_label = EXCLUDED.display_label,
                                cached_at = NOW(),
                                expires_at = NOW() + interval '%s hours'
                        """, (*r, ttl_hours))

                        # Append to history (for trails) — only if position exists
                        tenant, src, key, label, payload_json, lat, lon, alt, hdg, spd, _ = r
                        if lat is not None and lon is not None:
                            # Extract lightweight summary for history (not full payload)
                            summary = {}
                            try:
                                full = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
                                for sk in ('flight', 'r', 'squawk', 'shipname', 'mmsi', 'callsign', 't'):
                                    if sk in full:
                                        summary[sk] = full[sk]
                            except Exception:
                                pass

                            cur.execute("""
                                INSERT INTO live_history
                                    (tenant_id, source, key, latitude, longitude,
                                     altitude, heading, speed, payload_summary)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """, (tenant, src, key, lat, lon, alt, hdg, spd,
                                  json.dumps(summary, default=str)))

                    # Purge expired cache + old history (>24h)
                    cur.execute("DELETE FROM live_cache WHERE expires_at < NOW()")
                    cur.execute("DELETE FROM live_history WHERE recorded_at < NOW() - interval '24 hours'")
                conn.commit()
        except Exception as e:
            logger.warning(f"Hot cache write error: {e}")
