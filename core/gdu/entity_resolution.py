"""
GDU-Backed Entity Resolution — Unified Canonical Resolver

Single entry point for ALL entity creation across the platform:
  - Signal ingest (envelope.entities[])
  - Knowledge node enrichment (node_enrich.py D-stage)
  - Watch topic processing (processor.py)

Resolution flow:
  1. Load entityvocab pack via get_gdu() → strict mode, hash-verified
  2. Normalize candidate using per-type rules from the pack
  3. Strong path: identifier_hash → UPSERT on entities table
  4. Soft path: normalized_name → exact match
  5. Alias path: check against existing entity aliases[]
  6. Embedding path: kNN top-5 → LLM arbitration using governed prompt
  7. Spatial path: fuzzy name + proximity (< 50km)
  8. Log resolution decision to entity_resolution_log (GDU S1.4 audit)

Returns: (canonical_entity_id, action, confidence, gdu_canonical)

This replaces:
  - core/extraction/resolvers/entity_resolver.py (signal ingest)
  - core/knowledge/resolver.py (knowledge node dedup)
  - processor.py regex extraction (watch topics)

Authority: GDU Technical Build Plan v2.5.0 — Entity Resolution Pipeline
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Optional

from storage.postgres.db import get_connection
from core.ldu.lineage import get_lineage_stamp

_log = logging.getLogger("enki.gdu.entity_resolution")

# ─── Cached pack data ──────────────────────────────────────────────────────────
_RESOLUTION_RULES: Optional[dict] = None
_ENRICHMENT_TYPES: Optional[dict] = None
_GDU_CANONICAL: bool = False
_ACTIVE_SET_VERSION: Optional[str] = None

# Title prefixes to strip from person names during normalization
_TITLE_PREFIXES = re.compile(
    r'^(president|prime\s+minister|minister|secretary|general|admiral|'
    r'senator|governor|mayor|king|queen|prince|pope|ambassador|'
    r'ceo|chairman|director|chief|commander|leader|'
    r'mr\.?|mrs\.?|ms\.?|dr\.?|prof\.?)\s+',
    re.IGNORECASE,
)

_ORG_SUFFIXES = [
    " inc", " incorporated", " ltd", " limited", " llc", " l.l.c.",
    " corp", " corporation", " co", " company", " group", " holdings",
    " partners", " lp", " plc", " gmbh", " ag", " sa", " srl", " bv", " nv",
]

_WHITESPACE = re.compile(r"\s+")


class ResolutionResult:
    """Result of entity resolution."""
    __slots__ = ("entity_id", "action", "confidence", "match_reason",
                 "gdu_canonical", "is_new", "conflicts")

    def __init__(self, entity_id: Optional[int], action: str,
                 confidence: float, match_reason: str,
                 gdu_canonical: bool, is_new: bool,
                 conflicts: Optional[list] = None):
        self.entity_id = entity_id
        self.action = action          # merge | create | review
        self.confidence = confidence
        self.match_reason = match_reason
        self.gdu_canonical = gdu_canonical
        self.is_new = is_new
        self.conflicts = conflicts or []


def _load_entityvocab() -> tuple[dict, dict, bool, Optional[str]]:
    """Load resolution rules and enrichment types from the entityvocab pack."""
    global _RESOLUTION_RULES, _ENRICHMENT_TYPES, _GDU_CANONICAL, _ACTIVE_SET_VERSION

    if _RESOLUTION_RULES is not None:
        return _RESOLUTION_RULES, _ENRICHMENT_TYPES or {}, _GDU_CANONICAL, _ACTIVE_SET_VERSION

    try:
        from core.gdu.bootstrap import get_gdu
        from core.gdu.config import PACK_PATHS

        gdu = get_gdu()
        pack_path = PACK_PATHS.get("entityvocab")
        if pack_path is None:
            raise ValueError("entityvocab not in PACK_PATHS config")

        if not pack_path.exists():
            raise FileNotFoundError(f"entityvocab pack not found at {pack_path}")

        result = gdu.load_pack("entityvocab", pack_path)

        if result.ok and result.canonical:
            rules = {}
            enrichment = {}
            for defn in result.definitions:
                content = defn.content if hasattr(defn, "content") else {}
                if isinstance(content, dict):
                    if "resolution_strategies" in content:
                        rules = content
                    elif "enrichment_types" in content:
                        enrichment = content

            if rules:
                _RESOLUTION_RULES = rules
                _ENRICHMENT_TYPES = enrichment
                _GDU_CANONICAL = True
                _ACTIVE_SET_VERSION = gdu.active_set.version
                _log.info("entityvocab loaded CANONICALLY — active set v%s",
                          _ACTIVE_SET_VERSION)
                return _RESOLUTION_RULES, _ENRICHMENT_TYPES, True, _ACTIVE_SET_VERSION

        _log.warning("entityvocab pack load failed or non-canonical")

    except Exception as e:
        _log.warning("GDU unavailable for entityvocab: %s (falling back to filesystem)", e)

    # Fallback: load directly from filesystem
    try:
        from pathlib import Path
        project_root = Path(__file__).resolve().parent.parent.parent
        rules_path = project_root / "libraries" / "entityvocab" / "definitions" / "entityvocab.resolution_rules.json"
        enrichment_path = project_root / "libraries" / "entityvocab" / "definitions" / "entityvocab.enrichment_types.json"

        with open(rules_path) as f:
            _RESOLUTION_RULES = json.load(f).get("content", {})
        with open(enrichment_path) as f:
            _ENRICHMENT_TYPES = json.load(f).get("content", {})

        _GDU_CANONICAL = False
        _ACTIVE_SET_VERSION = None
        _log.warning("entityvocab loaded via FILESYSTEM FALLBACK (non-canonical)")
        return _RESOLUTION_RULES, _ENRICHMENT_TYPES, False, None

    except Exception as e2:
        _log.error("entityvocab UNAVAILABLE: %s", e2)
        _RESOLUTION_RULES = {}
        _ENRICHMENT_TYPES = {}
        _GDU_CANONICAL = False
        return {}, {}, False, None


_GARBAGE_CHARS = re.compile(r"[®©™^<>~`|{}\\]")
_PDF_SPACED = re.compile(r"^(?:[A-Za-z0-9] ){4,}[A-Za-z0-9]?$")
_LOWER_THEN_CAPS = re.compile(r"[a-z][A-Z]{3,}")
_MOSTLY_PUNCT = re.compile(r"[A-Za-z0-9]")
# Doubled-letter OCR artefacts: "TTrruummpp" → "TT","rr","uu","mm","pp"
_DOUBLED_PAIRS = re.compile(r"(.)\1")


def is_garbage_name(name: str) -> tuple[bool, str]:
    """Cheap shape filter for OCR/PDF-extraction noise. Returns
    (is_garbage, reason). Conservative — preserves real acronyms (NASA,
    IDF), brand camelCase (iPhone, OpenAI, JPMorgan, eBay), and hyphenated
    designators (M-37, F-35)."""
    if not name:
        return True, "empty"
    s = name.strip()
    if len(s) < 2 or len(s) > 120:
        return True, f"len={len(s)}"
    if _GARBAGE_CHARS.search(s):
        return True, "garbage chars"
    # PDF column-split: "U u i l e K l t y" — every other char is space
    if _PDF_SPACED.match(s):
        return True, "pdf column split"
    # Mostly punctuation
    letter_count = len(_MOSTLY_PUNCT.findall(s))
    if letter_count < max(2, len(s) // 3):
        return True, "mostly punct"
    # Doubled-letter OCR (TTrruummpp): 3+ doubled pairs covering >40% of the name
    pairs = _DOUBLED_PAIRS.findall(s)
    if len(pairs) >= 3 and (len(pairs) * 2) > len(s) * 0.4:
        return True, "doubled-letter OCR"
    # Single-word camel/garbage. Two sub-rules:
    #  (a) a lowercase letter followed by 3+ uppercase in a row
    #      (MsNORUS, ObSERVeR-like fragments) — real names don't have this
    #  (b) 4+ internal capital letters in a single token
    #      (CahnersPublishingCompanyCorporateResearchDepartment)
    if len(s.split()) == 1:
        if _LOWER_THEN_CAPS.search(s):
            return True, "lowercase then 3+ caps"
        internal_caps = sum(1 for c in s[1:] if c.isupper())
        if internal_caps >= 4:
            return True, "4+ internal caps"
        # Long PascalCase with multiple caps = concatenated PDF text
        if len(s) >= 20 and internal_caps >= 2:
            return True, "long pascal-case (likely concatenated PDF text)"
    return False, ""


# ─── Public API ────────────────────────────────────────────────────────────────

def resolve_entity(
    *,
    tenant_id: str,
    name: str,
    entity_type: str,
    identifiers: Optional[dict] = None,
    context: str = "",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    source_pipeline: str = "unknown",
    provider=None,
    dsn: Optional[str] = None,
) -> ResolutionResult:
    """
    Resolve a candidate entity against the canonical entities table.

    This is the ONLY path to creating or matching entities. Every pipeline
    (signals, enrichment, watch topics) must call this function.

    Args:
        tenant_id: Tenant scope
        name: Raw entity name as extracted
        entity_type: nodevocab entity subtype (person, organization, vessel, etc.)
        identifiers: Structured identifiers {mmsi: "...", email: "...", etc.}
        context: Surrounding text for LLM arbitration
        latitude/longitude: Location for spatial matching
        source_pipeline: Origin pipeline name for audit
        provider: LLM provider for embedding/arbitration (optional)
        dsn: Database connection string

    Returns:
        ResolutionResult with canonical entity_id, action, confidence
    """
    rules, enrichment_types, gdu_canonical, active_set = _load_entityvocab()
    identifiers = identifiers or {}

    garbage, reason = is_garbage_name(name)
    if garbage:
        return ResolutionResult(None, "skip", 0.0, f"garbage: {reason}", gdu_canonical, False)

    # Subtype lockdown — pass 1 of 2: accept-only normalization.
    # Validate against entityvocab's closed vocabulary up front so an
    # aliased type ('org', 'ngo') becomes its canonical form
    # ('organization') BEFORE name normalization and the match strategies
    # run. The route/drop/unknown branch fires LATER (just before the
    # _create_entity insert) so legitimate matches against grandfathered
    # entities of any type are preserved.
    entity_type = (entity_type or "").strip()
    _ldu_verdict = None
    try:
        from core.ldu.subtype_validator import validate_for_ldu
        _ldu_verdict = validate_for_ldu(entity_type)
        if _ldu_verdict.action == "accept":
            if (_ldu_verdict.canonical_subtype
                    and _ldu_verdict.canonical_subtype != entity_type):
                entity_type = _ldu_verdict.canonical_subtype
    except Exception as e:
        _log.warning("subtype_validator failed for type=%r: %s — passing through",
                     entity_type, e)

    normalized = normalize_entity_name(name, entity_type)
    if not normalized or len(normalized) < 2:
        return ResolutionResult(None, "skip", 0.0, "name too short", gdu_canonical, False)

    identifier_hash = _compute_identifier_hash(identifiers)

    with get_connection(dsn) as conn:
        # Strategy 1: Identifier hash (strongest)
        if identifier_hash:
            result = _match_by_identifier_hash(conn, tenant_id, identifier_hash,
                                                identifiers, normalized, entity_type,
                                                gdu_canonical)
            if result:
                _log_resolution(conn, tenant_id, name, entity_type, identifiers,
                                result, "identifier_hash", source_pipeline, gdu_canonical, active_set)
                _reconcile_on_match(conn, result.entity_id, tenant_id, name, context, identifiers, enrichment_types)
                return result

        # Strategy 2: Normalized name exact match
        result = _match_by_normalized_name(conn, tenant_id, normalized, entity_type, gdu_canonical)
        if result:
            # Collapse-policy gate: if source pipeline's dataset is
            # review_required, do NOT auto-merge name-only into the existing
            # canonical. Instead create a new LDU entity and surface a
            # collapse_candidate. (Identifier-hash matches in Strategy 1
            # bypass this gate — those are strong enough.)
            gated = _apply_collapse_policy_gate(
                conn, tenant_id, name, normalized, entity_type, identifiers,
                identifier_hash, source_pipeline, result, "normalized_name",
                gdu_canonical, active_set,
            )
            if gated is not None:
                return gated
            _log_resolution(conn, tenant_id, name, entity_type, identifiers,
                            result, "normalized_name", source_pipeline, gdu_canonical, active_set)
            _reconcile_on_match(conn, result.entity_id, tenant_id, name, context, identifiers, enrichment_types)
            return result

        # Strategy 3: Alias match
        result = _match_by_alias(conn, tenant_id, normalized, entity_type, gdu_canonical)
        if result:
            gated = _apply_collapse_policy_gate(
                conn, tenant_id, name, normalized, entity_type, identifiers,
                identifier_hash, source_pipeline, result, "alias_match",
                gdu_canonical, active_set,
            )
            if gated is not None:
                return gated
            _log_resolution(conn, tenant_id, name, entity_type, identifiers,
                            result, "alias_match", source_pipeline, gdu_canonical, active_set)
            _reconcile_on_match(conn, result.entity_id, tenant_id, name, context, identifiers, enrichment_types)
            return result

        # Strategy 3.4: Single-token person fallback. Runs BEFORE the
        # cross-type collision check (3.6) and loose-name check (3.5)
        # because for persons, same-class match against a canonical
        # entity should win over cross-class collisions to fragments
        # (e.g. "Hensley" mention should go to person 'Pat Hensley'
        # #885, not the wrong-type fragment 'Hensleyøe' region #1234
        # the Wikidata validator hallucinated). Only fires for bare
        # last-name mentions of persons.
        if (entity_type == "person"
                and " " not in normalized
                and len(normalized) >= 3):
            result = _match_person_by_last_token(
                conn, tenant_id, normalized, gdu_canonical,
            )
            if result:
                _log_resolution(conn, tenant_id, name, entity_type, identifiers,
                                result, "last_token_person", source_pipeline,
                                gdu_canonical, active_set)
                if result.action == "merge":
                    _reconcile_on_match(conn, result.entity_id, tenant_id, name,
                                        context, identifiers, enrichment_types)
                return result

        # Strategy 3.5: Loose-name fallback — variant collision detection.
        # Strict miss + loose hit means same root name but different middle
        # initials / variant tokens. We do NOT auto-merge: different initials
        # may indicate different people (John A. Smith ≠ John B. Smith) and
        # may also indicate the same person under a different style (Donald
        # Vale vs Dana J. Vale). Decision deferred until verification —
        # we attach the loose match to the new mention as a candidate and
        # return action='review' so the caller can prompt for human or
        # identifier-based confirmation.
        loose = normalize_entity_name_loose(name, entity_type)
        if loose and loose != normalized:
            result = _match_by_loose_name(
                conn, tenant_id, loose, entity_type, gdu_canonical, identifiers,
            )
            if result:
                _log_resolution(conn, tenant_id, name, entity_type, identifiers,
                                result, "loose_name_review", source_pipeline,
                                gdu_canonical, active_set)
                # No reconcile — we are NOT merging here. The candidate match
                # is recorded on the result; caller decides.
                return result

        # Strategy 3.6: Cross-type case-fold collision. If the same name
        # exists under a DIFFERENT canonical_type, we likely have a class-bleed
        # bug (Tehran=location vs TEHRAN=organization both refer to the city,
        # but the resolver's per-type lookup never collided). Within compatible
        # class groups (place ↔ place, org ↔ org) we auto-merge into the
        # existing entity; across incompatible class boundaries we return the
        # existing entity_id with action='review' so the caller doesn't create
        # a new dup but a human still has to confirm.
        # Phase 8b.5.2 of synthesis plan (May 10 2026).
        result = _match_cross_type_for_review(
            conn, tenant_id, normalized, entity_type, gdu_canonical,
        )
        if result:
            _log_resolution(conn, tenant_id, name, entity_type, identifiers,
                            result, "cross_type_collision", source_pipeline,
                            gdu_canonical, active_set)
            if result.action == "merge":
                _reconcile_on_match(conn, result.entity_id, tenant_id, name,
                                    context, identifiers, enrichment_types)
            return result

        # Strategy 4: Embedding kNN + LLM arbitration
        if provider:
            result = _match_by_embedding_llm(conn, tenant_id, name, entity_type,
                                              identifiers, context, provider,
                                              rules, gdu_canonical, dsn)
            if result:
                _log_resolution(conn, tenant_id, name, entity_type, identifiers,
                                result, "embedding_knn_llm", source_pipeline, gdu_canonical, active_set)
                if result.action == "create":
                    result = _create_entity(conn, tenant_id, name, normalized,
                                             entity_type, identifiers, identifier_hash,
                                             gdu_canonical, active_set,
                                             source_pipeline=source_pipeline)
                    _log_resolution(conn, tenant_id, name, entity_type, identifiers,
                                    result, "embedding_knn_llm_create", source_pipeline, gdu_canonical, active_set)
                    _maybe_queue_enrichment(conn, result.entity_id, tenant_id, entity_type, enrichment_types)
                else:
                    _reconcile_on_match(conn, result.entity_id, tenant_id, name, context, identifiers, enrichment_types)
                return result

        # Strategy 5: Fuzzy name + spatial proximity
        if latitude is not None and longitude is not None:
            result = _match_by_name_and_location(conn, tenant_id, normalized, entity_type,
                                                  latitude, longitude, gdu_canonical)
            if result:
                _log_resolution(conn, tenant_id, name, entity_type, identifiers,
                                result, "name_spatial", source_pipeline, gdu_canonical, active_set)
                _reconcile_on_match(conn, result.entity_id, tenant_id, name, context, identifiers, enrichment_types)
                return result

        # No match — create new entity (gated by _create_entity's subtype check)
        result = _create_entity(conn, tenant_id, name, normalized, entity_type,
                                 identifiers, identifier_hash, gdu_canonical, active_set,
                                 source_pipeline=source_pipeline)
        _log_resolution(conn, tenant_id, name, entity_type, identifiers,
                        result, "no_match_create", source_pipeline, gdu_canonical, active_set)
        _maybe_queue_enrichment(conn, result.entity_id, tenant_id, entity_type, enrichment_types)
        return result


def resolve_redirect(entity_id: int, dsn: Optional[str] = None) -> int:
    """Chase redirect chain to find the canonical entity_id."""
    visited = set()
    current = entity_id
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            while current not in visited:
                visited.add(current)
                cur.execute(
                    "SELECT new_entity_id FROM entity_redirects WHERE old_entity_id = %s",
                    (current,),
                )
                row = cur.fetchone()
                if row:
                    current = row[0]
                else:
                    break
    return current


def merge_entities(
    winner_id: int,
    loser_ids: list[int],
    tenant_id: str,
    merged_by: str = "system",
    reason: str = "",
    dsn: Optional[str] = None,
) -> dict:
    """Merge loser entities into the winner. Creates redirects, re-points FKs."""
    rules, _, gdu_canonical, _ = _load_entityvocab()
    merge_policy = rules.get("merge_policy", {})

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            merged_count = 0
            for loser_id in loser_ids:
                # Create redirect
                cur.execute("""
                    INSERT INTO entity_redirects (old_entity_id, new_entity_id, merged_by, merge_reason)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (old_entity_id) DO UPDATE SET
                        new_entity_id = EXCLUDED.new_entity_id,
                        merged_at = NOW()
                """, (loser_id, winner_id, merged_by, reason))

                # Merge aliases
                cur.execute("""
                    UPDATE entities SET
                        aliases = ARRAY(
                            SELECT DISTINCT unnest(
                                COALESCE(e1.aliases, '{}') ||
                                COALESCE(e2.aliases, '{}') ||
                                ARRAY[e2.canonical_name]
                            )
                        ),
                        signal_count = e1.signal_count + e2.signal_count,
                        first_seen = LEAST(e1.first_seen, e2.first_seen),
                        last_seen = GREATEST(e1.last_seen, e2.last_seen),
                        identifiers = COALESCE(e1.identifiers, '{}'::jsonb) || COALESCE(e2.identifiers, '{}'::jsonb),
                        updated_at = NOW()
                    FROM entities e1, entities e2
                    WHERE entities.entity_id = %s
                      AND e1.entity_id = %s
                      AND e2.entity_id = %s
                """, (winner_id, winner_id, loser_id))

                # Re-point document_mentions. Without this the loser's
                # evidence is stranded on a tombstoned entity: the redirect
                # exists but nothing on the READ path chases it (only the
                # gateway write path calls resolve_redirect), so the merged
                # mentions simply vanish from the winner's dossier.
                cur.execute("""
                    UPDATE document_mentions SET entity_id = %s WHERE entity_id = %s
                """, (winner_id, loser_id))

                # Re-point assessments. collapse_key is derived from
                # (subj, edge, obj) and is UNIQUE, so a naive repoint either
                # collides with the winner's equivalent edge or leaves a stale
                # key that stops future collapses. Recompute per row: fold
                # corroboration into the surviving edge on collision, drop
                # self-loops the rewrite produces.
                cur.execute("""
                    SELECT assessment_id, subj_entity_id, predicate_edge_type,
                           obj_entity_id, obj_value,
                           corroborated_by_count, contradicted_by_count
                      FROM assessments
                     WHERE (subj_entity_id = %s OR obj_entity_id = %s)
                       AND demoted_at IS NULL
                """, (loser_id, loser_id))
                for a_id, subj, edge, obj, oval, corr, contra in cur.fetchall():
                    new_subj = winner_id if subj == loser_id else subj
                    new_obj = winner_id if obj == loser_id else obj
                    if new_subj == new_obj:
                        cur.execute("DELETE FROM assessments WHERE assessment_id = %s", (a_id,))
                        continue
                    cur.execute("SELECT compute_assessment_collapse_key(%s,%s,%s,%s)",
                                (new_subj, edge, new_obj, oval))
                    new_key = cur.fetchone()[0]
                    cur.execute("""SELECT assessment_id FROM assessments
                                    WHERE collapse_key = %s AND assessment_id <> %s""",
                                (new_key, a_id))
                    clash = cur.fetchone()
                    if clash:
                        cur.execute("""
                            UPDATE assessments
                               SET corroborated_by_count = corroborated_by_count + %s,
                                   contradicted_by_count = contradicted_by_count + %s,
                                   last_seen = NOW()
                             WHERE assessment_id = %s
                        """, (corr or 0, contra or 0, clash[0]))
                        cur.execute("DELETE FROM assessments WHERE assessment_id = %s", (a_id,))
                    else:
                        cur.execute("""
                            UPDATE assessments
                               SET subj_entity_id = %s, obj_entity_id = %s,
                                   collapse_key = %s, last_seen = NOW()
                             WHERE assessment_id = %s
                        """, (new_subj, new_obj, new_key, a_id))

                # Re-point knowledge_nodes
                cur.execute("""
                    UPDATE knowledge_nodes SET master_entity_id = %s, updated_at = NOW()
                    WHERE master_entity_id = %s
                """, (winner_id, loser_id))

                # Re-point watch_topic_entities
                cur.execute("""
                    UPDATE watch_topic_entities SET canonical_entity_id = %s
                    WHERE canonical_entity_id = %s
                """, (winner_id, loser_id))

                # Re-point enrichment queue
                cur.execute("""
                    UPDATE entity_enrichment_queue SET entity_id = %s
                    WHERE entity_id = %s AND status IN ('pending', 'running')
                """, (winner_id, loser_id))

                # Phase 8b.6 follow-up: re-point entity_relationships in both
                # directions. Done row-by-row with savepoints so a
                # idx_entity_rel_unique collision on the canonical winner
                # row deletes the orphan rather than crashing the whole merge.
                cur.execute("""
                    SELECT rid FROM entity_relationships
                     WHERE source_entity_id = %s OR target_entity_id = %s
                """, (loser_id, loser_id))
                rids = [r[0] for r in cur.fetchall()]
                for rid in rids:
                    cur.execute("SAVEPOINT sp_rel_repoint")
                    try:
                        cur.execute("""
                            UPDATE entity_relationships
                               SET source_entity_id = CASE WHEN source_entity_id = %s THEN %s ELSE source_entity_id END,
                                   target_entity_id = CASE WHEN target_entity_id = %s THEN %s ELSE target_entity_id END
                             WHERE rid = %s
                        """, (loser_id, winner_id, loser_id, winner_id, rid))
                        cur.execute("RELEASE SAVEPOINT sp_rel_repoint")
                    except Exception:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_rel_repoint")
                        cur.execute("DELETE FROM entity_relationships WHERE rid = %s", (rid,))
                # Kill any self-loops the rewrite produced
                cur.execute(
                    "DELETE FROM entity_relationships WHERE source_entity_id = target_entity_id",
                )

                merged_count += 1

        conn.commit()

    _log.info("Merged %d entities into %d (by %s): %s", merged_count, winner_id, merged_by, reason)
    return {"winner_id": winner_id, "merged_count": merged_count}


# ─── Name Normalization ────────────────────────────────────────────────────────

def normalize_entity_name(name: str, entity_type: str = "") -> str:
    """Normalize entity name through the pack-driven normalizer.

    Single source of truth: core/gdu/normalize.py reads
    libraries/entityvocab/definitions/entityvocab.resolution_rules.json
    and applies title strip, suffix strip (Jr/Sr/II/III/...),
    reject patterns (Q-IDs, ISO codes, URLs, hashes), and minimum length.

    This used to be a duplicate normalizer with a smaller title list and no
    person-suffix stripping — the bug behind the May 4 "×7 Vales" audit
    finding. Routing through the pack means every entity writer (resolver,
    gateway, watch-topic processor) collapses identically.

    Returns empty string for any rejected name (caller should treat as
    "skip create").
    """
    if not name:
        return ""

    from core.gdu.normalize import normalize as _pack_normalize

    # Pack rule semantics: role="person" applies title-prefix strip; role
    # "organization" applies legal-suffix strip; "name"/unknown applies both.
    role = entity_type if entity_type in ("person", "organization") else "name"
    result = _pack_normalize(name, role=role)
    if result.rejected:
        # Rejected by pack (Q-ID, ISO code, etc.) — empty signals "skip".
        return ""
    return result.normalized


def normalize_entity_name_loose(name: str, entity_type: str = "") -> str:
    """Looser normalization for variant collision detection.

    Same as normalize_entity_name plus:
      - Strip standalone middle initials ("dana j. vale" → "dana vale")
      - Strip trailing standalone single letters ("vicki a" → "vicki")

    Used as the LAST-RESORT match before creating a new entity. If a strict
    miss matches an existing entity by loose form, we mark for human review
    rather than auto-merge — different middle initials may be different
    people (John A. Smith ≠ John B. Smith) and require verification.
    """
    s = normalize_entity_name(name, entity_type)
    if not s:
        return s

    # Strip standalone single-letter "words" (with optional period) that are
    # surrounded by spaces — these are middle initials. We do NOT strip
    # leading/trailing single letters that may be the entity's actual name
    # (e.g. "X" the company; "J. Doe" — though the period-strip in pack
    # already lowercased "p.").
    # Only collapse if at least 3 tokens remain so we don't gut short names.
    parts = s.split()
    if len(parts) >= 3:
        kept = []
        for i, p in enumerate(parts):
            stripped = p.rstrip(".")
            is_initial = len(stripped) == 1 and stripped.isalpha()
            # Drop only if it's an interior token (not first or last)
            if is_initial and 0 < i < len(parts) - 1:
                continue
            # Also drop if it's the last token AND there are 2+ kept tokens already
            if is_initial and i == len(parts) - 1 and len(kept) >= 2:
                continue
            kept.append(p)
        s = " ".join(kept)

    return s


# ─── Resolution Strategies ─────────────────────────────────────────────────────

def _match_by_identifier_hash(
    conn, tenant_id: str, identifier_hash: str,
    identifiers: dict, normalized: str, entity_type: str,
    gdu_canonical: bool,
) -> Optional[ResolutionResult]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT entity_id, canonical_name FROM entities
            WHERE tenant_id = %s AND identifier_hash = %s
            LIMIT 1
        """, (tenant_id, identifier_hash))
        row = cur.fetchone()
        if row:
            cur.execute("""
                UPDATE entities SET last_seen = NOW(), signal_count = signal_count + 1,
                    updated_at = NOW()
                WHERE entity_id = %s
            """, (row[0],))
            conn.commit()
            return ResolutionResult(
                row[0], "merge", 1.0,
                f"identifier_hash match: {list(identifiers.keys())}",
                gdu_canonical, False,
            )
    return None


