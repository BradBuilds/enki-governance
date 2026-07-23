"""
Reference acquisition worker.

Walk pending bibliographic_references and try each source in order
to acquire a PDF. On success: download, mark acquired, write file
metadata, queue for ingest. On failure: mark unavailable, set next
retry date.

Try-order:
  1. archive.org (if we have an archive_id, or a search-by-title hit)
  2. Direct URL in identifiers["url"] if it looks like a PDF
  3. Google Books free preview (rarely a true PDF)
  4. (Future: scholar / publisher OA / FOIA archives)

Each attempt is HTTP-bounded with timeouts. Files land under
/tmp/enki_acquired/<library_id>/<reference_id>__<slug>.pdf

After download, mark acquisition_status='acquired' and let the ingest
worker pick it up by walking acquired-but-not-yet-ingested rows.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, "/app")

from core.discovery import archive_org
from storage.postgres.db import get_connection

log = logging.getLogger("enki.discovery.acquirer")

_ACQUIRED_ROOT = Path("/tmp/enki_acquired")
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_HOURS = [24, 168, 720]   # 1d / 1w / 1mo
_BETWEEN_ITEMS_DELAY_S = 0.5


def _slug(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s or "ref").strip("_")
    return s[:max_len] or "ref"


def _now_iso():
    return datetime.now(timezone.utc)


_TITLE_TOKEN_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "for", "to",
    "with", "by", "from", "at", "as", "is", "are", "was", "were",
    "vol", "volume", "part", "no", "ed", "edition", "book", "books",
    "report", "study", "article", "paper", "draft", "final",
}


def _title_overlap_score(want: str, got: str) -> tuple[int, float]:
    """Return (shared_token_count, asymmetric_jaccard). Asymmetric so a
    short legitimate candidate title isn't penalised just for being
    shorter than the reference's verbose title. Stopwords and tokens
    shorter than 3 chars are dropped."""
    def _toks(s: str) -> set[str]:
        import re as _re
        return {
            t for t in _re.findall(r"[a-z0-9]{3,}", (s or "").lower())
            if t not in _TITLE_TOKEN_STOPWORDS
        }
    a, b = _toks(want), _toks(got)
    if not a or not b:
        return (0, 0.0)
    shared = len(a & b)
    return (shared, shared / min(len(a), len(b)))


# Acceptance rule: must share ≥2 meaningful tokens AND ≥25% of the smaller
# title's tokens. Two tokens is the floor because single-word matches
# (e.g. just "Acme") are too weak — many books have one word in
# common with the reference. 25% asymmetric Jaccard means even a short
# legitimate match ("Harold Whitcombe: A Life in Finance" vs a verbose
# reference) clears the bar.
_MIN_SHARED_TOKENS = 2
_MIN_ASYM_JACCARD  = 0.25


def _attempt_archive_org(ref: dict, dest: Path) -> Optional[dict]:
    """Try archive.org. Either we have an archive_id directly (trust it),
    or we search by title — in which case we MUST verify the candidate's
    title overlaps the reference title before downloading. Without the
    overlap check, the first search hit was downloaded blindly even
    when it was a totally different book (e.g. requesting "Howard
    Whitcombe Acme Neighbor Tether" downloaded "Donald Barr — Space
    Relations.pdf" because that was archive.org's first result for
    those keywords)."""
    ident = (ref.get("identifiers") or {}).get("archive_id")
    if not ident:
        title = ref.get("title")
        if not title:
            return None
        results = archive_org.search(title, max_results=5, mediatype="texts")
        # Walk candidates in order; accept the first that meets the
        # token-overlap floor.
        for cand in results:
            cand_title = cand.get("title") or ""
            shared, jacc = _title_overlap_score(title, cand_title)
            if shared >= _MIN_SHARED_TOKENS and jacc >= _MIN_ASYM_JACCARD:
                ident = (cand.get("identifiers") or {}).get("archive_id")
                log.info(
                    "archive_org match: ref %r -> %r (shared=%d, asymJ=%.2f, id=%s)",
                    title[:60], cand_title[:60], shared, jacc, ident,
                )
                break
            else:
                log.debug(
                    "archive_org reject: ref %r vs cand %r (shared=%d, asymJ=%.2f)",
                    title[:60], cand_title[:60], shared, jacc,
                )
    if not ident:
        return None
    result = archive_org.download(ident, dest)
    if result:
        result["method"] = "archive_org"
        return result
    return None


def _attempt_direct_url(ref: dict, dest: Path) -> Optional[dict]:
    """Try the identifiers.url if it looks like a PDF."""
    url = (ref.get("identifiers") or {}).get("url")
    if not url:
        return None
    if not url.lower().endswith(".pdf"):
        return None
    import hashlib
    import requests
    headers = {"User-Agent": "EnkiResearch/0.1 (intel-platform)"}
    dest.mkdir(parents=True, exist_ok=True)
    fname = _slug(url.rsplit("/", 1)[-1] or "direct.pdf")
    if not fname.endswith(".pdf"):
        fname += ".pdf"
    target = dest / fname
    sha = hashlib.sha256()
    size = 0
    try:
        with requests.get(url, stream=True, timeout=120, headers=headers) as r:
            if r.status_code != 200:
                return None
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "pdf" not in ctype and "octet-stream" not in ctype:
                return None
            with open(target, "wb") as fp:
                for chunk in r.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    fp.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
                    if size > 200_000_000:
                        target.unlink(missing_ok=True)
                        return None
    except Exception as e:
        log.debug("direct url download failed: %s", e)
        return None
    return {
        "method": "direct_url",
        "url": url,
        "file_path": str(target),
        "file_size": size,
        "file_sha256": sha.hexdigest(),
    }


_METHOD_ORDER = [
    ("archive_org", _attempt_archive_org),
    ("direct_url",  _attempt_direct_url),
]


def _next_retry_time(attempt: int) -> Optional[datetime]:
    if attempt >= _MAX_ATTEMPTS:
        return None
    hours = _RETRY_BACKOFF_HOURS[min(attempt, len(_RETRY_BACKOFF_HOURS) - 1)]
    return _now_iso() + timedelta(hours=hours)


def acquire_reference(reference_id: int) -> dict:
    """Acquire a single reference. Returns status dict."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT library_id, citation_text, title, identifiers,
                       acquisition_attempts, acquisition_status
                  FROM bibliographic_references WHERE reference_id = %s
                """,
                (reference_id,),
            )
            row = cur.fetchone()
    if not row:
        return {"reference_id": reference_id, "error": "not_found"}
    library_id, citation, title, identifiers, attempts, status = row

    if status not in ("pending",):
        return {"reference_id": reference_id, "status": status,
                "message": "not_pending_skipped"}

    ref = {
        "title":       title,
        "identifiers": identifiers or {},
        "citation_text": citation,
    }

    dest = _ACQUIRED_ROOT / str(library_id) / str(reference_id)
    dest.mkdir(parents=True, exist_ok=True)

    new_attempts = (attempts or 0) + 1
    result = None
    method_tried: list[str] = []
    for name, fn in _METHOD_ORDER:
        try:
            method_tried.append(name)
            r = fn(ref, dest)
            if r:
                result = r
                break
        except Exception as e:
            log.debug("acquisition method %s failed: %s", name, e)

    # Persist the original into the durable, content-addressed EVIDENCE store
    # (not /tmp, which clears and took our originals with it). The evidence path
    # becomes the document's on_disk_path, so ingest / OCR / page-imaging always
    # read a permanent, hash-verified, signed original — the SHA links every
    # derived fact back to the exact source file.
    if result:
        try:
            from core.evidence import store as evidence
            data = Path(result["file_path"]).read_bytes()
            ev = evidence.store_original(data, sha=result.get("file_sha256"),
                                         source_url=result.get("url"))
            result["file_path"] = ev["path"]
            log.info("evidence stored %s (signed=%s) for ref %s",
                     ev["sha256"][:16], ev["signed"], reference_id)
        except Exception as e:
            log.warning("evidence store failed for ref %s: %s", reference_id, e)

    with get_connection() as conn:
        with conn.cursor() as cur:
            if result:
                cur.execute(
                    """
                    UPDATE bibliographic_references
                       SET acquisition_status = 'acquired',
                           acquisition_attempts = %s,
                           last_attempted_at    = NOW(),
                           acquisition_method   = %s,
                           acquired_url         = %s,
                           file_path            = %s,
                           file_size_bytes      = %s,
                           file_sha256          = %s,
                           error_message        = NULL,
                           next_retry_at        = NULL
                     WHERE reference_id = %s
                    """,
                    (new_attempts, result["method"], result["url"],
                     result["file_path"], result["file_size"], result["file_sha256"],
                     reference_id),
                )
            else:
                next_retry = _next_retry_time(new_attempts)
                new_status = "unavailable" if next_retry is None else "pending"
                cur.execute(
                    """
                    UPDATE bibliographic_references
                       SET acquisition_status = %s,
                           acquisition_attempts = %s,
                           last_attempted_at    = NOW(),
                           next_retry_at        = %s,
                           error_message        = %s
                     WHERE reference_id = %s
                    """,
                    (new_status, new_attempts, next_retry,
                     f"no_method_succeeded:{','.join(method_tried)}",
                     reference_id),
                )
        conn.commit()
    return {
        "reference_id": reference_id,
        "success": bool(result),
        "method":  result["method"] if result else None,
        "size":    result.get("file_size") if result else None,
        "attempts": new_attempts,
    }


def run_queue(library_id: Optional[int] = None, limit: int = 50) -> dict:
    """Process up to `limit` pending references (optionally for a single library)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT reference_id FROM bibliographic_references
                 WHERE acquisition_status = 'pending'
                   AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                """
            params: list = []
            if library_id is not None:
                sql += " AND library_id = %s"
                params.append(library_id)
            sql += " ORDER BY reference_id LIMIT %s"
            params.append(limit)
            cur.execute(sql, params)
            ids = [r[0] for r in cur.fetchall()]

    stats = {"scanned": len(ids), "acquired": 0, "failed": 0, "methods": {}}
    for rid in ids:
        try:
            r = acquire_reference(rid)
            if r.get("success"):
                stats["acquired"] += 1
                m = r.get("method") or "unknown"
                stats["methods"][m] = stats["methods"].get(m, 0) + 1
            else:
                stats["failed"] += 1
        except Exception:
            log.exception("acquire failed for ref %d", rid)
            stats["failed"] += 1
        time.sleep(_BETWEEN_ITEMS_DELAY_S)
    return stats


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--library", type=int, default=None)
    p.add_argument("--limit", type=int, default=20)
    args = p.parse_args()
    print(json.dumps(run_queue(args.library, args.limit), indent=2))
