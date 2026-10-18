"""
Hub install companion — Anthropic SDK wrapper.

Short-lived chat that lives at /status/{token} between approval and first
heartbeat. Uses claude-haiku-4-5 with prompt caching on the install playbook
so identical context across users hits the cache (~10x cheaper input tokens
after the first call of the hour).

ANTHROPIC_API_KEY is populated via .env.hub → compose.hub.yaml → container env.
When absent, is_configured() returns False and callers should respond 503 —
the wizard still works without the sidebar.

See the install-assistant design notes for the rescope decision and
the eight application-layer gates that bound spend.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import anthropic

logger = logging.getLogger("enki.hub.install_companion")

MODEL_ID = "claude-haiku-4-5"
MAX_OUTPUT_TOKENS = 1000
MAX_HISTORY_TURNS = 10

# services/api/src/lib/claude.py → repo root is parents[4]
_PLAYBOOK_PATH = Path(__file__).resolve().parents[4] / "docs" / "INSTALL_PLAYBOOK.md"

_SYSTEM_HEADER = (
    "You are the Enki install companion at /status/{token}. Your only job is to "
    "help this user finish their Enki install. The chat retires permanently the "
    "moment their node sends its first heartbeat to the hub. Follow the "
    "tone-and-scope rules at the end of the install playbook strictly — "
    "anything outside install gets a one-line refusal and a redirect to the "
    "node-side assistant the user will have after install completes.\n\n"
    "The install playbook follows."
)


def _load_playbook() -> str:
    try:
        return _PLAYBOOK_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("install playbook missing at %s", _PLAYBOOK_PATH)
        return (
            "(install playbook not bundled in this build — refuse every question "
            "and tell the user to file an issue at "
            "https://github.com/BradBuilds/enki-governance/issues)"
        )


_PLAYBOOK_TEXT = _load_playbook()


def is_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _client() -> anthropic.Anthropic:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)


def chat(history: list[dict[str, str]], user_message: str) -> dict[str, Any]:
    """Send one turn to the install companion.

    history: prior turns in chronological order, each {"role": "user"|"assistant",
             "content": "..."}. Trimmed to the last MAX_HISTORY_TURNS before sending.
    user_message: the new user turn (not included in history).

    Returns a dict with the assistant text and usage counters so the route
    can persist token totals against install_conversations.

    Raises RuntimeError if ANTHROPIC_API_KEY is unset. Propagates anthropic.*
    exceptions on API failures so the route can map them to HTTP status.
    """
    trimmed = history[-MAX_HISTORY_TURNS:] if len(history) > MAX_HISTORY_TURNS else history
    messages: list[dict[str, str]] = [
        {"role": h["role"], "content": h["content"]} for h in trimmed
    ]
    messages.append({"role": "user", "content": user_message})

    client = _client()
    response = client.messages.create(
        model=MODEL_ID,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=[
            {"type": "text", "text": _SYSTEM_HEADER},
            {
                "type": "text",
                "text": _PLAYBOOK_TEXT,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=messages,
    )

    text = next((b.text for b in response.content if b.type == "text"), "")

    return {
        "text": text,
        "stop_reason": response.stop_reason,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
    }
