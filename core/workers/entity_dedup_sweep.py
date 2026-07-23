"""
Entity Dedup Sweep — auto-merge entity duplicates with matching identity.

Phase 3.4 of the internal pipeline restart plan.

The boundary failure this fixes:
    `scripts/merge_entity_forks.py` had the merge logic but only ran on
    demand. Two "Alex Acme" rows, "Jane Doe" rows, etc.
    accumulated in production unmerged.

Dedup rule:
    same normalized_name AND same canonical_type — conservative. Won't
    merge "Doe" (place, NM) with "Doe" (person). Won't merge
    cross-type collisions that share a surface form.

This worker:
    - Polls every CHECK_INTERVAL seconds (default 10 min)
    - Finds groups of entities matching the rule
    - Merges each group's duplicates into the highest-signal-count
      survivor using the same merge_one logic as the one-shot script
    - Dry-run mode (ENKI_DEDUP_DRY_RUN=1) reports without writing —
      enabled by default for first 24h after deploy

Tier 1 — always runs (no LLM dependency).
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.entity_dedup_sweep")

CHECK_INTERVAL = 600   # 10 min between sweeps
BATCH_SIZE = 50        # process this many fork groups per tick

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()

_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "tick_count": 0,
    "groups_found_total": 0,
    "merges_committed": 0,
    "merges_dry_run": 0,
    "errors": 0,
    "last_error": None,
    "dry_run": False,
}


def _dry_run_enabled() -> bool:
    return os.environ.get("ENKI_DEDUP_DRY_RUN", "0") == "1"


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default") -> dict:
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _STATUS["dry_run"] = _dry_run_enabled()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="entity-dedup-sweep", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Entity dedup sweep started (dry_run=%s)", _STATUS["dry_run"])
    return {"status": "started", "dry_run": _STATUS["dry_run"]}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=5)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _find_fork_groups(conn, tenant_id: str, limit: int):
    """Return [(tenant_id, normalized_name, canonical_type, [entity_ids canon-first])]."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tenant_id, normalized_name, canonical_type,
                   array_agg(entity_id ORDER BY signal_count DESC NULLS LAST,
                                                entity_id ASC) AS ids
              FROM entities
             WHERE tenant_id = %s
               AND gdu_canonical = TRUE
               AND normalized_name IS NOT NULL AND normalized_name <> ''
               AND canonical_type  IS NOT NULL AND canonical_type  <> ''
               AND NOT (envelope ? 'merged_into')
               AND NOT (envelope ? 'reclassification')
             GROUP BY tenant_id, normalized_name, canonical_type
            HAVING COUNT(*) > 1
             ORDER BY COUNT(*) DESC
             LIMIT %s
            """,
            (tenant_id, limit),
        )
        return [
            (r[0], r[1], r[2], list(r[3])) for r in cur.fetchall()
        ]


def _find_alias_forks(conn, tenant_id: str, limit: int):
    """Alias-backed fragments: an LLM-discovered entity (source_kind IS NULL)
    whose NORMALIZED name equals a normalized alias of exactly ONE curated
    reference entity (source_kind='wikipedia_ref') of the same type.

    This catches the fragments same-normalized-name dedup can't — "A.
    Reed" vs "Alan Reed", "Al Acme" vs "Alex
    Acme", "Whitfield" vs "Henry Whitfield" — because the identity
    library's own alias list is the authority on who they are. Normalization
    (lower, strip punctuation, collapse spaces) makes it robust to OCR/punct
    variants ("Alex E Acme" == "Alex E. Acme"). The exactly-one-ref
    guard drops genuinely ambiguous surnames ("David Jones" -> two people),
    which must fall to context/coref, not a blind merge.

    Returns the same shape as _find_fork_groups, canon-first: the reference
    entity is always the survivor, the LLM fragment the duplicate.
    """
    norm = (r"trim(regexp_replace(regexp_replace(lower({c}),"
            r"'[^a-z0-9 ]',' ','g'),'\s+',' ','g'))")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH ref_alias AS (
                SELECT ref.entity_id AS ref_id, ref.canonical_type AS ty,
                       {norm.format(c='a')} AS na
                  FROM entities ref, unnest(ref.aliases) AS a
                 WHERE ref.source_kind = 'wikipedia_ref'
                   AND ref.tenant_id = %s
                   AND NOT (ref.envelope ? 'merged_into')
            ),
            llm AS (
                -- gdu_canonical is NOT required here: a fragment can be flagged
                -- non-canonical yet still own document_mentions that split the
                -- dossier surface, so it's exactly what we want to fold in.
                SELECT entity_id AS llm_id, canonical_type AS ty,
                       {norm.format(c='canonical_name')} AS nn
                  FROM entities
                 WHERE source_kind IS NULL
                   AND tenant_id = %s
                   AND canonical_name IS NOT NULL
                   AND NOT (envelope ? 'merged_into')
            ),
            matched AS (
                SELECT llm.llm_id, llm.nn, llm.ty,
                       count(DISTINCT ra.ref_id) AS nref,
                       min(ra.ref_id) AS ref_id
                  FROM llm
                  JOIN ref_alias ra ON ra.na = llm.nn AND ra.ty = llm.ty
                 WHERE length(llm.nn) >= 5
                   AND ra.ref_id <> llm.llm_id
                 GROUP BY llm.llm_id, llm.nn, llm.ty
            )
            SELECT nn, ty, ref_id, llm_id
              FROM matched
             WHERE nref = 1
             LIMIT %s
            """,
            (tenant_id, tenant_id, limit),
        )
        # canon (ref) first, dup (llm) second — same tuple shape as forks.
        return [
            (tenant_id, r[0], r[1], [r[2], r[3]]) for r in cur.fetchall()
        ]


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _name_parts(norm: str):
    """(first, last, {middle-initials}, ntokens) from a normalized name, or None."""
    toks = [t for t in (norm or "").split() if t]
    if len(toks) < 2:
        return None
    mids = toks[1:-1]
    mid_initials = {m[0] for m in mids if len(m) <= 2}
    return toks[0], toks[-1], mid_initials, len(toks)


