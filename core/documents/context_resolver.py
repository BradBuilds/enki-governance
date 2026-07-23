"""
Context-bag mention clustering — the multi-signal resolver.

Mentions of the same surface form don't always refer to the same entity.
"Doe" in Acme context and "Doe" in Roswell context are different
people. This module clusters mentions WITHIN a corpus by the OTHER surface
forms appearing in the same paragraph (the context bag). Two mentions of
the same surface_form_norm cluster together iff they share at least one
context form.

Stored in document_mention_clusters (one row per cluster) + a cluster_id
column on document_mentions. After clustering, the resolver routes each
cluster (not each surface form globally) through internal-node and
Wikipedia lookups, passing the cluster's context_keywords as
disambiguation evidence.

Public surface:

    cluster_corpus(corpus_id) -> summary
        Idempotent. Re-running re-computes clusters from scratch.

    get_clusters(corpus_id, surface_form_norm=None) -> list[dict]
        For inspection.
"""

from __future__ import annotations

import logging
from collections import defaultdict, Counter
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.context_resolver")

# A cluster member needs at least this many shared context forms with
# another member to join the same cluster. 1 = very aggressive (any shared
# entity merges them); higher = more conservative (multiple shared entities
# required). For adversarial corpora we want >=1 because most paragraphs
# only have a handful of mentions.
_MIN_SHARED_CONTEXT = 1

# We exclude very short forms from the context bag (they're high-noise
# fragments like "M", "P", "II" that would falsely link unrelated paragraphs).
_MIN_CONTEXT_FORM_LENGTH = 4

# Cluster's stored context_keywords cap (top N most-common co-occurring forms)
_MAX_CONTEXT_KEYWORDS = 12


def _fetch_corpus_mentions(corpus_id: str) -> list[tuple]:
    """Return (mention_id, document_id, paragraph_id, surface_form,
    surface_form_norm, suggested_type) for every mention in the corpus."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.mention_id, m.document_id, m.paragraph_id,
                       m.surface_form, m.surface_form_norm, m.suggested_type
                  FROM document_mentions m
                  JOIN documents d ON d.document_id = m.document_id
                 WHERE d.dataset_id = %s
                   AND m.surface_form_norm IS NOT NULL
                """,
                (corpus_id,),
            )
            return cur.fetchall()


