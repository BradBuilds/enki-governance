"""
GDELT GKG 2.0 ingestion.

Every 15 minutes, GDELT publishes a fresh 15-minute slice of three files:
  - <timestamp>.export.CSV.zip   (EVENTS)
  - <timestamp>.mentions.CSV.zip (MENTIONS)
  - <timestamp>.gkg.csv.zip      (GLOBAL KNOWLEDGE GRAPH)

The map layer needs lat/lon mentions, which live in the GKG file's
``V2.1ENHANCEDLOCATIONS`` column. Each cell is ``#``-delimited per
location and ``;``-delimited between locations:

    1#United States#US#USA#39.8283#-98.5795#FIPS#42

    field 0 : location type   (1=country, 2=USstate, 3=UScity,
                                4=worldcity, 5=worldstate)
    field 1 : location name
    field 2 : FIPS / country code
    field 3 : ADM1 code
    field 4 : latitude
    field 5 : longitude
    field 6 : feature ID source
    field 7 : char offset in document  (we discard this)

Usage:
    from core.connectors.gdelt_gkg import sync_latest_gkg
    result = await sync_latest_gkg()
    # { "fetched_url": "...", "inserted": 12345, "purged": 8200, "error": None }
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import Optional

import httpx

from storage.postgres.db import get_connection

log = logging.getLogger("enki.connectors.gdelt_gkg")

LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
TIMEOUT = 60.0
RETENTION_HOURS = 48

# GKG 2.0 column indices (0-based). The full schema has 27 columns —
# we only need a handful for the map.
_GKG_COL_RECORD_ID    = 0   # GKGRECORDID
_GKG_COL_DATE         = 1   # V2.1DATE      (YYYYMMDDHHMMSS)
_GKG_COL_SOURCE_NAME  = 3   # V2SOURCECOMMONNAME
_GKG_COL_DOC_URL      = 4   # V2DOCUMENTIDENTIFIER
_GKG_COL_THEMES       = 7   # V1THEMES (semicolon-delimited)
_GKG_COL_LOCATIONS    = 9   # V2.1ENHANCEDLOCATIONS
_GKG_COL_TONE         = 15  # V1.5TONE  (first value of comma-list)


async def sync_latest_gkg(*, dsn: Optional[str] = None) -> dict:
    """Pull the most recent 15-minute GKG slice and insert its mentions.

    Returns a summary dict with fetched_url, inserted count, purged count,
    and any error encountered. Idempotent: if the file has already been
    processed (recorded in gdelt_sync_state.last_processed_url) the call
    returns inserted=0.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            # 1. Find the latest GKG file URL.
            resp = await client.get(LASTUPDATE_URL)
            resp.raise_for_status()
            gkg_url = _parse_lastupdate_for_gkg(resp.text)
            if not gkg_url:
                return _record_error("no GKG url in lastupdate.txt", url=None)

            # 2. Skip if we've already processed this exact file.
            already = _is_already_processed(gkg_url, dsn=dsn)
            if already:
                return {"fetched_url": gkg_url, "inserted": 0, "purged": 0,
                        "error": None, "skipped": True}

            # 3. Download + unzip.
            zresp = await client.get(gkg_url)
            zresp.raise_for_status()
            csv_text = _unzip_to_text(zresp.content)
            if csv_text is None:
                return _record_error("zip extraction failed", url=gkg_url)

        # 4. Parse + insert.
        rows = list(_iter_mentions_from_gkg(csv_text))
        inserted = _bulk_insert(rows, dsn=dsn)

        # 5. Purge anything older than the retention window.
        purged = _purge_old(RETENTION_HOURS, dsn=dsn)

        # 6. Record success.
        _record_success(gkg_url, inserted, dsn=dsn)

        return {"fetched_url": gkg_url, "inserted": inserted, "purged": purged,
                "error": None, "skipped": False}
    except Exception as e:
        log.warning("gkg sync failed: %s", e)
        return _record_error(str(e), url=None, dsn=dsn)


# ─── lastupdate.txt parsing ────────────────────────────────────────────────

_GKG_LINE_RE = re.compile(r"http\S+\.gkg\.csv\.zip", re.IGNORECASE)


def _parse_lastupdate_for_gkg(text: str) -> Optional[str]:
    """lastupdate.txt is 3 lines: EXPORT, MENTIONS, GKG — pick the GKG one."""
    m = _GKG_LINE_RE.search(text)
    return m.group(0) if m else None


# ─── zip extraction ────────────────────────────────────────────────────────

