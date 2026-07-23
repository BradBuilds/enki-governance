"""
Corroboration scoring — cross-document mention rollup.

When the same surface form (or canonical entity) appears across many
documents, that's evidence of importance. When it appears across many
documents from MANY DISTINCT SOURCES, that's stronger evidence than the
same count concentrated in one source.

This module computes a per-(corpus, surface_form_norm) score and writes it
to `corpus_corroboration` for cheap querying. Re-runnable; idempotent on
(corpus_id, surface_form_norm).

Scoring (initial, transparent, tunable):

    raw_score   = mention_count
    source_boost = log2(unique_docs + 1)         # diminishing returns
    diversity_boost = log2(unique_datasets + 1)  # cross-corpus carries more
    fragment_penalty = 0.0 if not _looks_like_fragment(form) else -3.0
    corroboration_score = raw_score * source_boost * diversity_boost + fragment_penalty

Fragments (regex-fallback false positives) get penalized so they fall to
the bottom of rankings rather than topping them. The penalty doesn't
delete them — operators can still see "DEAR MR" if they ask, just not
in the default ranked view.

Public surface:

    score_corpus(corpus_id) -> summary
    top_corroborated(corpus_id, limit=50) -> list[dict]

After mention extraction lands a new wave of mentions, call score_corpus()
again. New rows replace old ones for the same (corpus, form).
"""

from __future__ import annotations

import logging
import math
import re
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.corroboration")


# ─── Fragment / noise detection ────────────────────────────────────────

# Patterns that the regex mention extractor often emits but are NOT real
# entities. We penalize these in scoring so they don't top the rankings.
# An LLM extractor would reject them up front; we patch the gap here.
_FRAGMENT_PATTERNS = [
    # Letter salutations / closings
    re.compile(r"^DEAR\s+(MR|MS|MRS|DR|SIR|MADAM|JOHN|JANE)\b", re.IGNORECASE),
    re.compile(r"^SINCERELY\b", re.IGNORECASE),
    re.compile(r"^TRULY\s+YOURS\b", re.IGNORECASE),
    # Date-phrase fragments — "On December", "In January 1947"
    re.compile(r"^ON\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)", re.IGNORECASE),
    re.compile(r"^IN\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)", re.IGNORECASE),
    # Bureaucratic phrase fragments
    re.compile(r"^REPORT\s+NO\b", re.IGNORECASE),
    re.compile(r"^EXHIBIT\s+[A-Z0-9]+\b", re.IGNORECASE),
    re.compile(r"^PAGE\s+\d+\b", re.IGNORECASE),
    # Generic articles + filler
    re.compile(r"^THE\s+[A-Z]$", re.IGNORECASE),    # "The S" / "The X"
    re.compile(r"^[A-Z]\.\s*[A-Z]?$"),               # "A." / "A. B"
]

# Forms shorter than this are almost certainly fragments (regex false positives).
_MIN_FORM_LENGTH = 4


def _looks_like_fragment(form: str) -> bool:
    """True iff this surface form looks like a regex-extractor false
    positive (not an actual entity reference)."""
    if not form or len(form.strip()) < _MIN_FORM_LENGTH:
        return True
    s = form.strip()
    return any(p.match(s) for p in _FRAGMENT_PATTERNS)


# ─── Schema bootstrap ──────────────────────────────────────────────────

def _ensure_table() -> None:
    """Create corpus_corroboration if it doesn't exist. Idempotent."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS corpus_corroboration (
                    corpus_id              TEXT NOT NULL,
                    surface_form_norm      TEXT NOT NULL,
                    representative_form    TEXT,
                    mention_count          INT NOT NULL,
                    document_count         INT NOT NULL,
                    dataset_count          INT NOT NULL,
                    is_fragment            BOOLEAN NOT NULL DEFAULT FALSE,
                    corroboration_score    REAL NOT NULL,
                    suggested_type         TEXT,
                    last_scored_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (corpus_id, surface_form_norm)
                );
                CREATE INDEX IF NOT EXISTS idx_corroboration_score
                    ON corpus_corroboration(corpus_id, corroboration_score DESC);
                CREATE INDEX IF NOT EXISTS idx_corroboration_docs
                    ON corpus_corroboration(corpus_id, document_count DESC);
                """
            )
        conn.commit()


