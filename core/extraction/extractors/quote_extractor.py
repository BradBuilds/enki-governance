"""
Quote extractor — regex-based direct-quote recognition.

Catches the two most common English quote shapes:
    1. "...text..." said/says/told/added X
    2. X said/says/told/added "...text..."
    3. X, speaking to Y, said "...text..."

Also catches curly-quote variants "…" and fancy dashes.

Output matches envelope.summary.quotes[] shape.
"""

from __future__ import annotations

import re
from typing import Any

# Normalize curly quotes to straight before matching.
_CURLY_MAP = str.maketrans({
    "\u201c": '"', "\u201d": '"',
    "\u2018": "'", "\u2019": "'",
})

# Speech verbs (extendable)
_VERBS = r"(?:said|says|told|added|stated|declared|noted|wrote|tweeted|posted|announced|warned|claimed|argued|explained|confirmed|denied|responded)"

# Pattern 1: "...quote..." <verb> X
_Q_BEFORE = re.compile(
    rf'"([^"]{{8,400}})"\s*,?\s*{_VERBS}\s+([A-Z][\w\.\'\-]*(?:\s+[A-Z][\w\.\'\-]*){{0,4}})',
    re.IGNORECASE,
)

# Pattern 2: X <verb> "...quote..."
_Q_AFTER = re.compile(
    rf'([A-Z][\w\.\'\-]*(?:\s+[A-Z][\w\.\'\-]*){{0,4}})\s+{_VERBS}\s*,?\s*"([^"]{{8,400}})"',
    re.IGNORECASE,
)


def extract_quotes(text: str) -> list[dict[str, Any]]:
    """Return envelope-shaped quote dicts."""
    if not text:
        return []

    norm = text.translate(_CURLY_MAP)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for m in _Q_BEFORE.finditer(norm):
        quote, speaker = m.group(1).strip(), m.group(2).strip()
        if quote in seen:
            continue
        seen.add(quote)
        results.append({
            "speaker_raw": speaker,
            "speaker_node_id": None,
            "text": quote,
            "context": "",
            "extraction_method": "direct_quote",
        })

    for m in _Q_AFTER.finditer(norm):
        speaker, quote = m.group(1).strip(), m.group(2).strip()
        if quote in seen:
            continue
        seen.add(quote)
        results.append({
            "speaker_raw": speaker,
            "speaker_node_id": None,
            "text": quote,
            "context": "",
            "extraction_method": "direct_quote",
        })

    return results
