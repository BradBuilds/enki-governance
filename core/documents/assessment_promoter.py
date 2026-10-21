"""
Assessment promoter — collapse statements into canonical assessments.

A statement is one DOCUMENT'S utterance: "doc D paragraph P claims that
subject S did predicate Q to object O." An assessment is the canonical
claim: across all corpora and documents, the system asserts that
(S, edge_type(Q), O) is true / contested / unverified.

Pipeline:
  1. Walk validation_status='pending' statements.
  2. Resolve predicate_lemma → edge_type via predicate_dictionary.
  3. Validate (subj_type, edge_type, obj_type) via GDU gate.
  4. Compute collapse_key over canonical (subj, edge_type, obj).
  5. Find-or-create assessment row.
  6. Append statement_id to source_statement_ids; recompute
     corroboration count + promote tier:
       1 source  → observation
       ≥2        → fact (if no contradictions)
       contradicted → claim
  7. Mint base-60 address at assessment/{tier} on first creation.
  8. Mark statement validation_status='promoted' OR 'rejected'.
  9. Update predicate_dictionary observation_count for the verb.

Idempotent. Safe to re-run as new statements arrive.

Run inside the api container:
    docker exec app-api-1 python -c "
        import sys; sys.path.insert(0,'/app')
        from core.documents.assessment_promoter import promote_corpus
        print(promote_corpus('example_corpus'))
    "
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from typing import Optional

from core.gdu.address import assign_address
from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.assessment_promoter")


# ─── Predicate resolution ──────────────────────────────────────────────

def _variants(lemma: str) -> list[str]:
    """Return likely surface variants of a lemma to try in the dictionary.

    The statement extractor's lemmatizer is imperfect — past-tense or
    participle forms sometimes survive. Try the raw input plus stripped
    common suffixes."""
    if not lemma:
        return []
    s = lemma.strip().lower()
    out = [s]
    # past tense
    if s.endswith("ied") and len(s) > 4:
        out.append(s[:-3] + "y")
    if s.endswith("ed") and len(s) > 4:
        if len(s) >= 5 and s[-3] == s[-4] and s[-3] not in "aeiou":
            out.append(s[:-3])     # stopped → stop
        out.append(s[:-2])         # walked → walk
        out.append(s[:-1])         # described → describe
    # present participle
    if s.endswith("ing") and len(s) > 4:
        if len(s) >= 6 and s[-4] == s[-5] and s[-4] not in "aeiou":
            out.append(s[:-4])     # running → run
        out.append(s[:-3])         # walking → walk
    # 3rd-person -s — guard length so "is"/"was"/"has" survive
    if s.endswith("s") and not s.endswith("ss") and len(s) > 3:
        out.append(s[:-1])         # walks → walk
    # Irregular forms — bidirectional. Looking up a present-tense root
    # should find past-tense dictionary entries too, and vice versa.
    pairs = [
        ("say", "said"), ("see", "saw"), ("tell", "told"),
        ("have", "had"),
        ("do", "did"), ("go", "went"), ("come", "came"),
        ("take", "took"), ("give", "gave"),
        ("meet", "met"), ("speak", "spoke"), ("write", "wrote"),
        ("know", "knew"), ("fly", "flew"), ("get", "got"),
        ("find", "found"), ("think", "thought"), ("hear", "heard"),
        ("bring", "brought"), ("buy", "bought"), ("run", "ran"),
        ("ask", "asked"), ("reply", "replied"), ("report", "reported"),
        ("call", "called"), ("phone", "phoned"),
    ]
    for present, past in pairs:
        if s == present:
            out.append(past)
        elif s == past:
            out.append(present)
        # also handle past participle for the few with distinct forms
    pp_to_present = {
        "seen": "see", "told": "tell", "had": "have",
        "done": "do", "gone": "go", "taken": "take",
        "given": "give", "spoken": "speak", "written": "write",
        "known": "know", "flown": "fly", "gotten": "get",
        "found": "find", "thought": "think", "heard": "hear",
        "brought": "bring", "bought": "buy", "ran": "run",
    }
    if s in pp_to_present:
        out.append(pp_to_present[s])
    # dedup preserving order
    seen, deduped = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v); deduped.append(v)
    return deduped


def _resolve_predicate(predicate_lemma: str) -> Optional[tuple[str, float]]:
    """Map a verb lemma to an edge_type via predicate_dictionary.

    Tries the raw lemma plus several morphological variants (past
    tense, participle, irregular) before giving up. Highest-confidence
    match wins.
    """
    if not predicate_lemma:
        return None
    candidates = _variants(predicate_lemma)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT edge_type, confidence, canonical_lemma
                  FROM predicate_dictionary
                 WHERE canonical_lemma = ANY(%s)
                 ORDER BY confidence DESC LIMIT 1
                """,
                (candidates,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return (row[0], float(row[1] or 0.7))


def _validate_type_pair(src_id: Optional[int], edge_type: str, dst_id: Optional[int]) -> Optional[str]:
    """Returns None if (src_type, edge_type, dst_type) is valid; otherwise
    a short rejection reason.

    gdu_type_constraints uses GDU CATEGORIES (entity, place, event, …) but
    entities carry the canonical_type SUBTYPE (person, country, aircraft, …).
    A 'person' is an 'entity'; a 'country' is a 'place'. Try the subtype
    literal first (handles subtype-specific edges like country↔country),
    then fall back to its category.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            src_type = dst_type = None
            if src_id:
                cur.execute("SELECT canonical_type FROM entities WHERE entity_id = %s", (src_id,))
                r = cur.fetchone(); src_type = r[0] if r else None
            if dst_id:
                cur.execute("SELECT canonical_type FROM entities WHERE entity_id = %s", (dst_id,))
                r = cur.fetchone(); dst_type = r[0] if r else None

            def _resolve(t: Optional[str]) -> list[Optional[str]]:
                """Return [subtype, category] candidates to try."""
                if not t:
                    return [None]
                cur.execute(
                    "SELECT category FROM gdu_address_sequences WHERE subtype = %s LIMIT 1",
                    (t,),
                )
                row = cur.fetchone()
                return [t, row[0]] if row and row[0] and row[0] != t else [t]

            src_candidates = _resolve(src_type)
            dst_candidates = _resolve(dst_type)

            last_reason: Optional[str] = None
            for s in src_candidates:
                for d in dst_candidates:
                    cur.execute(
                        "SELECT validate_edge_type_pair(%s, %s, %s)",
                        (s, edge_type, d),
                    )
                    reason = cur.fetchone()[0]
                    if reason is None:
                        return None  # pair valid
                    last_reason = reason
            return last_reason


# ─── Promotion ─────────────────────────────────────────────────────────

def _is_symmetric_edge(conn, edge_type: str) -> bool:
    """Lookup edge symmetry from gdu_type_constraints. Symmetric edges
    use a canonicalized collapse_key (sorted subj/obj) so A→B and B→A
    merge into a single assessment row."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_symmetric FROM gdu_type_constraints WHERE edge_type = %s",
            (edge_type,),
        )
        row = cur.fetchone()
        return bool(row and row[0])


