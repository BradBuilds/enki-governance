"""
Garbage title detector — heuristics for metadata that can't be trusted.

The "Article 4567" failure mode: some feeds (Hacker News, some aggregators,
some RSS scrapers) produce titles that carry no information. When the
cheap pass detects a garbage title, the runner force-triggers the deep
pass so the LLM can synthesize a real title from the body text.

Rules implemented (all case-insensitive):
    1. Empty / whitespace-only
    2. Too short (< 10 chars after trim, unless CJK where single chars count)
    3. Numeric-only or mostly numeric
    4. Starts with literal 'untitled', 'item ', 'article ', 'post '
    5. Looks like a URL or path fragment
    6. "Show HN: N..." ellipsis-truncated HN pattern
    7. All-ASCII-punctuation
    8. Bare filename extension
"""

from __future__ import annotations

import re

_URL_FRAGMENT = re.compile(r"^(https?://|/|www\.|[a-z0-9_-]+\.(com|org|net|io|co)/)", re.I)
_MOSTLY_DIGITS = re.compile(r"^[\s\d\W]{1,20}$")
_ELLIPSIS_TRUNC = re.compile(r"\.{3}\s*$|…\s*$")
_BARE_EXT = re.compile(r"^[\w\-]+\.(pdf|html|htm|txt|doc|docx)$", re.I)


def is_garbage_title(title: str | None) -> tuple[bool, str]:
    """Return (is_garbage, reason). reason is empty if not garbage."""
    if not title:
        return True, "empty"

    t = title.strip()
    if not t:
        return True, "whitespace_only"

    low = t.lower()

    # 1. Too short (ignoring CJK/single-char scripts)
    if len(t) < 10 and not any("\u4e00" <= c <= "\u9fff" for c in t):
        return True, "too_short"

    # 2. Numeric or punctuation-only
    if _MOSTLY_DIGITS.match(t):
        return True, "numeric_only"

    # 3. Placeholder prefixes
    for prefix in ("untitled", "item ", "article ", "post ", "entry "):
        if low.startswith(prefix):
            rest = low[len(prefix):].strip()
            if not rest or rest.replace("#", "").strip().isdigit():
                return True, f"placeholder_{prefix.strip().replace(' ','_')}"

    # 4. URL / path fragment
    if _URL_FRAGMENT.match(low):
        return True, "url_fragment"

    # 5. Ellipsis-truncated (very common HN pattern: "Show HN: N...")
    if _ELLIPSIS_TRUNC.search(t) and len(t) < 40:
        return True, "ellipsis_truncated"

    # 6. All punctuation
    if all(not c.isalnum() for c in t):
        return True, "punctuation_only"

    # 7. Bare filename with extension
    if _BARE_EXT.match(t):
        return True, "bare_filename"

    return False, ""