def _unzip_to_text(zbytes: bytes) -> Optional[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
            names = zf.namelist()
            if not names:
                return None
            with zf.open(names[0]) as fp:
                # GKG files are 'utf-8' but tolerant of malformed bytes —
                # replace errors so we never crash on one bad article.
                return fp.read().decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        return None


# ─── CSV row → mention dicts ───────────────────────────────────────────────

def _iter_mentions_from_gkg(csv_text: str):
    """Yield mention dicts from a GKG 2.0 file (tab-separated despite .csv).

    GDELT does not quote fields, so a plain split('\\t') is safe — but a
    line CAN be missing trailing tabs. Use a tolerant slice.
    """
    for line in csv_text.splitlines():
        if not line:
            continue
        cols = line.split("\t")
        if len(cols) <= _GKG_COL_LOCATIONS:
            continue

        gkg_record_id = cols[_GKG_COL_RECORD_ID] or ""
        date_str = cols[_GKG_COL_DATE] or ""
        published_at = _parse_gkg_date(date_str)
        if not published_at:
            continue

        locations_field = cols[_GKG_COL_LOCATIONS] or ""
        if not locations_field:
            continue

        # Optional fields — pull defensively.
        source_domain = cols[_GKG_COL_SOURCE_NAME] if len(cols) > _GKG_COL_SOURCE_NAME else None
        doc_url = cols[_GKG_COL_DOC_URL] if len(cols) > _GKG_COL_DOC_URL else None
        themes_field = cols[_GKG_COL_THEMES] if len(cols) > _GKG_COL_THEMES else ""
        themes = _parse_themes(themes_field)
        tone = _parse_tone(cols[_GKG_COL_TONE] if len(cols) > _GKG_COL_TONE else "")

        # Dedupe locations within a record so we don't write the same
        # (record, lat, lon) tuple twice when GDELT lists "USA" four times.
        seen_in_record: set[tuple[str, float, float]] = set()
        for loc in locations_field.split(";"):
            parsed = _parse_location(loc)
            if not parsed:
                continue
            key = (gkg_record_id, round(parsed["latitude"], 4), round(parsed["longitude"], 4))
            if key in seen_in_record:
                continue
            seen_in_record.add(key)

            yield {
                "gkg_record_id": gkg_record_id[:128],
                "published_at": published_at,
                "latitude": parsed["latitude"],
                "longitude": parsed["longitude"],
                "location_name": parsed["name"],
                "location_type": parsed["type"],
                "country_code": parsed["country"],
                "adm1_code": parsed["adm1"],
                "feature_id": parsed["feature_id"],
                "themes": themes,
                "tone": tone,
                "source_domain": (source_domain or "")[:255] or None,
                "document_url": doc_url or None,
            }


def _parse_gkg_date(s: str) -> Optional[datetime]:
    """V2.1DATE format: YYYYMMDDHHMMSS."""
    if not s or len(s) < 14:
        return None
    try:
        return datetime(
            int(s[0:4]), int(s[4:6]), int(s[6:8]),
            int(s[8:10]), int(s[10:12]), int(s[12:14]),
            tzinfo=timezone.utc,
        )
    except (ValueError, IndexError):
        return None


def _parse_location(s: str) -> Optional[dict]:
    """Each `;`-delimited location is `#`-delimited internally:
       type#name#country#adm1#lat#lon#feature_id#charoffset
    Returns None if any required field is missing or malformed.
    """
    if not s:
        return None
    parts = s.split("#")
    if len(parts) < 6:
        return None
    try:
        loc_type = int(parts[0]) if parts[0] else None
        name = parts[1].strip()
        country = (parts[2] or "").strip()[:8] or None
        adm1 = (parts[3] or "").strip()[:16] or None
        lat = float(parts[4])
        lon = float(parts[5])
        feature_id = (parts[6] if len(parts) > 6 else "").strip()[:64] or None
    except (ValueError, IndexError):
        return None
    # Reject 0,0 (sentinel "couldn't geocode" value GDELT uses)
    if abs(lat) < 0.0001 and abs(lon) < 0.0001:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"type": loc_type, "name": name, "country": country, "adm1": adm1,
            "latitude": lat, "longitude": lon, "feature_id": feature_id}


def _parse_themes(s: str) -> list[str]:
    """V1THEMES is `;`-delimited bare strings, each often suffixed with a
    char offset like `TAX_TERROR,1234`. Strip offsets, dedupe, cap at 12."""
    if not s:
        return []
    seen: list[str] = []
    seen_set: set[str] = set()
    for raw in s.split(";"):
        t = raw.split(",")[0].strip()
        if not t or t in seen_set:
            continue
        seen.append(t)
        seen_set.add(t)
        if len(seen) >= 12:
            break
    return seen


def _parse_tone(s: str) -> Optional[float]:
    """V1.5TONE is comma-separated; first value is overall tone (-100..+100)."""
    if not s:
        return None
    try:
        return float(s.split(",")[0])
    except (ValueError, IndexError):
        return None


# ─── DB ────────────────────────────────────────────────────────────────────

def _is_already_processed(url: str, *, dsn: Optional[str] = None) -> bool:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_processed_url FROM gdelt_sync_state WHERE id = 1")
            row = cur.fetchone()
            return bool(row and row[0] == url)


def _bulk_insert(rows: list[dict], *, dsn: Optional[str] = None) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO gdelt_mentions (
            gkg_record_id, published_at, latitude, longitude,
            location_name, location_type, country_code, adm1_code, feature_id,
            themes, tone, source_domain, document_url
        ) VALUES (
            %(gkg_record_id)s, %(published_at)s, %(latitude)s, %(longitude)s,
            %(location_name)s, %(location_type)s, %(country_code)s,
            %(adm1_code)s, %(feature_id)s,
            %(themes)s, %(tone)s, %(source_domain)s, %(document_url)s
        )
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # psycopg2 / psycopg3 both accept executemany with a dict list.
            cur.executemany(sql, rows)
        conn.commit()
    return len(rows)


def _purge_old(hours: int, *, dsn: Optional[str] = None) -> int:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM gdelt_mentions WHERE published_at < NOW() - %s::interval",
                (f"{hours} hours",),
            )
            n = cur.rowcount
        conn.commit()
    return n or 0


def _record_success(url: str, count: int, *, dsn: Optional[str] = None) -> None:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE gdelt_sync_state
                SET last_processed_url = %s,
                    last_processed_at  = NOW(),
                    last_record_count  = %s,
                    last_error         = NULL,
                    consecutive_errors = 0
                WHERE id = 1
            """, (url, count))
        conn.commit()


def _record_error(msg: str, *, url: Optional[str], dsn: Optional[str] = None) -> dict:
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE gdelt_sync_state
                    SET last_error         = %s,
                        consecutive_errors = consecutive_errors + 1
                    WHERE id = 1
                """, (msg[:500],))
            conn.commit()
    except Exception:
        pass
    return {"fetched_url": url, "inserted": 0, "purged": 0, "error": msg}