def cluster_corpus(corpus_id: str) -> dict:
    """Re-cluster every mention in a corpus by paragraph-context overlap.
    Wipes prior clusters for the corpus and rebuilds (idempotent)."""
    mentions = _fetch_corpus_mentions(corpus_id)
    if not mentions:
        return {"corpus_id": corpus_id, "ok": True,
                "reason": "no mentions", "clusters": 0}

    # 1. paragraph_id → set of (surface_form_norm) appearing in it
    paragraph_forms: dict[int, set[str]] = defaultdict(set)
    for _, _, pid, _, sfn, _ in mentions:
        if pid and sfn and len(sfn) >= _MIN_CONTEXT_FORM_LENGTH:
            paragraph_forms[pid].add(sfn)

    # 1b. page_id → set of (surface_form_norm) appearing anywhere on the page
    #     (combines text-paragraph mentions AND image-OCR/caption mentions).
    #     This is the key to merging image-sourced and text-sourced mentions
    #     into the same cluster: a "Doe" extracted from image OCR on
    #     page 36 shares page context with "Doe Mary" extracted from
    #     a paragraph on page 36.
    page_forms: dict[int, set[str]] = defaultdict(set)
    mention_page: dict[int, int] = {}
    # Re-pull page_id per mention. The 6-tuple fetch doesn't include it,
    # so query once explicitly.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.mention_id, m.page_id
                  FROM document_mentions m
                  JOIN documents d ON d.document_id = m.document_id
                 WHERE d.dataset_id = %s AND m.page_id IS NOT NULL
                """,
                (corpus_id,),
            )
            for mid, page in cur.fetchall():
                mention_page[mid] = page

    for mid, _, _, _, sfn, _ in mentions:
        if sfn and len(sfn) >= _MIN_CONTEXT_FORM_LENGTH:
            pg = mention_page.get(mid)
            if pg:
                page_forms[pg].add(sfn)

    # 2. mention_id → its context bag.
    #
    #    The bag is the UNION of:
    #      a. other surface forms in the SAME PARAGRAPH (existing logic —
    #         tight context, strongest signal)
    #      b. other surface forms on the SAME PAGE  (new — lets image
    #         mentions, which have no paragraph, still find context from
    #         the text mentions on the page they were extracted from)
    #
    #    Both layers are dropped if too short (avoids 1-2 char fragments).
    mention_context: dict[int, frozenset[str]] = {}
    for mid, _, pid, _, sfn, _ in mentions:
        ctx: set[str] = set()
        if pid:
            ctx |= paragraph_forms.get(pid, set())
        pg = mention_page.get(mid)
        if pg:
            ctx |= page_forms.get(pg, set())
        ctx.discard(sfn)
        mention_context[mid] = frozenset(ctx)

    # 3. Group mentions by surface_form_norm
    by_form: dict[str, list[tuple]] = defaultdict(list)
    for row in mentions:
        by_form[row[4]].append(row)

    # 4. Within each surface_form_norm, cluster by context overlap
    clusters: list[dict] = []
    for sfn, group in by_form.items():
        mids = [r[0] for r in group]
        if len(mids) == 1:
            r = group[0]
            clusters.append({
                "surface_form_norm": sfn,
                "representative_form": r[3],
                "mention_ids": mids,
                "document_ids": {r[1]},
                "context": list(mention_context.get(r[0], set()))[:_MAX_CONTEXT_KEYWORDS],
                "suggested_type": r[5],
            })
            continue

        # Union-find
        parent = {m: m for m in mids}
        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Compare each pair. Quadratic per surface form but bounded
        # because mentions-per-form is typically small (5-50).
        #
        # Union criteria (any of these triggers a merge):
        #   A. Both mentions are on the SAME PAGE — co-location is itself
        #      strong evidence (same Doe mentioned twice on page 36
        #      of the contact list is the same Doe, full stop).
        #   B. paragraph/page context overlap >= _MIN_SHARED_CONTEXT
        #      (existing rule — shared other surface forms).
        for i, m1 in enumerate(mids):
            ctx1 = mention_context.get(m1, frozenset())
            page1 = mention_page.get(m1)
            for m2 in mids[i + 1:]:
                page2 = mention_page.get(m2)
                # Criterion A: same page = merge
                if page1 and page2 and page1 == page2:
                    union(m1, m2)
                    continue
                # Criterion B: shared context (only if both have context)
                if not ctx1:
                    continue
                ctx2 = mention_context.get(m2, frozenset())
                if not ctx2:
                    continue
                if len(ctx1 & ctx2) >= _MIN_SHARED_CONTEXT:
                    union(m1, m2)

        # Extract groups + metadata
        groups: dict[int, list[int]] = defaultdict(list)
        by_mid: dict[int, tuple] = {r[0]: r for r in group}
        for m in mids:
            groups[find(m)].append(m)

        for cluster_mids in groups.values():
            doc_ids = set()
            sample_form = None
            ctx_counter: Counter[str] = Counter()
            types: list[str] = []
            for m in cluster_mids:
                r = by_mid[m]
                doc_ids.add(r[1])
                if not sample_form:
                    sample_form = r[3]
                if r[5]:
                    types.append(r[5])
                ctx_counter.update(mention_context.get(m, frozenset()))
            most_common = [k for k, _ in ctx_counter.most_common(_MAX_CONTEXT_KEYWORDS)]
            # Modal suggested_type (most-common type across members)
            modal_type = Counter(types).most_common(1)[0][0] if types else None
            clusters.append({
                "surface_form_norm": sfn,
                "representative_form": sample_form,
                "mention_ids": cluster_mids,
                "document_ids": doc_ids,
                "context": most_common,
                "suggested_type": modal_type,
            })

    # 5. Persist
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Wipe prior clusters for this corpus (we re-build from scratch).
            # First we have to clean up cluster-keyed entity_external_lookups
            # rows — if we just delete the clusters, the FK ON DELETE SET NULL
            # would null-out their cluster_id, which can then collide with
            # the legacy surface-form-level unique key.
            cur.execute(
                """
                DELETE FROM entity_external_lookups
                 WHERE cluster_id IN (
                   SELECT cluster_id FROM document_mention_clusters
                    WHERE corpus_id = %s)
                """,
                (corpus_id,),
            )
            # Null-out cluster_id on member mentions to avoid orphans.
            cur.execute(
                "UPDATE document_mentions SET cluster_id = NULL "
                "WHERE document_id IN (SELECT document_id FROM documents WHERE dataset_id = %s)",
                (corpus_id,),
            )
            cur.execute(
                "DELETE FROM document_mention_clusters WHERE corpus_id = %s",
                (corpus_id,),
            )

            singles = 0
            multi = 0
            for c in clusters:
                cur.execute(
                    """
                    INSERT INTO document_mention_clusters
                        (corpus_id, surface_form_norm, representative_form,
                         mention_count, document_count, context_keywords,
                         representative_mention_id, suggested_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING cluster_id
                    """,
                    (
                        corpus_id, c["surface_form_norm"], c["representative_form"],
                        len(c["mention_ids"]), len(c["document_ids"]),
                        c["context"], c["mention_ids"][0], c["suggested_type"],
                    ),
                )
                cid = cur.fetchone()[0]
                # Assign cluster_id to each member mention
                cur.execute(
                    "UPDATE document_mentions SET cluster_id = %s "
                    "WHERE mention_id = ANY(%s)",
                    (cid, c["mention_ids"]),
                )
                if len(c["mention_ids"]) == 1:
                    singles += 1
                else:
                    multi += 1
        conn.commit()

    return {
        "corpus_id": corpus_id,
        "ok": True,
        "mentions_processed": len(mentions),
        "clusters_total": len(clusters),
        "clusters_singleton": singles,
        "clusters_multi_member": multi,
        "min_shared_context": _MIN_SHARED_CONTEXT,
    }


def get_clusters(
    corpus_id: str,
    surface_form_norm: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Inspection helper: return clusters in this corpus."""
    where = ["corpus_id = %s"]
    params: list = [corpus_id]
    if surface_form_norm:
        where.append("surface_form_norm = %s")
        params.append(surface_form_norm)
    params.append(limit)
    sql = f"""
        SELECT cluster_id, surface_form_norm, representative_form,
               mention_count, document_count, context_keywords,
               suggested_type
        FROM document_mention_clusters
        WHERE {' AND '.join(where)}
        ORDER BY mention_count DESC, surface_form_norm
        LIMIT %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            out = []
            for r in cur.fetchall():
                out.append({
                    "cluster_id": r[0],
                    "surface_form_norm": r[1],
                    "representative_form": r[2],
                    "mention_count": r[3],
                    "document_count": r[4],
                    "context_keywords": r[5] or [],
                    "suggested_type": r[6],
                })
            return out