def _match_by_normalized_name(
    conn, tenant_id: str, normalized: str, entity_type: str,
    gdu_canonical: bool,
) -> Optional[ResolutionResult]:
    with conn.cursor() as cur:
        # ORDER BY signal_count so when fragments coexist for the same
        # name (two Dana Vales, two Pam Bondis from past resolver
        # bugs), the canonical-by-corpus-signal wins. Dedup_sweep is
        # responsible for actually merging them, but until it does,
        # at least new mentions land on the strongest existing row.
        cur.execute("""
            SELECT entity_id, canonical_name FROM entities
            WHERE tenant_id = %s AND normalized_name = %s AND canonical_type = %s
            ORDER BY signal_count DESC NULLS LAST, entity_id ASC
            LIMIT 1
        """, (tenant_id, normalized, entity_type))
        row = cur.fetchone()
        if row:
            cur.execute("""
                UPDATE entities SET last_seen = NOW(), signal_count = signal_count + 1,
                    updated_at = NOW()
                WHERE entity_id = %s
            """, (row[0],))
            conn.commit()
            return ResolutionResult(
                row[0], "merge", 0.95,
                f"normalized name match: '{normalized}' = '{row[1]}'",
                gdu_canonical, False,
            )
    return None


# ── Cross-type class groups (Phase 8b.5.2) ──
# When the same name appears under different canonical_types, the resolver
# checks whether the two types belong to the same semantic class. If they
# do, auto-merge (Tehran=location → Tehran=city is the same place). If
# they don't, escalate to review (Apple=organization vs apple=product
# might or might not be the same — defer to human).
_CLASS_GROUPS = {
    "place": {"city", "country", "place", "region", "admin_region",
              "location", "facility_location", "island", "peninsula",
              "archipelago", "river", "ocean", "sea", "strait",
              "maritime_area"},
    "org":   {"organization", "government_agency", "mass_media",
              "military_unit", "university", "unit"},
    "person": {"person"},
    "asset": {"aircraft", "vessel", "vehicle", "spacecraft", "device",
              "weapon_system", "weapon"},
}


