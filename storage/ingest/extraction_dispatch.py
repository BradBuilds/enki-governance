"""
Post-ingestion extraction dispatch.

Ingestion stores a Signal row in the database (the WHAT). This module
runs the stored signal through the universal extraction pipeline (the
MEANING): entity resolution, reality check, completeness scoring,
signal_extractions persistence, watch-topic fanout, knowledge_nodes
upsert, edges.

This is the hook that was missing from the live path. Without it,
every signal ingested through the API or RSS poller stopped at the
`signals` table and only got processed if a periodic backfill script
touched it.

Non-blocking: failures here MUST NOT break ingestion. The raw signal
is already durably stored (LDU-T + signals row); if extraction fails
we log and move on. A re-extraction script can clean up later.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("enki.ingest.extract_dispatch")


def dispatch_extraction(
    *,
    signal_id: str,
    signal_rid: int,
    signal_type: str,
    payload: dict,
    metadata: dict,
    tenant_id: str,
    source_name: str = "",
    language: str = "en",
    dsn: Optional[str] = None,
) -> None:
    """Run the just-stored signal through the universal extraction
    pipeline. Called from ingest_signal and _apply_profile_fast_path.
    Synchronous for now — if latency becomes an issue, wrap this in a
    thread-pool dispatch or a queue. Each call is fully independent so
    parallelism is safe when we're ready for it."""
    try:
        from storage.postgres.db import get_connection
        from core.extraction.runner import run_extraction
        from core.extraction.auto_node import upsert_knowledge_node
        from core.extraction.edges import upsert_edges_from_envelope
    except Exception as e:
        log.warning("extraction dispatch import failed: %s", e)
        return

    runner_signal = {
        "type": signal_type,
        "signal_rid": signal_rid,
        "payload": payload or {},
        "source": {
            "slug": (source_name or "")[:60] or "ingest",
            "outlet": source_name or "",
            "language": language or "en",
        },
        "metadata": metadata or {},
    }

    node_id_for_enrich: Optional[str] = None
    try:
        # Stage 1: run the extraction pipeline (may internally commit/rollback
        # on sub-stages like events, media, resolvers). This is the LONG stage
        # that uses the LLM.
        with get_connection(dsn) as conn:
            envelope = run_extraction(
                runner_signal, conn=conn, tenant_id=tenant_id,
            )

        # Stage 2: persist the DOCUMENT node into knowledge_nodes. Uses a
        # FRESH connection because the runner's connection may be corrupted
        # by nested commit/rollback cycles in sub-stages (events.py's
        # per-event commit, media pipeline's process_image_url, etc.).
        try:
            with get_connection(dsn) as conn2:
                upsert_knowledge_node(
                    envelope, conn2, tenant_id=tenant_id,
                    project_id="default", signal_rid=signal_rid,
                )
                node_id_for_enrich = envelope.get("node_id") or None
        except Exception as e:
            log.warning("auto_node upsert failed for signal %s: %s",
                        signal_id, e)

        # Stage 3: write article→entity/place/topic/event edges. Also a
        # FRESH connection — independent of both the runner and auto_node.
        try:
            with get_connection(dsn) as conn3:
                upsert_edges_from_envelope(envelope, conn3)
        except Exception as e:
            log.warning("edges upsert failed for signal %s: %s",
                        signal_id, e)
    except Exception as e:
        log.warning("extraction dispatch failed for signal %s (%s): %s",
                    signal_id, signal_type, e)

    # ─── Enrichment (LDU pipeline) ──────────────────────────────────
    # After the knowledge_node row exists, schedule the enrichment
    # pipeline so it can reverse-geocode, run the contradiction /
    # corroboration scan, detect gaps, and auto-link to matching cases
    # + watch threads. Running in a background thread keeps ingestion
    # latency bounded while still giving every node full treatment.
    if node_id_for_enrich:
        _schedule_enrichment(node_id_for_enrich, tenant_id, dsn)


def _schedule_enrichment(node_id: str, tenant_id: str,
                         dsn: Optional[str] = None) -> None:
    """Fire off core.enrichment.node_enrich.enrich_node in a daemon
    thread. Never raises — enrichment failures don't block ingestion."""
    try:
        import threading
        from core.enrichment.node_enrich import enrich_node
    except Exception as e:
        log.debug("enrichment import failed: %s", e)
        return

    def _run():
        try:
            enrich_node(node_id, tenant_id, dsn=dsn)
        except Exception as e:
            log.debug("enrich_node threw for %s: %s", node_id, e)

    t = threading.Thread(target=_run, daemon=True,
                         name=f"enrich-{node_id[:24]}")
    t.start()
