"""Pre-provider candidate filter.

Cheap deterministic rejection for candidates that CAN'T be standalone
entities regardless of what an external source says. Runs before any
network call so the provider chain never wastes effort on obvious junk.

Policy:
  - Emails, phone numbers, URLs, hashes → NOT entities themselves, but
    they ARE valid identifiers of some other entity. The resolver can
    attach them as identifiers.email / identifiers.phone / etc.
  - Hashtags (#Chinook) → queue for topic linkage, not entity
  - Social handles (@USArmy) → queue for handle-resolution (maps to real org)
  - Single stop-words, pure punctuation, pure numbers → drop entirely
  - Very short strings (<2 chars) → drop
"""
from __future__ import annotations

import re
from typing import Optional

from core.entity_validation.types import Candidate, CandidateDecision

# ── detectors ─────────────────────────────────────────────────────────
# All case-insensitive; all operate on candidate.raw_text stripped.

_EMAIL     = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.I)
_URL       = re.compile(r"^(https?://|www\.)", re.I)
_PHONE     = re.compile(r"^\+?[\d\-.\s()]{7,}$")
_HASHTAG   = re.compile(r"^#\w+$")
_HANDLE    = re.compile(r"^@[\w\.]+$")
_NUMERIC   = re.compile(r"^[\d.,\s\-+]+$")
_HASH_HEX  = re.compile(r"^[0-9a-f]{16,}$", re.I)

_STOP_WORDS = {
    "the", "a", "an", "this", "that", "these", "those",
    "he", "she", "it", "they", "we", "you", "i",
    "who", "what", "when", "where", "why", "how",
    "is", "are", "was", "were", "be", "been",
    "and", "or", "but", "if", "then",
    "of", "in", "on", "at", "to", "for", "by",
}


def filter_candidate(cand: Candidate) -> CandidateDecision:
    """Inspect the candidate and decide whether it should be passed to
    the provider chain. Returns an accept/reject decision plus, if
    rejected for being a valid identifier-of-something, a side-channel
    identifier payload the resolver can attach elsewhere."""
    text = (cand.raw_text or "").strip()

    if len(text) < 2:
        return CandidateDecision(accept=False, reason="too_short")

    if text.lower() in _STOP_WORDS:
        return CandidateDecision(accept=False, reason="stop_word_only")

    if _EMAIL.match(text):
        return CandidateDecision(
            accept=False, reason="looks_like_email",
            as_identifier={"type": "email", "value": text.lower()},
        )

    if _URL.match(text):
        return CandidateDecision(
            accept=False, reason="looks_like_url",
            as_identifier={"type": "url", "value": text},
        )

    # Phone check runs before numeric check, because a phone pattern is a
    # superset of a pure-numeric string. Only treat as phone if the string
    # has ≥7 digits (rules out model names like "F-35" or "CH-53").
    digit_count = sum(1 for c in text if c.isdigit())
    if _PHONE.match(text) and digit_count >= 7:
        return CandidateDecision(
            accept=False, reason="looks_like_phone",
            as_identifier={"type": "phone", "value": text},
        )

    if _NUMERIC.match(text):
        return CandidateDecision(accept=False, reason="numeric_only")

    if _HASH_HEX.match(text):
        return CandidateDecision(
            accept=False, reason="looks_like_hash",
            as_identifier={"type": "content_hash", "value": text.lower()},
        )

    if _HASHTAG.match(text):
        # Strip leading # and pass as topic candidate instead
        return CandidateDecision(
            accept=False, reason="looks_like_hashtag",
            as_identifier={"type": "hashtag", "value": text[1:]},
        )

    if _HANDLE.match(text):
        # Strip leading @ — the handle belongs to SOME entity; resolver
        # needs to find which one (Twitter/X lookup, known-accounts cache)
        return CandidateDecision(
            accept=False, reason="looks_like_handle",
            as_identifier={"type": "social_handle", "value": text[1:].lower(),
                           "platform_hint": "twitter"},
        )

    # Survived every negative filter — pass through to providers
    return CandidateDecision(accept=True, reason="plausible")