def _class_group_for(entity_type: str) -> Optional[str]:
    et = (entity_type or "").lower()
    for group, members in _CLASS_GROUPS.items():
        if et in members:
            return group
    return None


def _match_cross_type_for_review(
    conn, tenant_id: str, normalized: str, entity_type: str,
    gdu_canonical: bool,
) -> Optional[ResolutionResult]:
    """Look up entities with the same normalized name but a different type.

    If found and the two types share a class group → action='merge' (auto-
    correct place↔place, org↔org). If types are in different class groups
    → action='review', returning the existing entity_id so the caller
    doesn't create a new dup (human still has to verify the cross-class
    collision).
    """
    new_group = _class_group_for(entity_type)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT entity_id, canonical_name, canonical_type, signal_count
              FROM entities
             WHERE tenant_id = %s AND normalized_name = %s
               AND canonical_type != %s
             ORDER BY signal_count DESC NULLS LAST
             LIMIT 5
        """, (tenant_id, normalized, entity_type))
        rows = cur.fetchall()
    if not rows:
        return None

    # Prefer a row in the same class group as the new admission
    same_group = [r for r in rows if _class_group_for(r[2]) == new_group and new_group is not None]
    if same_group:
        winner = same_group[0]
        cur = conn.cursor()
        try:
            cur.execute("""
                UPDATE entities SET last_seen = NOW(),
                    signal_count = signal_count + 1, updated_at = NOW()
                WHERE entity_id = %s
            """, (winner[0],))
            conn.commit()
        finally:
            cur.close()
        return ResolutionResult(
            winner[0], "merge", 0.88,
            f"cross-type same-class merge: '{normalized}' "
            f"({entity_type} → {winner[2]} entity {winner[0]} '{winner[1]}')",
            gdu_canonical, False,
            conflicts=[r[0] for r in rows if r[0] != winner[0]],
        )

    # Different class groups — return the strongest as a review candidate.
    # The caller gets the existing entity_id so no new row is created, but
    # action='review' signals that human verification is needed.
    best = rows[0]
    return ResolutionResult(
        best[0], "review", 0.55,
        f"cross-type cross-class collision: '{normalized}' "
        f"({entity_type} vs existing {best[2]} entity {best[0]} '{best[1]}') "
        f"— review required",
        gdu_canonical, False,
        conflicts=[r[0] for r in rows[1:]],
    )


def _match_person_by_last_token(
    conn, tenant_id: str, token: str, gdu_canonical: bool,
) -> Optional[ResolutionResult]:
    """Match a bare last-name mention against canonical persons whose
    name ends with that token. Returns:
      - None if no candidate exists
      - action='merge'  with high confidence if exactly one candidate
      - action='review' with the strongest candidate if multiple

    Only canonical, validated persons are considered. Concept-derived
    fragments that haven't validated yet are excluded, so we don't
    chain new fragments onto unvalidated ones.
    """
    norm_lower = token.lower().strip()
    if len(norm_lower) < 3:
        return None
    with conn.cursor() as cur:
        # NOTE: deliberately not filtering by gdu_canonical. Existing
        # canonical person entities are exactly what we want to match
        # against, regardless of how the *caller's* pipeline is configured.
        # When entityvocab pack load falls back, the resolver runs with
        # gdu_canonical=False, but the entities we're trying to find are
        # gdu_canonical=True — filtering would miss them.
        cur.execute("""
            SELECT entity_id, canonical_name, signal_count
              FROM entities
             WHERE tenant_id = %s
               AND canonical_type = 'person'
               AND validation_status = 'validated'
               AND (
                    LOWER(canonical_name) LIKE %s
                    OR LOWER(normalized_name) LIKE %s
               )
             ORDER BY signal_count DESC NULLS LAST
             LIMIT 3
        """, (tenant_id,
              f"% {norm_lower}", f"% {norm_lower}"))
        rows = cur.fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        eid, cname, sig = rows[0]
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE entities SET last_seen = NOW(),
                       signal_count = signal_count + 1,
                       updated_at = NOW()
                 WHERE entity_id = %s
            """, (eid,))
        conn.commit()
        return ResolutionResult(
            eid, "merge", 0.85,
            f"last-token match: '{token}' → '{cname}' (sig {sig})",
            gdu_canonical, False,
        )
    # Multiple candidates — ambiguous. Surface the top one as a review
    # target so the caller doesn't silently mint a new fragment, but
    # don't bump signal_count (we didn't actually decide).
    best_eid, best_cname, best_sig = rows[0]
    others = ", ".join(r[1] for r in rows[1:])
    return ResolutionResult(
        best_eid, "review", 0.55,
        f"ambiguous last-token '{token}': {best_cname} (sig {best_sig}) + {others}",
        gdu_canonical, False,
        conflicts=[r[0] for r in rows[1:]],
    )