def _fuzzy_name_ok(garbled_norm: str, canonical_norm: str, garbled_raw: str = "") -> bool:
    """VALIDATE a proposed fuzzy name correction. Precision-first — accept only
    an OCR/spelling garble of the SAME person ("Jayne Doe"->"Jane
    Doe", "JON DURAN"->"John Duran", "Alex C Acme"->"Alex
    Acme"). Everything uncertain ABSTAINS (a missed correction beats a wrong
    merge). Similarity proposes; this gates. Rules:
      - EXACT surname. Near-miss surnames are usually different people, not OCR
        (Johnson/Johnston, Green/Greene, Reagan/Regan, Powers/Power).
      - reject comma / surname-first forms ("Roberts, Bradley" is Bradley
        Roberts, not Robert Bradley) — let exact/alias dedup handle those.
      - first name: exact, an initial of the other, or high char-similarity
        (rejects Joe vs Joel).
      - no conflicting middle initial (John M. vs John B. Smith).
      - never fold a generic name into a more-specific one: any distinctive
        middle NAME on the reference must also be on the garbled side
        ("Robert Smith" !-> "Robert Sloan Smith").

    Returns 'exact' (surname matches exactly — safe to merge on name alone),
    'context' (surname is an OCR near-miss but first name + structure match —
    merge ONLY with document co-occurrence corroboration), or None (reject)."""
    if "," in (garbled_raw or ""):
        return None
    g = _name_parts(garbled_norm)
    r = _name_parts(canonical_norm)
    if not g or not r:
        return None
    gf, gl, gmi, _ = g
    rf, rl, rmi, _ = r
    first_ok = (gf == rf
                or (len(gf) <= 2 and rf.startswith(gf[0]))
                or (len(rf) <= 2 and gf.startswith(rf[0]))
                or _ratio(gf, rf) >= 0.88)
    if not first_ok:
        return None
    if gmi and rmi and gmi.isdisjoint(rmi):       # conflicting middle initials
        return None
    g_mid_full = {t for t in garbled_norm.split()[1:-1] if len(t) > 2}
    r_mid_full = {t for t in canonical_norm.split()[1:-1] if len(t) > 2}
    if r_mid_full - g_mid_full:                    # ref has a distinctive middle name garbled lacks
        return None
    if gl == rl:
        return "exact"
    if _ratio(gl, rl) >= 0.80:                      # surname OCR near-miss — needs context
        return "context"
    return None


