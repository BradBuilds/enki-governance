"""
LDU subtree clustering — discover concept identity from token overlap.

For tier-G floating concepts (no thesaurus hit) within the same subtype
subtree, cluster surface forms whose tokens overlap with rare-token
weighting. Each cluster collapses to one concept without an LLM call.

This is the cheap fallback for the long tail. The expensive fallback
(LLM arbitration) is post-wipe.

Algorithm:
  1. Pull tier-G concepts with signal_count >= MIN_SIGNAL_FOR_CLUSTER,
     grouped by subtype_path.
  2. Tokenize each canonical_name (split on whitespace, drop stopwords,
     keep tokens of length >= 2).
  3. Compute document frequency of each token within the subtype group.
     Rare tokens (df=1 or 2) weight more than common ones.
  4. For each pair, compute weighted Jaccard:
       sum(weight(t) for t in A ∩ B) / sum(weight(t) for t in A ∪ B)
  5. Pairs with similarity >= CLUSTER_THRESHOLD form an edge.
  6. Run connected components → clusters of size >= 2.
  7. Within each cluster: oldest concept wins, others merge into it via
     core.ldu.collapse._merge_concept.

Skips concepts that carry conflicting tier-A identifiers (same guard
as strategy-3 in collapse.py).
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.ldu.cluster")


# Tuning knobs ─────────────────────────────────────────────────────────────

MIN_SIGNAL_FOR_CLUSTER = 2     # don't cluster single-mention concepts
CLUSTER_THRESHOLD = 0.4        # weighted Jaccard score floor for an edge
MIN_RARE_TOKEN_OVERLAP = 1     # need at least 1 rare (df<=2) shared token
MAX_PER_SUBTYPE_PER_TICK = 200 # bound work per subtype per pass


# Stopwords + token rules ──────────────────────────────────────────────────

_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "and", "or", "in", "on", "at", "to",
    "by", "from", "with", "as", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "may", "might", "can", "could", "should", "this", "that", "these",
    "those", "it", "its", "their", "her", "his", "our", "your",
    "co", "corp", "ltd", "inc", "llc", "lp", "plc",  # legal suffixes
})

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")


def _tokenize(s: str) -> set[str]:
    """Lowercase + tokenize, drop stopwords + single chars."""
    if not s:
        return set()
    tokens = _TOKEN_RE.findall(s.lower())
    return {t for t in tokens if len(t) >= 2 and t not in _STOPWORDS}


def _token_weight(df: int, n_docs: int) -> float:
    """IDF-style weight: rare tokens are more identifying."""
    if df <= 0:
        return 0.0
    return math.log((n_docs + 1) / df)


# ── Public entry point ────────────────────────────────────────────────────

def cluster_floating_concepts(
    *,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> dict:
    """Run a clustering pass across pending tier-G concepts.

    Returns ``{'subtype_groups': N, 'clusters_found': C, 'merged': M,
              'errors': E}``.
    """
    subtype_groups = 0
    total_clusters = 0
    total_merged = 0
    errors = 0

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Pull all tier-G candidates grouped by subtype_path.
                cur.execute(
                    """
                    SELECT subtype_path,
                           array_agg(concept_id ORDER BY concept_id)
                    FROM concept_nodes
                    WHERE tenant_id = %s
                      AND promotion_status = 'pending'
                      AND evidence_tier = 'G'
                      AND signal_count >= %s
                    GROUP BY subtype_path
                    """,
                    (tenant_id, MIN_SIGNAL_FOR_CLUSTER),
                )
                groups = cur.fetchall()

                for subtype_path, cids in groups:
                    subtype_groups += 1
                    cids = cids[:MAX_PER_SUBTYPE_PER_TICK]
                    if len(cids) < 2:
                        continue

                    # Pull full data for this group.
                    cur.execute(
                        """SELECT concept_id, canonical_name,
                                  signal_count, identifiers
                             FROM concept_nodes
                            WHERE concept_id = ANY(%s)""",
                        (cids,),
                    )
                    rows = cur.fetchall()
                    if len(rows) < 2:
                        continue

                    clusters = _cluster_group(rows)
                    total_clusters += len(clusters)

                    for cluster in clusters:
                        # Sort by concept_id (oldest wins).
                        cluster_sorted = sorted(cluster, key=lambda r: r[0])
                        winner_id = cluster_sorted[0][0]

                        # Pull winner's identifiers for non-merge guard.
                        cur.execute(
                            "SELECT identifiers FROM concept_nodes WHERE concept_id = %s",
                            (winner_id,),
                        )
                        winner_idents = (cur.fetchone() or [{}])[0] or {}

                        from core.ldu.collapse import (
                            _has_conflicting_strong_ids, _merge_concept,
                        )

                        for loser_data in cluster_sorted[1:]:
                            loser_id = loser_data[0]
                            loser_idents = loser_data[3] or {}
                            if _has_conflicting_strong_ids(winner_idents,
                                                            loser_idents):
                                log.info(
                                    "cluster blocked: %s vs %s have conflicting strong IDs",
                                    winner_id, loser_id,
                                )
                                continue
                            if _merge_concept(cur, winner_id, loser_id,
                                              reason="strategy_cluster"):
                                total_merged += 1

            conn.commit()
    except Exception as e:
        log.exception("cluster tick failed: %s", e)
        return {
            "subtype_groups": subtype_groups,
            "clusters_found": total_clusters,
            "merged": total_merged,
            "errors": errors + 1, "error": str(e),
        }

    if total_merged:
        log.info(
            "LDU cluster: subtype_groups=%d clusters=%d merged=%d",
            subtype_groups, total_clusters, total_merged,
        )
    return {
        "subtype_groups": subtype_groups,
        "clusters_found": total_clusters,
        "merged": total_merged,
        "errors": errors,
    }


def _cluster_group(rows: list) -> list:
    """Cluster concepts within a single subtype group by token overlap.

    rows: [(concept_id, canonical_name, signal_count, identifiers), ...]
    Returns: list of clusters (each cluster is a list of rows).
    """
    # Tokenize each concept's canonical_name.
    tokenized = []
    for r in rows:
        cid, cname, sig, idents = r
        tokens = _tokenize(cname)
        if tokens:
            tokenized.append((r, tokens))

    if len(tokenized) < 2:
        return []

    # Compute document frequency for each token across the group.
    df: Counter = Counter()
    for _, toks in tokenized:
        for t in toks:
            df[t] += 1
    n_docs = len(tokenized)

    # Pairwise similarity → edges.
    edges = []
    for i, (row_i, toks_i) in enumerate(tokenized):
        for row_j, toks_j in tokenized[i + 1:]:
            inter = toks_i & toks_j
            union = toks_i | toks_j
            if not inter or not union:
                continue
            # Require at least one rare-token overlap (df <= 2).
            rare_overlap = sum(1 for t in inter if df[t] <= 2)
            if rare_overlap < MIN_RARE_TOKEN_OVERLAP:
                continue
            # Weighted Jaccard.
            num = sum(_token_weight(df[t], n_docs) for t in inter)
            den = sum(_token_weight(df[t], n_docs) for t in union)
            if den <= 0:
                continue
            sim = num / den
            if sim >= CLUSTER_THRESHOLD:
                edges.append((row_i[0], row_j[0]))

    if not edges:
        return []

    # Build clusters via connected components (union-find).
    parent = {}
    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in edges:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        union(a, b)

    # Group rows by their component root.
    components = defaultdict(list)
    row_by_id = {r[0]: r for r, _ in tokenized}
    for cid in parent:
        components[find(cid)].append(row_by_id[cid])

    return [members for members in components.values() if len(members) >= 2]
