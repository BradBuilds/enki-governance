"""
Boilerplate detection — corpus-wide SimHash clustering of paragraphs.

After a batch of documents is ingested into a corpus (= dataset_id), this
module clusters their paragraphs by SimHash. Paragraphs with hamming distance
< THRESHOLD form a cluster. Clusters that span > N documents are marked as
`template_phrase`, their member paragraphs flagged `is_boilerplate=TRUE`, and
the mention extractor automatically skips them.

This is what stops the system from extracting "United States District Court"
as an organization 1,892 times in a court-records corpus, or "[REDACTED]" as
a person name 312 times. The boilerplate becomes a node in its own right
(with frequency), and entity extraction focuses on signal paragraphs.

Run after ingestion:
    from core.documents.boilerplate import classify_corpus
    summary = classify_corpus("example_corpus_2024")
    # → {"clusters_found": 47, "boilerplate_paragraphs": 12891, ...}

Two passes:
  1. Bucket-by-prefix scan: group paragraphs whose SimHashes share the top
     8 bits, then do hamming-distance comparison inside each bucket.
     ~10K paragraphs/sec on a modern CPU; doesn't need a vector DB.
  2. Cluster classification: anything in N+ distinct documents → boilerplate.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

from core.documents.simhash import hamming, unsigned_64
from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.boilerplate")

# Hamming-distance threshold for "same cluster". Empirically 3-5 works well
# for English legal/news prose; lower = stricter clusters, higher = noisier.
_DEFAULT_HAMMING_THRESHOLD = 4

# A cluster must appear in at least this many distinct documents to be
# classified as boilerplate. For "recurring" the threshold is lower.
_DEFAULT_BOILERPLATE_DOC_FRACTION = 0.5  # ≥50% of corpus docs
_DEFAULT_RECURRING_DOC_FRACTION = 0.05   # ≥5% of corpus docs

# Intra-doc frequency threshold. A paragraph cluster that occurs >= this many
# times in a SINGLE document is also boilerplate — typical of FOIA / court
# releases where the same page header ("*Redactions for personal contact
# information") appears on every page of a 100-page contact list. Without
# this rule the cross-doc threshold misses purely-intra-doc noise.
_DEFAULT_INTRA_DOC_MIN_OCCURRENCES = 10

# Bucket prefix length (in bits). 8 → 256 buckets, good balance for ≤100K
# paragraph corpora. Bump to 12 for million-paragraph corpora.
_BUCKET_BITS = 8


def _bucket_of(unsigned_simhash: int) -> int:
    """Top `_BUCKET_BITS` bits of the simhash; used to group candidates so
    we don't have to do O(n²) hamming comparisons across the whole corpus."""
    shift = 64 - _BUCKET_BITS
    return unsigned_simhash >> shift


def _fetch_corpus_paragraphs(corpus_id: str) -> list[tuple[int, int, int]]:
    """Returns (paragraph_id, document_id, simhash_unsigned) for every
    non-redacted paragraph in the corpus."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.paragraph_id, p.document_id, p.simhash
                FROM document_paragraphs p
                JOIN documents d ON d.document_id = p.document_id
                WHERE d.dataset_id = %s
                  AND p.simhash IS NOT NULL
                  AND p.is_redacted = FALSE
                """,
                (corpus_id,),
            )
            rows = cur.fetchall()
    return [(pid, did, unsigned_64(sig)) for (pid, did, sig) in rows]


def _cluster_paragraphs(
    paragraphs: list[tuple[int, int, int]],
    hamming_threshold: int,
) -> list[list[tuple[int, int, int]]]:
    """Group paragraphs into clusters by hamming-distance < threshold.

    Single-pass greedy clustering with bucket-prefix bucketing for speed.
    Each cluster is a list of (paragraph_id, document_id, simhash) tuples.
    Singletons (clusters with one member) are dropped at this stage."""
    buckets: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for row in paragraphs:
        buckets[_bucket_of(row[2])].append(row)

    clusters: list[list] = []
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
                if hamming(row_i[2], bucket_rows[j][2]) <= hamming_threshold:
                    cluster.append(bucket_rows[j])
                    seen[j] = True
            if len(cluster) > 1:
                clusters.append(cluster)

    return clusters


def _doc_count_of(cluster: list[tuple[int, int, int]]) -> int:
    return len({row[1] for row in cluster})


def _representative_text(cluster: list[tuple[int, int, int]]) -> str:
    """Return text of the first paragraph in the cluster as a representative
    sample. Caller queries DB; this is just the id selection."""
    return f"paragraph_id={cluster[0][0]}"