# Open-contribution gate: assessments whose source is a low-trust contributor
# (anonymous / repeatedly-flagged) need extra corroboration before they promote
# to `fact`. Neutral 0.5 default leaves legacy + system-acquired docs unaffected.
PROMOTE_WEIGHT_THRESHOLD = 0.5


def _statement_source_weight(cur, statement_id: int) -> float:
    """Contributor weight backing a statement's source document. 0.5 (neutral)
    when there's no contributor — i.e. legacy/system corpus keeps promoting as
    before."""
    cur.execute(
        """SELECT COALESCE(lm.contributor_weight, ct.trust_score, 0.5)
             FROM statements s
             LEFT JOIN documents d ON d.document_id = s.document_id
             LEFT JOIN contributors ct ON ct.contributor_id = d.contributor_id
             LEFT JOIN library_members lm
                    ON lm.member_id = s.document_id AND lm.member_kind = 'document'
            WHERE s.statement_id = %s
            LIMIT 1""",
        (statement_id,),
    )
    r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else 0.5


def _upsert_assessment(
    tenant_id: str,
    subj_id: int, edge_type: str, obj_id: Optional[int], obj_value: Optional[str],
    polarity: str, statement_id: int, statement_confidence: float,
    event_time, location_entity_id: Optional[int],
    source_weight: float = 0.5,
    _retry: int = 0,
) -> tuple[int, bool, str]:
    """Find-or-create the canonical assessment. Returns (assessment_id,
    is_new, current_tier).

    For symmetric edge types (is_symmetric=TRUE in gdu_type_constraints)
    the collapse key sorts (subj, obj) so A→B and B→A collide into one
    assessment. The persisted row keeps the original direction it was
    inserted with; statements still record the per-document direction.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            symmetric = _is_symmetric_edge(conn, edge_type)
            # For symmetric edges, sort the endpoint IDs so (A,B) and
            # (B,A) produce the same hash.
            if symmetric and subj_id is not None and obj_id is not None and obj_id < subj_id:
                key_subj, key_obj = obj_id, subj_id
            else:
                key_subj, key_obj = subj_id, obj_id
            cur.execute(
                "SELECT compute_assessment_collapse_key(%s,%s,%s,%s)",
                (key_subj, edge_type, key_obj, obj_value),
            )
            ckey = cur.fetchone()[0]
            cur.execute(
                """
                SELECT assessment_id, assessment_tier, corroborated_by_count,
                       contradicted_by_count, source_statement_ids,
                       earliest_event_time, latest_event_time
                  FROM assessments WHERE collapse_key = %s
                """,
                (ckey,),
            )
            existing = cur.fetchone()

            if existing:
                a_id, tier, corr, contr, ids, ets, lts = existing
                if statement_id in (ids or []):
                    return a_id, False, tier
                new_ids = list(ids or []) + [statement_id]
                new_corr = corr + (1 if polarity == 'affirmative' else 0)
                new_contr = contr + (1 if polarity == 'negative' else 0)
                new_tier = tier
                if new_contr > 0 and new_corr > 0:
                    new_tier = "claim"
                elif new_corr >= 2 and (source_weight >= PROMOTE_WEIGHT_THRESHOLD or new_corr >= 3):
                    # low-trust (anonymous) sources stay parked at observation
                    # until a 3rd corroboration; trusted/legacy promote at 2.
                    new_tier = "fact"
                elif new_corr == 1 and new_contr == 0:
                    new_tier = "observation"
                new_ets = ets
                new_lts = lts
                if event_time and (new_ets is None or event_time < new_ets):
                    new_ets = event_time
                if event_time and (new_lts is None or event_time > new_lts):
                    new_lts = event_time
                cur.execute(
                    """
                    UPDATE assessments
                       SET corroborated_by_count = %s,
                           contradicted_by_count = %s,
                           source_statement_ids  = %s,
                           assessment_tier       = %s,
                           earliest_event_time   = %s,
                           latest_event_time     = %s,
                           last_seen             = NOW(),
                           confidence            = GREATEST(COALESCE(confidence, 0), %s)
                     WHERE assessment_id = %s
                    """,
                    (new_corr, new_contr, new_ids, new_tier,
                     new_ets, new_lts, statement_confidence, a_id),
                )
                conn.commit()
                return a_id, False, new_tier

            # New assessment — mint address. Assessment inherits its
            # extraction_profile_id from the source statement so the
            # federation publisher can discover it.
            tier = "observation"
            addr = assign_address(conn, "assessment", tier)
            cur.execute(
                "SELECT extraction_profile_id FROM statements WHERE statement_id = %s",
                (statement_id,),
            )
            _row = cur.fetchone()
            stmt_profile_id = _row[0] if _row else None
            cur.execute(
                """
                INSERT INTO assessments
                    (tenant_id, base60_address, assessment_tier,
                     subj_entity_id, predicate_edge_type, obj_entity_id, obj_value,
                     polarity, collapse_key,
                     corroborated_by_count, contradicted_by_count,
                     source_statement_ids,
                     earliest_event_time, latest_event_time,
                     primary_location_entity_id, confidence,
                     extraction_profile_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (collapse_key) DO NOTHING
                RETURNING assessment_id
                """,
                (tenant_id, addr, tier,
                 subj_id, edge_type, obj_id, obj_value,
                 polarity, ckey,
                 1 if polarity == 'affirmative' else 0,
                 1 if polarity == 'negative' else 0,
                 [statement_id],
                 event_time, event_time,
                 location_entity_id, statement_confidence,
                 stmt_profile_id),
            )
            row = cur.fetchone()
            if row is None:
                # A concurrent promoter inserted this collapse_key first
                # (ON CONFLICT DO NOTHING -> no row returned). Commit the
                # no-op and re-enter: the SELECT at the top now finds the
                # row and takes the UPDATE (corroboration) path. Bounded so
                # a not-yet-committed peer txn can't spin us forever.
                conn.commit()
                if _retry < 3:
                    return _upsert_assessment(
                        tenant_id, subj_id, edge_type, obj_id, obj_value,
                        polarity, statement_id, statement_confidence,
                        event_time, location_entity_id, source_weight, _retry + 1)
                with get_connection() as _c2:
                    with _c2.cursor() as _cu2:
                        _cu2.execute(
                            "SELECT assessment_id, assessment_tier "
                            "FROM assessments WHERE collapse_key = %s", (ckey,))
                        _r2 = _cu2.fetchone()
                if _r2:
                    return _r2[0], False, _r2[1]
                raise RuntimeError('assessment upsert failed: collapse_key contention')
            a_id = row[0]
            conn.commit()
            return a_id, True, tier


def _mark_statement(stmt_id: int, status: str, reason: Optional[str],
                    assessment_id: Optional[int] = None) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE statements
                   SET validation_status = %s,
                       validation_reason = %s,
                       assessment_entity_id = %s
                 WHERE statement_id = %s
                """,
                (status, reason, None, stmt_id),  # assessment_entity_id is for entity-typed assessments; we use assessments table
            )
        conn.commit()


