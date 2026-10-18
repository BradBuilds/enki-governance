"""Validated, context-scoped alias store.

Aliases learned by the governed alias-collapse (flight-log "AA" → Alex Acme)
are context-dependent — they hold only inside the scope they were proven in (a
library), never globally. This module records them (with the equation evidence that
validated them) and looks them up within a scope. It deliberately does NOT touch the
global `entities.aliases[]` array for scoped aliases, so "AA" can never leak to mean
Acme outside the flight-log corpus.
"""

from __future__ import annotations

import json
from typing import Optional

from storage.postgres.db import get_connection
from core.collapse.equation import _norm


def record_validated_alias(entity_id: int, surface_form: str, *, scope_kind: str = "library",
                           scope_id: Optional[int] = None, confidence: float = 0.0,
                           evidence: Optional[dict] = None, tenant_id: str = "default",
                           validation_status: str = "validated_by_collapse",
                           source: str = "alias_collapse", dsn: Optional[str] = None) -> None:
    """Upsert a validated alias binding. Scoped by default (library) — a short/
    ambiguous surface never becomes a global alias."""
    norm = _norm(surface_form)
    if not norm or not entity_id:
        return
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO entity_aliases
                    (tenant_id, entity_id, surface_form, normalized, scope_kind, scope_id,
                     confidence, evidence, validation_status, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, normalized, scope_kind, COALESCE(scope_id, -1), entity_id)
                DO UPDATE SET confidence = GREATEST(entity_aliases.confidence, EXCLUDED.confidence),
                              evidence = EXCLUDED.evidence,
                              validation_status = EXCLUDED.validation_status
            """, (tenant_id, entity_id, surface_form[:500], norm, scope_kind, scope_id,
                  float(confidence), json.dumps(evidence or {}), validation_status, source))
        conn.commit()


def record_discrepancy(surface_form: str, *, suspected_type: str = "person",
                       scope_kind: str = "library", scope_id: Optional[int] = None,
                       document_id: Optional[int] = None, candidates: Optional[list] = None,
                       top_score: float = 0.0, margin: float = 0.0, reason: str = "ambiguous",
                       tenant_id: str = "default", dsn: Optional[str] = None) -> str:
    """Park an UNRESOLVED discrepancy in the LDU with rich context so a human/AI can
    resolve it fast — the near-miss candidates, their scores, the scope, and why it
    parked. Upserts by (scope, surface). Returns the ldu_p_id."""
    norm = _norm(surface_form)
    ldu_p_id = f"disc:{scope_kind}:{scope_id if scope_id is not None else '-'}:{norm}"
    evidence = {"scope_kind": scope_kind, "scope_id": scope_id, "document_id": document_id,
                "candidates": (candidates or [])[:6], "margin": round(float(margin), 3),
                "reason": reason}
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO entity_ldu_p
                    (ldu_p_id, tenant_id, raw_name, normalized_name, suspected_type,
                     source_pipeline, confidence, evidence, rejection_reason, status, metadata)
                VALUES (%s,%s,%s,%s,%s,'alias_collapse',%s,%s,%s,'pending',%s)
                ON CONFLICT (ldu_p_id) DO UPDATE
                   SET confidence = EXCLUDED.confidence,
                       evidence = EXCLUDED.evidence,
                       rejection_reason = EXCLUDED.rejection_reason
                 WHERE entity_ldu_p.status = 'pending'
            """, (ldu_p_id, tenant_id, surface_form[:255], norm, suspected_type,
                  float(top_score), json.dumps(evidence), reason[:500],
                  json.dumps({"kind": "alias_discrepancy"})))
        conn.commit()
    return ldu_p_id


def list_discrepancies(*, scope_kind: Optional[str] = None, scope_id: Optional[int] = None,
                       status: str = "pending", limit: int = 40,
                       tenant_id: str = "default", dsn: Optional[str] = None) -> list[dict]:
    """Open alias-collapse discrepancies with their candidates/context, for review
    (chat `ldu_queue` + the governance board). Filters by scope when given."""
    rows = []
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ldu_p_id, raw_name, suspected_type, confidence, evidence,
                       rejection_reason, status, created_at
                  FROM entity_ldu_p
                 WHERE tenant_id = %s AND source_pipeline = 'alias_collapse'
                   AND status = %s
                 ORDER BY confidence DESC, created_at DESC
                 LIMIT %s
            """, (tenant_id, status, min(limit, 200)))
            for r in cur.fetchall():
                ev = r[4] if isinstance(r[4], dict) else (json.loads(r[4]) if r[4] else {})
                if scope_kind and ev.get("scope_kind") != scope_kind:
                    continue
                if scope_id is not None and ev.get("scope_id") != scope_id:
                    continue
                rows.append({
                    "ldu_p_id": r[0], "surface_form": r[1], "suspected_type": r[2],
                    "top_score": r[3], "candidates": ev.get("candidates", []),
                    "scope_kind": ev.get("scope_kind"), "scope_id": ev.get("scope_id"),
                    "reason": ev.get("reason") or r[5], "status": r[6],
                    "created_at": r[7].isoformat() if r[7] else None,
                })
    return rows


def demote_fragment_to_ldup(entity_id: int, reason: str, *, tenant_id: str = "default",
                            dsn: Optional[str] = None) -> bool:
    """Move a decomposed manifest-fragment entity's identity to the LDU (parked,
    pending) and mark it reclassified so it drops off the entity surfaces. Reversible
    (no delete) — the row stays for FK integrity; the flag hides it. Returns True if
    the entity was demoted."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT canonical_name, canonical_type, normalized_name FROM entities WHERE entity_id=%s",
                        (entity_id,))
            row = cur.fetchone()
            if not row:
                return False
            cname, ctype, norm = row
            cur.execute("""
                INSERT INTO entity_ldu_p
                    (ldu_p_id, tenant_id, raw_name, normalized_name, suspected_type,
                     source_pipeline, rejection_reason, status, metadata)
                VALUES (%s,%s,%s,%s,%s,'alias_collapse_demote',%s,'pending',%s)
                ON CONFLICT (ldu_p_id) DO NOTHING
            """, (f"demoted:{entity_id}", tenant_id, cname, norm or (cname or "").lower(),
                  ctype or "person", reason[:500], json.dumps({"from_entity_id": entity_id})))
            cur.execute("""
                UPDATE entities
                   SET gdu_canonical = FALSE,
                       envelope = COALESCE(envelope, '{}'::jsonb)
                                  || jsonb_build_object('reclassified_to','ldu_p',
                                                        'demoted_reason', %s),
                       updated_at = NOW()
                 WHERE entity_id = %s
            """, (reason[:300], entity_id))
        conn.commit()
    return True


def lookup_scoped_alias(surface_form: str, *, scope_kind: str = "library",
                        scope_id: Optional[int] = None, tenant_id: str = "default",
                        dsn: Optional[str] = None) -> Optional[int]:
    """Fast-path: a previously-validated alias for this surface WITHIN the given
    scope (or a global one). Returns entity_id or None. Only 'validated_by_collapse'
    / 'operator' bindings resolve."""
    norm = _norm(surface_form)
    if not norm:
        return None
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT entity_id FROM entity_aliases
                 WHERE tenant_id = %s AND normalized = %s
                   AND validation_status IN ('validated_by_collapse','operator')
                   AND (scope_kind = 'global'
                        OR (scope_kind = %s AND scope_id = %s))
                 ORDER BY confidence DESC LIMIT 1
            """, (tenant_id, norm, scope_kind, scope_id))
            r = cur.fetchone()
    return int(r[0]) if r else None
