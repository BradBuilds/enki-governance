"""
Hub install companion — chat + state endpoints.

Lives at /api/assistant/* (Caddy strips /api/ → /assistant/* on FastAPI).
Three endpoints, all keyed by status_token:

  POST /assistant/chat   — one turn through Claude (enforces 8 gates)
  GET  /assistant/state  — mode, wizard step, conversation history
  POST /assistant/state  — update wizard step (no LLM call)

The 8 gates that bound spend per the install-assistant design notes:

  1. Status gate         — `preregistrations.status = 'approved'`        (403 if not)
  2. Heartbeat gate      — `install_conversations.retired_at IS NULL`
                           AND node has never heartbeat                   (410 Gone if retired)
  3. Per-token cap       — `install_conversations.turn_count < 20`        (429 if exceeded)
  4. Per-IP rate (6/min) — enforced by RateLimitMiddleware                (in middleware/rate_limit.py)
  5. Output cap (1k)     — set in lib/claude.py via max_tokens=1000
  6. Window cap (10)     — set in lib/claude.py via MAX_HISTORY_TURNS
  7. Scope-locked prompt — set in lib/claude.py via the cached system prompt
  8. Anthropic key       — `claude.is_configured()`                       (503 if absent)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from storage.postgres.db import get_connection
from services.api.src.lib import claude

logger = logging.getLogger("enki.hub.install_assistant")

router = APIRouter(prefix="/assistant", tags=["install-companion"])

TURN_CAP = 20  # gate 3 — per-token lifetime cap


# ─── Request / response models ──────────────────────────────────────────────

class ChatRequest(BaseModel):
    status_token: str
    message: str = Field(min_length=1, max_length=4000)


class StateUpdateRequest(BaseModel):
    status_token: str
    wizard_step: Optional[str] = Field(default=None, max_length=64)


# ─── Mode derivation ────────────────────────────────────────────────────────

def _resolve(status_token: str) -> dict[str, Any]:
    """Single read returning everything the route needs to gate + render."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.status, p.node_id, nr.last_heartbeat,
                       ic.wizard_step, ic.turns, ic.turn_count, ic.retired_at
                  FROM preregistrations p
                  LEFT JOIN node_registry        nr ON nr.node_id = p.node_id
                  LEFT JOIN install_conversations ic ON ic.status_token = p.status_token
                 WHERE p.status_token = %s
                """,
                (status_token,),
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Unknown status token")

    (prereg_id, status, node_id, last_heartbeat,
     wizard_step, turns, turn_count, retired_at) = row

    # mode derivation
    if status == "rejected":
        mode = "REJECTED"
    elif status == "pending":
        mode = "WAITLIST"
    elif retired_at is not None or last_heartbeat is not None:
        mode = "HANDOFF"
    else:
        mode = "INSTALL"

    return {
        "prereg_id": prereg_id,
        "status": status,
        "node_id": node_id,
        "last_heartbeat": last_heartbeat,
        "mode": mode,
        "wizard_step": wizard_step,
        "turns": turns or [],
        "turn_count": turn_count or 0,
        "retired_at": retired_at,
    }


# ─── GET /assistant/state ────────────────────────────────────────────────────

@router.get("/state")
def get_state(status_token: str):
    """Return current mode + wizard step + chat history for the status page."""
    s = _resolve(status_token)
    return {
        "mode": s["mode"],
        "wizard_step": s["wizard_step"],
        "turns": s["turns"],
        "turn_count": s["turn_count"],
        "turn_cap": TURN_CAP,
        "turns_remaining": max(0, TURN_CAP - s["turn_count"]),
        "assistant_available": claude.is_configured() and s["mode"] == "INSTALL",
        "node_alive": s["last_heartbeat"] is not None,
    }


# ─── POST /assistant/state ───────────────────────────────────────────────────

@router.post("/state")
def update_state(body: StateUpdateRequest):
    """Update wizard step. No Claude call. Lazy-creates the install_conversations row."""
    _resolve(body.status_token)  # 404 if unknown token

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO install_conversations (status_token, wizard_step)
                VALUES (%s, %s)
                ON CONFLICT (status_token) DO UPDATE
                   SET wizard_step     = EXCLUDED.wizard_step,
                       last_message_at = NOW()
                """,
                (body.status_token, body.wizard_step),
            )
        conn.commit()

    return {"ok": True, "wizard_step": body.wizard_step}


