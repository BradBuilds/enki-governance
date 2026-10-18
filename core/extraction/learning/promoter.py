"""
Promoter — turns a learned candidate into a live mapping file.

State machine (learned_mappings.status):
    observing → candidate → promoted
                         ↘ rejected

Transitions:
    observing  → candidate : mapping_learner saw enough samples and
                             mapping_eval scored it above threshold
    candidate  → promoted  : human approved (or auto-promote policy met)
                             — export YAML to storage/mappings/learned/
                             — set promoted_at, promoted_path, reviewed_by
    candidate  → rejected  : human declined
    promoted   → superseded: a later version replaces this one

The promoter ONLY writes to:
    - the learned_mappings DB row (status + metadata)
    - storage/mappings/learned/<signal_type>.yaml  (deployment-local,
      gitignored)

It does NOT:
    - touch libraries/ (GDU packs stay sealed)
    - edit hand-written mappings under core/extraction/workers/mappings/
    - modify the active_set
    - restart services — the registry picks up promoted files on next
      process boot via workers/__init__._register_declarative_mappings()

Idempotency: promoting the same candidate twice is a no-op (file
already exists and row already at status=promoted). Rejecting a
previously-promoted candidate deletes the YAML and flips status back.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from core.ldu.lineage import get_lineage_stamp

log = logging.getLogger("enki.extraction.learning.promoter")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LEARNED_DIR = PROJECT_ROOT / "storage" / "mappings" / "learned"

# Auto-promotion policy (off by default). A deployment can set
# AUTO_PROMOTE_MIN_SCORE higher to enable hands-off learning.
AUTO_PROMOTE_MIN_SCORE = 0.85
AUTO_PROMOTE_MIN_SAMPLES = 50


def _safe_filename(signal_type: str) -> str:
    """Turn 'signal.environmental.seismic' into 'signal_environmental_seismic'
    for a filesystem-safe YAML filename."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", signal_type).strip("_")


def _load_candidate(conn, signal_type: str) -> Optional[dict]:
    """Load the current candidate row for a signal_type. Returns None
    if no candidate exists."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mapping_id, mapping_version, status, sample_count,
                   proposed_map, last_score, shape_profile,
                   admitted_under_active_set, admitted_under_pack_versions
              FROM learned_mappings
             WHERE signal_type = %s
               AND status IN ('candidate', 'observing')
             ORDER BY mapping_version DESC
             LIMIT 1
            """,
            (signal_type,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "mapping_id": row[0],
        "mapping_version": row[1],
        "status": row[2],
        "sample_count": row[3],
        "proposed_map": row[4],
        "last_score": row[5],
        "shape_profile": row[6],
        "admitted_under_active_set": row[7],
        "admitted_under_pack_versions": row[8],
    }


def _build_yaml_doc(candidate: dict, *, reviewer: str) -> dict:
    """Turn the stored proposed_map into a complete mapping YAML document
    with governance block and provenance metadata."""
    proposal = dict(candidate["proposed_map"] or {})
    # Drop the diagnostic unmapped-paths field — it's not part of
    # the execution schema.
    unmapped = proposal.pop("_unmapped_paths", [])

    stamp = get_lineage_stamp()

    proposal["governance"] = {
        "author": "learned",
        "reviewed": True,
        "reviewed_by": reviewer,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "version": int(candidate["mapping_version"] or 1),
        "learned_from_samples": candidate["sample_count"],
        "learner_score": float(candidate["last_score"] or 0.0),
        "admitted_under_active_set": stamp.get("active_set_version"),
        "admitted_under_pack_versions": stamp.get("pack_versions") or {},
        "promoted_from_active_set": candidate.get("admitted_under_active_set"),
        "unmapped_payload_paths": unmapped,
    }
    return proposal


