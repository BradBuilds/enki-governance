"""
LDU collapse — merge pending concepts that refer to the same referent.

Operates on ``concept_nodes`` (the post-6.5 substrate). Does not touch
``entities``. Merge is logical: the loser's promotion_status flips to
'merged', merged_to_concept_id points at the winner, signal counts +
source_signal_rids + identifiers + form_concept_bindings roll up.

Strategies (in priority order, per entityvocab.resolution_rules):
  1. canonical_identifier  — same QID/CIK/EIN/etc. in identifiers JSONB
  2. identifier_hash       — derived from identifiers; cheap duplicate check
  3. normalized_name_exact — same (canonical_name, subtype_path)
  4. alias_match           — driven by dictionary re-resolution (catch-up
                             pass for forms whose binding missed admit-time)

Non-merge guards prevent the Tim-Cook-CEO vs Tim-Cook-academic trap:
  - conflicting tier-A identifiers in type_evidence → do NOT merge
  - mismatched subtype_path → do NOT merge (categories differ)
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.ldu.collapse")


# ── Identifier hash ─────────────────────────────────────────────────────────

# Identifier keys that count as "strong" — same value = same entity.
_STRONG_ID_KEYS = (
    "wikidata_id", "wikidata_qid",
    "sec_cik", "cik", "ein", "duns", "lei", "ticker",
    "mmsi", "imo", "icao24", "registration", "tail_number",
    "fec_id", "committee_id",
)


def _identifier_hash(identifiers: dict) -> Optional[str]:
    """SHA-256 of strong identifiers sorted into a canonical form."""
    if not identifiers:
        return None
    strong = {k: str(identifiers[k]).strip().lower()
              for k in _STRONG_ID_KEYS if identifiers.get(k)}
    if not strong:
        return None
    payload = json.dumps(strong, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _has_conflicting_strong_ids(a: dict, b: dict) -> bool:
    """True if a and b carry the same strong-id KEY with DIFFERENT values."""
    if not a or not b:
        return False
    for k in _STRONG_ID_KEYS:
        va = a.get(k)
        vb = b.get(k)
        if va and vb:
            if str(va).strip().lower() != str(vb).strip().lower():
                return True
    return False


# ── Public entry point ─────────────────────────────────────────────────────

def collapse_pending_concepts(
    *,
    tenant_id: str = "default",
    limit: int = 200,
    dsn: Optional[str] = None,
) -> dict:
    """Walk pending concepts, run strategies 1–4 in priority order,
    merge duplicates via merged_to_concept_id pointer.

    Returns ``{'walked': N, 'merged_by_identifier': M1,
              'merged_by_name': M2, 'merged_by_dict': M3,
              'errors': E}``.
    """
    walked = 0
    by_identifier = 0
    by_name = 0
    by_dict = 0
    errors = 0

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # ── Strategy 1 + 2: collapse by strong identifier ─────
                # Find groups of pending concepts sharing any strong
                # identifier value.
                for id_key in _STRONG_ID_KEYS:
                    cur.execute(
                        """
                        SELECT identifiers->>%s AS id_val,
                               array_agg(concept_id ORDER BY concept_id) AS ids,
                               array_agg(subtype_path ORDER BY concept_id) AS paths,
                               array_agg(type_evidence ORDER BY concept_id) AS tev
                        FROM concept_nodes
                        WHERE tenant_id = %s
                          AND promotion_status = 'pending'
                          AND identifiers ? %s
                        GROUP BY identifiers->>%s
                        HAVING count(*) > 1
                        LIMIT %s
                        """,
                        (id_key, tenant_id, id_key, id_key, limit),
                    )
                    for id_val, cids, paths, tevs in cur.fetchall():
                        winner_id = cids[0]
                        # Verify subtype_paths match before merging — a
                        # QID can only legitimately bind to one
                        # subtype_path. Mismatch = data error, log and
                        # skip.
                        if len(set(paths)) > 1:
                            log.warning(
                                "strategy-1 mismatch: %s=%s across paths %s, skipping",
                                id_key, id_val, paths,
                            )
                            continue
                        for loser_id in cids[1:]:
                            if _merge_concept(cur, winner_id, loser_id,
                                              reason=f"strategy_1_{id_key}"):
                                by_identifier += 1
                                walked += 1

                # ── Strategy 3: same (canonical_name, subtype_path) ────
                cur.execute(
                    """
                    SELECT canonical_name, subtype_path,
                           array_agg(concept_id ORDER BY concept_id) AS ids,
                           array_agg(identifiers ORDER BY concept_id) AS idents
                    FROM concept_nodes
                    WHERE tenant_id = %s
                      AND promotion_status = 'pending'
                    GROUP BY canonical_name, subtype_path
                    HAVING count(*) > 1
                    LIMIT %s
                    """,
                    (tenant_id, limit),
                )
                for cname, spath, cids, idents in cur.fetchall():
                    winner_id = cids[0]
                    winner_idents = idents[0] or {}
                    for loser_id, loser_idents in zip(cids[1:], idents[1:]):
                        loser_idents = loser_idents or {}
                        # Non-merge guard: conflicting tier-A IDs.
                        if _has_conflicting_strong_ids(winner_idents, loser_idents):
                            log.info(
                                "strategy-3 blocked: %s identifier conflict between %s and %s",
                                cname, winner_id, loser_id,
                            )
                            continue
                        if _merge_concept(cur, winner_id, loser_id,
                                          reason="strategy_3_normname"):
                            by_name += 1
                            walked += 1

                # ── Strategy 4: dictionary catch-up pass ──────────────
                # For pending concepts whose canonical_name is NOT
                # already a dictionary canonical, look it up. If hit,
                # re-resolve and merge with any pending concept that
                # has the dictionary canonical.
                cur.execute(
                    """
                    SELECT concept_id, canonical_name, subtype_path,
                           identifiers
                    FROM concept_nodes
                    WHERE tenant_id = %s
                      AND promotion_status = 'pending'
                      AND evidence_tier = 'G'  -- floor, no dictionary hit yet
                    ORDER BY concept_id
                    LIMIT %s
                    """,
                    (tenant_id, limit),
                )
                floats = cur.fetchall()
                if floats:
                    from core.ldu.dictionaries import resolve
                    for cid, cname, spath, idents in floats:
                        try:
                            hit = resolve(cname)
                        except Exception:
                            hit = None
                        if not hit:
                            continue
                        # Check if a concept with this canonical already
                        # exists.
                        target_subtype = (
                            f"{hit.canonical_type}.{hit.canonical_subtype}"
                            if hit.canonical_subtype
                            else hit.canonical_type
                        )
                        cur.execute(
                            """
                            SELECT concept_id FROM concept_nodes
                            WHERE tenant_id = %s
                              AND canonical_name = %s
                              AND subtype_path = %s
                              AND promotion_status IN ('pending','promoted')
                              AND concept_id <> %s
                            ORDER BY concept_id
                            LIMIT 1
                            """,
                            (tenant_id, hit.canonical_name, target_subtype, cid),
                        )
                        target = cur.fetchone()
                        if target:
                            winner_id = int(target[0])
                            if _merge_concept(cur, winner_id, cid,
                                              reason=f"strategy_4_{hit.source}"):
                                by_dict += 1
                                walked += 1
                        else:
                            # No target concept yet — just update this
                            # concept's canonical to the dictionary form
                            # so future captures merge here.
                            cur.execute(
                                """
                                UPDATE concept_nodes SET
                                  canonical_name = %s,
                                  canonical_subtype = COALESCE(%s, canonical_subtype),
                                  subtype_path = %s,
                                  identifiers = identifiers
                                                || %s::jsonb,
                                  evidence_tier = %s,
                                  type_evidence = type_evidence
                                                  || jsonb_build_object('upgraded_via', %s),
                                  updated_at = NOW()
                                WHERE concept_id = %s
                                """,
                                (hit.canonical_name,
                                 hit.canonical_subtype,
                                 target_subtype,
                                 json.dumps(hit.identifiers or {}),
                                 hit.evidence_tier,
                                 hit.source,
                                 cid),
                            )
                            walked += 1

            conn.commit()
    except Exception as e:
        log.exception("LDU collapse tick failed: %s", e)
        return {
            "walked": walked, "merged_by_identifier": by_identifier,
            "merged_by_name": by_name, "merged_by_dict": by_dict,
            "errors": errors + 1, "error": str(e),
        }

    total_merged = by_identifier + by_name + by_dict
    if total_merged:
        log.info(
            "LDU collapse: walked=%d merged_id=%d merged_name=%d merged_dict=%d (tenant=%s)",
            walked, by_identifier, by_name, by_dict, tenant_id,
        )
    return {
        "walked": walked,
        "merged_by_identifier": by_identifier,
        "merged_by_name": by_name,
        "merged_by_dict": by_dict,
        "errors": errors,
    }


# Legacy alias — old worker called this name.
collapse_pending_candidates = collapse_pending_concepts


# ── Merge helper ───────────────────────────────────────────────────────────

def _merge_concept(cur, winner_id: int, loser_id: int, *, reason: str) -> bool:
    """Roll the loser's accumulation into the winner; mark loser merged.
    Also re-points loser's form_concept_bindings to the winner.

    Returns True if a merge happened.
    """
    # Re-check loser is still pending (an earlier strategy may have
    # flipped it).
    cur.execute(
        "SELECT promotion_status FROM concept_nodes WHERE concept_id = %s",
        (loser_id,),
    )
    row = cur.fetchone()
    if not row or row[0] != "pending":
        return False

    # Pull loser's accumulation.
    cur.execute(
        """SELECT signal_count, source_signal_rids, identifiers, type_evidence,
                  evidence_tier
             FROM concept_nodes WHERE concept_id = %s""",
        (loser_id,),
    )
    row = cur.fetchone()
    if not row:
        return False
    l_count, l_rids, l_ids, l_evidence, l_tier = row

    # Roll up onto winner.
    cur.execute(
        """
        UPDATE concept_nodes SET
          signal_count = signal_count + %s,
          updated_at   = NOW(),
          source_signal_rids = ARRAY(
            SELECT DISTINCT unnest(source_signal_rids || %s::bigint[])
          ),
          identifiers  = COALESCE(identifiers, '{}'::jsonb)
                         || COALESCE(%s::jsonb, '{}'::jsonb),
          type_evidence = COALESCE(type_evidence, '{}'::jsonb)
                          || COALESCE(%s::jsonb, '{}'::jsonb)
                          || jsonb_build_object('merged_from', %s),
          evidence_tier = CASE
            WHEN evidence_tier IS NULL THEN %s
            WHEN %s IS NULL THEN evidence_tier
            WHEN %s < evidence_tier THEN %s
            ELSE evidence_tier
          END
        WHERE concept_id = %s
        """,
        (
            int(l_count or 0),
            list(l_rids or []),
            json.dumps(l_ids) if l_ids else "{}",
            json.dumps(l_evidence) if l_evidence else "{}",
            loser_id,
            l_tier, l_tier, l_tier, l_tier,
            winner_id,
        ),
    )

    # Re-point loser's form bindings to the winner.
    cur.execute(
        """
        UPDATE form_concept_bindings
          SET concept_id = %s,
              binding_evidence = COALESCE(binding_evidence, '{}'::jsonb)
                                 || jsonb_build_object(
                                      'merged_from_concept', %s,
                                      'merge_reason', %s)
          WHERE concept_id = %s
            AND NOT EXISTS (
              SELECT 1 FROM form_concept_bindings b2
              WHERE b2.concept_id = %s
                AND b2.form_id = form_concept_bindings.form_id
            )
        """,
        (winner_id, loser_id, reason[:64], loser_id, winner_id),
    )
    # Any leftover loser bindings (where a winner binding already
    # exists with the same form_id) get deactivated.
    cur.execute(
        """
        UPDATE form_concept_bindings
          SET active = FALSE,
              binding_evidence = COALESCE(binding_evidence, '{}'::jsonb)
                                 || jsonb_build_object(
                                      'merged_into_concept', %s,
                                      'merge_reason', %s)
          WHERE concept_id = %s
        """,
        (winner_id, reason[:64], loser_id),
    )

    # Mark loser merged.
    cur.execute(
        """
        UPDATE concept_nodes SET
          promotion_status     = 'merged',
          merged_to_concept_id = %s,
          merge_reason         = %s,
          updated_at           = NOW()
        WHERE concept_id = %s
        """,
        (winner_id, reason[:128], loser_id),
    )

    return True