# ─── POST /assistant/chat ────────────────────────────────────────────────────

@router.post("/chat")
def chat(body: ChatRequest):
    """Single turn through Claude. Enforces all 8 gates."""

    # ── Gate 8 (key absent) — return 503, wizard still works without the sidebar
    if not claude.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Assistant temporarily unavailable. The install command below still works.",
        )

    s = _resolve(body.status_token)

    # ── Gate 1 (status)
    if s["status"] != "approved":
        raise HTTPException(
            status_code=403,
            detail="Chat is only available after your registration is approved.",
        )

    # ── Gate 2 (heartbeat / retired)
    if s["mode"] == "HANDOFF":
        raise HTTPException(
            status_code=410,
            detail=(
                "Your node is alive — the install companion has retired. "
                "Open your dashboard; the full assistant lives there now."
            ),
        )

    # ── Gate 3 (turn cap)
    if s["turn_count"] >= TURN_CAP:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Install chat capped at {TURN_CAP} turns per registration. "
                "Open a GitHub issue if you're still stuck."
            ),
        )

    # ── Call Claude (gates 5/6/7 enforced inside lib/claude.py)
    try:
        result = claude.chat(history=s["turns"], user_message=body.message)
    except anthropic.AuthenticationError:
        logger.exception("Anthropic auth failure — key may be revoked or wrong workspace")
        raise HTTPException(status_code=503, detail="Assistant unavailable.")
    except anthropic.RateLimitError:
        raise HTTPException(status_code=429, detail="Assistant is busy — try again in a minute.")
    except anthropic.OverloadedError:
        raise HTTPException(status_code=503, detail="Assistant is temporarily overloaded.")
    except anthropic.APIStatusError as e:
        logger.exception("Anthropic API status error: %s", e.status_code)
        raise HTTPException(status_code=502, detail="Assistant request failed.")
    except anthropic.APIConnectionError:
        raise HTTPException(status_code=502, detail="Could not reach the assistant.")

    # ── Persist both turns + usage counters
    now = datetime.now(timezone.utc).isoformat()
    new_turns = [
        {"role": "user",      "content": body.message, "ts": now},
        {"role": "assistant", "content": result["text"], "ts": now},
    ]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO install_conversations
                       (status_token, turns, turn_count,
                        total_input_tokens, total_output_tokens, last_message_at)
                VALUES (%s, %s::jsonb, 1, %s, %s, NOW())
                ON CONFLICT (status_token) DO UPDATE SET
                    turns               = install_conversations.turns || EXCLUDED.turns,
                    turn_count          = install_conversations.turn_count + 1,
                    total_input_tokens  = install_conversations.total_input_tokens  + EXCLUDED.total_input_tokens,
                    total_output_tokens = install_conversations.total_output_tokens + EXCLUDED.total_output_tokens,
                    last_message_at     = NOW()
                """,
                (
                    body.status_token,
                    json.dumps(new_turns),
                    # Bill cache reads at full input rate for accounting simplicity
                    # (Anthropic actually charges 0.1× — these counters are spend signals, not invoices)
                    result["input_tokens"] + result["cache_read_input_tokens"] + result["cache_creation_input_tokens"],
                    result["output_tokens"],
                ),
            )
        conn.commit()

    return {
        "text": result["text"],
        "turn_count": s["turn_count"] + 1,
        "turns_remaining": max(0, TURN_CAP - (s["turn_count"] + 1)),
        "stop_reason": result["stop_reason"],
    }
