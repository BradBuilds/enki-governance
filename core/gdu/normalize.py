"""
core/gdu/normalize.py — pure name normalizer driven by entityvocab rules.

Phase 2.4: every writer (gateway, resolver, watch-topic processor) calls
into this module instead of implementing its own ``name.lower().strip()``
logic. The rules — title prefixes, legal suffixes, reject patterns —
live in ``libraries/entityvocab/definitions/entityvocab.resolution_rules.json``
under ``content.normalize_rules``.

The function is pure. Same input → same output. No DB, no I/O beyond
loading the pack file once at import.

Returns:
    NormalizeResult(
        normalized=str,            # the normalized form
        rejected=bool,              # True if a reject_pattern matched
        rejection_reason=str|None,  # why it was rejected
        applied=list[str],          # which rules ran (for audit)
    )
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("enki.gdu.normalize")


@dataclass
class NormalizeResult:
    normalized: str
    rejected: bool = False
    rejection_reason: Optional[str] = None
    applied: list[str] = field(default_factory=list)


# ── Pack loader ─────────────────────────────────────────────────────────────

_RULES: Optional[dict] = None
_PACK_VERSION: Optional[str] = None


def _pack_root() -> Path:
    try:
        from core.gdu.config import PROJECT_ROOT
        return PROJECT_ROOT / "libraries" / "entityvocab"
    except Exception:
        return Path(__file__).resolve().parents[2] / "libraries" / "entityvocab"


def _load_rules() -> None:
    global _RULES, _PACK_VERSION
    if _RULES is not None:
        return

    root = _pack_root()
    version = "unknown"
    try:
        with open(root / "pack.json") as f:
            meta = json.load(f) or {}
            version = meta.get("version", "unknown")
    except Exception as e:
        log.debug("entityvocab pack.json not readable: %s", e)

    rules: dict = {}
    try:
        with open(root / "definitions" / "entityvocab.resolution_rules.json") as f:
            data = json.load(f) or {}
            content = data.get("content") or {}
            rules = content.get("normalize_rules") or {}
    except Exception as e:
        log.warning("entityvocab normalize_rules not loaded: %s", e)

    _RULES = rules
    _PACK_VERSION = version
    log.info("normalize loaded entityvocab rules (v%s)", version)


def get_pack_version() -> str:
    _load_rules()
    return _PACK_VERSION or "unknown"


# ── Normalize ───────────────────────────────────────────────────────────────

# Compile regex patterns once.
_COMPILED_REJECT: list[tuple[str, re.Pattern, set[str], str]] = []


def _ensure_compiled() -> None:
    if _COMPILED_REJECT:
        return
    _load_rules()
    rules = _RULES or {}
    for spec in rules.get("reject_patterns") or []:
        rid = spec.get("id") or "unknown"
        pat = spec.get("regex") or ""
        reason = spec.get("reason") or rid
        exceptions = {(e or "").lower() for e in (spec.get("exceptions") or [])}
        try:
            _COMPILED_REJECT.append((rid, re.compile(pat, re.IGNORECASE), exceptions, reason))
        except re.error as e:
            log.warning("reject_pattern %s invalid regex: %s", rid, e)


def normalize(name: str, *, role: str = "name") -> NormalizeResult:
    """Normalize an entity name per entityvocab.normalize_rules.

    Args:
        name: raw input name from the LLM / extractor / user.
        role: "name" (default), "person", "organization", "place" — used
              to choose between strip_prefixes (titles) vs
              strip_legal_suffixes (Inc/LLC/etc). For unknown roles, both
              passes run.

    Returns NormalizeResult.
    """
    if not name:
        return NormalizeResult(normalized="", rejected=True,
                                rejection_reason="empty", applied=["empty"])

    _ensure_compiled()
    rules = _RULES or {}
    applied: list[str] = []

    # 1. Unicode normalize
    form = (rules.get("unicode_form") or "NFC")
    s = unicodedata.normalize(form, name)
    applied.append(f"unicode:{form}")

    # 2. Trim + collapse whitespace
    if rules.get("trim", True):
        s = s.strip()
        applied.append("trim")
    if rules.get("collapse_whitespace", True):
        s = re.sub(r"\s+", " ", s)
        applied.append("collapse_whitespace")

    # 3. Case fold
    if rules.get("case_fold", True):
        s = s.lower()
        applied.append("case_fold")

    # 3.5 EARLY reject pass — runs BEFORE strip_prefixes so position-title
    # fragment patterns ("Minister of Defence", "Director of School Education",
    # "Member of the Politburo of...") can match. Without this, strip_prefixes
    # removes the leading role word ('minister', 'director', 'member', ...)
    # and the position_title_fragment regex never sees what it's designed for.
    # Patterns that need the FULLY-normalized form still run again at step 8.
    # Phase 7a.3 of research-validation plan (May 9 2026).
    for rid, regex, exceptions, reason in _COMPILED_REJECT:
        if s in exceptions:
            continue
        if regex.match(s):
            return NormalizeResult(
                normalized=s, rejected=True,
                rejection_reason=f"{rid}:{reason}",
                applied=applied + [f"reject_early:{rid}"],
            )

    # 4. Strip prefixes (titles) — for person-ish names or unknown roles.
    if role in ("name", "person", "unknown") and rules.get("strip_prefixes"):
        s = _strip_leading_words(s, rules["strip_prefixes"])
        applied.append("strip_prefixes")

    # 5. Strip legal suffixes — for organizations or unknown roles.
    if role in ("name", "organization", "unknown") and rules.get("strip_legal_suffixes"):
        s = _strip_trailing_words(s, rules["strip_legal_suffixes"])
        applied.append("strip_legal_suffixes")

    # 6. Strip honorific suffixes (Jr, Sr, PhD) — for person-ish names.
    if role in ("name", "person", "unknown") and rules.get("strip_suffixes"):
        s = _strip_trailing_words(s, rules["strip_suffixes"])
        applied.append("strip_suffixes")

    # Final whitespace cleanup
    s = re.sub(r"\s+", " ", s).strip()

    # 7. Min length
    min_len = int(rules.get("min_length_after_normalize") or 2)
    if len(s) < min_len:
        return NormalizeResult(
            normalized=s, rejected=True,
            rejection_reason=f"min_length:{min_len}",
            applied=applied,
        )

    # 8. Reject patterns
    for rid, regex, exceptions, reason in _COMPILED_REJECT:
        if s in exceptions:
            continue
        if regex.match(s):
            return NormalizeResult(
                normalized=s, rejected=True,
                rejection_reason=f"{rid}:{reason}",
                applied=applied + [f"reject:{rid}"],
            )

    return NormalizeResult(normalized=s, applied=applied)


def _strip_leading_words(s: str, words: list[str]) -> str:
    """Repeatedly strip any leading word that matches `words` (with optional
    trailing dot). Keeps stripping until none of the words match."""
    if not s or not words:
        return s
    word_set = {w.lower() for w in words}
    changed = True
    while changed:
        changed = False
        m = re.match(r"^([\w.\-']+)\s+(.*)$", s)
        if not m:
            break
        head = m.group(1).rstrip(".").lower()
        if head in word_set:
            s = m.group(2)
            changed = True
    return s


def _strip_trailing_words(s: str, words: list[str]) -> str:
    """Strip trailing tokens (with optional comma + period) that match
    any of `words`. Removes one trailing token per pass; loops to handle
    'Foo, Inc., LLC' style stacks."""
    if not s or not words:
        return s
    word_set = {w.lower() for w in words}
    changed = True
    while changed:
        changed = False
        # Try `, suffix` then ` suffix`. Strip trailing dots inside the suffix.
        m = re.search(r"[,\s]+([\w.\-']+)\.?\s*$", s)
        if not m:
            break
        tail = m.group(1).rstrip(".").lower()
        if tail in word_set:
            s = s[: m.start()].rstrip(", ")
            changed = True
    return s


def reload_for_tests() -> None:
    global _RULES, _PACK_VERSION
    _RULES = None
    _PACK_VERSION = None
    _COMPILED_REJECT.clear()
