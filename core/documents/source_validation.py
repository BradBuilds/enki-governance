"""
Source validation — route top corroborated entities to external knowledge bases.

After corpus_corroboration is populated, this module takes the highest-ranked
surface forms and looks them up against:
  - Wikipedia (REST summary API — gives summary + Wikidata QID)
  - Wikidata (full entity record via QID)

Results land in `entity_external_lookups`. A surface form that matches:
  - Wikipedia → has summary + label + URL + (usually) Wikidata QID
  - Wikidata directly → has label + summary + structured claims

A surface form that does NOT match either is recorded with `matched=false`
so we don't re-query it every run.

Design choices:
  - We only look up forms with corroboration_score above a floor (default
    document_count>=2 AND not is_fragment) to avoid hammering Wikipedia
    with regex garbage.
  - Each lookup is rate-limited via the existing httpx client in
    core.research.sources (Wikipedia's 200/s rate budget covers our needs).
  - Idempotent: UNIQUE constraint on (corpus_id, surface_form_norm, source)
    means re-runs UPSERT.

Public surface:

    validate_corpus(corpus_id, limit=50) -> summary

    get_validated(corpus_id, limit=50) -> list[dict] for UI display
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.source_validation")

# Default cap on how many top forms we route per call. Keeps runs bounded.
_DEFAULT_VALIDATION_LIMIT = 50


def _ensure_table() -> None:
    """Create entity_external_lookups if it doesn't exist."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_external_lookups (
                    lookup_id        BIGSERIAL PRIMARY KEY,
                    corpus_id        TEXT NOT NULL,
                    surface_form_norm TEXT NOT NULL,
                    source           TEXT NOT NULL,
                    external_id      TEXT,
                    label            TEXT,
                    summary          TEXT,
                    url              TEXT,
                    matched          BOOLEAN NOT NULL DEFAULT FALSE,
                    confidence       REAL,
                    raw              JSONB,
                    looked_up_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (corpus_id, surface_form_norm, source)
                );
                CREATE INDEX IF NOT EXISTS idx_external_corpus
                    ON entity_external_lookups(corpus_id, matched);
                CREATE INDEX IF NOT EXISTS idx_external_qid
                    ON entity_external_lookups(external_id)
                    WHERE external_id IS NOT NULL;
                """
            )
        conn.commit()


def _save_lookup(
    corpus_id: str, surface_norm: str, source: str,
    external_id: Optional[str], label: Optional[str],
    summary: Optional[str], url: Optional[str],
    matched: bool, confidence: Optional[float],
    raw: Optional[dict],
) -> None:
    """UPSERT one lookup row."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entity_external_lookups
                    (corpus_id, surface_form_norm, source, external_id,
                     label, summary, url, matched, confidence, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (corpus_id, surface_form_norm, source) DO UPDATE SET
                    external_id = EXCLUDED.external_id,
                    label = EXCLUDED.label,
                    summary = EXCLUDED.summary,
                    url = EXCLUDED.url,
                    matched = EXCLUDED.matched,
                    confidence = EXCLUDED.confidence,
                    raw = EXCLUDED.raw,
                    looked_up_at = NOW()
                """,
                (
                    corpus_id, surface_norm, source, external_id,
                    label, summary, url, matched, confidence,
                    json.dumps(raw) if raw else None,
                ),
            )
        conn.commit()


# Collapse correctness: surface forms shorter than this should NEVER
# auto-collapse on name-only — too ambiguous. "ALEX", "PARIS", "GEORGE"
# are real first names but matching them to whatever entity our prior DB
# happens to have with that normalized name is a guaranteed false collapse.
# These get recorded as collapse_candidate (audit log) but NOT linked.
_MIN_AUTOCOLLAPSE_LENGTH = 5


def _log_collapse_decision(
    corpus_id: str, surface_norm: str, action: str,
    rule_fired: str, score: float, confidence_tier: str,
    entity_id: Optional[int] = None, merged_into: Optional[int] = None,
    evidence_for: Optional[list] = None, evidence_against: Optional[list] = None,
    decided_by: str = "auto:source_validation_v1",
    notes: Optional[str] = None,
) -> None:
    """Write one row to collapse_decisions. Every resolver decision (merge,
    keep_separate, candidate, etc.) goes here so we can audit + reverse."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO collapse_decisions
                        (corpus_id, surface_form_norm, action, rule_fired,
                         score, confidence_tier, entity_id, merged_into_entity_id,
                         evidence_for, evidence_against, decided_by, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                            %s, %s)
                    """,
                    (
                        corpus_id, surface_norm, action, rule_fired,
                        score, confidence_tier, entity_id, merged_into,
                        json.dumps(evidence_for or []),
                        json.dumps(evidence_against or []),
                        decided_by, notes,
                    ),
                )
            conn.commit()
    except Exception as e:
        log.debug("collapse_decisions insert failed: %s", e)


def _route_internal_nodes(
    corpus_id: str, surface: str, surface_norm: str,
    suggested_type: Optional[str] = None,
) -> dict:
    """Check the existing entities table BEFORE going external.

    Match order:
      1. Exact match on normalized_name
      2. Exact match on aliases[] (case-insensitive via upper(unnest))
    Returns a "matched" record if we find an existing entity; the doc-mention
    can then be linked to it (collapse) instead of creating a duplicate.

    Collapse correctness gates:
      • Surface form must be >= _MIN_AUTOCOLLAPSE_LENGTH (5) chars.
        Shorter → record as collapse_candidate, do NOT auto-collapse.
      • Existing entity's canonical_type must match suggested_type (when both
        known). Type mismatch → keep_separate.
    """
    # Gate 1: surface form too short for confident name-only collapse
    if len(surface_norm) < _MIN_AUTOCOLLAPSE_LENGTH:
        _save_lookup(
            corpus_id=corpus_id, surface_norm=surface_norm,
            source="internal_node",
            external_id=None, label=None, summary=None, url=None,
            matched=False, confidence=0.0, raw={"reason": "short_surface_form"},
        )
        _log_collapse_decision(
            corpus_id=corpus_id, surface_norm=surface_norm,
            action="collapse_candidate",
            rule_fired="short_surface_form_blocked",
            score=0.0, confidence_tier="suggested",
            notes=f"surface len {len(surface_norm)} < {_MIN_AUTOCOLLAPSE_LENGTH}; "
                  "needs co-occurrence or identifier evidence",
        )
        return {"source": "internal_node", "matched": False,
                "reason": "short_surface_form"}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, canonical_name, canonical_type,
                       signal_count, identifiers, aliases
                  FROM entities
                 WHERE normalized_name = %s
                 ORDER BY signal_count DESC
                 LIMIT 1
                """,
                (surface_norm,),
            )
            row = cur.fetchone()
            if not row:
                # Try aliases (upper-cased comparison)
                cur.execute(
                    """
                    SELECT entity_id, canonical_name, canonical_type,
                           signal_count, identifiers, aliases
                      FROM entities
                     WHERE UPPER(%s) = ANY (SELECT UPPER(a) FROM unnest(aliases) AS a)
                     ORDER BY signal_count DESC
                     LIMIT 1
                    """,
                    (surface,),
                )
                row = cur.fetchone()

    if not row:
        _save_lookup(
            corpus_id=corpus_id, surface_norm=surface_norm,
            source="internal_node",
            external_id=None, label=None, summary=None, url=None,
            matched=False, confidence=0.0, raw=None,
        )
        _log_collapse_decision(
            corpus_id=corpus_id, surface_norm=surface_norm,
            action="kept_separate",
            rule_fired="no_internal_match",
            score=0.0, confidence_tier="no",
        )
        return {"source": "internal_node", "matched": False}

    entity_id, canonical_name, canonical_type, signal_count, identifiers, aliases = row

    # Gate 2: canonical_type mismatch with suggested_type → reject
    # (e.g., suggested_type='place' shouldn't auto-merge with canonical_type='person')
    if suggested_type and canonical_type and suggested_type != "unknown":
        if canonical_type != suggested_type:
            _save_lookup(
                corpus_id=corpus_id, surface_norm=surface_norm,
                source="internal_node",
                external_id=None, label=canonical_name, summary=None, url=None,
                matched=False, confidence=0.0,
                raw={"reason": "type_mismatch",
                     "suggested_type": suggested_type,
                     "canonical_type": canonical_type,
                     "entity_id": entity_id},
            )
            _log_collapse_decision(
                corpus_id=corpus_id, surface_norm=surface_norm,
                action="kept_separate",
                rule_fired="type_mismatch_blocked",
                score=0.0, confidence_tier="no",
                evidence_against=[{
                    "axis": "type", "value": f"{suggested_type} vs {canonical_type}",
                    "weight": -1.0,
                }],
                notes=f"existing entity #{entity_id} ({canonical_name}) is "
                      f"{canonical_type}, but mention claims {suggested_type}",
            )
            return {"source": "internal_node", "matched": False,
                    "reason": "type_mismatch"}

    _save_lookup(
        corpus_id=corpus_id, surface_norm=surface_norm,
        source="internal_node",
        external_id=str(entity_id),
        label=canonical_name,
        summary=f"existing {canonical_type} (signal_count={signal_count}, aliases={len(aliases or [])})",
        url=None,
        matched=True,
        confidence=0.9,
        raw={"entity_id": entity_id, "canonical_type": canonical_type,
             "signal_count": signal_count, "identifiers": identifiers},
    )
    _log_collapse_decision(
        corpus_id=corpus_id, surface_norm=surface_norm,
        action="merged_into",
        rule_fired="name_match_internal",
        score=0.9, confidence_tier="soft",
        entity_id=entity_id, merged_into=entity_id,
        evidence_for=[{
            "axis": "name", "value": "normalized_name_exact_match",
            "weight": 0.7,
        }, {
            "axis": "signal_count", "value": signal_count, "weight": 0.2,
        }],
        notes=f"merged surface form '{surface}' into entity #{entity_id} "
              f"({canonical_name}, {canonical_type})",
    )
    return {
        "source": "internal_node",
        "matched": True,
        "entity_id": entity_id,
        "canonical_name": canonical_name,
        "canonical_type": canonical_type,
    }


