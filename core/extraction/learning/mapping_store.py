"""
Mapping store — DB access for the learned_mappings table.

This module holds the SQL that the learner and the CLI status command
share. The shape profiler has its own observe() because insertion needs
to be in a single transaction with the upsert-vs-update branch; this
module is the read side.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from core.extraction.learning.mapping_eval import evaluate_candidate
from core.extraction.learning.mapping_learner import propose_mapping

log = logging.getLogger("enki.extraction.learning.store")

CANDIDATE_MIN_SAMPLES = 10
CANDIDATE_MIN_SCORE = 0.4


def get_status(conn) -> list[dict[str, Any]]:
    """Return a summary of every row in learned_mappings — one entry
    per signal_type."""
    if conn is None:
        return []
    rows: list[dict[str, Any]] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT signal_type, mapping_version, status, sample_count,
                       last_score, first_observed_at, last_observed_at,
                       promoted_at, promoted_path,
                       admitted_under_active_set, gdu_canonical
                  FROM learned_mappings
                 ORDER BY sample_count DESC, last_observed_at DESC
                """
            )
            for row in cur.fetchall():
                rows.append({
                    "signal_type": row[0],
                    "version": row[1],
                    "status": row[2],
                    "samples": row[3],
                    "score": row[4],
                    "first_seen": row[5].isoformat() if row[5] else None,
                    "last_seen": row[6].isoformat() if row[6] else None,
                    "promoted_at": row[7].isoformat() if row[7] else None,
                    "promoted_path": row[8],
                    "admitted_under": row[9],
                    "canonical": row[10],
                })
    except Exception as e:
        log.warning("get_status failed: %s", e)
    return rows


def learn_from_observations(conn, *, signal_type: Optional[str] = None) -> list[dict[str, Any]]:
    """For every observing row with enough samples, propose a mapping,
    score it, and store the result. If signal_type is given, restrict
    to that type. Returns a list of {signal_type, score, proposed_map}
    dicts for reporting."""
    if conn is None:
        return []

    results: list[dict[str, Any]] = []
    try:
        with conn.cursor() as cur:
            where = "status = 'observing' AND sample_count >= %s"
            params: list[Any] = [CANDIDATE_MIN_SAMPLES]
            if signal_type:
                where += " AND signal_type = %s"
                params.append(signal_type)
            cur.execute(
                f"""
                SELECT mapping_id, signal_type, shape_profile, sample_count
                  FROM learned_mappings
                 WHERE {where}
                """,
                params,
            )
            rows = cur.fetchall()

        for mapping_id, st, profile, count in rows:
            proposal = propose_mapping(profile or {}, signal_type=st)
            score = evaluate_candidate(proposal, profile or {})
            status = "candidate" if score >= CANDIDATE_MIN_SCORE else "observing"
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE learned_mappings
                       SET proposed_map = %s::jsonb,
                           last_score = %s,
                           status = %s,
                           score_history = score_history || %s::jsonb,
                           updated_at = NOW()
                     WHERE mapping_id = %s
                    """,
                    (
                        json.dumps(proposal),
                        score,
                        status,
                        json.dumps([score]),
                        mapping_id,
                    ),
                )
            conn.commit()
            results.append({
                "signal_type": st,
                "samples": count,
                "score": score,
                "status": status,
                "mapping_fields": len(proposal.get("field_map") or {}),
            })
    except Exception as e:
        log.warning("learn_from_observations failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    return results
