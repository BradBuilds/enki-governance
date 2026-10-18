"""
TenantProviderClient — adapts a core.ai BaseLLMProvider to the
core.extraction.llm LLMClient protocol.

Why a bridge: extraction workers consume the LLMClient protocol
(complete / complete_json / health), while the rest of the platform
uses BaseLLMProvider.complete(messages) with a separate per-tenant
config table. Rather than migrate every caller we wrap one into the
other so setting a cloud provider in /ai/config immediately enables
Claude / Gemini / etc. for extraction too.

Limitations:
  - BaseLLMProvider has no format=json switch, so complete_json asks
    the model to return JSON and we parse defensively. Cloud models
    (Claude, Gemini, GPT-4o-mini) follow "return JSON only" prompts
    reliably; local small models may return prose. In that case the
    parse falls through and the caller sees parsed=None — same failure
    mode as the existing Ollama client on a bad response.
  - No streaming — extraction pipeline doesn't need it.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from core.extraction.llm.base import LLMResult
from core.ai.provider import BaseLLMProvider, LLMMessage

log = logging.getLogger("enki.extraction.llm.tenant")

_JSON_INSTRUCTION = (
    "Return ONLY valid JSON matching the schema in the user message. "
    "No prose, no markdown fences, no commentary — JSON only."
)


class TenantProviderClient:
    """Wraps a BaseLLMProvider in the LLMClient protocol."""

    def __init__(self, provider: BaseLLMProvider):
        self._provider = provider
        self.model_name = getattr(provider, "model", None) or provider.provider_name
        # model_version is used for LDU lineage — use provider name as
        # a coarse version stamp so audit logs can tell "this was
        # Anthropic claude-sonnet-4" apart from "this was Ollama gemma4:e2b".
        self.model_version = f"{provider.provider_name}:{self.model_name}"

    # ─── LLMClient protocol ────────────────────────────────────────

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        prompt_version: Optional[str] = None,
        timeout: float = 60.0,
    ) -> LLMResult:
        t0 = time.monotonic()
        messages = [LLMMessage(role="user", content=prompt)]
        try:
            resp = self._provider.complete(
                messages, max_tokens=max_tokens, temperature=temperature,
            )
            if resp.error:
                return LLMResult(
                    text="",
                    model_name=self.model_name,
                    model_version=self.model_version,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    success=False,
                    error=resp.error,
                    prompt_version=prompt_version,
                )
            return LLMResult(
                text=resp.content or "",
                model_name=self.model_name,
                model_version=self.model_version,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=True,
                prompt_version=prompt_version,
                extra={"usage": resp.usage},
            )
        except Exception as e:
            return LLMResult(
                text="", model_name=self.model_name,
                model_version=self.model_version,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=False, error=str(e),
                prompt_version=prompt_version,
            )

    def complete_json(
        self,
        prompt: str,
        *,
        schema: Optional[dict] = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        prompt_version: Optional[str] = None,
        timeout: float = 90.0,
    ) -> LLMResult:
        t0 = time.monotonic()
        schema_hint = ""
        if schema:
            schema_hint = f"\n\nJSON schema:\n{json.dumps(schema)}"
        messages = [
            LLMMessage(role="system", content=_JSON_INSTRUCTION + schema_hint),
            LLMMessage(role="user", content=prompt),
        ]
        try:
            resp = self._provider.complete(
                messages, max_tokens=max_tokens, temperature=temperature,
            )
            if resp.error:
                return LLMResult(
                    text="", parsed=None, model_name=self.model_name,
                    model_version=self.model_version,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    success=False, error=resp.error,
                    prompt_version=prompt_version,
                )
            text = (resp.content or "").strip()
            parsed = _parse_json_tolerant(text)
            return LLMResult(
                text=text,
                parsed=parsed,
                model_name=self.model_name,
                model_version=self.model_version,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=parsed is not None,
                error=None if parsed is not None else "json_parse_failed",
                prompt_version=prompt_version,
                extra={"usage": resp.usage},
            )
        except Exception as e:
            return LLMResult(
                text="", parsed=None, model_name=self.model_name,
                model_version=self.model_version,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=False, error=str(e),
                prompt_version=prompt_version,
            )

    def health(self) -> bool:
        try:
            return bool(self._provider.is_available())
        except Exception:
            return False


def _strip_reasoning_preamble(text: str) -> str:
    """Strip Gemma/o1-style reasoning chain that wraps the actual JSON.

    Gemma 3+ in 'think' mode emits:
        Thinking...
        Thinking Process:
        1. ... reasoning ...
        ...done thinking.

        <actual JSON here>

    The reasoning often contains the literal schema example like
    [{"name": "X"}], which fools .find('[') into slicing the wrong region.
    Strip the preamble first."""
    if not text:
        return text
    for marker in ("...done thinking.", "</think>", "</thinking>"):
        idx = text.rfind(marker)
        if idx >= 0:
            return text[idx + len(marker):].strip()
    return text


def _parse_json_tolerant(text: str) -> Any:
    """Parse JSON from a model response that may include a markdown
    fence, reasoning preamble, or a leading sentence."""
    if not text:
        return None
    # Strip Thinking/reasoning preamble first (Gemma 3+, o1-style models)
    text = _strip_reasoning_preamble(text)
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip markdown fence if present
    if "```" in text:
        parts = text.split("```")
        # Try each code fence
        for i in range(1, len(parts), 2):
            chunk = parts[i]
            # Drop language tag if any (e.g. "json\n{...}")
            if chunk.startswith(("json\n", "JSON\n")):
                chunk = chunk.split("\n", 1)[1] if "\n" in chunk else chunk
            try:
                return json.loads(chunk.strip())
            except json.JSONDecodeError:
                continue
    # Extract the first {...} / [...] block
    for opener, closer in (("{", "}"), ("[", "]")):
        first = text.find(opener)
        last = text.rfind(closer)
        if first != -1 and last > first:
            try:
                return json.loads(text[first : last + 1])
            except json.JSONDecodeError:
                continue
    return None
