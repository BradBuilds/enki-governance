"""
Topic extractor — LLM-driven only.

Phase 2.5: the bootstrap keyword bag (``_TOPIC_SEEDS``) was retired. Topics
in Enki should EMERGE from the deep LLM pass — keyword bootstrap was a
contamination shortcut that pre-shaped the topic distribution before any
real article was processed. If the LLM struggles without the bootstrap,
that is a model-quality finding to surface, not a justification for
re-introducing canned keyword sets.

Output shape matches ``envelope.topics[]``:
    {"node_id": None, "label": "<snake_case>", "weight": 0.0-1.0,
     "source": "llm" | "keyword_bag"}

The cheap pass is now a no-op. The deep pass (PII-respecting,
constrained-output Gemma prompt) is the single topic surface.
"""

from __future__ import annotations

from typing import Any, Optional


def extract_topics_cheap(text: str) -> list[dict[str, Any]]:
    """Retired bootstrap pass. Returns [].

    Kept as an importable symbol so callers don't break — but the cheap
    keyword path no longer contributes topics. All topic surfacing flows
    through ``extract_topics_deep``.
    """
    return []


def extract_topics_deep(
    text: str,
    *,
    llm: Optional[Any] = None,
    prompt_version: str = "topic_v1",
) -> list[dict[str, Any]]:
    """LLM-backed topic extraction.

    The prompt asks the model for short snake_case labels (< 40 chars)
    plus a 1-sentence rationale, capped at 5 topics per document. When
    no LLM client is provided the function returns [] — silent no-op
    (the watcher logs at the call site).
    """
    if llm is None or not text:
        return []

    prompt = (
        "Identify up to 5 distinct topics this article is about.\n"
        "Topics are abstract subjects ('energy_security', 'civilian_casualties',\n"
        "'cyber_intrusion'), not entities ('Russia', 'Apple') or events\n"
        "('strike', 'meeting').\n"
        "Return JSON: [{label: snake_case_under_40_chars, weight: 0.0-1.0,\n"
        "               rationale: one-sentence-why}]\n"
        "If no clear topic, return [].\n\n"
        "ARTICLE:\n"
    )

    try:
        # The provider (OllamaProvider) takes a list of LLMMessage and returns
        # a response object — not a raw string. (The original code called
        # llm.complete(str) and swallowed the error, so it always returned [].)
        from core.ai.provider import LLMMessage
        import json as _json
        messages = [LLMMessage(role="user", content=prompt + (text[:4000] or ""))]
        response = llm.complete(messages, max_tokens=500, temperature=0.0)
        if response is None or getattr(response, "error", None):
            return []
        raw = (response.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        if raw.lstrip().startswith("json"):
            raw = raw.lstrip()[4:].lstrip()
        try:
            parsed = _json.loads(raw)
        except Exception:
            s, e = raw.index("["), raw.rindex("]") + 1
            parsed = _json.loads(raw[s:e])
        out: list[dict[str, Any]] = []
        for item in parsed or []:
            label = (item.get("label") or "").strip().lower()
            if not label or len(label) > 40:
                continue
            weight = float(item.get("weight") or 0.5)
            out.append({
                "node_id": None,
                "label": label,
                "weight": max(0.0, min(1.0, weight)),
                "source": "llm",
            })
        return out
    except Exception:
        return []
