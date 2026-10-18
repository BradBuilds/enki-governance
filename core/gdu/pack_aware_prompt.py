"""
Pack-aware prompt builder + LDU-W → GDU boundary validator.

Implements the architectural rule: the LLM speaks LDU-W (free-form), the
mapper translates at the boundary into GDU positions. Only canonical
positions reach knowledge_nodes; non-mappable outputs go to LDU-P.

Two responsibilities:

  1. build_entity_prompt() — generates the entity NER prompt with the
     EXACT set of valid (category, subtype) pairs from the active pack
     baked in. The LLM is told which subtypes it may emit. When the pack
     expands (pack admission), the prompt automatically reflects the new
     options without code changes.

  2. validate_llm_output() — checks every (category, subtype) the LLM
     produced against the active pack:
       - valid pack position    → keep, mark gdu_canonical=True
       - unknown subtype        → coerce to "unmappable", route to LDU-P
       - cross-category mismatch (e.g., 'country' under 'entity')
                                → either remap (if alias known) or LDU-P

This module is the single source of truth for "is this LDU-W output
valid in the current GDU?". Every writer (entity_extractor, auto_validator,
llm_entity_validator, llm_mapper) should call validate_llm_output() before
storing anything.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from core.gdu.pack_registry import (
    get_valid_categories,
    get_valid_subtypes,
    is_valid_node_type,
)

log = logging.getLogger("enki.gdu.pack_prompt")


# ── Prompt builder ─────────────────────────────────────────────────────────

# Roles the LLM may assign to extracted entities. Stable across packs.
_VALID_ROLES = (
    "subject", "object", "author", "target",
    "perpetrator", "witness", "mention", "subject_matter",
)


def _format_subtype_table(category: str) -> str:
    """Return a human-readable bullet list of valid subtypes for a category.

    The list is sorted, deterministic, and short enough to inject into
    the prompt without blowing the context window."""
    subs = sorted(get_valid_subtypes(category))
    if not subs:
        return "  (none)"
    lines = []
    for s in subs:
        lines.append(f"  - {s}")
    return "\n".join(lines)


def build_entity_prompt(article_text: str, *, max_chars: int = 4000) -> str:
    """Build the entity NER prompt for the active pack.

    The prompt:
      - Lists the exact `entity` subtypes the LLM may emit (no others)
      - Reminds it that places, events, topics are extracted separately
      - Demands strict JSON output
      - Truncates the article to max_chars to fit context window

    Returns the full prompt text ready to send to the LLM."""
    entity_subs = _format_subtype_table("entity")

    return f"""Extract named ENTITY mentions from the article below. Return STRICT JSON
with one key "entities" — an array of objects. No prose, no markdown.

You MUST use only these `subtype` values (the active GDU pack):

{entity_subs}

If a name doesn't fit any subtype above, OMIT it. Do NOT invent new subtypes.
Do NOT use "country", "city", "region" — those are PLACES, not ENTITIES, and
are extracted by a different pass. Skip them here.

Roles (pick one):
  subject       — article is primarily about this entity
  perpetrator   — entity that took an action
  target        — entity that was acted upon
  author        — speaker / source
  mention       — mentioned but not central

Output shape:
  {{"entities": [
    {{"name": "Ivan Petrov", "subtype": "person", "role": "subject"}},
    {{"name": "Anthropic", "subtype": "organization", "role": "mention"}}
  ]}}

Rules:
  - Output ONLY valid JSON.
  - At most 12 entities, prioritize the most central.
  - Skip generic terms ("the president" without a name).
  - Skip places (countries, cities, regions, etc.).
  - If unsure of subtype, OMIT the entity rather than guess.

