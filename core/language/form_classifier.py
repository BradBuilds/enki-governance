"""
Surface-form classifier — heuristic, deterministic, no LLM.

Maps a surface form string to one of the eight `form` subtypes defined
in nodevocab v0.5.0:

    noun, verb, adjective, function_word, initialism,
    number_word, name_token, unknown

The classifier is intentionally cheap. It runs over every distinct
surface_form_norm in document_mentions during backfill, and on every
new occurrence at extraction time. LLM classification is reserved for
hard cases that get routed to the analyst queue.

The cyclical model assumption: the classifier produces a *prior*. The
final resolution (which address this occurrence points to) is decided
by the collapse layer with context, and recorded in collapse_decisions.
So mis-classification at this layer is corrigible — the form can be
re-subtyped if downstream evidence demands it.
"""

from __future__ import annotations

import re
from typing import Optional


# ─────────────────────────────────────────────────────────────────────
# Closed-class lexicons. Small, deterministic, English-first.
# Anything not in these falls through to open-class heuristics.
# ─────────────────────────────────────────────────────────────────────

_PRONOUNS = frozenset({
    "i", "me", "my", "mine", "myself",
    "you", "your", "yours", "yourself", "yourselves",
    "he", "him", "his", "himself",
    "she", "her", "hers", "herself",
    "it", "its", "itself",
    "we", "us", "our", "ours", "ourselves",
    "they", "them", "their", "theirs", "themselves",
    "this", "that", "these", "those",
    "who", "whom", "whose", "which", "what",
    "anyone", "someone", "everyone", "no one", "nobody",
    "anything", "something", "everything", "nothing",
    "anybody", "somebody", "everybody",
})

_DETERMINERS = frozenset({
    "a", "an", "the",
    "this", "that", "these", "those",
    "each", "every", "all", "some", "any", "no", "none",
    "both", "either", "neither",
    "much", "many", "more", "most", "less", "least", "few", "several",
    "my", "your", "his", "her", "its", "our", "their",
})

_PREPOSITIONS = frozenset({
    "of", "in", "on", "at", "to", "from", "by", "with", "without",
    "for", "about", "against", "between", "into", "through", "during",
    "before", "after", "above", "below", "over", "under", "around",
    "near", "next", "off", "out", "up", "down", "across",
})

_CONJUNCTIONS = frozenset({
    "and", "or", "but", "nor", "yet", "so",
    "because", "since", "although", "though", "if", "unless",
    "while", "whereas", "however", "therefore", "moreover", "also",
    "actually", "then",
})

_AUX_VERBS = frozenset({
    "is", "am", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "having",
    "do", "does", "did", "doing", "done",
    "will", "would", "shall", "should",
    "can", "could", "may", "might", "must",
    "ought", "need", "dare",
})

# Combined function-word set
_FUNCTION_WORDS = _PRONOUNS | _DETERMINERS | _PREPOSITIONS | _CONJUNCTIONS | _AUX_VERBS

# Hedging / discourse / turn-taking (from language.pragmatics)
_DISCOURSE_MARKERS = frozenset({
    "well", "so", "now", "actually", "really", "basically",
    "maybe", "perhaps", "sort", "kind",
})
_FUNCTION_WORDS = _FUNCTION_WORDS | _DISCOURSE_MARKERS


# Cardinal / ordinal number words
_NUMBER_WORDS = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
    "thousand", "million", "billion", "trillion",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "eleventh", "twelfth", "twentieth",
    "thirtieth", "fortieth", "fiftieth", "hundredth", "thousandth",
    "single", "double", "triple", "quadruple",
    "once", "twice", "thrice",
    "half", "quarter",
})


# Name-particle tokens (capitalized fragments that are usually part of
# a larger person/place name). When these appear standalone they should
# go to name_token rather than being treated as full names.
_NAME_PARTICLES = frozenset({
    "san", "von", "van", "der", "den", "de", "del", "la", "le",
    "el", "al", "mc", "mac", "o'", "st", "saint",
    "ibn", "bin", "abu", "abdul",
    "jr", "sr", "ii", "iii", "iv",
})