def _corpus_trust_class(corpus_id: str) -> Optional[str]:
    """Look up the corpus' trust class via dataset_policies.
    Returns None if not configured."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT trust_class FROM dataset_policies WHERE dataset_id = %s",
                    (corpus_id,),
                )
                row = cur.fetchone()
                if not row:
                    # Fall back to documents.trust_class on the corpus' docs
                    cur.execute(
                        "SELECT trust_class FROM documents WHERE dataset_id = %s LIMIT 1",
                        (corpus_id,),
                    )
                    row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


def _route_wikipedia(
    corpus_id: str, surface: str, surface_norm: str,
    suggested_type: Optional[str] = None,
) -> dict:
    """Look up one surface form against Wikipedia.

    Two-step: search the title first (case-insensitive, finds best article)
    THEN fetch the summary of the top hit. The direct /page/summary/{title}
    endpoint is case-sensitive and rejects "Air Force" when the article is
    "United States Air Force"; search handles redirects + disambiguation.

    Collapse correctness:
      • When suggested_type is set, we verify the matched Wikidata QID's
        instance_of (P31) claim is type-compatible. Closes the "From → TV
        series", "Case Files → Mystery Case Files video game" class of
        false positives.
    """
    try:
        from core.research.sources import wikipedia_search, wikipedia_summary
        from core.documents.wikidata_types import is_compatible
    except Exception as e:
        log.warning("wikipedia helpers import failed: %s", e)
        return {"matched": False, "source": "wikipedia"}

    # Step 1: search to find the best article title.
    try:
        hits = wikipedia_search(surface, limit=3)
    except Exception as e:
        log.debug("wikipedia search failed for %r: %s", surface, e)
        hits = []

    best_title = hits[0]["title"] if hits else surface

    # Step 2: fetch the article summary using the resolved title.
    try:
        data = wikipedia_summary(best_title)
    except Exception as e:
        log.debug("wikipedia summary failed for %r: %s", best_title, e)
        data = None

    matched = bool(data and (data.get("summary") or data.get("wikidata_id")))

    # Step 3: type compatibility gate
    # Always runs when we have a QID. For corpora with trust_class=
    # review_required, we also run a stricter check that rejects
    # creative_work/event matches even when suggested_type is unknown
    # (closes the "From To" → "From (TV series)" gap for adversarial corpora).
    type_reason = "no_gate"
    qid = (data or {}).get("wikidata_id")
    trust_class = _corpus_trust_class(corpus_id)
    strict_unknown = (trust_class == "review_required")
    compatible_check_failed = False
    if matched and qid:
        compatible, type_reason = is_compatible(
            qid, suggested_type, strict_unknown=strict_unknown,
        )
        compatible_check_failed = not compatible
    if compatible_check_failed:
            log.info("wikipedia match REJECTED for %r → %s (%s): %s",
                     surface, best_title, qid, type_reason)
            _save_lookup(
                corpus_id=corpus_id, surface_norm=surface_norm,
                source="wikipedia",
                external_id=qid, label=best_title, summary=None, url=None,
                matched=False, confidence=0.0,
                raw={"search_hits": hits[:3],
                     "summary": data,
                     "rejected_reason": type_reason,
                     "suggested_type": suggested_type,
                     "strict_unknown": strict_unknown,
                     "trust_class": trust_class},
            )
            _log_collapse_decision(
                corpus_id=corpus_id, surface_norm=surface_norm,
                action="rejected",
                rule_fired="wikipedia_type_mismatch",
                score=0.0, confidence_tier="no",
                evidence_against=[{
                    "axis": "wikidata_p31",
                    "value": type_reason,
                    "weight": -1.0,
                }],
                notes=f"Wikipedia hit '{best_title}' ({qid}) "
                      f"was {type_reason} for suggested_type={suggested_type}",
            )
            return {"source": "wikipedia", "matched": False,
                    "qid": qid, "label": best_title,
                    "rejected": True, "reason": type_reason}

    # Confidence reflects whether we got a clean summary back.
    confidence = 0.85 if matched else 0.0

    _save_lookup(
        corpus_id=corpus_id,
        surface_norm=surface_norm,
        source="wikipedia",
        external_id=qid,
        label=(data or {}).get("title"),
        summary=((data or {}).get("summary") or "")[:2000],
        url=(data or {}).get("url"),
        matched=matched,
        confidence=confidence,
        raw={"search_hits": hits[:3], "summary": data,
             "type_check": type_reason},
    )
    if matched:
        _log_collapse_decision(
            corpus_id=corpus_id, surface_norm=surface_norm,
            action="merged_into" if qid else "kept_separate",
            rule_fired="wikipedia_match" + (f"+type_ok:{type_reason}" if type_reason != "no_gate" else ""),
            score=confidence, confidence_tier="soft",
            evidence_for=[{
                "axis": "external_id", "value": qid or "no_qid",
                "weight": 0.7,
            }, {
                "axis": "name_match",
                "value": f"surface '{surface}' → wp '{best_title}'",
                "weight": 0.3,
            }],
            notes=f"matched Wikipedia '{best_title}' QID={qid} type_check={type_reason}",
        )
    return {
        "source": "wikipedia",
        "matched": matched,
        "qid": (data or {}).get("wikidata_id"),
        "label": (data or {}).get("title"),
    }


def validate_corpus(
    corpus_id: str,
    *,
    limit: int = _DEFAULT_VALIDATION_LIMIT,
    min_documents: int = 2,
    skip_existing: bool = True,
    delay_between_calls: float = 0.1,
) -> dict:
    """Route the top-N corroborated forms through Wikipedia.

    skip_existing: if True (default), only look up forms we haven't tried
                   yet. Set False to force refresh.
    delay_between_calls: seconds between Wikipedia calls (rate-limit safety)."""
    _ensure_table()

    with get_connection() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT c.representative_form, c.surface_form_norm,
                       c.mention_count, c.document_count, c.corroboration_score,
                       c.suggested_type
                FROM corpus_corroboration c
                WHERE c.corpus_id = %s
                  AND c.is_fragment = FALSE
                  AND c.document_count >= %s
            """
            params: list = [corpus_id, min_documents]
            if skip_existing:
                sql += """
                  AND NOT EXISTS (
                    SELECT 1 FROM entity_external_lookups e
                    WHERE e.corpus_id = c.corpus_id
                      AND e.surface_form_norm = c.surface_form_norm
                      AND e.source = 'wikipedia'
                  )
                """
            sql += " ORDER BY c.corroboration_score DESC LIMIT %s"
            params.append(limit)
            cur.execute(sql, params)
            rows = cur.fetchall()

    if not rows:
        return {"corpus_id": corpus_id, "ok": True, "reason": "nothing to validate",
                "looked_up": 0}

    internal_matched = 0
    wikipedia_matched = 0
    new_to_world: list[str] = []
    qid_hits: list[str] = []
    label_hits: list[str] = []
    short_form_blocked = 0
    type_rejected = 0

    for surface, surface_norm, mc, dc, score, suggested_type in rows:
        # Step 0: try internal node library first (gated on length + type).
        internal = _route_internal_nodes(
            corpus_id, surface, surface_norm,
            suggested_type=suggested_type,
        )
        if internal.get("matched"):
            internal_matched += 1
            # We collapsed to an existing entity — skip external lookup.
            continue
        if internal.get("reason") == "short_surface_form":
            short_form_blocked += 1
            # Don't go to Wikipedia for short, ambiguous forms either —
            # would just produce more false positives.
            continue

        # Step 1: not in internal — route to Wikipedia (with type gate).
        wp = _route_wikipedia(
            corpus_id, surface, surface_norm,
            suggested_type=suggested_type,
        )
        if wp.get("matched"):
            wikipedia_matched += 1
            if wp.get("qid"):
                qid_hits.append(wp["qid"])
            if wp.get("label"):
                label_hits.append(wp["label"])
        elif wp.get("rejected"):
            type_rejected += 1
        else:
            new_to_world.append(surface)

        if delay_between_calls > 0:
            time.sleep(delay_between_calls)

    return {
        "corpus_id": corpus_id,
        "ok": True,
        "looked_up": len(rows),
        "internal_matched": internal_matched,
        "wikipedia_matched": wikipedia_matched,
        "short_form_blocked": short_form_blocked,
        "wikipedia_type_rejected": type_rejected,
        "new_to_world": len(new_to_world),
        "match_rate_internal": round(internal_matched / len(rows), 3) if rows else 0.0,
        "match_rate_external": round(wikipedia_matched / len(rows), 3) if rows else 0.0,
        "sample_external_labels": label_hits[:10],
        "sample_external_qids": qid_hits[:10],
        "sample_new_to_world": new_to_world[:10],
    }


