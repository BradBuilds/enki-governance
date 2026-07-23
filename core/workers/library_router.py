"""library_router.py — routes RSS signals into libraries.

For every new RSS signal:
  1. KEYWORD match: title + summary + first_paragraph contains library
     focal_term or canonical_name (case-insensitive, word-boundary aware
     when feasible).
  2. ENTITY match: signal has an entity_mention on an entity that already
     appears in the library's documents (entity_documents OR entity_mentions
     joined through document_mentions). If yes, the signal corroborates the
     library by mentioning a known anchor.

Match outputs go to ``library_signals`` (one row per (library, signal)). The
table is what /feed cards and /library/[id] read from for "live news"
attribution.

Backfill mode: scan all signals.
Continuous mode: scan signals where rid > watermark.

Run modes:
  python -m core.workers.library_router               # one pass over all
  python -m core.workers.library_router --since 1h    # last hour only
  python -m core.workers.library_router --library 1   # specific library
  python -m core.workers.library_router --continuous  # loop forever (60s)
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.library_router")

# Continuous-loop scaffolding (matches auto_validator pattern)
_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "matches_total": 0,
    "errors": 0,
}
_CHECK_INTERVAL = 60  # seconds


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_continuous_loop, args=(tenant_id,),
        name="library-router", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _continuous_loop(tenant_id: str):
    """Window-based scan every _CHECK_INTERVAL seconds. Looks 15 min back
    so newly-fetched signals get routed even if the entity-extraction
    upstream is still catching up."""
    while not _STOP_EVENT.is_set():
        try:
            since = datetime.now(timezone.utc) - timedelta(minutes=15)
            stats = run_once(since=since, tenant_id=tenant_id)
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["matches_total"] += stats.get("matches_inserted", 0)
            _STATUS["phase"] = f"idle — {stats.get('matches_inserted', 0)} matches last pass"
        except Exception as e:
            log.warning("library_router cycle failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
        for _ in range(_CHECK_INTERVAL):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


# ─── Library load ──────────────────────────────────────────────────────────

def load_libraries(conn, tenant_id: str = "default") -> list[dict]:
    """Pull active libraries + their anchor entity_ids."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT library_id, canonical_name, focal_term, base60_address,
                   library_subtype, tenant_id
              FROM libraries
             WHERE tenant_id = %s
               AND discovery_status = 'active'
            """,
            (tenant_id,),
        )
        libs = []
        for r in cur.fetchall():
            libs.append({
                "library_id": r[0],
                "canonical_name": r[1],
                "focal_term": r[2],
                "base60_address": r[3],
                "library_subtype": r[4],
                "tenant_id": r[5],
            })

    # Pull anchor entities for each: any entity that appears in a document
    # already linked to this library (via bibliographic_references → documents
    # → document_mentions).
    for lib in libs:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT dm.entity_id
                  FROM bibliographic_references br
                  JOIN documents d ON d.document_id = br.document_id
                  JOIN document_mentions dm ON dm.document_id = d.document_id
                 WHERE br.library_id = %s
                   AND dm.entity_id IS NOT NULL
                """,
                (lib["library_id"],),
            )
            lib["anchor_entity_ids"] = {r[0] for r in cur.fetchall()}
        # Cache keyword tokens
        lib["keywords"] = _build_keywords(lib)
    return libs


