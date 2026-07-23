"""
Entity extractor — universal, works on any text.

Cheap pass: regex-based identifier extraction. This catches structured
identifiers (emails, phone numbers, MMSI, tail numbers, VINs, domains,
IPs, URLs) with high precision and zero dependencies. It does NOT try
to do proper-noun NER — that's the deep pass's job.

Deep pass: stubbed until Phase 4 wires Gemma. Interface is in place so
the worker code doesn't change when the LLM lands.

Extractor output shape matches the envelope's entities[] items:
    {
        "node_id": None,           # filled by resolver (Phase 3)
        "name_raw": "<surface form>",
        "name_canonical": None,    # filled by resolver
        "role": "mention",
        "confidence": 0.0-1.0,
        "first_offset": <int>,
        "identifier_type": "email"|"phone"|"mmsi"|...  # bonus field
    }
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Identifier patterns. The keys are the identifier_type values stamped on
# each extracted entity; they match the vocabulary in
# libraries/signalvocab/definitions/signalvocab.observable_types.json
# so downstream resolvers can normalize consistently.
_PATTERNS: list[tuple[str, re.Pattern, float]] = [
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), 0.95),
    ("url",           re.compile(r"https?://[^\s<>\"']+"), 0.95),
    ("ip_address",    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), 0.90),
    ("phone_number",  re.compile(r"(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"), 0.80),
    ("mmsi",          re.compile(r"\bMMSI[:\s]*(\d{9})\b", re.I), 0.98),
    ("imsi",          re.compile(r"\bIMSI[:\s]*(\d{14,15})\b", re.I), 0.98),
    ("imei",          re.compile(r"\bIMEI[:\s]*(\d{15})\b", re.I), 0.98),
    ("tail_number",   re.compile(r"\b(N\d{1,5}[A-Z]{0,2}|[A-Z]{1,2}-[A-Z]{3,4})\b"), 0.70),
    ("icao24",        re.compile(r"\bICAO24[:\s]*([0-9a-fA-F]{6})\b"), 0.98),
    ("callsign",      re.compile(r"\bcallsign[:\s]+([A-Z0-9]{3,8})\b", re.I), 0.85),
    # Financial / legal identifiers
    ("ein",           re.compile(r"\bEIN[:\s]*(\d{2}-\d{7})\b", re.I), 0.95),
    ("lei",           re.compile(r"\bLEI[:\s]*([A-Z0-9]{20})\b", re.I), 0.95),
    ("ticker_symbol", re.compile(r"\$([A-Z]{1,5})\b"), 0.60),
    # Crypto
    ("btc_address",   re.compile(r"\b(bc1[ac-hj-np-z02-9]{11,71}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b"), 0.85),
    # Hashes
    ("file_hash_sha256", re.compile(r"\b[a-f0-9]{64}\b", re.I), 0.85),
    ("file_hash_md5",    re.compile(r"\b[a-f0-9]{32}\b", re.I), 0.70),
    # Social
    ("hashtag",       re.compile(r"(?<!\w)#([A-Za-z][\w_]{1,48})"), 0.80),
    ("mention",       re.compile(r"(?<!\w)@([A-Za-z][\w_]{1,30})"), 0.80),
]


def extract_entities_cheap(text: str) -> list[dict[str, Any]]:
    """Rule-based entity extraction — identifiers only.

    Returns envelope-shaped entity dicts. Proper-noun NER (person,
    organization, place names) is deferred to the deep pass.
    """
    if not text:
        return []

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()  # (identifier_type, normalized_value)

    for ident_type, pattern, confidence in _PATTERNS:
        for m in pattern.finditer(text):
            surface = m.group(0)
            # Normalize for dedup
            norm_key = (ident_type, surface.lower().strip())
            if norm_key in seen:
                continue
            seen.add(norm_key)

            results.append({
                "node_id": None,
                "name_raw": surface,
                "name_canonical": None,
                "role": "mention",
                "confidence": confidence,
                "first_offset": m.start(),
                "identifier_type": ident_type,
            })

    return results


def extract_entities_deep(
    text: str,
    *,
    llm: Optional[Any] = None,
    prompt_version: str = "entity_v2_pack_aware",
) -> list[dict[str, Any]]:
    """LLM-backed proper-noun NER, pack-aware.

    Uses a prompt generated from the active GDU pack (so the LLM only
    sees subtypes that exist in the current pack version). Validates
    every output through the LDU-W → GDU boundary; entities that don't
    map cleanly are marked for LDU-P routing rather than silently
    canonicalized.

    Returns envelope-shaped entity dicts. Each carries:
      - identifier_type: "named_<canonical_subtype>" if accepted, or
        "named_unmappable" if it goes to LDU-P
      - _ldup_route: True when caller should route to provisional layer
      - _category: the GDU category the validator placed it in (may
        differ from what the LLM said — e.g., entity.country → place.country)
    """
    if llm is None or not text:
        return []

    # Lazy import to avoid pack registry init at module load
    from core.gdu.pack_aware_prompt import (
        build_entity_prompt,
        validate_llm_output_batch,
        render_validation_summary,
    )

    prompt = build_entity_prompt(text, max_chars=4000)
    result = llm.complete_json(prompt, max_tokens=600, prompt_version=prompt_version)
    if not result.success or not isinstance(result.parsed, dict):
        return []

    raw_entities = result.parsed.get("entities") or []
    if not isinstance(raw_entities, list):
        return []

    # Normalize to validator-input shape
    candidates = [
        {
            "name":     e.get("name", ""),
            "subtype":  e.get("subtype") or e.get("type") or "",
            "category": e.get("category") or "entity",
            "role":     e.get("role") or "mention",
        }
        for e in raw_entities[:20]
        if isinstance(e, dict)
    ]

    validations = validate_llm_output_batch(candidates)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for v in validations:
        if not v.name_raw or len(v.name_raw) < 2:
            continue
        # Dedup by (final-category, name) — accept the first occurrence
        key = (v.category, v.name_raw.lower())
        if key in seen:
            continue
        seen.add(key)

        if v.accepted and v.subtype:
            ident = f"named_{v.subtype}"
            ldup_route = False
        else:
            ident = "named_unmappable"
            ldup_route = True

        out.append({
            "node_id": None,
            "name_raw": v.name_raw,
            "name_canonical": None,
            "role": v.role,
            "confidence": 0.75 if v.accepted else 0.45,
            "first_offset": None,
            "identifier_type": ident,
            "_extraction_method": "llm_ner",
            "_category": v.category,
            "_subtype_raw": v.raw_subtype,
            "_ldup_route": ldup_route,
            "_validator_reason": v.reason,
        })

    # One-line observability
    summary = render_validation_summary(validations)
    if summary["total"]:
        import logging
        logging.getLogger("enki.extract.entity").info(
            "entity_extractor: %d candidates, %d accepted, %d to LDU-P (reasons=%s)",
            summary["total"], summary["accepted"], summary["rejected"], summary["reasons"],
        )

    return out