def _match_by_alias(
    conn, tenant_id: str, normalized: str, entity_type: str,
    gdu_canonical: bool,
) -> Optional[ResolutionResult]:
    with conn.cursor() as cur:
        # Fetch up to 2 to detect ambiguity. If two distinct entities share
        # the same alias (e.g. two different "John Smith" entities both have
        # the loose-form "john smith" seeded as alias), escalate to review
        # rather than picking the first one arbitrarily.
        cur.execute("""
            SELECT entity_id, canonical_name FROM entities
            WHERE tenant_id = %s AND canonical_type = %s
              AND %s = ANY(aliases)
            ORDER BY signal_count DESC
            LIMIT 2
        """, (tenant_id, entity_type, normalized))
        rows = cur.fetchall()

        if len(rows) > 1:
            # Multiple entities claim this alias — ambiguous. Return the
            # strongest as a candidate but mark for review so the caller
            # can disambiguate via identifier or human input.
            best = rows[0]
            return ResolutionResult(
                best[0], "review", 0.65,
                f"alias '{normalized}' shared by {len(rows)}+ entities — needs verification",
                gdu_canonical, False,
                conflicts=[r[0] for r in rows],
            )

        row = rows[0] if rows else None
        if row:
            cur.execute("""
                UPDATE entities SET last_seen = NOW(), signal_count = signal_count + 1,
                    updated_at = NOW()
                WHERE entity_id = %s
            """, (row[0],))
            conn.commit()
            return ResolutionResult(
                row[0], "merge", 0.90,
                f"alias match: '{normalized}' in aliases of '{row[1]}'",
                gdu_canonical, False,
            )
    return None


