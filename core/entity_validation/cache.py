"""
Persistent KB cache — Layer B of the validation stack.

Two tables:
  * entity_kb_cache      — normalized_name → canonical entity match
  * wikidata_qid_cache   — Q-ID → mapped canonical type (P279-walk results)

The resolver hits entity_kb_cache BEFORE any provider. A cache hit is one
DB read; a miss triggers the provider chain which writes its result back.

User contributions (kb_user_contributions) override the cache. Before any
automated match, the resolver checks for a user contribution for the same
normalized_name — if found, it wins.

This module is intentionally stateless — every call takes a conn so it
plays nicely with the governor's one-conn-per-worker model and with the
existing extraction pipeline.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from core.entity_validation.types import Match

log = logging.getLogger("enki.validation.cache")


def _normalize(raw: str) -> str:
    return " ".join((raw or "").strip().lower().split())


# ── Layer C: user contributions (authoritative) ───────────────────────

def check_user_contribution(
    conn,
    raw_text: str,
    kind: str,
    *,
    tenant_id: str = "default",
) -> Optional[Match]:
    """Return the user's most recent contribution for this candidate, if
    any. User overrides beat upstream: if a human said 'Strait of Hormuz
    is a strait', Wikidata can't silently retype it."""
    normalized = _normalize(raw_text)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT canonical_name, canonical_type, identifiers, action, user_id,
                   created_at
              FROM kb_user_contributions
             WHERE tenant_id = %s
               AND normalized_name = %s
               AND kind = %s
               AND action IN ('classify', 'confirm')
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (tenant_id, normalized, kind),
        )
        row = cur.fetchone()
    if not row:
        return None

    canonical_name, canonical_type, identifiers, action, user_id, created_at = row
    ids = identifiers or {}
    if isinstance(ids, memoryview): ids = json.loads(bytes(ids))
    elif isinstance(ids, str):
        try: ids = json.loads(ids)
        except Exception: ids = {}

    return Match(
        source="user",
        source_id=str(user_id),
        source_url="",
        canonical_name=canonical_name or raw_text,
        canonical_type=canonical_type or "",
        aliases=[],
        identifiers=ids,
        confidence=1.0,
        description=f"user contribution by {user_id} ({action})",
        extra={"contribution_at": created_at.isoformat() if created_at else None},
    )


# ── Layer B: persistent KB cache ──────────────────────────────────────

def lookup_cache(
    conn,
    raw_text: str,
    kind: str,
    *,
    tenant_id: str = "default",
) -> Optional[Match]:
    """Check entity_kb_cache. Bumps hit_count + last_hit_at on match."""
    normalized = _normalize(raw_text)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE entity_kb_cache SET
                hit_count = hit_count + 1,
                last_hit_at = NOW()
             WHERE tenant_id = %s AND normalized_name = %s AND kind = %s
            RETURNING canonical_name, canonical_type, aliases, source, source_id,
                      source_url, identifiers, description, thumbnail_url, confidence
            """,
            (tenant_id, normalized, kind),
        )
        row = cur.fetchone()
    if not row:
        return None

    (canonical_name, canonical_type, aliases, source, source_id, source_url,
     identifiers, description, thumbnail_url, confidence) = row
    ids = identifiers or {}
    if isinstance(ids, memoryview): ids = json.loads(bytes(ids))
    elif isinstance(ids, str):
        try: ids = json.loads(ids)
        except Exception: ids = {}

    conn.commit()   # commit the hit_count bump
    return Match(
        source=source,
        source_id=source_id or "",
        source_url=source_url or "",
        canonical_name=canonical_name,
        canonical_type=canonical_type,
        aliases=list(aliases or []),
        identifiers=ids,
        confidence=float(confidence or 0.0),
        description=description,
        thumbnail_url=thumbnail_url,
        extra={"from_cache": True},
    )


