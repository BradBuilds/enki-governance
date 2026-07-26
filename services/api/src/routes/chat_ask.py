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



def _looks_like_library(term: str):
    """True when a phrase names a library rather than an entity."""
    try:
        from core.ai.chat_tools import _resolve_library
        return _resolve_library(term)
    except Exception:
        return (None, None)

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

    # "how is X going" / "progress on X" / "how's the reading" — the daily check-in.
    m = re.match(r"(?:how(?:'s| is| are)?|hows)\s+(?:the\s+)?(.+?)\s+(?:going|doing|coming along)\??$", qlow)
    if m:
        subj = m.group(1).strip()
        generic = subj in ("it", "things", "reading", "everything", "we", "pipeline", "ingest")
        return {"tool": "library_progress", "args": {} if generic else {"library": subj}}
    m = re.match(r"(?:progress|status)\s+(?:on|for)\s+(.+?)\??$", qlow)
    if m:
        return {"tool": "library_progress", "args": {"library": m.group(1).strip()}}
    if qlow in ("progress", "how is it going", "hows it going", "how are we doing"):
        return {"tool": "library_progress", "args": {}}

    # "timeline of X" / "X timeline" — chronological, dated, traceable to a doc.
    m = re.match(r"(?:show me (?:the|a) |give me (?:the|a) )?timeline (?:of|for) (.+?)\??$", qlow) \
        or re.match(r"(.+?)\s+timeline\??$", qlow)
    if m:
        subj = m.group(1).strip()
        lib_id, _ = _looks_like_library(subj)
        return {"tool": "topic_timeline",
                "args": ({"library": subj} if lib_id else {"subject": subj})}

    # "who is connected to X" / "connections for X" / "who did X know"
    m = (re.match(r"who(?:'s| is| are)?\s+(?:connected|linked|related)\s+to\s+(.+?)\??$", qlow)
         or re.match(r"(?:connections?|links?|network)\s+(?:for|of|to|around)\s+(.+?)\??$", qlow)
         or re.match(r"who did\s+(.+?)\s+(?:know|work with|meet)\??$", qlow))
    if m:
        subj = m.group(1).strip()
        lib = None
        m2 = re.search(r"^(.*?)\s+in (?:the )?(.+)$", subj)
        if m2:
            subj, lib = m2.group(1).strip(), m2.group(2).strip()
        args = {"subject": subj}
        if lib:
            args["library"] = lib
        return {"tool": "entity_connections", "args": args}

    # "what do the files say about X (in Y)" — grounded, cited, abstains when empty.
    m = re.match(r"(?:what do (?:the )?(?:files|documents|docs) say about|"
                 r"what does the corpus say about|tell me about|who is|what is)\s+(.+?)\??$", qlow)
    if m:
        subj = m.group(1).strip()
        lib = None
        m2 = re.search(r"^(.*?)\s+in (?:the )?(mkultra|mk-ultra|example_program|artichoke|bluebird|"
                       r"parapsychology|psychological warfare|iran-contra|jfk|Acme|phoenix)\b", subj)
        if m2:
            subj, lib = m2.group(1).strip(), m2.group(2).strip()
        args = {"subject": subj, "question": q}
        if lib:
            args["library"] = lib
        return {"tool": "corpus_answer", "args": args}
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
