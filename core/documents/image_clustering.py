"""
pHash-based cross-doc image clustering — Phase 3.4.

For every corpus, cluster document_images by perceptual-hash Hamming
distance. Two images with the same pHash (or distance ≤ threshold) are
near-duplicates: usually the same photo appearing in multiple documents.

Same architectural pattern as core/documents/boilerplate.py for text:
  • bucket pre-filter by top N bits of the hash so we don't do O(n²)
    distance comparisons across the whole corpus
  • within each bucket, pairwise Hamming distance
  • clusters > 1 member get classified

A cluster appearing in many documents (member_count > N) is a TEMPLATE
(decorative letterhead, agency seal, page-numbering element) and gets
is_template=true so the analyst can filter it out. Same-photo-in-2-docs
clusters are intel-grade evidence (same Roswell debris photo in multiple
FBI files).

Public surface:
    cluster_corpus(corpus_id, *, hamming_threshold=8) -> dict
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.image_clustering")

# Hamming-distance threshold for "same image". pHash is 64 bits; distance
# 8 catches re-scans / re-compressions of the same photo without matching
# truly distinct photos. Tunable per corpus.
_DEFAULT_HAMMING_THRESHOLD = 8

# Top N bits of the pHash for bucket pre-filter. 8 bits = 256 buckets.
_BUCKET_BITS = 8

# A cluster appearing in this many distinct documents (or more) is
# treated as a template (decorative / page-furniture) and the suppression
# flag is set.
_TEMPLATE_DOC_FRACTION = 0.5    # ≥50% of corpus docs


def _bucket_of(phash_signed: int) -> int:
    """Top-N-bit bucket of the pHash. Postgres stores signed 64-bit;
    convert to unsigned for stable top-bit extraction."""
    if phash_signed is None:
        return 0
    if phash_signed < 0:
        unsigned = phash_signed + (1 << 64)
    else:
        unsigned = phash_signed
    return unsigned >> (64 - _BUCKET_BITS)


def _hamming(a: int, b: int) -> int:
    """Hamming distance between two signed 64-bit ints (Postgres BIGINT)."""
    if a is None or b is None:
        return 64
    if a < 0:
        a += (1 << 64)
    if b < 0:
        b += (1 << 64)
    return bin(a ^ b).count("1")


def _fetch_corpus_images(corpus_id: str) -> list[tuple[int, int, int]]:
    """(image_id, document_id, phash_signed) for every non-redacted image
    in the corpus."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.image_id, i.document_id, i.phash
                  FROM document_images i
                  JOIN documents d ON d.document_id = i.document_id
                 WHERE d.dataset_id = %s
                   AND i.phash IS NOT NULL
                   AND NOT i.is_redacted_block
                """,
                (corpus_id,),
            )
            return cur.fetchall()


def _fetch_classifications(image_ids: list[int]) -> dict[int, str]:
    """Vision classification for these images (if analyzed). Used to set
    cluster.classification + sample_caption."""
    if not image_ids:
        return {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT image_id, classification, caption
                  FROM document_image_analyses
                 WHERE engine = 'vision_gemma4'
                   AND image_id = ANY(%s)
                """,
                (image_ids,),
            )
            return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def cluster_corpus(
    corpus_id: str,
    *,
    hamming_threshold: int = _DEFAULT_HAMMING_THRESHOLD,
) -> dict:
    """Run pHash clustering across a corpus. Wipes prior clusters and
    rebuilds (idempotent)."""
    images = _fetch_corpus_images(corpus_id)
    if not images:
        return {"corpus_id": corpus_id, "ok": True,
                "reason": "no images with phash"}

    total_docs = len({i[1] for i in images})
    template_thresh = max(2, int(total_docs * _TEMPLATE_DOC_FRACTION))

    # Bucket pre-filter
    buckets: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for row in images:
        buckets[_bucket_of(row[2])].append(row)

    clusters: list[list[tuple[int, int, int]]] = []
    for bucket_rows in buckets.values():
        seen = [False] * len(bucket_rows)
        for i, row_i in enumerate(bucket_rows):
            if seen[i]:
                continue
            cluster = [row_i]
            seen[i] = True
            for j in range(i + 1, len(bucket_rows)):
                if seen[j]:
                    continue
                if _hamming(row_i[2], bucket_rows[j][2]) <= hamming_threshold:
                    cluster.append(bucket_rows[j])
                    seen[j] = True
            if len(cluster) > 1:
                clusters.append(cluster)

    # Pull classifications for the representative images so the cluster
    # row inherits a sensible label/caption.
    all_ids: list[int] = []
    for c in clusters:
        for row in c:
            all_ids.append(row[0])
    class_map = _fetch_classifications(all_ids)

    # Persist
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Clean prior data for this corpus
            cur.execute(
                """
                DELETE FROM document_image_cluster_members
                 WHERE cluster_id IN (
                   SELECT cluster_id FROM document_image_clusters
                    WHERE corpus_id = %s)
                """,
                (corpus_id,),
            )
            cur.execute(
                "DELETE FROM document_image_clusters WHERE corpus_id = %s",
                (corpus_id,),
            )

            stats = {"clusters_total": 0, "members_total": 0,
                     "templates_flagged": 0}
            for c in clusters:
                doc_count = len({r[1] for r in c})
                if doc_count < 1:
                    continue
                # Representative = first member
                rep_image_id, _, rep_phash = c[0]
                rep_class_caption = class_map.get(rep_image_id, (None, None))
                rep_class = rep_class_caption[0]
                rep_caption = rep_class_caption[1]

                is_template = doc_count >= template_thresh
                if is_template:
                    stats["templates_flagged"] += 1

                cur.execute(
                    """
                    INSERT INTO document_image_clusters
                        (corpus_id, representative_image_id, phash,
                         member_count, document_count, classification,
                         is_template, sample_caption)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING cluster_id
                    """,
                    (
                        corpus_id, rep_image_id, rep_phash,
                        len(c), doc_count, rep_class,
                        is_template, rep_caption,
                    ),
                )
                cid = cur.fetchone()[0]
                # Bulk insert members
                for img_id, _, phash in c:
                    dist = _hamming(rep_phash, phash)
                    cur.execute(
                        """
                        INSERT INTO document_image_cluster_members
                            (cluster_id, image_id, hamming_distance)
                        VALUES (%s, %s, %s)
                        """,
                        (cid, img_id, dist),
                    )
                    stats["members_total"] += 1
                stats["clusters_total"] += 1
        conn.commit()

    return {
        "ok": True,
        "corpus_id": corpus_id,
        "images_considered": len(images),
        "documents_in_corpus": total_docs,
        "hamming_threshold": hamming_threshold,
        "template_doc_threshold": template_thresh,
        **stats,
        "compression_ratio": (
            round(len(images) / stats["clusters_total"], 2)
            if stats["clusters_total"] else 1.0
        ),
    }


def get_clusters(
    corpus_id: str,
    *,
    only_templates: bool = False,
    only_intel: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Inspection helper."""
    where = ["corpus_id = %s"]
    params: list = [corpus_id]
    if only_templates:
        where.append("is_template = TRUE")
    if only_intel:
        where.append("is_template = FALSE")
        where.append("member_count > 1")
    params.append(limit)
    sql = f"""
        SELECT cluster_id, representative_image_id, phash,
               member_count, document_count, classification,
               is_template, sample_caption
          FROM document_image_clusters
         WHERE {' AND '.join(where)}
         ORDER BY member_count DESC, document_count DESC
         LIMIT %s
    """
    out: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                out.append({
                    "cluster_id": r[0],
                    "representative_image_id": r[1],
                    "phash": r[2],
                    "member_count": r[3],
                    "document_count": r[4],
                    "classification": r[5],
                    "is_template": r[6],
                    "sample_caption": (r[7] or "")[:200],
                })
    return out
