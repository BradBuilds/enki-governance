"""Topic auto-clusterer — proposes topics from the substrate.

Three sources feed the proposer; same primitive (create_topic + membership
rules) lands all three:

  1. signal_clusters    — breaking-story news clusters with >=3 articles,
                          >=2 sources, >=2 entities. Each becomes a topic
                          whose membership_rule is a co_occurrence over
                          the cluster's signal_ids.

  2. libraries          — curated document collections with >=5 docs.
                          Each library becomes a topic whose membership
                          is co_occurrence over the library's documents.

  3. high-signal entities — entities with signal_count >= 100 and at
                            least 1 trusted-predicate edge. Each becomes
                            a 2-hop network topic via predicate_walk.

Idempotent — uses create_topic with a deterministic collapse_key per
proposal, so re-running the worker doesn't dup. Soft-cap on new topics
per tick to keep substrate growth bounded.

Status endpoint: GET /workers/topic-clusterer/status

Run modes:
  python -m core.workers.topic_auto_clusterer --tick    # one-shot
  FastAPI startup → continuous daemon thread (ticks every 5 min)
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.topic_auto_clusterer")

# Worker scaffolding (same pattern as the other workers)
_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "ticks": 0,
    "proposed_total": 0,
    "by_source": {"signal_cluster": 0, "library": 0, "entity_network": 0},
    "errors": 0,
    "last_error": None,
}

_TICK_INTERVAL_S = 300        # every 5 minutes
_MAX_NEW_PER_TICK = 10        # soft cap per tick
_IDLE_BACKOFF_S = 900         # back off when no candidates


def _pick_topic_subtype(label: str, keywords: list[str] | None = None) -> str:
    """Heuristic subtype selection from a topic's label + keywords.

    Maps natural-language signals to nodevocab.topic subtypes:
      conflict, operation, treaty, protocol, technology, ideology,
      market, supply_chain, program, doctrine, investigation.

    Default: investigation (catch-all for ad-hoc topics).
    """
    text = ((label or "") + " " + " ".join(keywords or [])).lower()

    # ARMED CONFLICT / ATTACKS
    if any(w in text for w in (
        "war", "strike", "attack", "bombing", "conflict", "missile",
        "ceasefire", "invasion", "battle", "combat", "shelling",
        "airstrike", "drone strike", "casualties", "killed", "wounded",
    )):
        return "conflict"

    # NAMED OPERATIONS / MISSIONS
    if any(w in text for w in (
        "operation ", "mission ", "program ", "project ", "task force",
        "raid", "exercise", "deployment",
    )):
        return "operation"

    # TREATIES / AGREEMENTS / DIPLOMACY
    if any(w in text for w in (
        "treaty", "accord", "agreement", "pact", "deal", "summit",
        "diplomatic", "convention", "protocol signed",
    )):
        return "treaty"

    # TECHNICAL PROTOCOLS / STANDARDS
    if any(w in text for w in (
        "protocol", "standard", "specification", "rfc ", "ieee ",
        "iso ", "encryption", "tls", "tcp", "udp",
    )):
        return "protocol"

    # TECHNOLOGY DOMAIN
    if any(w in text for w in (
        "llm", "language model", "ai ", "machine learning", "neural",
        "algorithm", "model", "transformer", "deep learning",
        "quantum", "blockchain", "crypto", "semiconductor", "chip",
        "gpu", "compute", "training", "inference", "alignment",
    )):
        return "technology"

    # IDEOLOGY / DOCTRINE
    if any(w in text for w in (
        "ideology", "doctrine", "communism", "marxism", "fascism",
        "liberalism", "conservatism", "populism", "nationalism",
        "religious", "belief system",
    )):
        return "ideology"

    # MARKET / SECTOR
    if any(w in text for w in (
        "market", "sector", "industry", "stock", "shares", "ipo",
        "earnings", "revenue", "investors", "trading", "commodity",
        "oil price", "gas price",
    )):
        return "market"

    # SUPPLY CHAIN
    if any(w in text for w in (
        "supply chain", "shipping", "logistics", "import", "export",
        "tariff", "shortage", "manufacturing line",
    )):
        return "supply_chain"

    # GOVERNMENT / INDUSTRY PROGRAM
    if any(w in text for w in (
        "act ", "bill ", "law passed", "federal program", "subsidy",
        "appropriation", "funding bill", "tax credit", "regulation",
        "supreme court", "ruling", "verdict",
    )):
        return "program"

    # MILITARY DOCTRINE
    if any(w in text for w in (
        "deterrence", "containment", "warfare doctrine", "nuclear posture",
        "force projection", "blockade",
    )):
        return "doctrine"

    # Default: investigation (the catch-all for ad-hoc, case-style topics)
    return "investigation"

# Filtering thresholds (conservative — high-signal candidates only)
SIGNAL_CLUSTER_MIN_ARTICLES = 3
SIGNAL_CLUSTER_MIN_SOURCES = 2
SIGNAL_CLUSTER_MIN_ENTITIES = 2
LIBRARY_MIN_DOCS = 5
ENTITY_MIN_SIGNAL_COUNT = 200
ENTITY_MAX_PROPOSALS = 5       # only propose for top-N high-signal entities per tick


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default") -> dict:
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,),
        name="topic-auto-clusterer", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Topic auto-clusterer started")
    return {"status": "started"}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _loop(tenant_id: str) -> None:
    backoff = _TICK_INTERVAL_S
    while not _STOP_EVENT.is_set():
        try:
            count = run_tick(tenant_id=tenant_id, max_new=_MAX_NEW_PER_TICK)
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["ticks"] += 1
            _STATUS["proposed_total"] += count
            _STATUS["phase"] = f"proposed {count} this tick"
            backoff = _IDLE_BACKOFF_S if count == 0 else _TICK_INTERVAL_S
        except Exception as e:
            log.warning("topic_auto_clusterer tick failed: %s", e, exc_info=False)
            _STATUS["errors"] += 1
            _STATUS["last_error"] = str(e)[:300]
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = _IDLE_BACKOFF_S

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False


# ─── Candidate sources ─────────────────────────────────────────────────────

def _existing_topic_collapse_keys() -> set[str]:
    """Read the collapse-key set so we can skip already-admitted topics
    cheaply. Pulled from knowledge_nodes via the gateway's collapse_key
    metadata (admitted_under_pack_versions does not carry it; we use the
    node_id deterministic-hash prefix produced by gateway.admit_node)."""
    keys = set()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT node_id FROM knowledge_nodes WHERE node_type='topic'"
            )
            for (nid,) in cur.fetchall():
                # gateway.admit_node generates node_id = topic:<hash12>
                # via sha1(collapse_key)[:12]. We use a different idempotency
                # check (rule-level signature) below since reconstructing the
                # collapse_key from node_id alone is one-way.
                keys.add(nid)
    return keys


def _propose_signal_cluster_topics(tenant_id: str, max_new: int) -> int:
    """Propose topics from breaking-story signal clusters."""
    from core.topics.rules import rule_set, co_occurrence
    from core.topics.materialize import create_topic

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cluster_id, topic_summary, article_count, source_count,
                       array_length(entity_ids, 1) AS n_ent,
                       title_keywords
                  FROM signal_clusters
                 WHERE article_count >= %s
                   AND source_count >= %s
                   AND array_length(entity_ids, 1) >= %s
                 ORDER BY article_count DESC, last_seen DESC
                 LIMIT %s
                """,
                (SIGNAL_CLUSTER_MIN_ARTICLES, SIGNAL_CLUSTER_MIN_SOURCES,
                 SIGNAL_CLUSTER_MIN_ENTITIES, max_new * 3),
            )
            rows = cur.fetchall()

    created = 0
    for cluster_id, summary, articles, sources, n_ent, keywords in rows:
        if created >= max_new:
            break
        # Get the cluster's signal_rids
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT signal_rid FROM signal_cluster_memberships "
                    " WHERE cluster_id=%s LIMIT 100",
                    (cluster_id,),
                )
                sig_ids = [r[0] for r in cur.fetchall()]
        if len(sig_ids) < SIGNAL_CLUSTER_MIN_ARTICLES:
            continue

        # Label: prefer the summary, else stitch keywords
        label = (summary or "").strip()
        if not label:
            label = "News cluster: " + " ".join(
                (keywords or [])[:4]
            )
        label = label[:300]

        rule = rule_set([
            co_occurrence(
                scope={"signal_ids": sig_ids},
                min_documents=2,
                clause_id=f"signal_cluster_{cluster_id}",
                score_method="cooccurrence_count",
            ),
        ])
        subtype = _pick_topic_subtype(label, list(keywords or []))
        try:
            result = create_topic(
                tenant_id=tenant_id,
                label=label,
                subtype=subtype,
                description=f"Auto-clustered from signal_cluster {cluster_id} "
                            f"({articles} articles, {sources} sources)",
                membership_rule=rule,
                created_by="auto_cluster",
                materialize=True,
            )
            if result.get("member_count", 0) > 0:
                created += 1
                _STATUS["by_source"]["signal_cluster"] += 1
                log.info("auto_cluster: proposed topic %s from signal_cluster %d (%d members)",
                         result.get("base60_address"), cluster_id, result["member_count"])
        except Exception as e:
            log.debug("signal_cluster %d topic creation failed: %s", cluster_id, e)
    return created