_DIGIT_RE = re.compile(r"^\d+([.,]\d+)*$")
_INITIALISM_RE = re.compile(r"^[A-Z][A-Z0-9.&\-]{0,8}$")
_ALL_CAPS_WORD_RE = re.compile(r"^[A-Z]{2,}$")
_CAPITALIZED_RE = re.compile(r"^[A-Z][a-z]+([-' ][A-Z][a-z]+)*$")


def classify_form(surface_norm: str, surface_display: Optional[str] = None) -> str:
    """Classify a surface form to one of eight subtypes.

    surface_norm: lowercase / trimmed form (the dedup key)
    surface_display: original-case form, used for capitalization signal
                     (falls back to surface_norm if not provided)

    Returns one of:
      noun, verb, adjective, function_word, initialism,
      number_word, name_token, unknown
    """
    if not surface_norm:
        return "unknown"

    raw = surface_norm.strip()
    if not raw:
        return "unknown"

    # Mention surface_form_norm is stored UPPERCASE in this codebase
    # (see ingester / extractor convention). Lowercase for closed-set
    # lookups so 'THIS' / 'this' / 'This' all classify identically.
    s = raw.lower()
    display = (surface_display or raw).strip()

    # Digit-only tokens are numbers
    if _DIGIT_RE.match(s):
        return "number_word"

    # Number words (closed set)
    if s in _NUMBER_WORDS:
        return "number_word"

    # Function words (closed set, pronouns/determiners/prepositions/
    # conjunctions/aux/discourse markers)
    if s in _FUNCTION_WORDS:
        return "function_word"

    # Name particles
    if s in _NAME_PARTICLES:
        return "name_token"

    # Multi-word: classify by whether all tokens look name-like
    if " " in display:
        toks = display.split()
        # Accept either CamelCase or ALL CAPS tokens as name-like
        def _looks_name_like(t: str) -> bool:
            if not t:
                return False
            if t.lower() in _NAME_PARTICLES:
                return True
            if _CAPITALIZED_RE.match(t):
                return True
            if _ALL_CAPS_WORD_RE.match(t):
                return True
            return False
        if len(toks) <= 5 and all(_looks_name_like(t) for t in toks):
            return "name_token"
        # Multi-word common phrase, keep as noun (compound noun)
        return "noun"

    # Initialism: short, all-caps in the DISPLAY form
    if 1 < len(display) <= 8 and _ALL_CAPS_WORD_RE.match(display.replace(".", "").replace("&", "").replace("-", "")):
        return "initialism"

    # Single capitalized OR all-caps token in the display form → name fragment
    if (_CAPITALIZED_RE.match(display) or _ALL_CAPS_WORD_RE.match(display)) and len(display) >= 3:
        # If it ends in common verb suffixes, prefer verb
        if s.endswith(("ing", "ed", "tion")):
            return "verb" if s.endswith("ing") or s.endswith("ed") else "noun"
        return "name_token"

    # Open-class heuristics by suffix
    # (deliberately small — these are priors, not ground truth)
    if s.endswith(("ly",)):
        return "adjective"
    if s.endswith(("ing", "ed")) and len(s) > 4:
        return "verb"
    if s.endswith(("tion", "sion", "ment", "ness", "ity", "ship", "hood", "dom")):
        return "noun"
    if s.endswith(("able", "ible", "al", "ous", "ive", "ful", "less")):
        return "adjective"
    if s.endswith(("ize", "ise", "ify", "ate")):
        return "verb"
    if s.endswith(("er", "or", "ist")) and len(s) > 4:
        return "noun"

    # Default for unmarked alphabetic tokens
    return "noun"


def display_form(surface_norm: str, samples: list[str]) -> str:
    """Pick a display form from observed surface variants.

    Prefer the variant that:
      1. starts with a capital letter (more name-like, more readable)
      2. otherwise the most common variant

    `samples` are the actual surface_form values seen (not normalized).
    `surface_norm` is the dedup key (lowercase).
    """
    if not samples:
        return surface_norm
    # Find a capitalized variant if any
    for v in samples:
        if v and v[0].isupper():
            return v
    return samples[0]