def write_cache(
    conn,
    raw_text: str,
    kind: str,
    match: Match,
    *,
    tenant_id: str = "default",
) -> None:
    """Persist a provider match for future reuse."""
    normalized = _normalize(raw_text)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO entity_kb_cache (
                tenant_id, normalized_name, kind,
                canonical_name, canonical_type, aliases,
                source, source_id, source_url,
                identifiers, description, thumbnail_url, confidence,
                hit_count, first_seen_at, last_hit_at, last_verified_at
            ) VALUES (
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s::jsonb, %s, %s, %s,
                1, NOW(), NOW(), NOW()
            )
            ON CONFLICT (tenant_id, normalized_name, kind) DO UPDATE SET
                canonical_name    = EXCLUDED.canonical_name,
                canonical_type    = EXCLUDED.canonical_type,
                aliases           = EXCLUDED.aliases,
                source            = EXCLUDED.source,
                source_id         = EXCLUDED.source_id,
                source_url        = EXCLUDED.source_url,
                identifiers       = EXCLUDED.identifiers,
                description       = COALESCE(EXCLUDED.description, entity_kb_cache.description),
                thumbnail_url     = COALESCE(EXCLUDED.thumbnail_url, entity_kb_cache.thumbnail_url),
                confidence        = GREATEST(EXCLUDED.confidence, entity_kb_cache.confidence),
                last_verified_at  = NOW()
            """,
            (
                tenant_id, normalized, kind,
                match.canonical_name, match.canonical_type, match.aliases or [],
                match.source, match.source_id, match.source_url,
                json.dumps(match.identifiers or {}),
                match.description, match.thumbnail_url,
                float(match.confidence or 0.0),
            ),
        )
    conn.commit()


# ── Wikidata Q-ID → type cache (for P279-walk results) ────────────────

def lookup_qid_type(conn, qid: str) -> Optional[tuple[Optional[str], str]]:
    """Returns (mapped_type, resolved_via) or None if not cached.
    mapped_type can be None — caches the negative result so we don't
    re-walk a Q-ID that previously had no known ancestor."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE wikidata_qid_cache SET
                hit_count = hit_count + 1,
                last_hit_at = NOW()
             WHERE qid = %s
            RETURNING mapped_type, resolved_via
            """,
            (qid,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    conn.commit()
    return (row[0], row[1])


def write_qid_type(
    conn,
    qid: str,
    mapped_type: Optional[str],
    resolved_via: str,
    depth: int = 0,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wikidata_qid_cache (qid, mapped_type, resolved_via, depth)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (qid) DO UPDATE SET
                mapped_type  = EXCLUDED.mapped_type,
                resolved_via = EXCLUDED.resolved_via,
                depth        = EXCLUDED.depth,
                resolved_at  = NOW()
            """,
            (qid, mapped_type, resolved_via, depth),
        )
    conn.commit()


# ── User contribution writes (called by /nodes/:id/reclassify etc.) ──

def record_user_contribution(
    conn,
    *,
    user_id: str,
    raw_text: str,
    kind: str,
    action: str,          # classify | reject | confirm | merge
    canonical_name: Optional[str] = None,
    canonical_type: Optional[str] = None,
    target_node_id: Optional[str] = None,
    identifiers: Optional[dict] = None,
    note: Optional[str] = None,
    tenant_id: str = "default",
) -> int:
    """Insert a row into kb_user_contributions. Returns contribution_id.
    Phase 5C endpoints call this."""
    normalized = _normalize(raw_text)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kb_user_contributions (
                tenant_id, user_id, kind, raw_text, normalized_name,
                action, canonical_name, canonical_type, target_node_id,
                identifiers, note
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s::jsonb, %s
            )
            ON CONFLICT (tenant_id, normalized_name, kind, user_id, action) DO UPDATE SET
                canonical_name = EXCLUDED.canonical_name,
                canonical_type = EXCLUDED.canonical_type,
                target_node_id = EXCLUDED.target_node_id,
                identifiers    = EXCLUDED.identifiers,
                note           = EXCLUDED.note,
                created_at     = NOW()
            RETURNING contribution_id
            """,
            (
                tenant_id, user_id, kind, raw_text, normalized,
                action, canonical_name, canonical_type, target_node_id,
                json.dumps(identifiers or {}),
                note,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return int(row[0]) if row else 0