def _match_by_loose_name(
    conn, tenant_id: str, loose_normalized: str, entity_type: str,
    gdu_canonical: bool, mention_identifiers: dict,
) -> Optional[ResolutionResult]:
    """Variant-collision detection layer.

    Returns a candidate match WITHOUT merging. Two outcomes:

      1. Loose match found, identifiers compatible → action='merge',
         confidence=0.85. "Compatible" means no shared identifier key has
         a conflicting value (e.g. mention.dob differs from candidate.dob).
         If the mention has identifiers and they all align, treat as same
         person under variant spelling.

      2. Loose match found, identifier conflict → action='review',
         confidence=0.70. Two people may share a root name; require
         human/identifier verification before merge.

    Either way, _reconcile_on_match is NOT called by the caller — the
    decision is deferred. The candidate entity_id is on the result.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT entity_id, canonical_name, identifiers
            FROM entities
            WHERE tenant_id = %s AND canonical_type = %s
              AND normalized_name_loose = %s
              AND gdu_canonical = TRUE
            ORDER BY signal_count DESC
            LIMIT 5
        """, (tenant_id, entity_type, loose_normalized))
        rows = cur.fetchall()

    if not rows:
        return None

    # Look at the strongest candidate (highest signal_count). If only one
    # candidate exists and identifiers don't conflict, this is a clean
    # variant match — auto-merge with reduced confidence. If multiple
    # candidates share the loose name, OR identifiers conflict, escalate
    # to review.
    candidates_count = len(rows)
    best = rows[0]
    best_id, best_name, best_identifiers = best[0], best[1], best[2] or {}

    identifier_conflict = False
    for k, v in (mention_identifiers or {}).items():
        existing = best_identifiers.get(k)
        if existing and str(existing).strip().lower() != str(v).strip().lower():
            identifier_conflict = True
            break

    if candidates_count == 1 and not identifier_conflict:
        return ResolutionResult(
            best_id, "merge", 0.85,
            f"loose-name match: '{loose_normalized}' = '{best_name}' "
            f"(strict differed; identifiers compatible)",
            gdu_canonical, False,
        )

    reason = (
        f"loose-name conflict: '{loose_normalized}' matches "
        f"{candidates_count} entities"
        + (" with identifier conflict" if identifier_conflict else "")
    )
    return ResolutionResult(
        best_id, "review", 0.70, reason, gdu_canonical, False,
        conflicts=[r[0] for r in rows],
    )