def classify_corpus(
    corpus_id: str,
    *,
    hamming_threshold: int = _DEFAULT_HAMMING_THRESHOLD,
    boilerplate_doc_fraction: float = _DEFAULT_BOILERPLATE_DOC_FRACTION,
    recurring_doc_fraction: float = _DEFAULT_RECURRING_DOC_FRACTION,
    intra_doc_min_occurrences: int = _DEFAULT_INTRA_DOC_MIN_OCCURRENCES,
) -> dict:
    """Run the boilerplate detector on one corpus. Idempotent: rerunning
    after new docs land just expands existing clusters and may upgrade
    'recurring' to 'boilerplate' as the threshold is crossed."""
    paragraphs = _fetch_corpus_paragraphs(corpus_id)
    if not paragraphs:
        return {"corpus_id": corpus_id, "ok": True, "reason": "no paragraphs"}

    total_docs = len({p[1] for p in paragraphs})
    if total_docs == 0:
        return {"corpus_id": corpus_id, "ok": True, "reason": "no documents"}

    log.info(
        "Boilerplate scan: corpus=%s paragraphs=%d docs=%d",
        corpus_id, len(paragraphs), total_docs,
    )

    clusters = _cluster_paragraphs(paragraphs, hamming_threshold)
    log.info("Boilerplate scan: %d non-singleton clusters", len(clusters))

    boilerplate_thresh = max(2, int(total_docs * boilerplate_doc_fraction))
    recurring_thresh = max(2, int(total_docs * recurring_doc_fraction))

    boilerplate_paragraph_ids: list[int] = []
    template_rows: list[tuple] = []

    for cluster in clusters:
        doc_count = _doc_count_of(cluster)
        occurrence_count = len(cluster)
        # Three paths to "boilerplate":
        #   (a) cluster spans >= boilerplate_thresh distinct docs (cross-doc template)
        #   (b) cluster occurs >= intra_doc_min_occurrences times in one+ docs
        #       (intra-doc template, e.g. page header on every page)
        # Plus "recurring" for the lighter cross-doc threshold.
        is_cross_doc_boiler = doc_count >= boilerplate_thresh
        is_intra_doc_boiler = (
            occurrence_count >= intra_doc_min_occurrences and doc_count < boilerplate_thresh
        )
        is_recurring = doc_count >= recurring_thresh

        if not (is_cross_doc_boiler or is_intra_doc_boiler or is_recurring):
            continue

        if is_cross_doc_boiler or is_intra_doc_boiler:
            classification = "boilerplate"
        else:
            classification = "recurring"
        sig = cluster[0][2]
        # Postgres BIGINT signed conversion
        from core.documents.simhash import signed_64
        template_rows.append((
            corpus_id, _representative_text(cluster), signed_64(sig),
            len(cluster), doc_count, classification,
            classification == "boilerplate",  # suppressed flag
        ))
        if classification == "boilerplate":
            boilerplate_paragraph_ids.extend(row[0] for row in cluster)

    # Persist template_phrases + flag paragraphs
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Wipe + replace for this corpus — clustering is non-incremental.
            cur.execute(
                "DELETE FROM template_phrases WHERE corpus_id = %s",
                (corpus_id,),
            )
            cur.executemany(
                """
                INSERT INTO template_phrases
                    (corpus_id, representative_text, simhash,
                     occurrence_count, document_count, classification,
                     suppressed)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                template_rows,
            )

            if boilerplate_paragraph_ids:
                # Flag the actual paragraph rows so the mention extractor
                # skips them on the next pass.
                cur.execute(
                    """
                    UPDATE document_paragraphs
                    SET is_boilerplate = TRUE
                    WHERE paragraph_id = ANY(%s)
                    """,
                    (boilerplate_paragraph_ids,),
                )
        conn.commit()

    return {
        "corpus_id": corpus_id,
        "ok": True,
        "total_paragraphs": len(paragraphs),
        "total_documents": total_docs,
        "clusters_found": len(clusters),
        "templates_recorded": len(template_rows),
        "boilerplate_paragraphs_flagged": len(boilerplate_paragraph_ids),
        "boilerplate_doc_threshold": boilerplate_thresh,
        "recurring_doc_threshold": recurring_thresh,
        "intra_doc_min_occurrences": intra_doc_min_occurrences,
    }


def list_templates(
    corpus_id: str,
    classification: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """List template phrases for a corpus, with their representative text
    fetched. Drives the 'top N boilerplate phrases' panel in the corpus UI."""
    where = ["t.corpus_id = %s"]
    params: list = [corpus_id]
    if classification:
        where.append("t.classification = %s")
        params.append(classification)
    params.append(limit)

    sql = f"""
        WITH t AS (
            SELECT template_id, corpus_id, representative_text,
                   occurrence_count, document_count, classification,
                   suppressed
            FROM template_phrases
            WHERE {' AND '.join(where)}
            ORDER BY document_count DESC, occurrence_count DESC
            LIMIT %s
        )
        SELECT t.template_id, t.corpus_id, t.occurrence_count,
               t.document_count, t.classification, t.suppressed,
               -- Fetch one representative paragraph text for each template
               COALESCE(
                  (SELECT raw_text FROM document_paragraphs p
                   WHERE p.is_boilerplate = (t.classification = 'boilerplate')
                     AND p.simhash IS NOT NULL
                   LIMIT 1),
                  t.representative_text
               ) AS sample_text
        FROM t
    """
    out = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                out.append({
                    "template_id": r[0],
                    "corpus_id": r[1],
                    "occurrence_count": r[2],
                    "document_count": r[3],
                    "classification": r[4],
                    "suppressed": r[5],
                    "sample_text": (r[6] or "")[:300],
                })
    return out