def _propose_library_topics(tenant_id: str, max_new: int) -> int:
    """Propose topics for each well-populated library."""
    from core.topics.rules import rule_set, co_occurrence
    from core.topics.materialize import create_topic

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT lm.library_id, COUNT(*) AS doc_count,
                       (SELECT canonical_name FROM libraries WHERE library_id = lm.library_id) AS lib_name
                  FROM library_members lm
                 WHERE lm.member_kind = 'document'
                 GROUP BY lm.library_id
                HAVING COUNT(*) >= %s
                 ORDER BY doc_count DESC
                 LIMIT %s
                """,
                (LIBRARY_MIN_DOCS, max_new * 3),
            )
            rows = cur.fetchall()

    created = 0
    for lib_id, doc_count, lib_name in rows:
        if created >= max_new:
            break
        label = f"{lib_name or f'Library {lib_id}'} corpus"
        rule = rule_set([
            co_occurrence(
                scope={"library_id": lib_id},
                min_documents=3,
                entity_types=["person", "organization", "government_agency",
                              "place", "country", "city", "facility"],
                clause_id=f"library_{lib_id}",
                score_method="cooccurrence_count",
            ),
        ])
        subtype = _pick_topic_subtype(label)
        try:
            result = create_topic(
                tenant_id=tenant_id,
                label=label,
                subtype=subtype,
                description=f"Auto-clustered from library {lib_id} "
                            f"({doc_count} documents)",
                membership_rule=rule,
                created_by="auto_cluster",
                materialize=True,
            )
            if result.get("member_count", 0) > 0:
                created += 1
                _STATUS["by_source"]["library"] += 1
                log.info("auto_cluster: proposed topic %s from library %d (%d members)",
                         result.get("base60_address"), lib_id, result["member_count"])
        except Exception as e:
            log.debug("library %d topic creation failed: %s", lib_id, e)
    return created


def _propose_entity_network_topics(tenant_id: str, max_new: int) -> int:
    """Propose 2-hop network topics for the top high-signal entities."""
    from core.topics.rules import rule_set, predicate_walk
    from core.topics.materialize import create_topic

    # Trusted predicates for cascade — keeps the network from blowing up
    TRUSTED = [
        'communicated_with', 'met_with', 'accused', 'endorsed',
        'officer_of', 'director_of', 'subsidiary_of', 'parent_org',
        'founded_by', 'owned_by', 'controls',
    ]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, canonical_name, canonical_type, signal_count
                  FROM entities
                 WHERE signal_count >= %s
                   AND canonical_type IN ('person','organization','government_agency')
                 ORDER BY signal_count DESC
                 LIMIT %s
                """,
                (ENTITY_MIN_SIGNAL_COUNT, ENTITY_MAX_PROPOSALS * 2),
            )
            rows = cur.fetchall()

    created = 0
    for ent_id, name, etype, sig_count in rows[:ENTITY_MAX_PROPOSALS]:
        if created >= max_new:
            break
        # Verify the entity has at least 1 trusted-predicate edge before proposing
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*) FROM assessments
                        WHERE (subj_entity_id=%s OR obj_entity_id=%s)
                          AND predicate_edge_type = ANY(%s)""",
                    (ent_id, ent_id, TRUSTED),
                )
                edge_count = cur.fetchone()[0]
        if edge_count < 1:
            continue

        label = f"{name} network"
        rule = rule_set([
            predicate_walk(
                seed_entity_ids=[ent_id],
                max_hops=2,
                allowed_predicates=TRUSTED,
                clause_id=f"entity_network_{ent_id}",
                score_method="hop_decay",
            ),
        ])
        try:
            result = create_topic(
                tenant_id=tenant_id,
                label=label,
                subtype="investigation",
                description=f"Auto-clustered 2-hop trusted-predicate network "
                            f"from entity {ent_id} ({sig_count} signals)",
                membership_rule=rule,
                created_by="auto_cluster",
                materialize=True,
            )
            if result.get("member_count", 0) > 1:
                created += 1
                _STATUS["by_source"]["entity_network"] += 1
                log.info("auto_cluster: proposed network topic %s for entity %d (%d members)",
                         result.get("base60_address"), ent_id, result["member_count"])
        except Exception as e:
            log.debug("entity network %d topic creation failed: %s", ent_id, e)
    return created


def run_tick(*, tenant_id: str = "default", max_new: int = 10) -> int:
    """One tick: try each source until max_new is hit. Returns total created."""
    _STATUS["phase"] = "scanning signal clusters"
    created = _propose_signal_cluster_topics(tenant_id, max_new)
    remaining = max_new - created
    if remaining > 0:
        _STATUS["phase"] = "scanning libraries"
        created += _propose_library_topics(tenant_id, remaining)
        remaining = max_new - created
    if remaining > 0:
        _STATUS["phase"] = "scanning high-signal entities"
        created += _propose_entity_network_topics(tenant_id, remaining)
    return created


if __name__ == "__main__":
    import argparse, json
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--max-new", type=int, default=10)
    p.add_argument("--tick", action="store_true")
    args = p.parse_args()
    if args.tick:
        n = run_tick(max_new=args.max_new)
        print(json.dumps({"created": n, "status": _STATUS}, indent=2, default=str))