def _match_by_embedding_llm(
    conn, tenant_id: str, name: str, entity_type: str,
    identifiers: dict, context: str, provider,
    rules: dict, gdu_canonical: bool, dsn: Optional[str],
) -> Optional[ResolutionResult]:
    """kNN embedding search + LLM arbitration for soft entity matching."""
    try:
        if not hasattr(provider, "embed"):
            return None

        candidate_text = f"{entity_type}: {name}"
        if context:
            candidate_text += f" — {context[:200]}"
        embedding = provider.embed(candidate_text)
        if not embedding:
            return None

        with conn.cursor() as cur:
            cur.execute("""
                SELECT entity_id, canonical_name, canonical_type, aliases,
                       identifiers, last_seen, signal_count
                FROM entities
                WHERE tenant_id = %s AND canonical_type = %s
                  AND embedding IS NOT NULL
                ORDER BY embedding <-> %s::vector
                LIMIT 5
            """, (tenant_id, entity_type, embedding))
            candidates = cur.fetchall()

        if not candidates:
            return ResolutionResult(None, "create", 0.0, "no embedding candidates", gdu_canonical, True)

        # Build LLM prompt from governed template
        prompt_template = rules.get("llm_arbitration_prompt", "")
        if not prompt_template:
            return None

        matches_text = ""
        for i, c in enumerate(candidates):
            aliases_list = c[3] or []
            idents = c[4] or {}
            matches_text += (
                f"{i+1}. {c[1]} ({c[2]})\n"
                f"   - Aliases: {', '.join(aliases_list[:5]) if aliases_list else 'none'}\n"
                f"   - Identifiers: {json.dumps(idents) if idents else 'none'}\n"
                f"   - Last seen: {c[5]}\n"
                f"   - Signal count: {c[6]}\n"
            )

        prompt = prompt_template.replace("{{candidate_name}}", name)
        prompt = prompt.replace("{{candidate_type}}", entity_type)
        prompt = prompt.replace("{{candidate_context}}", context[:300])
        prompt = prompt.replace("{{candidate_identifiers}}", json.dumps(identifiers))
        prompt = prompt.replace("{{match_count}}", str(len(candidates)))
        prompt = prompt.replace("{{#matches}}", "").replace("{{/matches}}", "")
        prompt = re.sub(r"\{\{index\}\}.*?\{\{/matches\}\}", "", prompt, flags=re.DOTALL)
        prompt = prompt.replace("{{#matches}}", matches_text)

        from core.ai.provider import LLMMessage
        response = provider.complete(
            [LLMMessage(role="user", content=prompt)],
            max_tokens=300, temperature=0.0,
        )

        if not response.content or response.error:
            return None

        match = re.search(r'\{[^{}]*"decision"[^{}]*\}', response.content)
        if not match:
            return None

        decision = json.loads(match.group())
        llm_decision = decision.get("decision", "new")
        llm_confidence = float(decision.get("confidence", 0.5))
        llm_index = decision.get("matched_index")
        llm_reasoning = decision.get("reasoning", "")

        merge_threshold = rules.get("merge_policy", {}).get("auto_merge_threshold", 0.90)
        review_threshold = rules.get("merge_policy", {}).get("review_threshold", 0.70)

        if llm_decision == "match" and llm_index and 1 <= llm_index <= len(candidates):
            matched = candidates[llm_index - 1]
            matched_id = matched[0]

            if llm_confidence >= merge_threshold:
                cur2 = conn.cursor()
                cur2.execute("""
                    UPDATE entities SET last_seen = NOW(), signal_count = signal_count + 1,
                        updated_at = NOW()
                    WHERE entity_id = %s
                """, (matched_id,))
                conn.commit()
                cur2.close()
                return ResolutionResult(
                    matched_id, "merge", llm_confidence,
                    f"LLM match: {llm_reasoning}",
                    gdu_canonical, False,
                )
            elif llm_confidence >= review_threshold:
                return ResolutionResult(
                    matched_id, "review", llm_confidence,
                    f"LLM ambiguous: {llm_reasoning}",
                    gdu_canonical, False,
                )

        return ResolutionResult(None, "create", llm_confidence,
                                f"LLM: no match — {llm_reasoning}", gdu_canonical, True)

    except Exception as e:
        _log.debug("Embedding/LLM resolution failed: %s", e)
        return None


def _match_by_name_and_location(
    conn, tenant_id: str, normalized: str, entity_type: str,
    latitude: float, longitude: float, gdu_canonical: bool,
) -> Optional[ResolutionResult]:
    prefix = normalized[:min(len(normalized), 10)]
    with conn.cursor() as cur:
        cur.execute("""
            SELECT e.entity_id, e.canonical_name,
                   ST_Distance(
                       ST_MakePoint(kn.longitude, kn.latitude)::geography,
                       ST_MakePoint(%s, %s)::geography
                   ) as dist_m
            FROM entities e
            JOIN knowledge_nodes kn ON kn.master_entity_id = e.entity_id
            WHERE e.tenant_id = %s AND e.canonical_type = %s
              AND e.normalized_name LIKE %s
              AND kn.latitude IS NOT NULL
              AND ST_DWithin(
                  ST_MakePoint(kn.longitude, kn.latitude)::geography,
                  ST_MakePoint(%s, %s)::geography,
                  50000)
            ORDER BY dist_m ASC LIMIT 3
        """, (longitude, latitude, tenant_id, entity_type,
              f"{prefix}%", longitude, latitude))

        for row in cur.fetchall():
            name_sim = _name_similarity(normalized, row[1].lower())
            if name_sim > 0.7:
                action = "merge" if name_sim >= 0.9 else "review"
                return ResolutionResult(
                    row[0], action, name_sim * 0.9,
                    f"name+location: '{normalized}' ≈ '{row[1]}' ({row[2]:.0f}m apart)",
                    gdu_canonical, False,
                )
    return None