def _build_keywords(lib: dict) -> list[tuple[str, str]]:
    """Return [(keyword, kind), ...] tokens to match against signal text.

    kind: 'focal' = full multi-word phrase from focal_term or canonical_name.

    Single tokens are intentionally excluded — "blue" from "Project Blue Book"
    or "great" from "Great Pyramid of Giza" matches too much noise. Only the
    full multi-word phrase (or single-word distinctive name like "Roswell",
    "Acme") qualifies.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(s: str, kind: str):
        s = (s or "").strip()
        if not s or len(s) < 4:
            return
        norm = s.lower()
        if norm in seen:
            return
        seen.add(norm)
        out.append((s, kind))

    # Generic single words that even as a "focal term" would be too noisy.
    SINGLE_WORD_STOP = {
        "news", "article", "incident", "project", "book", "system",
        "great", "blue", "red", "white", "the",
    }

    if lib["focal_term"]:
        ft = lib["focal_term"]
        # Single-word focal: accept only if it's distinctive (not a stopword).
        if " " in ft.strip():
            add(ft, "focal")
        elif ft.strip().lower() not in SINGLE_WORD_STOP and len(ft) >= 5:
            add(ft, "focal")

    if lib["canonical_name"]:
        cn = lib["canonical_name"]
        if "/" in cn:
            cn = cn.split("/", 1)[1]
        if " " in cn.strip():
            add(cn, "focal")
        elif cn.strip().lower() not in SINGLE_WORD_STOP and len(cn) >= 5:
            add(cn, "focal")
    return out


# Minimum entity-only overlaps required to count as a match. A single shared
# entity (e.g. both library and article mention "Vale") is too weak.
MIN_ENTITY_OVERLAP = 3


# ─── Matching ──────────────────────────────────────────────────────────────

def signal_matches(
    title: str, summary: str, first_paragraph: str,
    entity_ids: set[int],
    lib: dict,
) -> tuple[bool, list[str], list[str], list[int]]:
    """Return (matched, match_kinds, keyword_hits, entity_id_hits).

    Match rules (May 16 2026 tightening — entity-overlap alone is too noisy
    because common entities like 'Vale' or 'United States' appear in nearly
    every article):

      - keyword hit (word-boundary, multi-word phrase preferred): always counts
      - entity overlap: counts only if >= MIN_ENTITY_OVERLAP shared entities,
        OR if there's also a keyword hit
    """
    haystack_parts = [
        (title or "").lower(),
        (summary or "").lower(),
        (first_paragraph or "").lower(),
    ]
    haystack = " ".join(haystack_parts)

    keyword_hits: list[str] = []
    for keyword, kind in lib["keywords"]:
        kw_lower = keyword.lower()
        if re.search(rf"\b{re.escape(kw_lower)}\b", haystack):
            keyword_hits.append(keyword)

    entity_overlap = entity_ids & lib["anchor_entity_ids"]
    entity_hits = list(entity_overlap)

    # Entity-only matching is currently DISABLED. Anchor entity sets are too
    # broad — every news story shares Vale/US/Iran with every library —
    # so entity-overlap-alone produces false positives. We require a keyword
    # hit; entity overlap then acts as a confidence boost ('both' kind).
    # Future work: tf-idf weight anchor entities by how library-specific
    # they are, then re-enable entity-only routing.
    if keyword_hits and entity_hits:
        return True, ["both"], keyword_hits, entity_hits
    if keyword_hits:
        return True, ["keyword"], keyword_hits, []
    return False, [], [], []


def score_match(keyword_hits: list[str], entity_hits: list[int],
                libs: dict) -> float:
    """Map hit-counts to 0..1 confidence."""
    score = 0.0
    # Focal term hit = 0.6
    # Entity overlap = 0.5
    # Both = 1.0 (capped)
    if keyword_hits:
        score += 0.6
    if entity_hits:
        score += 0.5
    return min(score, 1.0)


# ─── Worker entry ──────────────────────────────────────────────────────────

def run_once(
    since: Optional[datetime] = None,
    library_filter: Optional[int] = None,
    tenant_id: str = "default",
) -> dict:
    """Single scan over signals + emit library_signals rows."""
    stats = {"signals_scanned": 0, "matches_inserted": 0, "libraries": 0}

    with get_connection() as conn:
        libs = load_libraries(conn, tenant_id)
        if library_filter is not None:
            libs = [l for l in libs if l["library_id"] == library_filter]
        if not libs:
            log.info("no libraries to match")
            return stats
        stats["libraries"] = len(libs)
        log.info(
            "scanning signals against %d libraries: %s",
            len(libs), [(l["library_id"], l["focal_term"]) for l in libs],
        )

        # Watermark: per (tenant, library), how far did we get?
        # For "since=" backfill, ignore watermark and replay window.
        wm_clause = ""
        wm_params: tuple = ()
        if since:
            wm_clause = "AND s.timestamp >= %s"
            wm_params = (since,)

        # Pull signals + their entity_mention entity_ids in one shot, grouped.
        # Cap to RSS for now — only news signals route into libraries.
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.rid,
                       s.payload->>'title' AS title,
                       s.payload->>'summary' AS summary,
                       s.payload->>'first_paragraph' AS first_paragraph,
                       COALESCE(
                         ARRAY_AGG(DISTINCT em.entity_id)
                           FILTER (WHERE em.entity_id IS NOT NULL),
                         '{{}}'::bigint[]
                       ) AS entity_ids
                  FROM signals s
                  LEFT JOIN entity_mentions em ON em.signal_rid = s.rid
                 WHERE s.tenant_id = %s
                   AND s.signal_type = 'signal.content.rss_entry'
                   {wm_clause}
                 GROUP BY s.rid, s.payload
                 ORDER BY s.rid
                """,
                (tenant_id, *wm_params),
            )
            signals = cur.fetchall()

        log.info("scanning %d signals", len(signals))

        with conn.cursor() as cur:
            for sig_rid, title, summary, first_para, entity_ids in signals:
                stats["signals_scanned"] += 1
                ent_set = set(entity_ids or [])
                for lib in libs:
                    matched, kinds, kw_hits, ent_hits = signal_matches(
                        title, summary, first_para, ent_set, lib,
                    )
                    if not matched:
                        continue
                    score = score_match(kw_hits, ent_hits, lib)
                    cur.execute(
                        """
                        INSERT INTO library_signals (
                            library_id, signal_rid, tenant_id,
                            match_kind, match_score,
                            keyword_hit, entity_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (library_id, signal_rid) DO UPDATE SET
                            match_kind = EXCLUDED.match_kind,
                            match_score = GREATEST(library_signals.match_score, EXCLUDED.match_score),
                            keyword_hit = COALESCE(EXCLUDED.keyword_hit, library_signals.keyword_hit),
                            entity_id = COALESCE(EXCLUDED.entity_id, library_signals.entity_id)
                        """,
                        (
                            lib["library_id"], sig_rid, tenant_id,
                            kinds[0], score,
                            kw_hits[0] if kw_hits else None,
                            ent_hits[0] if ent_hits else None,
                        ),
                    )
                    stats["matches_inserted"] += 1
            conn.commit()
    log.info("library_router done: %s", stats)
    return stats


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--since", default=None,
                   help="ISO datetime OR shorthand: 1h, 24h, 7d")
    p.add_argument("--library", type=int, default=None,
                   help="Only route to one library_id")
    p.add_argument("--tenant", default="default")
    p.add_argument("--continuous", action="store_true",
                   help="Loop every 60s")
    args = p.parse_args()

    since: Optional[datetime] = None
    if args.since:
        if args.since.endswith("h"):
            since = datetime.now(timezone.utc) - timedelta(hours=int(args.since[:-1]))
        elif args.since.endswith("d"):
            since = datetime.now(timezone.utc) - timedelta(days=int(args.since[:-1]))
        elif args.since.endswith("m"):
            since = datetime.now(timezone.utc) - timedelta(minutes=int(args.since[:-1]))
        else:
            since = datetime.fromisoformat(args.since)

    if args.continuous:
        log.info("continuous mode — polling every 60s")
        # In continuous mode, narrow to last 10 min each cycle for efficiency
        while True:
            try:
                run_once(
                    since=datetime.now(timezone.utc) - timedelta(minutes=10),
                    library_filter=args.library,
                    tenant_id=args.tenant,
                )
            except Exception as e:
                log.error("cycle failed: %s", e)
            time.sleep(60)
    else:
        run_once(since=since, library_filter=args.library, tenant_id=args.tenant)
    return 0


if __name__ == "__main__":
    sys.exit(main())