Article:
{article_text[:max_chars]}
"""


# ── Output validator (LDU-W → GDU boundary) ────────────────────────────────

@dataclass(frozen=True)
class ValidationResult:
    """Result of validating one LLM-produced entity against the GDU pack."""

    accepted: bool                 # True = clean GDU mapping, False = LDU-P
    category: str                  # always one of: entity, place, event, topic, ...
    subtype: Optional[str]         # canonical subtype if accepted; None if LDU-P
    reason: str                    # "valid", "unknown_subtype", "cross_category", ...
    name_raw: str
    role: str
    raw_subtype: str               # what the LLM actually said (preserved for audit)


# Aliases that a writer can apply to remap LLM outputs onto GDU positions
# WITHIN the same category. Keys are lowercase. New aliases are added
# manually as they emerge.
_INTRA_CATEGORY_ALIASES: dict[str, dict[str, str]] = {
    "entity": {
        "company":          "organization",
        "corporation":      "organization",
        "corp":             "organization",
        "ngo":              "organization",
        "agency":           "organization",
        "ship":             "vessel",
        "boat":             "vessel",
        "tanker":           "vessel",
        "frigate":          "vessel",
        "destroyer":        "vessel",
        "carrier":          "vessel",
        "submarine":        "vessel",
        "plane":            "aircraft",
        "drone":            "aircraft",
        "uav":              "aircraft",
        "fighter":          "aircraft",
        "bomber":           "aircraft",
        "helicopter":       "aircraft",
        "car":              "vehicle",
        "truck":            "vehicle",
        "tank":             "vehicle",
        "weapon":           "weapon_system",
        "missile":          "weapon_system",
        "rocket":           "weapon_system",
        "rifle":            "weapon_system",
        "battalion":        "unit",
        "brigade":          "unit",
        "regiment":         "unit",
        "division":         "unit",
        "platoon":          "unit",
    },
}


# Cross-category remaps: when LLM puts something under the wrong category.
# (raw_category, raw_subtype) → (correct_category, correct_subtype)
_CROSS_CATEGORY_REMAPS: dict[tuple[str, str], tuple[str, str]] = {
    ("entity", "country"):       ("place", "country"),
    ("entity", "city"):          ("place", "city"),
    ("entity", "region"):        ("place", "admin_region"),
    ("entity", "state"):         ("place", "admin_region"),
    ("entity", "province"):      ("place", "admin_region"),
    ("entity", "continent"):     ("place", "named_area"),
    ("entity", "island"):        ("place", "named_area"),
    ("entity", "peninsula"):     ("place", "named_area"),
    ("entity", "strait"):        ("place", "maritime_area"),
    ("entity", "sea"):           ("place", "maritime_area"),
    ("entity", "gulf"):          ("place", "maritime_area"),
    ("entity", "reef"):          ("place", "maritime_area"),
    ("entity", "airport"):       ("place", "facility_location"),
    ("entity", "port"):          ("place", "facility_location"),
    # LLM occasionally emits canonical place subtypes literally under entity.
    # Accept these as direct remaps so they land in places instead of LDU-P.
    ("entity", "admin_region"):       ("place", "admin_region"),
    ("entity", "maritime_area"):      ("place", "maritime_area"),
    ("entity", "named_area"):         ("place", "named_area"),
    ("entity", "facility_location"):  ("place", "facility_location"),
    ("entity", "university"):    ("entity", "organization"),
    ("entity", "school"):        ("entity", "organization"),
    ("entity", "hospital"):      ("entity", "organization"),
    ("entity", "research_institute"): ("entity", "organization"),
    ("entity", "military_unit"): ("entity", "unit"),
    ("entity", "battalion"):     ("entity", "unit"),
    ("entity", "airliner"):      ("entity", "aircraft"),
    ("entity", "spacecraft"):    ("entity", "vehicle"),
    # Topic miscategorizations
    ("topic", "topic"):          ("topic", None),  # null subtype = drop / LDU-P
    ("topic", "government_agency"): ("entity", "government_agency"),
    ("topic", "organization"):   ("entity", "organization"),
    ("topic", "event"):          ("event", None),
    ("place", "region"):         ("place", "admin_region"),
    ("place", "strait"):         ("place", "maritime_area"),
}


def validate_llm_output(
    *,
    name: str,
    category: str,
    raw_subtype: str,
    role: str = "mention",
) -> ValidationResult:
    """Validate one LLM-produced entity against the active GDU pack.

    Resolution order:
      1. Lowercase + strip the inputs
      2. Direct hit on (category, subtype) in pack → accept
      3. Intra-category alias (e.g., 'company' → 'organization') → accept
      4. Cross-category remap (e.g., entity.country → place.country) → accept
         under the corrected category
      5. None of the above → LDU-P (accepted=False)
    """
    name = (name or "").strip()
    cat = (category or "").strip().lower()
    sub = (raw_subtype or "").strip().lower()
    role = (role or "mention").strip().lower()
    if role not in _VALID_ROLES:
        role = "mention"

    if not name or len(name) < 2:
        return ValidationResult(False, cat, None, "empty_name",
                                name, role, sub)

    # 1. Direct pack position hit
    if is_valid_node_type(cat, sub):
        return ValidationResult(True, cat, sub, "valid",
                                name, role, sub)

    # 2. Intra-category alias
    aliases = _INTRA_CATEGORY_ALIASES.get(cat, {})
    if sub in aliases:
        new_sub = aliases[sub]
        if is_valid_node_type(cat, new_sub):
            return ValidationResult(True, cat, new_sub, "intra_alias",
                                    name, role, sub)

    # 3. Cross-category remap
    if (cat, sub) in _CROSS_CATEGORY_REMAPS:
        new_cat, new_sub = _CROSS_CATEGORY_REMAPS[(cat, sub)]
        if new_sub is None:
            return ValidationResult(False, new_cat, None, "cross_category_drop",
                                    name, role, sub)
        if is_valid_node_type(new_cat, new_sub):
            return ValidationResult(True, new_cat, new_sub, "cross_remap",
                                    name, role, sub)

    # 4. Try category alone (subtype unknown but category known)
    if cat in get_valid_categories():
        return ValidationResult(False, cat, None, "unknown_subtype",
                                name, role, sub)

    # 5. Nothing matches
    return ValidationResult(False, "entity", None, "unknown_category",
                            name, role, sub)


def validate_llm_output_batch(items: list[dict]) -> list[ValidationResult]:
    """Validate a batch of LLM entity dicts. Convenience wrapper.

    Each input dict must have keys: name, subtype (or 'type'),
    optionally category and role. Returns list of ValidationResult
    in the same order."""
    out: list[ValidationResult] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("name_raw") or ""
        # Support both old "type" and new "subtype" keys
        sub = it.get("subtype") or it.get("type") or ""
        cat = it.get("category") or "entity"   # entity is the default for NER
        role = it.get("role") or "mention"
        out.append(validate_llm_output(
            name=name, category=cat, raw_subtype=sub, role=role,
        ))
    return out


def render_validation_summary(results: list[ValidationResult]) -> dict:
    """Compact stats for logging/observability."""
    accepted = sum(1 for r in results if r.accepted)
    rejected = len(results) - accepted
    reasons: dict[str, int] = {}
    for r in results:
        reasons[r.reason] = reasons.get(r.reason, 0) + 1
    return {
        "total": len(results),
        "accepted": accepted,
        "rejected": rejected,
        "reasons": reasons,
    }