def _find_fuzzy_forks(conn, tenant_id: str, limit: int):
    """Fuzzy OCR/spelling correction against the identity library, GOVERNED via
    merge_entities like the other forks. Propose candidates by trigram
    similarity (index-accelerated), then VALIDATE each with name-component
    analysis; a garbled LLM entity (source_kind NULL) merges into a reference
    entity only when EXACTLY ONE reference validates — ambiguous matches
    ("David Jones" -> two people) abstain. Reference (wikipedia_ref) is always
    the survivor. Same canon-first tuple shape as the other finders."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_limit(0.60)")
        cur.execute(
            """
            SELECT llm.entity_id, llm.normalized_name, llm.canonical_name,
                   ref.entity_id, ref.normalized_name
              FROM entities llm
              JOIN entities ref
                ON ref.source_kind = 'wikipedia_ref'
               AND ref.canonical_type = llm.canonical_type
               AND ref.entity_id <> llm.entity_id
               AND llm.normalized_name %% ref.normalized_name
             WHERE llm.source_kind IS NULL
               AND llm.canonical_type IN ('person','organization')
               AND llm.tenant_id = %s
               AND NOT (llm.envelope ? 'reclassified_to')
               AND NOT (llm.envelope ? 'merged_into')
               AND length(llm.normalized_name) >= 8
               AND llm.normalized_name <> ref.normalized_name
               AND similarity(llm.normalized_name, ref.normalized_name) >= 0.60
             LIMIT 4000
            """,
            (tenant_id,),
        )
        by_llm: dict = {}
        for lid, lnorm, lraw, rid, rnorm in cur.fetchall():
            tier = _fuzzy_name_ok(lnorm, rnorm, lraw)
            if tier:
                by_llm.setdefault((lid, lnorm), {})[rid] = tier

        out = []
        for (lid, lnorm), matches in by_llm.items():
            if len(matches) != 1:               # ambiguous — abstain
                continue
            rid, tier = next(iter(matches.items()))
            if tier == "context":
                # surname is an OCR near-miss — require the pair to co-occur in
                # at least one document (the garbled variant appears alongside
                # the clean form = same entity, OCR-inconsistent). Different
                # people with look-alike surnames don't co-occur, so this cleanly
                # separates "Ann Car.er"/"Lee Park" (real, co-occur)
                # from "Pat Shelton"/"Pat Welton" (distinct, don't).
                cur.execute(
                    """SELECT COUNT(DISTINCT dm1.document_id)
                         FROM document_mentions dm1
                         JOIN document_mentions dm2
                           ON dm2.document_id = dm1.document_id AND dm2.entity_id = %s
                        WHERE dm1.entity_id = %s""",
                    (rid, lid),
                )
                if (cur.fetchone()[0] or 0) < 1:
                    continue
            out.append((tenant_id, lnorm, tier, [rid, lid]))
            if len(out) >= limit:
                break
    return out


def _merge_one(conn, tenant_id: str, canon_id: int, dup_id: int) -> dict:
    """Merge dup_id INTO canon_id within a tenant. Caller commits.

    Identical to scripts/merge_entity_forks.py:merge_one — duplicated
    here so the worker has no dependency on the scripts package layout
    (some deployments don't include scripts/ on PYTHONPATH).
    """
    canon_node = f"entity:{canon_id}"
    dup_node = f"entity:{dup_id}"
    stats = {
        "redirected_edges_src": 0, "redirected_edges_tgt": 0,
        "deleted_redundant_src": 0, "deleted_redundant_tgt": 0,
        "knowledge_node_master_updates": 0,
        "entity_relationship_updates": 0,
    }

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO entity_redirects (old_entity_id, new_entity_id,
                                          merged_by, merge_reason)
            VALUES (%s, %s, 'entity_dedup_sweep', 'normalized_name_type_collision')
            ON CONFLICT (old_entity_id) DO NOTHING
            """,
            (dup_id, canon_id),
        )

        # Delete redundant outbound edges
        cur.execute(
            """
            DELETE FROM knowledge_edges old
             WHERE old.source_node_id = %s
               AND EXISTS (
                 SELECT 1 FROM knowledge_edges new
                  WHERE new.source_node_id = %s
                    AND new.target_node_id = old.target_node_id
                    AND new.relationship   = old.relationship
               )
            """,
            (dup_node, canon_node),
        )
        stats["deleted_redundant_src"] = cur.rowcount or 0

        cur.execute(
            "UPDATE knowledge_edges SET source_node_id = %s WHERE source_node_id = %s",
            (canon_node, dup_node),
        )
        stats["redirected_edges_src"] = cur.rowcount or 0

        # Same for inbound
        cur.execute(
            """
            DELETE FROM knowledge_edges old
             WHERE old.target_node_id = %s
               AND EXISTS (
                 SELECT 1 FROM knowledge_edges new
                  WHERE new.target_node_id = %s
                    AND new.source_node_id = old.source_node_id
                    AND new.relationship   = old.relationship
               )
            """,
            (dup_node, canon_node),
        )
        stats["deleted_redundant_tgt"] = cur.rowcount or 0

        cur.execute(
            "UPDATE knowledge_edges SET target_node_id = %s WHERE target_node_id = %s",
            (canon_node, dup_node),
        )
        stats["redirected_edges_tgt"] = cur.rowcount or 0

        # knowledge_nodes.master_entity_id
        cur.execute(
            "UPDATE knowledge_nodes SET master_entity_id = %s WHERE master_entity_id = %s",
            (canon_id, dup_id),
        )
        stats["knowledge_node_master_updates"] = cur.rowcount or 0

        # entity_relationships — point source/target at canon, dedupe triples
        cur.execute(
            """
            DELETE FROM entity_relationships old
             WHERE (old.source_entity_id = %s OR old.target_entity_id = %s)
               AND EXISTS (
                 SELECT 1 FROM entity_relationships new
                  WHERE new.source_entity_id = CASE WHEN old.source_entity_id=%s THEN %s ELSE old.source_entity_id END
                    AND new.target_entity_id = CASE WHEN old.target_entity_id=%s THEN %s ELSE old.target_entity_id END
                    AND new.relationship = old.relationship
                    AND new.rid <> old.rid
               )
            """,
            (dup_id, dup_id, dup_id, canon_id, dup_id, canon_id),
        )
        cur.execute(
            "UPDATE entity_relationships SET source_entity_id = %s WHERE source_entity_id = %s",
            (canon_id, dup_id),
        )
        cur.execute(
            "UPDATE entity_relationships SET target_entity_id = %s WHERE target_entity_id = %s",
            (canon_id, dup_id),
        )
        stats["entity_relationship_updates"] = (cur.rowcount or 0)

        # Fold dup signal_count and aliases into canon, then retire dup
        cur.execute("SELECT signal_count, aliases FROM entities WHERE entity_id = %s", (dup_id,))
        row = cur.fetchone()
        dup_signal_count = int(row[0] or 0) if row else 0
        dup_aliases = list(row[1] or []) if row else []

        cur.execute(
            """
            UPDATE entities
               SET signal_count = COALESCE(signal_count, 0) + %s,
                   aliases = (
                     SELECT ARRAY(SELECT DISTINCT unnest(
                       COALESCE(aliases, ARRAY[]::text[]) || %s::text[]
                     ))
                   ),
                   updated_at = NOW()
             WHERE entity_id = %s
            """,
            (dup_signal_count, dup_aliases, canon_id),
        )

        cur.execute(
            """
            UPDATE entities
               SET gdu_canonical = FALSE,
                   envelope = COALESCE(envelope, '{}'::jsonb)
                              || jsonb_build_object(
                                   'merged_into', %s,
                                   'merged_at',   NOW()::text,
                                   'merged_by',   'entity_dedup_sweep'
                                 ),
                   updated_at = NOW()
             WHERE entity_id = %s
            """,
            (canon_id, dup_id),
        )

    return stats


