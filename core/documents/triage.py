"""
Document extraction triage — classify each PDF before extraction so the
right extractor runs against it.

Phase 1 of the two-tier doc pipeline. Tier 1 (this module) examines what
we already have on disk + in document_pages and decides:

  born_digital          → pdfplumber works (default)
  scanned_pure          → needs PaddleOCR
  scanned_with_overlay  → mixed-source FBI/court docs; OCR for body,
                           pdfplumber for cover sheets
  redaction_index       → "DELETED PAGE INFORMATION SHEET" docs; only
                           the index pages are readable, no body to extract
  mixed                 → page-by-page routing required

Tier 2 (the re-extractor) reads extraction_quality_class and routes
accordingly.

The classifier deliberately works off existing document_pages text (not
the raw PDF) so it can run after any extractor pass. Recurring re-triage
is fine — the verdict is stored idempotently on documents.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.triage")


# Patterns that mark a page as part of an FBI FOIA redaction-cover sheet.
_REDACTION_OVERLAY_PATTERNS = (
    "DELETED PAGE INFORMATION SHEET",
    "FEDERAL BUREAU OF INVESTIGATION",
    "FOI/PA",
    "Civil Action#",
    "Total Deleted",
    "b3, b6, b7C",
    "b7C - 1, - 1, b7D",
)

# PDF font-encoding artefacts (pdfplumber falls back to these when there's
# no real text layer). High density of (cid: tokens means OCR is needed.
_CID_RE = re.compile(r"\(cid:\d+\)")

# Long runs of single-char-then-space sequences ("F EDER AL BURE AU") —
# PDF text fragmentation from low-quality embedded text layers.
_FRAGMENTED_RE = re.compile(r"(?:[A-Za-z] ){5,}")


def _page_stats(text: str) -> dict:
    """Per-page diagnostics used by the document-level classifier."""
    if not text:
        return {"chars": 0, "cid_density": 0.0, "frag_density": 0.0,
                "redaction_overlay": False, "alpha_ratio": 0.0}
    chars = len(text)
    cid_hits = len(_CID_RE.findall(text))
    frag_hits = len(_FRAGMENTED_RE.findall(text))
    overlay = any(p in text for p in _REDACTION_OVERLAY_PATTERNS)
    alpha = sum(1 for c in text if c.isalpha())
    return {
        "chars": chars,
        "cid_density": cid_hits / max(1, chars / 1000),  # hits per 1k chars
        "frag_density": frag_hits / max(1, chars / 1000),
        "redaction_overlay": overlay,
        "alpha_ratio": alpha / max(1, chars),
    }


def classify_doc(document_id: int) -> dict:
    """Read document_pages for this doc and return a triage verdict +
    supporting evidence. Does NOT write to the documents row — pass the
    result to ``persist_triage`` to commit.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT page_number, raw_text
                     FROM document_pages
                    WHERE document_id = %s
                    ORDER BY page_number""",
                (document_id,),
            )
            pages = cur.fetchall()

    if not pages:
        return {"verdict": "pending",
                "evidence": {"reason": "no pages"},
                "page_count": 0}

    # Per-page stats
    page_diag = [(pn, _page_stats(t or "")) for pn, t in pages]
    n_pages = len(page_diag)

    redaction_pages = sum(1 for _, s in page_diag if s["redaction_overlay"])
    empty_pages = sum(1 for _, s in page_diag if s["chars"] < 50)
    low_alpha_pages = sum(1 for _, s in page_diag
                          if s["chars"] > 0 and s["alpha_ratio"] < 0.55)
    cid_heavy_pages = sum(1 for _, s in page_diag if s["cid_density"] > 3)
    fragmented_pages = sum(1 for _, s in page_diag if s["frag_density"] > 2)
    avg_chars = sum(s["chars"] for _, s in page_diag) / max(1, n_pages)

    evidence = {
        "n_pages": n_pages,
        "avg_chars_per_page": round(avg_chars, 1),
        "redaction_overlay_pages": redaction_pages,
        "empty_pages": empty_pages,
        "low_alpha_pages": low_alpha_pages,
        "cid_heavy_pages": cid_heavy_pages,
        "fragmented_pages": fragmented_pages,
    }

    # Verdict ladder — first match wins. Order matters.
    if n_pages > 0 and redaction_pages / n_pages > 0.4:
        # ≥40% pages are FBI redaction cover-sheets → no real body content
        verdict = "redaction_index"
    elif avg_chars < 50:
        verdict = "scanned_pure"
    elif cid_heavy_pages / n_pages > 0.15 or fragmented_pages / n_pages > 0.15:
        # Text layer present but >15% pages are garbled — pdfplumber chose
        # the wrong font map. PaddleOCR usually wins on these.
        verdict = "scanned_with_overlay"
    elif low_alpha_pages / n_pages > 0.4 and avg_chars < 500:
        verdict = "scanned_with_overlay"
    elif redaction_pages / n_pages > 0.1:
        # 10-40% redaction overlay pages → mixed body + FBI cover sheets
        verdict = "mixed"
    elif empty_pages > 0 and empty_pages / n_pages > 0.3:
        verdict = "mixed"
    else:
        verdict = "born_digital"

    return {"verdict": verdict, "evidence": evidence, "page_count": n_pages}


def persist_triage(document_id: int, verdict: str, evidence: dict) -> None:
    """Write the triage verdict back to documents."""
    import json
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE documents
                      SET extraction_quality_class = %s,
                          triage_decided_at        = NOW(),
                          triage_evidence          = %s::jsonb
                    WHERE document_id = %s""",
                (verdict, json.dumps(evidence), document_id),
            )
        conn.commit()


def triage_one(document_id: int) -> dict:
    """Classify + persist for a single doc."""
    result = classify_doc(document_id)
    persist_triage(document_id, result["verdict"], result["evidence"])
    log.info("triage[%d]: %s (%s)", document_id, result["verdict"],
             result["evidence"])
    return result


def triage_pending(limit: int = 100) -> dict:
    """Walk pending docs (or all docs if no triage yet) and classify each.
    Returns a per-class count summary."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT document_id FROM documents
                    WHERE content_sha256 IS NOT NULL
                      AND (extraction_quality_class = 'pending'
                           OR triage_decided_at IS NULL)
                    ORDER BY document_id
                    LIMIT %s""",
                (limit,),
            )
            doc_ids = [r[0] for r in cur.fetchall()]

    summary = {"born_digital": 0, "scanned_pure": 0,
               "scanned_with_overlay": 0, "redaction_index": 0,
               "mixed": 0, "pending": 0, "errors": 0}
    for did in doc_ids:
        try:
            r = triage_one(did)
            summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
        except Exception as e:
            log.exception("triage crashed for doc %d", did)
            summary["errors"] += 1

    summary["total_processed"] = len(doc_ids)
    return summary


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        # Single doc
        import json
        print(json.dumps(triage_one(int(sys.argv[1])), indent=2))
    else:
        # Pending batch
        limit = int(sys.argv[1]) if len(sys.argv) > 1 else 100
        import json
        print(json.dumps(triage_pending(limit), indent=2))
