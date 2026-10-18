"""
SimHash — locality-sensitive hash for paragraph dedup / boilerplate clustering.

Paragraphs that are textually similar produce similar 64-bit signatures.
Hamming distance between two SimHashes < ~3 → very likely the same template.
< 8 → likely template variants. > 12 → independent content.

Used by:
  - core/documents/ingester.py     (stamps simhash on every paragraph)
  - core/documents/boilerplate.py  (clusters paragraphs across a corpus)
  - core/documents/pdf_ingester.py (pre-computes per paragraph during ingest)

We keep this pure-Python (no `simhash` package dependency) because it's
called once per paragraph and the implementation is ~30 lines.
"""

from __future__ import annotations

import hashlib
import re

_WORD_RE = re.compile(r"\w+")
_BITS = 64
_MASK = (1 << _BITS) - 1


def _shingles(text: str, n: int = 3) -> list[str]:
    """N-gram shingles. n=3 gives a good signal/noise balance for English
    legal text and news prose."""
    words = _WORD_RE.findall((text or "").lower())
    if len(words) < n:
        return words
    return [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]


def _token_hash(s: str) -> int:
    """64-bit hash of a token. SHA-256 truncated for determinism + spread."""
    h = hashlib.sha256(s.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")


def simhash(text: str, shingle_n: int = 3) -> int:
    """Compute a 64-bit SimHash for `text`. Returns 0 for empty/no-content text."""
    tokens = _shingles(text, shingle_n)
    if not tokens:
        return 0

    # Accumulator: for each bit position, sum +1 if set in token hash, else -1.
    accumulator = [0] * _BITS
    for tok in tokens:
        h = _token_hash(tok)
        for i in range(_BITS):
            if (h >> i) & 1:
                accumulator[i] += 1
            else:
                accumulator[i] -= 1

    # Final bit is set iff accumulator > 0 at that position.
    out = 0
    for i in range(_BITS):
        if accumulator[i] > 0:
            out |= (1 << i)
    return out


def hamming(a: int, b: int) -> int:
    """Count bits that differ between two SimHashes (0..64)."""
    return bin((a ^ b) & _MASK).count("1")


def signed_64(unsigned: int) -> int:
    """Postgres BIGINT is signed; convert our 64-bit unsigned SimHash to the
    signed range that fits in BIGINT."""
    if unsigned >= (1 << 63):
        return unsigned - (1 << 64)
    return unsigned


def unsigned_64(signed: int) -> int:
    """Convert a Postgres BIGINT back to our unsigned working representation."""
    if signed < 0:
        return signed + (1 << 64)
    return signed