# ─── Scoring ───────────────────────────────────────────────────────────

def _score_row(
    mention_count: int, doc_count: int, dataset_count: int, is_fragment: bool,
) -> float:
    """The actual score function. Tunable knobs centralized here."""
    raw = float(mention_count)
    source_boost = math.log2(doc_count + 1)
    diversity_boost = math.log2(dataset_count + 1)
    base = raw * source_boost * diversity_boost
    if is_fragment:
        return base - 3.0
    return base


def score_corpus(corpus_id: str) -> dict:
    """Compute corroboration scores for every surface form in a corpus.
    Stores results in corpus_corroboration. Re-run after new mentions land."""
    _ensure_table()

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Pull surface-form rollups joined to docs (for dataset diversity).
            # We group on the SAME corpus_id — the dataset_count is # of
            # distinct dataset_ids the form appears under, which is currently
            # always 1 inside a single corpus_id, but the score function
            # supports cross-corpus scoring if we generalize the query later.
            cur.execute(
                """
                SELECT m.surface_form_norm,
                       MAX(m.surface_form) AS sample,
                       MAX(m.suggested_type) AS suggested_type,
                       COUNT(*) AS mention_count,
                       COUNT(DISTINCT m.document_id) AS doc_count,
                       COUNT(DISTINCT d.dataset_id) AS dataset_count
                FROM document_mentions m
                JOIN documents d ON d.document_id = m.document_id
                WHERE d.dataset_id = %s
                  AND m.surface_form_norm IS NOT NULL
                GROUP BY m.surface_form_norm
                """,
                (corpus_id,),
            )
            rows = cur.fetchall()

    if not rows:
        return {"corpus_id": corpus_id, "ok": True, "reason": "no mentions",
                "rows_written": 0}

    out_rows: list[tuple] = []
    for surface_norm, sample, suggested_type, mc, dc, ds in rows:
        is_frag = _looks_like_fragment(sample or "")
        score = _score_row(mc, dc, ds, is_frag)
        out_rows.append((
            corpus_id, surface_norm, sample, mc, dc, ds, is_frag,
            score, suggested_type,
        ))

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Wipe and replace for this corpus — scoring is non-incremental.
            cur.execute(
                "DELETE FROM corpus_corroboration WHERE corpus_id = %s",
                (corpus_id,),
            )
            cur.executemany(
                """
                INSERT INTO corpus_corroboration
                    (corpus_id, surface_form_norm, representative_form,
                     mention_count, document_count, dataset_count,
                     is_fragment, corroboration_score, suggested_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                out_rows,
            )
        conn.commit()

    n_total = len(out_rows)
    n_fragments = sum(1 for r in out_rows if r[6])
    return {
        "corpus_id": corpus_id,
        "ok": True,
        "rows_written": n_total,
        "fragment_rows": n_fragments,
        "non_fragment_rows": n_total - n_fragments,
    }


def top_corroborated(
    corpus_id: str,
    *,
    limit: int = 50,
    include_fragments: bool = False,
    min_documents: int = 1,
) -> list[dict]:
    """Return the highest-corroboration surface forms for a corpus.

    include_fragments=False (default) drops obvious regex false positives.
    min_documents=N filters to forms appearing in at least N distinct docs."""
    where = ["corpus_id = %s", "document_count >= %s"]
    params: list = [corpus_id, min_documents]
    if not include_fragments:
        where.append("is_fragment = FALSE")
    sql = f"""
        SELECT representative_form, surface_form_norm, mention_count,
               document_count, dataset_count, corroboration_score,
               is_fragment, suggested_type
        FROM corpus_corroboration
        WHERE {' AND '.join(where)}
        ORDER BY corroboration_score DESC, mention_count DESC
        LIMIT %s
    """
    params.append(limit)
    out: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                out.append({
                    "form": r[0],
                    "form_norm": r[1],
                    "mention_count": r[2],
                    "document_count": r[3],
                    "dataset_count": r[4],
                    "score": round(float(r[5]), 2),
                    "is_fragment": r[6],
                    "suggested_type": r[7],
                })
    return out