def _bump_predicate_observation(lemma: str, edge_type: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE predicate_dictionary
                   SET observation_count = observation_count + 1,
                       last_seen = NOW()
                 WHERE canonical_lemma = %s AND edge_type = %s
                """,
                (lemma, edge_type),
            )
        conn.commit()


# ─── Structural grade (Phase 2) ───────────────────────────────────────

GRADE_COLLAPSIBLE = "collapsible"
GRADE_UNTRUSTED = "untrusted"
GRADE_EVIDENCE_ONLY = "evidence_only"


def _grade_for_promotion(stmt_id, tenant, subj_id, obj_id, evt_time, loc_id,
                         lemma, verb_surf, raw) -> Optional[dict]:
    """Grade one statement and persist the verdict.

    Returns None if grading is unavailable, in which case the promoter falls
    through to its original checks — a node that cannot grade must keep
    working, not stop promoting.
    """
    try:
        from core.documents.statement_grade import GRADE_VERSION, grade_statement
        with get_connection() as conn:
            with conn.cursor() as cur:
                g = grade_statement(
                    cur, speaker_entity_id=subj_id, object_entity_id=obj_id,
                    event_time=evt_time, event_location_entity_id=loc_id,
                    predicate_lemma=lemma, predicate_verb_surface=verb_surf,
                    raw_text=raw, tenant=tenant)
                cur.execute(
                    """
                    UPDATE statements
                       SET structure_grade = %s, structure_reasons = %s,
                           grade_version = %s, graded_at = now()
                     WHERE statement_id = %s
                    """,
                    (g["grade"], g["reasons"], GRADE_VERSION, stmt_id),
                )
            conn.commit()
        return g
    except Exception:
        log.exception("grading failed for statement %s — falling through", stmt_id)
        return None


# ─── Public ───────────────────────────────────────────────────────────

def promote_pending(tenant_id: str = "default", limit: Optional[int] = None) -> dict:
    """Promote every pending statement to its canonical assessment."""
    stats = {
        "scanned": 0, "promoted": 0, "rejected_no_predicate": 0,
        "rejected_no_subject": 0, "rejected_type_pair": 0,
        "rejected_other": 0, "new_assessments": 0,
        "untrusted": 0, "evidence_only": 0,
        "tier_changes": defaultdict(int),
    }
    with get_connection() as conn:
        with conn.cursor(name="promote_cursor") as cur:
            cur.itersize = 200
            cur.execute(
                """
                SELECT statement_id, tenant_id, speaker_entity_id,
                       predicate_lemma, predicate_verb_surface,
                       object_entity_id, raw_text,
                       polarity, confidence, event_time,
                       event_location_entity_id, modality
                  FROM statements
                 WHERE validation_status = 'pending'
                 ORDER BY statement_id
                """ + (f" LIMIT {limit}" if limit else "")
            )
            rows = list(cur)

    for (stmt_id, t_id, subj_id, lemma, verb_surf, obj_id, raw,
         polarity, conf, evt_time, loc_id, modality) in rows:
        stats["scanned"] += 1

        # ── Structure first (Phase 2) ──
        # Collapse eligibility is a FUNCTION of the structural grade, not a
        # gate in front of it. A statement that is not structurally sound is
        # never promoted, but it is not rejected either — `untrusted` keeps
        # its reasons so a substrate change can re-ask the question, and
        # `evidence_only` keeps it searchable as what it is. The only thing
        # that changes here is which lane a NON-promotion lands in; nothing
        # that promoted before stops promoting.
        grade = _grade_for_promotion(
            stmt_id, t_id or tenant_id, subj_id, obj_id, evt_time, loc_id,
            lemma, verb_surf, raw)
        if grade and grade["grade"] == GRADE_EVIDENCE_ONLY:
            _mark_statement(stmt_id, "evidence_only",
                            ",".join(grade["reasons"])[:200])
            stats["evidence_only"] += 1
            continue
        if grade and grade["grade"] == GRADE_UNTRUSTED:
            _mark_statement(stmt_id, "untrusted",
                            ",".join(grade["reasons"])[:200])
            stats["untrusted"] += 1
            continue

        if not subj_id:
            # Unreachable once grading is on (the grade catches it first) and
            # kept so a node with no grader still behaves.
            _mark_statement(stmt_id, "untrusted", "no_subject_entity")
            stats["untrusted"] += 1
            continue
        if obj_id is not None and subj_id == obj_id:
            _mark_statement(stmt_id, "evidence_only", "self_reference")
            stats["evidence_only"] += 1
            continue
        lemma = lemma or ""
        edge_resolution = _resolve_predicate(lemma)
        if not edge_resolution:
            # A VOCABULARY GAP, not a defect in the statement. This was the
            # single largest rejection class (4,323) and it was terminal —
            # 2,063 of those statements are structurally sound and were held
            # only because nothing had a word for their verb yet. Untrusted
            # keeps them re-askable when the dictionary grows.
            _mark_statement(stmt_id, "untrusted", f"no_predicate_mapping:{lemma}")
            stats["rejected_no_predicate"] += 1
            continue
        edge_type, _pred_conf = edge_resolution

        reason = _validate_type_pair(subj_id, edge_type, obj_id)
        if reason is not None:
            _mark_statement(stmt_id, "untrusted", f"type_pair_invalid:{reason}")
            stats["rejected_type_pair"] += 1
            continue

        try:
            obj_value = None
            with get_connection() as _wconn:
                with _wconn.cursor() as _wcur:
                    src_weight = _statement_source_weight(_wcur, stmt_id)
            a_id, is_new, tier = _upsert_assessment(
                tenant_id=t_id or tenant_id,
                subj_id=subj_id, edge_type=edge_type,
                obj_id=obj_id, obj_value=obj_value,
                polarity=polarity, statement_id=stmt_id,
                statement_confidence=float(conf or 0.6),
                event_time=evt_time, location_entity_id=loc_id,
                source_weight=src_weight,
            )
            _mark_statement(stmt_id, "promoted", f"assessment:{a_id}")
            _bump_predicate_observation(lemma, edge_type)
            stats["promoted"] += 1
            if is_new:
                stats["new_assessments"] += 1
            stats["tier_changes"][tier] += 1

            # Spawn event if predicate is action-class (Phase 2A)
            try:
                from core.documents.event_spawner import spawn_from_statement
                spawn_from_statement(stmt_id)
            except Exception:
                log.exception("event_spawner failed for statement %d (non-fatal)", stmt_id)
        except Exception:
            log.exception("promotion failed for statement %d", stmt_id)
            _mark_statement(stmt_id, "rejected", "exception_during_promotion")
            stats["rejected_other"] += 1

    stats["tier_changes"] = dict(stats["tier_changes"])
    return stats


def promote_corpus(dataset_id: str, tenant_id: str = "default") -> dict:
    """Convenience: promote every pending statement attached to a corpus."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.statement_id
                  FROM statements s
                  JOIN documents d ON d.document_id = s.document_id
                 WHERE d.dataset_id = %s AND s.validation_status = 'pending'
                """,
                (dataset_id,),
            )
            ids = [r[0] for r in cur.fetchall()]
    log.info("Promoting %d pending statements for dataset=%s", len(ids), dataset_id)
    return promote_pending(tenant_id=tenant_id)
