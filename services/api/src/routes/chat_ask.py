"""
/api/chat/ask — the universal entry-point chat endpoint.

User asks a question → LLM picks one of 8 tools → tool executes against
the cyclical model → response is {text, cards[]} where each card maps
to a Svelte component the UI renders inline.

The frontend uses this from any context: top-bar palette, /chat full
page, hover-ask on cards, etc.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.ai.chat_tools import (
    TOOL_SCHEMAS, TOOL_FUNCTIONS, execute_tool, render_tool_menu_for_prompt,
)

log = logging.getLogger("enki.api.chat_ask")

router = APIRouter(prefix="/chat", tags=["chat"])


class AskRequest(BaseModel):
    question: str
    context: Optional[dict] = None   # current page, focal entity, etc.


_SYSTEM_PROMPT_TEMPLATE = """You are Enki, an investigative intelligence assistant.
You answer questions about the user's knowledge graph (libraries, entities,
facts, events, references) by calling exactly ONE tool from the menu below.

{tool_menu}

CRITICAL RULES:
- Return ONLY a JSON object like: {{"tool": "<name>", "args": {{...}}}}
- No commentary, no markdown fences.
- If the user's question doesn't fit any tool, pick the closest one (often
  entity_lookup or fact_search) with a best-effort guess.
- Base-60 addresses look like N.N.N (e.g., 13.3.1, 0.0.171025, 7.0.971).
  Use address_resolve for those.
- Words like "Roswell", "Acme", "Khufu" are library focal terms.
  Use library_search.
- "what do we know about X" → entity_lookup or library_search depending
  on whether X is a person/thing or topic.
- "facts about X" → fact_search with subject=X
- "what's new" / "recent" / "system state" → pipeline_status or recent_facts

USER QUESTION:
{question}
"""


def _rule_pick_tool(question: str) -> Optional[dict]:
    """Pre-LLM heuristic for common patterns. Saves an LLM round-trip
    for obvious cases and degrades gracefully when no LLM is available."""
    q = (question or "").strip()
    if not q:
        return None
    # Base-60 address
    if re.fullmatch(r"\d+\.\d+\.\d+", q):
        return {"tool": "address_resolve", "args": {"address": q}}
    qlow = q.lower()
    if qlow in ("status", "what's new", "what is new", "system status", "depth", "pipeline"):
        return {"tool": "pipeline_status", "args": {}}
    if qlow.startswith("recent fact") or qlow == "recent":
        return {"tool": "recent_facts", "args": {"limit": 10}}
    m = re.match(r"facts? about (.+)$", qlow)
    if m:
        return {"tool": "fact_search", "args": {"subject": m.group(1).strip()}}
    m = re.match(r"^libraries?$", qlow) or re.match(r"^all libraries?$", qlow)
    if m:
        return {"tool": "library_search", "args": {"query": "all"}}
    return None


def _llm_pick_tool(question: str, context: Optional[dict]) -> Optional[dict]:
    """Ask the LLM to pick a tool + args. Returns dict or None on failure."""
    try:
        from core.ai.config import get_tenant_provider
        from core.ai.provider import LLMMessage
        provider = get_tenant_provider("default")
        if not provider:
            return None
        prompt = (_SYSTEM_PROMPT_TEMPLATE
                  .replace("{tool_menu}", render_tool_menu_for_prompt())
                  .replace("{question}", question))
        messages = [
            LLMMessage(role="system", content="You return only valid JSON."),
            LLMMessage(role="user", content=prompt),
        ]
        response = provider.complete(messages, max_tokens=400, temperature=0.0)
    except Exception as e:
        log.debug("LLM provider unavailable: %s", e)
        return None
    if response is None or getattr(response, "error", None):
        return None
    raw = (response.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    if raw.startswith("json"):
        raw = raw[4:].lstrip()
    try:
        parsed = json.loads(raw)
    except Exception:
        try:
            s, e = raw.index("{"), raw.rindex("}") + 1
            parsed = json.loads(raw[s:e])
        except Exception:
            return None
    if not isinstance(parsed, dict) or "tool" not in parsed:
        return None
    return parsed


@router.post("/ask")
def ask(req: AskRequest):
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(400, "question is required")

    # Stage 1: rule-based shortcuts for common patterns (fast path).
    plan = _rule_pick_tool(question)
    routing = "rule"

    # Stage 2: LLM picks a tool from the menu.
    if not plan:
        plan = _llm_pick_tool(question, req.context)
        routing = "llm"

    # Stage 3: graceful fallback if the LLM is unreachable.
    if not plan:
        plan = {"tool": "entity_lookup", "args": {"query": question}}
        routing = "fallback"

    tool_name = plan.get("tool")
    tool_args = plan.get("args") or {}
    if tool_name not in TOOL_FUNCTIONS:
        plan["tool"] = "entity_lookup"
        plan["args"] = {"query": question}
        tool_name = "entity_lookup"
        tool_args = {"query": question}

    result = execute_tool(tool_name, tool_args)

    return {
        "question":    question,
        "tool":        tool_name,
        "tool_args":   tool_args,
        "routing":     routing,
        "text":        result.get("text", ""),
        "cards":       result.get("cards", []),
    }


@router.get("/tools")
def list_tools():
    """Expose the tool menu for UI documentation / power users."""
    return {"tools": TOOL_SCHEMAS}