def get_validated(
    corpus_id: str,
    *,
    limit: int = 50,
    matched_only: bool = True,
) -> list[dict]:
    """Return enriched top corroborated entities (joined with Wikipedia lookups)."""
    where = ["c.corpus_id = %s", "c.is_fragment = FALSE"]
    params: list = [corpus_id]
    if matched_only:
        where.append("e.matched = TRUE")
    sql = f"""
        SELECT c.representative_form, c.surface_form_norm,
               c.mention_count, c.document_count,
               ROUND(c.corroboration_score::numeric, 1) AS score,
               c.suggested_type,
               e.external_id AS qid, e.label AS wp_label,
               e.summary AS wp_summary, e.url AS wp_url, e.matched
        FROM corpus_corroboration c
        LEFT JOIN entity_external_lookups e
          ON e.corpus_id = c.corpus_id
         AND e.surface_form_norm = c.surface_form_norm
         AND e.source = 'wikipedia'
        WHERE {' AND '.join(where)}
        ORDER BY c.corroboration_score DESC
        LIMIT %s
    """
    params.append(limit)
    out: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                out.append({
                    "form": r[0],
                    "form_norm": r[1],
                    "mention_count": r[2],
                    "document_count": r[3],
                    "score": float(r[4]),
                    "suggested_type": r[5],
                    "wikidata_qid": r[6],
                    "wikipedia_label": r[7],
                    "wikipedia_summary": (r[8] or "")[:300],
                    "wikipedia_url": r[9],
                    "validated": r[10],
                })
    return out