def _apply_collapse_policy_gate(
    conn, tenant_id: str, name: str, normalized: str, entity_type: str,
    identifiers: dict, identifier_hash: Optional[str],
    source_pipeline: str, match_result: "ResolutionResult", matched_via: str,
    gdu_canonical: bool, active_set_version: Optional[str],
) -> Optional["ResolutionResult"]:
    """Collapse-policy gate. Returns a NEW ResolutionResult if the merge
    should be deferred (sensitive dataset), or None to let the caller proceed
    with the normal merge.

    When deferred:
      1. A new entity row is created for THIS source's mention (so the
         flight-log Bill Marsh exists as its own LDU entity, not silently
         joined to the politician).
      2. A `collapse_candidates` row is recorded so the review UI can show
         the suggested merge with evidence.

    Identifier-hash matches (Strategy 1) bypass this gate — those have
    stronger evidence than a name-only match and are trusted across
    trust classes.
    """
    try:
        from core.gdu.dataset_policy import (
            should_block_auto_merge, pipeline_to_dataset_id, trust_class_for,
        )
    except Exception:
        return None  # policy module unavailable — proceed with normal merge

    if not should_block_auto_merge(source_pipeline):
        return None  # normal merge path

    # Strong evidence override: if the identifiers payload is non-empty AND
    # at least one identifier matches the canonical entity, allow merge.
    # (Already covered by Strategy 1 in most cases, but defensive.)
    if identifiers and identifier_hash:
        return None

    dataset_id = pipeline_to_dataset_id(source_pipeline)

    # Create the LDU-class entity for THIS mention so it doesn't auto-merge.
    new_entity = _create_entity(
        conn, tenant_id, name, normalized, entity_type,
        identifiers, identifier_hash, gdu_canonical, active_set_version,
        source_pipeline=source_pipeline,
    )

    # Record the merge proposal with evidence
    if new_entity.entity_id and match_result.entity_id and \
       new_entity.entity_id != match_result.entity_id:
        try:
            with conn.cursor() as cur:
                a, b = sorted([new_entity.entity_id, match_result.entity_id])
                evidence_for = [
                    {"type": matched_via, "weight": 0.7,
                     "detail": f"surface form '{name}' matched existing canonical entity by {matched_via}"},
                ]
                evidence_against = [
                    {"type": "policy",
                     "weight": 0.9,
                     "detail": f"dataset_id={dataset_id} trust_class={trust_class_for(source_pipeline)} — auto-merge gated"},
                ]
                cur.execute(
                    """
                    INSERT INTO collapse_candidates
                        (tenant_id, entity_a_id, entity_b_id, suggested_action,
                         evidence_for, evidence_against, score, dataset_id, status)
                    VALUES (%s, %s, %s, 'review', %s::jsonb, %s::jsonb, %s, %s, 'pending')
                    ON CONFLICT (entity_a_id, entity_b_id, dataset_id) DO UPDATE SET
                        evidence_for = EXCLUDED.evidence_for,
                        evidence_against = EXCLUDED.evidence_against,
                        score = EXCLUDED.score
                    """,
                    (tenant_id, a, b,
                     json.dumps(evidence_for), json.dumps(evidence_against),
                     0.6, dataset_id),
                )
            conn.commit()
        except Exception as e:
            _log.warning(
                "Failed to record collapse_candidate for %d ↔ %d (%s): %s",
                new_entity.entity_id, match_result.entity_id, dataset_id, e,
            )

    return ResolutionResult(
        new_entity.entity_id,
        action="review",
        confidence=0.6,
        reason=f"collapse_policy_review:{dataset_id}:{matched_via}",
        gdu_canonical=gdu_canonical,
        created=True,
    )


def _create_entity(
    conn, tenant_id: str, name: str, normalized: str,
    entity_type: str, identifiers: dict, identifier_hash: Optional[str],
    gdu_canonical: bool, active_set_version: Optional[str],
    source_pipeline: str = "unknown",
) -> ResolutionResult:
    # Subtype lockdown — pass 2 of 2 (final gate before INSERT).
    # Mirrors admit_candidate. accept→normalize subtype; route→stage in
    # entity_ldu_p so the future router can forward to the correct table;
    # drop→stage with audit reason; unknown→stage so ldup_promoter can pick
    # it up once vocab expands. Single point of enforcement covers all
    # three resolve_entity call sites (no-match, embedding-create, policy-gate).
    try:
        from core.ldu.subtype_validator import validate_for_ldu
        _v = validate_for_ldu(entity_type)
        if _v.action == "accept":
            if _v.canonical_subtype and _v.canonical_subtype != entity_type:
                entity_type = _v.canonical_subtype
        elif _v.action in ("route", "drop", "unknown"):
            if _v.action == "route" and _v.route_to:
                rej = f"route:{_v.route_to[0]}.{_v.route_to[1]}"
            elif _v.action == "drop":
                rej = f"ldu_drop:{_v.reason}"
            else:
                rej = f"ldu_unknown:{entity_type or '<empty>'}"
            try:
                from core.gdu.entity_governance import stage_provisional
                stage_provisional(
                    tenant_id=tenant_id, raw_name=name,
                    suspected_type=entity_type or "unknown",
                    source_pipeline=source_pipeline,
                    rejection_reason=rej,
                )
            except Exception:
                _log.debug("stage_provisional failed for %r (%s)", name, rej,
                           exc_info=True)
            return ResolutionResult(None, "skip", 0.0, rej, False, False)
    except Exception as e:
        _log.warning("subtype_validator failed in _create_entity for type=%r: %s "
                     "— passing through (will hit DB CHECK)", entity_type, e)

    stamp = get_lineage_stamp()
    loose = normalize_entity_name_loose(name, entity_type)

    # Seed aliases on creation so future variants find this entity.
    # Includes: the original raw name (for case/whitespace variants), the
    # strict normalized form, and the loose form if it differs. Future
    # mentions matching any of these via Layer 3 (alias_match) merge here.
    seed_aliases: list[str] = []
    raw_lower = name.lower().strip()
    if raw_lower and raw_lower != normalized:
        seed_aliases.append(raw_lower)
    if normalized and normalized not in seed_aliases:
        seed_aliases.append(normalized)
    if loose and loose != normalized and loose not in seed_aliases:
        seed_aliases.append(loose)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO entities (
                tenant_id, canonical_type, canonical_name, normalized_name,
                normalized_name_loose,
                aliases, identifiers, identifier_hash, signal_count,
                admitted_under_active_set, admitted_under_pack_versions,
                gdu_canonical, ldu_t_id
            ) VALUES (
                %s, %s, %s, %s,
                %s,
                %s, %s::jsonb, %s, 1,
                %s, %s::jsonb, %s, %s
            )
            RETURNING entity_id
        """, (
            tenant_id, entity_type, name, normalized,
            loose or None,
            seed_aliases,
            json.dumps(identifiers),
            identifier_hash,
            stamp.get("active_set_version"),
            json.dumps(stamp.get("pack_versions", {})),
            gdu_canonical,
            stamp.get("ldu_t_id"),
        ))
        row = cur.fetchone()
    conn.commit()
    entity_id = row[0] if row else None
    return ResolutionResult(entity_id, "create", 1.0, "new entity", gdu_canonical, True)


# ─── Reconciliation on Match ───────────────────────────────────────────────────

# How many days before a dossier is considered stale and re-research is queued
_STALENESS_DAYS = 7

def _reconcile_on_match(
    conn, entity_id: Optional[int], tenant_id: str,
    mention_name: str, context: str, mention_identifiers: dict,
    enrichment_types: dict,
):
    """Runs on every successful merge. Updates aliases, detects context changes,
    queues re-research if stale, and records property conflicts."""
    if not entity_id:
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT canonical_name, canonical_type, aliases, identifiers, last_seen,
                       completeness_score, updated_at, normalized_name_loose
                FROM entities WHERE entity_id = %s
            """, (entity_id,))
            row = cur.fetchone()
            if not row:
                return

            canonical_name = row[0]
            entity_type = row[1]
            existing_aliases = row[2] or []
            existing_identifiers = row[3] or {}
            last_seen = row[4]
            completeness = float(row[5]) if row[5] else 0.0
            updated_at = row[6]
            existing_loose = row[7]

            updates = []
            params = []

            # 1. Alias accumulation — add the strict-normalized form, the
            # loose form, AND the raw mention if any are new. Future
            # variants (different middle initial / different casing) hit
            # Layer 3 alias_match and merge cleanly without going through
            # the loose-name review layer.
            normalized_mention = normalize_entity_name(mention_name, entity_type)
            loose_mention = normalize_entity_name_loose(mention_name, entity_type)
            all_known = {canonical_name.lower()} | {a.lower() for a in existing_aliases}

            new_aliases: list[str] = []
            for cand in (mention_name.lower(), normalized_mention, loose_mention):
                if cand and cand not in all_known and cand not in new_aliases:
                    new_aliases.append(cand)
                    all_known.add(cand)
            for alias in new_aliases:
                updates.append("aliases = array_append(COALESCE(aliases, '{}'), %s)")
                params.append(alias)

            # Lazy backfill of normalized_name_loose if migration 043 just
            # added the column on a populated table.
            if not existing_loose and loose_mention:
                updates.append("normalized_name_loose = %s")
                params.append(loose_mention)

            # 2. Property versioning — compare context-derived properties against stored ones
            #    Don't overwrite — record changes with timestamps in identifiers JSONB
            if context:
                context_props = _extract_context_properties(context)
                conflicts = []
                new_identifiers = dict(existing_identifiers)
                changed = False

                for key, new_val in context_props.items():
                    if not new_val:
                        continue
                    old_val = existing_identifiers.get(key)
                    if old_val and str(old_val).lower() != str(new_val).lower():
                        # Property conflict detected — record both with timestamps
                        history_key = f"_history_{key}"
                        history = existing_identifiers.get(history_key, [])
                        if not isinstance(history, list):
                            history = []
                        # Add old value to history if not already there
                        if old_val and not any(h.get("value") == str(old_val) for h in history):
                            history.append({"value": str(old_val), "recorded_at": _now()})
                        history.append({"value": str(new_val), "recorded_at": _now(), "source": "article_context"})
                        new_identifiers[history_key] = history
                        new_identifiers[key] = new_val  # Update to latest
                        changed = True
                        conflicts.append({
                            "property": key,
                            "old_value": str(old_val),
                            "new_value": str(new_val),
                            "entity_id": entity_id,
                        })
                    elif not old_val and new_val:
                        new_identifiers[key] = new_val
                        changed = True

                if changed:
                    updates.append("identifiers = %s::jsonb")
                    params.append(json.dumps(new_identifiers))

                # Record conflicts as knowledge nodes
                if conflicts:
                    _record_property_conflicts(conn, entity_id, tenant_id, conflicts)

            # 3. Merge new identifiers (MMSI, email, etc.)
            if mention_identifiers:
                merged_idents = dict(existing_identifiers)
                for k, v in mention_identifiers.items():
                    if v and k not in merged_idents:
                        merged_idents[k] = v
                if merged_idents != existing_identifiers:
                    if not any("identifiers" in u for u in updates):
                        updates.append("identifiers = %s::jsonb")
                        params.append(json.dumps(merged_idents))

            # 4. Staleness check — if entity is old and being mentioned again, re-research
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            if updated_at:
                age_days = (now - updated_at.replace(tzinfo=timezone.utc)).days if updated_at.tzinfo is None else (now - updated_at).days
                if age_days > _STALENESS_DAYS and completeness < 0.75:
                    _maybe_queue_enrichment(conn, entity_id, tenant_id,
                                             "", enrichment_types)

            # Apply updates
            if updates:
                updates.append("last_seen = NOW()")
                updates.append("signal_count = signal_count + 1")
                updates.append("updated_at = NOW()")
                params.append(entity_id)
                cur.execute(f"""
                    UPDATE entities SET {', '.join(updates)}
                    WHERE entity_id = %s
                """, params)

        conn.commit()
    except Exception as e:
        _log.debug("Reconciliation failed for entity %s: %s", entity_id, e)


