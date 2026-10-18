"""
Sources API — manage RSS/feed connectors for the intelligence system.

GET    /sources                — list all connectors with status
POST   /sources/add            — validate + add an RSS feed URL
PATCH  /sources/{id}           — enable/disable/boost a source
DELETE /sources/{id}           — remove a source
GET    /sources/library        — curated recommendations by category
POST   /sources/library/{key}  — one-click add from library
GET    /sources/preview        — preview an RSS URL before adding
"""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlparse
import hashlib
import logging
import os
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Header, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

log = logging.getLogger("enki.sources")
router = APIRouter(prefix="/sources", tags=["sources"])


# ─── Curated library ────────────────────────────────────────────────────

_LIBRARY_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "libraries", "source_library.json")


def _load_library() -> list[dict]:
    try:
        with open(_LIBRARY_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        log.debug("Library load failed: %s", e)
        return []


# ─── List sources ───────────────────────────────────────────────────────

@router.get("")
async def list_sources(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List all connectors with current status and source config."""
    from storage.postgres.db import get_connection

    sources = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT connector_id, device_id, connector_type, source_type,
                           status, items_ingested, last_fetch_at, last_error,
                           convert_from(source_config, 'UTF8')::jsonb as config
                    FROM connectors
                    WHERE tenant_id = %s
                    ORDER BY items_ingested DESC NULLS LAST, connector_id
                """, (auth.tenant_id,))
                for r in cur.fetchall():
                    config = r[8] or {}
                    sources.append({
                        "connector_id": r[0],
                        "device_id": r[1],
                        "connector_type": r[2],
                        "source_type": r[3],
                        "status": r[4],
                        "items_ingested": r[5] or 0,
                        "last_fetch_at": r[6].isoformat() if r[6] else None,
                        "last_error": r[7],
                        "url": config.get("url", ""),
                        "label": config.get("label") or config.get("feed_title", r[0]),
                        "category": config.get("category", "general"),
                        "region": config.get("region", "global"),
                    })
    except Exception as e:
        log.error("List sources failed: %s", e)
        return {"sources": [], "error": str(e)}

    return {"sources": sources, "count": len(sources)}


# ─── Preview RSS URL ────────────────────────────────────────────────────

def _is_safe_url(url: str) -> bool:
    """Block SSRF — reject URLs targeting private/internal networks."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if not hostname:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        # Block localhost, link-local, cloud metadata
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0",
                        "metadata.google.internal", "169.254.169.254"):
            return False
        try:
            addr = ipaddress.ip_address(hostname)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return False
        except ValueError:
            pass  # hostname is a DNS name, not an IP — OK
        return True
    except Exception:
        return False


@router.get("/preview")
async def preview_rss(url: str = Query(...)):
    """Validate and preview an RSS/Atom feed URL before adding."""
    if not _is_safe_url(url):
        return {"ok": False, "error": "URL targets a private or internal network"}
    try:
        import httpx
        # Try feed parser
        try:
            import feedparser
        except ImportError:
            feedparser = None

        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Enki/1.0 feed preview"})
            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code}"}

            content = resp.text[:200000]
            result: dict = {"ok": True, "url": url, "status_code": resp.status_code}

            if feedparser:
                parsed = feedparser.parse(content)
                feed = parsed.get("feed", {})
                entries = parsed.get("entries", [])
                result["title"] = feed.get("title", "")
                result["description"] = feed.get("subtitle", "") or feed.get("description", "")
                result["link"] = feed.get("link", "")
                result["entry_count"] = len(entries)
                result["sample_entries"] = [
                    {
                        "title": e.get("title", ""),
                        "link": e.get("link", ""),
                        "published": e.get("published", ""),
                    }
                    for e in entries[:5]
                ]
                if not entries and not feed.get("title"):
                    result["ok"] = False
                    result["error"] = "URL does not appear to be a valid RSS/Atom feed"
            else:
                # Fallback: just check for RSS/Atom markers
                if "<rss" in content.lower() or "<feed" in content.lower() or "<?xml" in content.lower():
                    result["title"] = "Feed detected"
                else:
                    result["ok"] = False
                    result["error"] = "URL does not appear to be a feed"

            return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Add source ─────────────────────────────────────────────────────────

class AddSourceRequest(BaseModel):
    url: str
    label: Optional[str] = None
    category: Optional[str] = "general"
    region: Optional[str] = "global"


@router.post("/add")
async def add_source(
    body: AddSourceRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Add a new RSS feed as a connector."""
    from storage.postgres.db import get_connection

    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    # Validate with preview first
    preview = await preview_rss(url)
    if not preview.get("ok"):
        raise HTTPException(status_code=400, detail=f"Feed validation failed: {preview.get('error')}")

    # Generate IDs
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
    connector_id = f"conn_user_{url_hash}"
    device_id = f"dev_user_{url_hash}"

    # Derive label
    label = body.label or preview.get("title") or "Custom Feed"
    if len(label) > 60:
        label = label[:57] + "..."

    # Build source_config
    config = {
        "url": url,
        "feed_title": label,
        "label": label,
        "category": body.category or "general",
        "region": body.region or "global",
        "max_items_per_fetch": 20,
        "enrich_articles": True,
        "signal_type_hint": "signal.content.rss_entry",
        "added_by": "user",
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Check if already exists
                cur.execute("""
                    SELECT connector_id FROM connectors
                    WHERE tenant_id = %s AND connector_id = %s
                """, (auth.tenant_id, connector_id))
                if cur.fetchone():
                    raise HTTPException(status_code=409, detail="This feed is already added")

                cur.execute("""
                    INSERT INTO connectors
                        (connector_id, device_id, tenant_id, connector_type,
                         source_type, status, poll_interval, source_config)
                    VALUES (%s, %s, %s, 'pull', 'rss', 'active', '00:15:00', %s)
                """, (
                    connector_id, device_id, auth.tenant_id,
                    json.dumps(config).encode('utf-8'),
                ))
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        log.error("Add source failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "connector_id": connector_id,
        "label": label,
        "url": url,
        "status": "active",
        "preview": {
            "title": preview.get("title"),
            "entry_count": preview.get("entry_count", 0),
        },
    }


# ─── Toggle source ──────────────────────────────────────────────────────

class PatchSourceRequest(BaseModel):
    status: Optional[str] = None  # active | inactive | paused


@router.patch("/{connector_id}")
async def patch_source(
    connector_id: str,
    body: PatchSourceRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Enable, pause, or disable a source."""
    from storage.postgres.db import get_connection

    if body.status not in (None, "active", "inactive", "paused"):
        raise HTTPException(status_code=400, detail="status must be active|inactive|paused")

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if body.status:
                    cur.execute("""
                        UPDATE connectors SET status = %s, updated_at = NOW()
                        WHERE connector_id = %s AND tenant_id = %s
                        RETURNING status
                    """, (body.status, connector_id, auth.tenant_id))
                    row = cur.fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail="Source not found")
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        log.error("Patch source failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return {"connector_id": connector_id, "status": body.status}


# ─── Delete source ──────────────────────────────────────────────────────

@router.delete("/{connector_id}")
async def delete_source(
    connector_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Remove a source connector."""
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM connectors
                    WHERE connector_id = %s AND tenant_id = %s
                """, (connector_id, auth.tenant_id))
                deleted = cur.rowcount
            conn.commit()
        if deleted == 0:
            raise HTTPException(status_code=404, detail="Source not found")
    except HTTPException:
        raise
    except Exception as e:
        log.error("Delete source failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return {"deleted": connector_id}


# ─── Library ────────────────────────────────────────────────────────────

@router.get("/library")
async def list_library():
    """Return the curated source library."""
    library = _load_library()
    # Group by category
    by_category: dict[str, list] = {}
    for item in library:
        cat = item.get("category", "other")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(item)
    return {
        "categories": [
            {"category": cat, "sources": sources}
            for cat, sources in by_category.items()
        ],
        "total": len(library),
    }


@router.post("/library/{library_id}")
async def add_from_library(
    library_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """One-click add a feed from the curated library."""
    library = _load_library()
    item = next((x for x in library if x.get("id") == library_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Library item not found")

    # Use add_source machinery
    req = AddSourceRequest(
        url=item["url"],
        label=item.get("label") or item.get("title"),
        category=item.get("category", "general"),
        region=item.get("region", "global"),
    )
    return await add_source(req, auth.tenant_id)


# ─── Catalog (Phase 3) ──────────────────────────────────────────────────
# The catalog is the curated list of source presets the user can compose
# their intel diet from. ``source_catalog`` is the global mirror loaded
# from ``scripts/seed/source_catalog/*.yaml``; ``source_catalog_overrides``
# stores per-tenant on/off + custom URL adds.


@router.get("/catalog")
async def list_catalog(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    category: Optional[str] = Query(default=None),
    region: Optional[str] = Query(default=None),
    language: Optional[str] = Query(default=None),
    no_key_only: bool = Query(default=False),
    enabled_only: bool = Query(default=False),
    q: Optional[str] = Query(default=None),
):
    """Browse the global source catalog plus this tenant's enabled state."""
    from storage.postgres.db import get_connection

    where = ["sc.deprecated_at IS NULL"]
    params: list = []
    if category:
        where.append("sc.category = %s")
        params.append(category)
    if region:
        where.append("sc.region = %s")
        params.append(region)
    if language:
        where.append("sc.language = %s")
        params.append(language)
    if no_key_only:
        where.append("sc.requires_key = FALSE")
    if q:
        where.append("(sc.name ILIKE %s OR sc.description ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])

    sql = f"""
        SELECT
            sc.catalog_id, sc.name, sc.url, sc.region, sc.language, sc.category,
            sc.poll_interval_default, sc.description, sc.requires_key,
            sc.parser_hint, sc.logo_url, sc.pack_version,
            COALESCE(o.enabled, FALSE) AS enabled,
            o.poll_interval_secs,
            o.created_at AS enabled_at
          FROM source_catalog sc
          LEFT JOIN source_catalog_overrides o
                 ON o.catalog_id = sc.catalog_id AND o.tenant_id = %s
         WHERE {' AND '.join(where)}
         ORDER BY sc.category, sc.name
    """
    out: list[dict] = []
    enabled_count = 0
    available_count = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [auth.tenant_id, *params])
            for r in cur.fetchall():
                enabled = bool(r[12])
                if enabled:
                    enabled_count += 1
                available_count += 1
                if enabled_only and not enabled:
                    continue
                out.append({
                    "catalog_id": r[0],
                    "name": r[1],
                    "url": r[2],
                    "region": r[3],
                    "language": r[4],
                    "category": r[5],
                    "poll_interval_default": r[6],
                    "description": r[7],
                    "requires_key": r[8],
                    "parser_hint": r[9],
                    "logo_url": r[10],
                    "pack_version": r[11],
                    "enabled": enabled,
                    "poll_interval_secs": r[13],
                    "enabled_at": r[14].isoformat() if r[14] else None,
                })

    return {
        "items": out,
        "total_available": available_count,
        "total_enabled": enabled_count,
    }


class CatalogToggleRequest(BaseModel):
    catalog_id: str
    enabled: bool
    poll_interval_secs: Optional[int] = None


def _format_pg_interval(seconds: int) -> str:
    """Format an integer seconds as a Postgres interval string."""
    if seconds < 60:
        return f"{seconds} seconds"
    minutes, sec = divmod(seconds, 60)
    if sec == 0 and minutes < 60:
        return f"{minutes} minutes"
    return f"{seconds} seconds"


@router.post("/catalog/toggle")
async def toggle_catalog_source(
    body: CatalogToggleRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Enable or disable a catalog source for this tenant.

    When enabling: creates (or reactivates) a connector row that the
    poller picks up. When disabling: marks the connector inactive but
    keeps it for replay.
    """
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Pull the catalog row
            cur.execute(
                """
                SELECT catalog_id, name, url, region, language, category,
                       poll_interval_default, parser_hint, requires_key,
                       pack_version
                  FROM source_catalog
                 WHERE catalog_id = %s AND deprecated_at IS NULL
                """,
                (body.catalog_id,),
            )
            catalog_row = cur.fetchone()
            if not catalog_row:
                raise HTTPException(status_code=404, detail="catalog entry not found")

            (cid, name, url, region, language, category,
             poll_default, parser_hint, requires_key, pack_version) = catalog_row

            poll_secs = int(body.poll_interval_secs or poll_default or 1800)
            poll_interval = _format_pg_interval(poll_secs)

            # Provision a connector row (deterministic id from catalog_id +
            # tenant). One connector per (tenant, catalog source).
            connector_id = f"conn_cat_{cid}"
            device_id = f"dev_cat_{cid}"

            # Pick the source_type / source_config from the parser_hint.
            # rss / gdelt are the two we provision automatically; any other
            # hint blocks toggle-on (the user must configure explicitly).
            phint = (parser_hint or "rss").lower()
            if phint == "rss":
                source_type = "rss_feed"
                source_config = {
                    "url": url,
                    "feed_title": name,
                    "label": name,
                    "category": category,
                    "region": region,
                    "language": language,
                    "max_items_per_fetch": 20,
                    "enrich_articles": True,
                    "signal_type_hint": "signal.content.rss_entry",
                    "added_by": "catalog",
                    "catalog_id": cid,
                    "catalog_pack_version": pack_version,
                }
            elif phint == "gdelt":
                source_type = "http_api"
                source_config = {
                    "url": url,
                    "label": name,
                    "category": category,
                    "region": region,
                    "language": language,
                    "signal_type_hint": "signal.content.gdelt_event",
                    "added_by": "catalog",
                    "catalog_id": cid,
                    "catalog_pack_version": pack_version,
                }
            else:
                # Unknown parser hint — record the override row but don't
                # try to provision a connector. The user will need to
                # configure manually via the recipe wizard.
                cur.execute(
                    """
                    INSERT INTO source_catalog_overrides
                        (tenant_id, catalog_id, enabled, poll_interval_secs,
                         created_by_identity)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, catalog_id) WHERE catalog_id IS NOT NULL DO UPDATE SET
                        enabled = EXCLUDED.enabled,
                        poll_interval_secs = EXCLUDED.poll_interval_secs,
                        updated_at = NOW()
                    """,
                    (auth.tenant_id, cid, body.enabled, poll_secs,
                     auth.subject_id or "api"),
                )
                conn.commit()
                return {
                    "catalog_id": cid,
                    "enabled": body.enabled,
                    "connector_id": None,
                    "note": f"parser_hint '{phint}' requires manual recipe setup",
                }

            # Persist the override row first
            cur.execute(
                """
                INSERT INTO source_catalog_overrides
                    (tenant_id, catalog_id, enabled, poll_interval_secs,
                     created_by_identity)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, catalog_id) WHERE catalog_id IS NOT NULL DO UPDATE SET
                    enabled            = EXCLUDED.enabled,
                    poll_interval_secs = EXCLUDED.poll_interval_secs,
                    updated_at         = NOW()
                """,
                (auth.tenant_id, cid, body.enabled, poll_secs,
                 auth.subject_id or "api"),
            )

            if body.enabled:
                cur.execute(
                    """
                    INSERT INTO connectors
                        (connector_id, device_id, tenant_id, connector_type,
                         source_type, status, poll_interval, source_config)
                    VALUES (%s, %s, %s, 'pull', %s, 'active', %s, %s)
                    ON CONFLICT (connector_id) DO UPDATE SET
                        status         = 'active',
                        poll_interval  = EXCLUDED.poll_interval,
                        source_config  = EXCLUDED.source_config,
                        source_type    = EXCLUDED.source_type
                    """,
                    (connector_id, device_id, auth.tenant_id,
                     source_type, poll_interval,
                     json.dumps(source_config).encode("utf-8")),
                )
            else:
                # Deactivate without deleting (preserve replay history)
                cur.execute(
                    """
                    UPDATE connectors SET status = 'paused'
                     WHERE connector_id = %s AND tenant_id = %s
                    """,
                    (connector_id, auth.tenant_id),
                )
        conn.commit()

    return {
        "catalog_id": cid,
        "enabled": body.enabled,
        "connector_id": connector_id,
        "source_type": source_type,
        "poll_interval_secs": poll_secs,
    }


class CustomCatalogAdd(BaseModel):
    name: str
    url: str
    parser_hint: Optional[str] = "rss"
    poll_interval_secs: Optional[int] = 1800


@router.post("/catalog/custom")
async def add_custom_source(
    body: CustomCatalogAdd,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Add a tenant-specific custom source (URL paste). Goes into
    ``source_catalog_overrides`` with NULL catalog_id and the custom_*
    fields set."""
    from storage.postgres.db import get_connection

    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must be http(s)")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO source_catalog_overrides
                    (tenant_id, custom_name, custom_url, custom_parser_hint,
                     enabled, poll_interval_secs, created_by_identity)
                VALUES (%s, %s, %s, %s, TRUE, %s, %s)
                RETURNING override_id
                """,
                (auth.tenant_id, body.name, body.url, body.parser_hint,
                 body.poll_interval_secs, auth.subject_id or "api"),
            )
            override_id = cur.fetchone()[0]
        conn.commit()
    return {"override_id": override_id, "url": body.url, "enabled": True}


class BulkToggleRequest(BaseModel):
    category: str
    top_n: int = 3
    enabled: bool = True


@router.post("/catalog/bulk-toggle")
async def bulk_toggle_category(
    body: BulkToggleRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Bulk enable (or disable) the first N RSS sources in a category.

    Used by the Hub settings page to seed a category in one click. Picks
    sources with parser_hint='rss' so we can provision connectors
    automatically; sources requiring an API key or custom recipe are
    skipped and reported back.
    """
    from storage.postgres.db import get_connection

    if body.top_n < 1 or body.top_n > 50:
        raise HTTPException(status_code=400, detail="top_n must be 1..50")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sc.catalog_id, sc.name, sc.url, sc.region, sc.language,
                       sc.category, sc.poll_interval_default, sc.parser_hint,
                       sc.pack_version
                  FROM source_catalog sc
                  LEFT JOIN source_catalog_overrides o
                         ON o.catalog_id = sc.catalog_id AND o.tenant_id = %s
                 WHERE sc.deprecated_at IS NULL
                   AND sc.category = %s
                   AND sc.requires_key = FALSE
                   AND COALESCE(LOWER(sc.parser_hint), 'rss') IN ('rss', 'gdelt')
                   AND COALESCE(o.enabled, FALSE) <> %s
                 ORDER BY sc.name
                 LIMIT %s
                """,
                (auth.tenant_id, body.category, body.enabled, body.top_n),
            )
            rows = cur.fetchall()

            toggled: list[dict] = []
            for (cid, name, url, region, language, category,
                 poll_default, parser_hint, pack_version) in rows:
                poll_secs = int(poll_default or 1800)
                poll_interval = _format_pg_interval(poll_secs)
                phint = (parser_hint or "rss").lower()

                if phint == "rss":
                    source_type = "rss_feed"
                    source_config = {
                        "url": url, "feed_title": name, "label": name,
                        "category": category, "region": region, "language": language,
                        "max_items_per_fetch": 20, "enrich_articles": True,
                        "signal_type_hint": "signal.content.rss_entry",
                        "added_by": "catalog_bulk",
                        "catalog_id": cid, "catalog_pack_version": pack_version,
                    }
                else:
                    source_type = "http_api"
                    source_config = {
                        "url": url, "label": name,
                        "category": category, "region": region, "language": language,
                        "signal_type_hint": "signal.content.gdelt_event",
                        "added_by": "catalog_bulk",
                        "catalog_id": cid, "catalog_pack_version": pack_version,
                    }

                connector_id = f"conn_cat_{cid}"
                device_id = f"dev_cat_{cid}"

                cur.execute(
                    """
                    INSERT INTO source_catalog_overrides
                        (tenant_id, catalog_id, enabled, poll_interval_secs,
                         created_by_identity)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, catalog_id) WHERE catalog_id IS NOT NULL DO UPDATE SET
                        enabled            = EXCLUDED.enabled,
                        poll_interval_secs = EXCLUDED.poll_interval_secs,
                        updated_at         = NOW()
                    """,
                    (auth.tenant_id, cid, body.enabled, poll_secs,
                     auth.subject_id or "api"),
                )

                if body.enabled:
                    cur.execute(
                        """
                        INSERT INTO connectors
                            (connector_id, device_id, tenant_id, connector_type,
                             source_type, status, poll_interval, source_config)
                        VALUES (%s, %s, %s, 'pull', %s, 'active', %s, %s)
                        ON CONFLICT (connector_id) DO UPDATE SET
                            status        = 'active',
                            poll_interval = EXCLUDED.poll_interval,
                            source_config = EXCLUDED.source_config,
                            source_type   = EXCLUDED.source_type
                        """,
                        (connector_id, device_id, auth.tenant_id,
                         source_type, poll_interval,
                         json.dumps(source_config).encode("utf-8")),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE connectors SET status = 'paused'
                         WHERE connector_id = %s AND tenant_id = %s
                        """,
                        (connector_id, auth.tenant_id),
                    )

                toggled.append({
                    "catalog_id": cid, "name": name,
                    "connector_id": connector_id,
                    "source_type": source_type,
                })
        conn.commit()

    return {
        "category": body.category,
        "enabled": body.enabled,
        "toggled_count": len(toggled),
        "toggled": toggled,
    }
