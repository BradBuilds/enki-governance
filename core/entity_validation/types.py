"""Type definitions shared across the validation module."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Candidate:
    """A thing extraction wants to turn into a node, before it has one."""
    raw_text: str                       # what the extractor saw verbatim
    kind: str                           # "entity" | "place" | "topic"
    hint_type: Optional[str] = None     # extractor's best-guess type
    context: Optional[str] = None       # surrounding sentence(s), for disambiguation
    identifiers: dict = field(default_factory=dict)    # already-extracted MMSI, email, etc.


# Reasons the filter rejects a candidate before it hits a provider.
# The worker stores the reason on the entity row so the UI can render
# the right badge instead of silently swallowing the entity.
FILTER_REJECTIONS = {
    "looks_like_email",
    "looks_like_url",
    "looks_like_phone",
    "looks_like_hashtag",
    "looks_like_handle",
    "too_short",
    "stop_word_only",
    "numeric_only",
}


@dataclass
class CandidateDecision:
    """Result of the pre-provider filter."""
    accept: bool
    # Reason for rejection or acceptance note
    reason: str
    # If the candidate was rejected but the value IS meaningful as an
    # identifier on an OTHER entity, we surface it here. The resolver
    # can attach it as identifiers.email / identifiers.handle on the
    # entity that actually owns the quote/mention.
    as_identifier: Optional[dict] = None    # {type: "email", value: "x@y.z"}


@dataclass
class Match:
    """A provider confirmed the candidate."""
    source: str                         # "wikidata", "wikipedia", "geonames", "llm", "internal_cache"
    source_id: str                      # "Q66", "12345", etc — provider-specific
    source_url: str                     # link the UI can show
    canonical_name: str                 # authoritative name
    canonical_type: str                 # mapped to our taxonomy (person, organization, aircraft, …)
    aliases: list[str] = field(default_factory=list)
    identifiers: dict = field(default_factory=dict)
    confidence: float = 0.0             # 0.0 – 1.0
    description: Optional[str] = None
    thumbnail_url: Optional[str] = None
    extra: dict = field(default_factory=dict)    # provider-specific extras (wikidata P-values, etc.)