def _worker_loop(tenant_id: str) -> None:
    backoff = CHECK_INTERVAL
    while not _STOP_EVENT.is_set():
        try:
            _STATUS["phase"] = "sweeping"
            _STATUS["dry_run"] = _dry_run_enabled()
            processed = _sweep_batch(tenant_id, BATCH_SIZE)
            _STATUS["tick_count"] += 1
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["last_error"] = None
            if processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = f"swept {processed} group(s)"
            else:
                # No forks found — back off but cap.
                backoff = min(backoff + 60, CHECK_INTERVAL * 3)
                _STATUS["phase"] = "idle — no duplicates"
        except Exception as e:
            log.warning("Entity dedup sweep tick failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["last_error"] = str(e)[:300]
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, 1800)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False


def _sweep_batch(tenant_id: str, batch: int) -> int:
    """Find + merge one batch of duplicate groups. Returns merges performed."""
    dry = _dry_run_enabled()
    # Collapse-aware merge: repoints document_mentions/statements/assessments/
    # library_members (the layers the dossier reads), not just the knowledge
    # graph. The legacy _merge_one only did the graph layer, so its merges left
    # the collapse surfaces split — this is the fix for that too.
    from core.gdu.entity_merge import merge_entities

    merges = 0
    with get_connection() as conn:
        # Two fork classes: exact same-normalized-name+type, and alias-backed
        # (an LLM fragment that is a documented alias of one reference entity).
        forks = [(g, "dedup_same_name") for g in _find_fork_groups(conn, tenant_id, batch)]
        forks += [(g, "alias_backed_fork") for g in _find_alias_forks(conn, tenant_id, batch)]
        forks += [(g, "fuzzy_ocr") for g in _find_fuzzy_forks(conn, tenant_id, batch)]
    _STATUS["groups_found_total"] += len(forks)
    if not forks:
        return 0
    for (_tid, normalized, ctype, ids), reason in forks:
        canon_id, *dup_ids = ids
        for dup_id in dup_ids:
            try:
                if dry:
                    log.info(
                        "[dry-run] would merge entity_id=%s into %s (name=%r type=%s reason=%s)",
                        dup_id, canon_id, normalized, ctype, reason,
                    )
                    _STATUS["merges_dry_run"] += 1
                else:
                    # merge_entities opens its own connection and commits
                    # atomically per merge, so one bad pair can't poison others.
                    res = merge_entities(canon_id, dup_id, reason=reason)
                    if not res.get("ok"):
                        raise RuntimeError(res.get("error") or "merge failed")
                    log.info(
                        "merged entity_id=%s into %s (name=%r type=%s reason=%s) %s",
                        dup_id, canon_id, normalized, ctype, reason,
                        json.dumps(res.get("changed", {})),
                    )
                    _STATUS["merges_committed"] += 1
                merges += 1
            except Exception as e:
                log.warning(
                    "merge failed dup=%s canon=%s name=%r: %s",
                    dup_id, canon_id, normalized, e,
                )
                _STATUS["errors"] += 1
                _STATUS["last_error"] = str(e)[:300]
                continue
    return merges