def promote(
    conn,
    signal_type: str,
    *,
    reviewer: str = "auto",
    force: bool = False,
) -> dict:
    """Promote a candidate mapping to a live learned YAML file.

    Args:
        conn:        psycopg2 connection
        signal_type: the signal type whose candidate to promote
        reviewer:    name/id of approving reviewer ('auto' for policy-driven)
        force:       allow promotion even if status is 'observing' (skips
                     the candidate gate — use sparingly)

    Returns:
        {"status": "promoted"|"skipped"|"not_found", "path": <str or None>,
         "score": <float or None>, "reason": <str or None>}
    """
    if conn is None:
        return {"status": "error", "reason": "no db connection"}

    cand = _load_candidate(conn, signal_type)
    if cand is None:
        return {"status": "not_found", "reason": f"no candidate for {signal_type}"}

    if cand["status"] == "observing" and not force:
        return {
            "status": "skipped",
            "reason": "candidate has not been scored yet — run --learn first, or pass force=True",
        }

    if not cand["proposed_map"]:
        return {
            "status": "skipped",
            "reason": "candidate has no proposed_map (learner did not run)",
        }

    # Build the YAML doc
    doc = _build_yaml_doc(cand, reviewer=reviewer)

    # Write to file
    LEARNED_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{_safe_filename(signal_type)}.yaml"
    path = LEARNED_DIR / fname

    header = (
        f"# Auto-promoted mapping for {signal_type}\n"
        f"# Promoted by: {reviewer} at {datetime.now(timezone.utc).isoformat()}\n"
        f"# Learner score: {cand['last_score']:.3f} over {cand['sample_count']} samples\n"
        f"# LDU lineage: active_set={get_lineage_stamp().get('active_set_version')}\n"
        f"#\n"
        f"# This file is auto-generated by core.extraction.learning.promoter.\n"
        f"# Hand-edit if needed, but note that future re-promotions will\n"
        f"# overwrite it unless you move this file under\n"
        f"# core/extraction/workers/mappings/ (shipped, hand-written).\n\n"
    )
    with open(path, "w") as f:
        f.write(header)
        yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)

    # Update DB row
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE learned_mappings
                   SET status        = 'promoted',
                       promoted_at   = NOW(),
                       promoted_path = %s,
                       reviewed_by   = %s,
                       updated_at    = NOW()
                 WHERE mapping_id    = %s
                """,
                (str(path), reviewer, cand["mapping_id"]),
            )
        conn.commit()
    except Exception as e:
        log.error("failed to update learned_mappings row: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"status": "error", "reason": str(e)}

    log.info(
        "promoted %s → %s (score=%.3f, samples=%d, reviewer=%s)",
        signal_type, path, cand["last_score"] or 0.0,
        cand["sample_count"], reviewer,
    )
    return {
        "status": "promoted",
        "path": str(path),
        "score": float(cand["last_score"] or 0.0),
        "samples": cand["sample_count"],
    }


def reject(conn, signal_type: str, *, reviewer: str = "auto", reason: str = "") -> dict:
    """Mark a candidate as rejected. If a promoted file exists, delete it."""
    if conn is None:
        return {"status": "error", "reason": "no db connection"}

    cand = _load_candidate(conn, signal_type)
    # Also check for already-promoted rows
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mapping_id, promoted_path
              FROM learned_mappings
             WHERE signal_type = %s
             ORDER BY mapping_version DESC
             LIMIT 1
            """,
            (signal_type,),
        )
        row = cur.fetchone()

    if row is None:
        return {"status": "not_found"}

    mapping_id, promoted_path = row

    # Delete YAML if it exists
    if promoted_path:
        try:
            p = Path(promoted_path)
            if p.exists():
                p.unlink()
                log.info("removed promoted file %s", p)
        except Exception as e:
            log.warning("failed to remove %s: %s", promoted_path, e)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE learned_mappings
                   SET status = 'rejected',
                       reviewed_by = %s,
                       promoted_path = NULL,
                       updated_at = NOW()
                 WHERE mapping_id = %s
                """,
                (reviewer, mapping_id),
            )
        conn.commit()
    except Exception as e:
        log.error("reject update failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"status": "error", "reason": str(e)}

    return {"status": "rejected", "signal_type": signal_type}


def list_candidates(conn) -> list[dict]:
    """Return every row at status='candidate'."""
    if conn is None:
        return []
    out: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT signal_type, mapping_version, sample_count, last_score,
                   first_observed_at, last_observed_at,
                   admitted_under_active_set
              FROM learned_mappings
             WHERE status = 'candidate'
             ORDER BY last_score DESC NULLS LAST, sample_count DESC
            """,
        )
        for row in cur.fetchall():
            out.append({
                "signal_type": row[0],
                "version": row[1],
                "samples": row[2],
                "score": row[3],
                "first_seen": row[4].isoformat() if row[4] else None,
                "last_seen": row[5].isoformat() if row[5] else None,
                "admitted_under": row[6],
            })
    return out


def auto_promote_eligible(conn, *, dry_run: bool = True) -> list[dict]:
    """Return candidates that meet the AUTO_PROMOTE thresholds. If
    dry_run=False, actually promote them."""
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT signal_type, sample_count, last_score
              FROM learned_mappings
             WHERE status = 'candidate'
               AND sample_count >= %s
               AND last_score   >= %s
            """,
            (AUTO_PROMOTE_MIN_SAMPLES, AUTO_PROMOTE_MIN_SCORE),
        )
        rows = cur.fetchall()

    results = []
    for signal_type, samples, score in rows:
        entry = {"signal_type": signal_type, "samples": samples, "score": score}
        if not dry_run:
            result = promote(conn, signal_type, reviewer="auto-policy")
            entry["result"] = result
        results.append(entry)
    return results
