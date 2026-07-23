"""Deterministic base-60 addressing for lexical senses.

THE crux of the cross-node language model: a sense's address is derived from the
LIBRARY's own stable sense id — not a node-local counter — so two nodes that load
the same library put the same sense at the same address, and their collapse models
merge by SUM (not retrain). Mirrors the form layer (form_address = 11.0.<sha16>)
and the entity ckey lesson: identity is content, never a count.

Address format:  12.<pos_index>.<sha16(library_id:sense_id)>
  - 12        = concept category (per migration 093)
  - pos_index = stable base-60 digit for the part of speech / sense class
  - sha16     = first 16 hex of sha256(library_id ':' library_sense_id)

Library-less (LLM-synthesized) senses have no stable id → they get a PROVISIONAL
address from the node-local allocator elsewhere and cannot federate until a library
anchors them. This module only mints the deterministic, federatable kind.
"""
from __future__ import annotations

import hashlib

CONCEPT_CATEGORY = 12  # the concept layer (migration 093)

# Part-of-speech / sense-class → stable base-60 digit. Order is LOCKED (changing
# it moves addresses). 'other' is the catch-all parking slot.
POS_INDEX: dict[str, int] = {
    "noun": 0, "verb": 1, "adjective": 2, "adverb": 3,
    "function_word": 4, "proper_noun": 5, "phrase": 6,
    "numeral": 7, "interjection": 8, "other": 9,
}

# Governed register taxonomy (schema-first in the pack; this mirrors it for the
# fail-closed write-time check). A sense whose register isn't here is rejected.
REGISTER_TAXONOMY: frozenset[str] = frozenset({
    "literal", "slang", "technical", "proper_noun", "idiom", "archaic", "regional",
})


def _h16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def sense_address(library_id: str, sense_id: str, pos: str = "other") -> str:
    """Deterministic concept address for a library-anchored sense.

    Same (library_id, sense_id, pos) → same address on every node, every run."""
    sub = POS_INDEX.get((pos or "other").strip().lower(), POS_INDEX["other"])
    return f"{CONCEPT_CATEGORY}.{sub}.{_h16(f'{library_id}:{sense_id}')}"


def is_governed_register(register: str | None) -> bool:
    return (register or "").strip().lower() in REGISTER_TAXONOMY


def is_governed_pos(pos: str | None) -> bool:
    return (pos or "").strip().lower() in POS_INDEX