def _extract_context_properties(context: str) -> dict:
    """Extract role/org/location hints from the article context string."""
    props = {}
    ctx = context.lower().strip()

    # Role patterns
    import re
    role_match = re.search(
        r'(president|prime minister|ceo|director|chairman|minister|general|'
        r'secretary|ambassador|governor|mayor|leader|chief|commander|king|queen)\s+'
        r'(?:of\s+)?(?:the\s+)?(.+?)(?:\.|,|$)',
        ctx, re.IGNORECASE
    )
    if role_match:
        props["current_role"] = role_match.group(0).strip().rstrip(".,")

    # Organization patterns
    org_match = re.search(
        r'(?:of|at|from|for)\s+(?:the\s+)?([A-Z][^.,]+)',
        context.strip()
    )
    if org_match:
        props["organization"] = org_match.group(1).strip()

    return props


def _record_property_conflicts(conn, entity_id: int, tenant_id: str, conflicts: list):
    """Create knowledge_node cards for property conflicts on an entity."""
    import uuid
    try:
        with conn.cursor() as cur:
            for conflict in conflicts:
                node_id = f"node-conflict-{uuid.uuid4().hex[:12]}"
                title = f"Property change: {conflict['property']}"
                desc = (
                    f"Entity property '{conflict['property']}' changed from "
                    f"'{conflict['old_value']}' to '{conflict['new_value']}'. "
                    f"This may indicate a role change, correction, or source disagreement."
                )
                # Admit conflict node through GDU gateway
                from core.gdu.enforce import admit_node as _gdu_admit
                _gdu_admit(
                    tenant_id=tenant_id, category="assessment", subtype="claim",
                    title=title[:200], description=desc[:500],
                    signal_rid=0, collapse_key=node_id,
                )
                cur.execute("""
                    INSERT INTO knowledge_nodes
                        (node_id, tenant_id, title, description, epistemic_type,
                         master_entity_id, source_authority, confidence,
                         enrichment_status, metadata, gdu_canonical)
                    VALUES (%s, %s, %s, %s, 'claim', %s, 'unverified', 0.5,
                            'complete', %s, TRUE)
                    ON CONFLICT (node_id) DO UPDATE SET
                        gdu_canonical = TRUE, updated_at = NOW()
                """, (
                    node_id, tenant_id, title[:500], desc[:2000],
                    entity_id,
                    json.dumps({"conflict": conflict, "source": "reconciliation"}),
                ))
        conn.commit()
    except Exception as e:
        _log.debug("Failed to record conflict for entity %s: %s", entity_id, e)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ─── Enrichment Queue ──────────────────────────────────────────────────────────

def _maybe_queue_enrichment(
    conn, entity_id: Optional[int], tenant_id: str,
    entity_type: str, enrichment_types: dict,
):
    # Auto-queue disabled. Enrichment is now operator-triggered per-type from the
    # dossier UI via POST /entities/{id}/enrich/{type}. Background spam was filling
    # the queue with unrequested work; on-demand keeps every run deliberate.
    return


# ─── Audit Logging ─────────────────────────────────────────────────────────────

def _log_resolution(
    conn, tenant_id: str, name: str, entity_type: str,
    identifiers: dict, result: ResolutionResult,
    method: str, source_pipeline: str,
    gdu_canonical: bool, active_set_version: Optional[str],
):
    """Write resolution decision to entity_resolution_log (GDU S1.4 transcript)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO entity_resolution_log
                    (tenant_id, candidate_name, candidate_type, candidate_identifiers,
                     action, matched_entity_id, confidence, match_reason,
                     resolution_method, gdu_canonical, active_set_version, source_pipeline)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                tenant_id, name, entity_type, json.dumps(identifiers),
                result.action, result.entity_id, result.confidence,
                result.match_reason, method, gdu_canonical,
                active_set_version, source_pipeline,
            ))
        conn.commit()
    except Exception as e:
        _log.debug("Failed to log resolution: %s", e)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _compute_identifier_hash(identifiers: dict) -> Optional[str]:
    if not identifiers:
        return None
    normalized = {
        k: (v.lower().strip() if isinstance(v, str) else v)
        for k, v in sorted(identifiers.items())
        if v
    }
    if not normalized:
        return None
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_bigrams = set(a[i:i+2] for i in range(len(a) - 1))
    b_bigrams = set(b[i:i+2] for i in range(len(b) - 1))
    if not a_bigrams or not b_bigrams:
        return 0.0
    intersection = len(a_bigrams & b_bigrams)
    union = len(a_bigrams | b_bigrams)
    return intersection / union if union > 0 else 0.0


def reset():
    """Reset cached state (for testing only)."""
    global _RESOLUTION_RULES, _ENRICHMENT_TYPES, _GDU_CANONICAL, _ACTIVE_SET_VERSION
    _RESOLUTION_RULES = None
    _ENRICHMENT_TYPES = None
    _GDU_CANONICAL = False
    _ACTIVE_SET_VERSION = None
