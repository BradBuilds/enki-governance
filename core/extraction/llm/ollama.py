"""
Ollama LLM client.

Uses the Ollama HTTP API at http://localhost:11434 (configurable via
OLLAMA_BASE_URL env var). Defaults to the gemma4:e2b model. Implements
the LLMClient protocol from core.extraction.llm.base.

No external pip dependency — uses urllib from the standard library.
This keeps the install footprint minimal and avoids the langchain /
ollama-python release churn.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from core.extraction.llm.base import LLMResult

log = logging.getLogger("enki.extraction.llm.ollama")

DEFAULT_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("ENKI_LLM_MODEL", "gemma4:e2b")


class OllamaClient:
    """Minimal Ollama client. Implements the LLMClient protocol."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
    ):
        self.model_name = model_name
        self.model_version: Optional[str] = None  # populated on first health check
        self.base_url = base_url.rstrip("/")

    # ─── Internal HTTP ─────────────────────────────────────────────────

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str, timeout: float = 5.0) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ─── Public API ────────────────────────────────────────────────────

    def health(self) -> bool:
        """Ping /api/tags. Cache the model digest as model_version on
        first successful call so audit logs carry it."""
        try:
            tags = self._get("/api/tags", timeout=5.0)
            for m in tags.get("models", []):
                if m.get("name") == self.model_name or m.get("model") == self.model_name:
                    self.model_version = (m.get("digest") or "")[:12]
                    return True
            log.warning("Ollama reachable but model %s not loaded", self.model_name)
            return False
        except Exception as e:
            log.warning("Ollama health check failed: %s", e)
            return False

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        prompt_version: Optional[str] = None,
        timeout: float = 60.0,
    ) -> LLMResult:
        """Free-form completion via /api/generate."""
        t0 = time.monotonic()
        body = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": float(temperature),
                "num_predict": int(max_tokens),
            },
        }
        try:
            resp = self._post("/api/generate", body, timeout=timeout)
            text = (resp.get("response") or "").strip()
            return LLMResult(
                text=text,
                model_name=self.model_name,
                model_version=self.model_version,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=True,
                prompt_version=prompt_version,
                extra={
                    "eval_count": resp.get("eval_count"),
                    "prompt_eval_count": resp.get("prompt_eval_count"),
                },
            )
        except urllib.error.HTTPError as e:
            return LLMResult(
                text="",
                model_name=self.model_name,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=False,
                error=f"HTTP {e.code}: {e.reason}",
                prompt_version=prompt_version,
            )
        except Exception as e:
            return LLMResult(
                text="",
                model_name=self.model_name,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=False,
                error=str(e),
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
        """Schema-constrained completion. Ollama supports a `format`
        parameter that constrains output to JSON (or to a JSON schema).

        We pass `format: "json"` for the broad case and let the caller
        validate the result against their schema afterward — Ollama's
        full schema constraint is finicky across model families and
        we'd rather degrade gracefully than fail hard."""
        t0 = time.monotonic()
        body = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": float(temperature),
                "num_predict": int(max_tokens),
            },
        }
        try:
            resp = self._post("/api/generate", body, timeout=timeout)
            text = (resp.get("response") or "").strip()
            parsed: Any = None
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                log.debug("Ollama JSON parse failed for prompt_version=%s: %s",
                          prompt_version, e)
            return LLMResult(
                text=text,
                parsed=parsed,
                model_name=self.model_name,
                model_version=self.model_version,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=parsed is not None,
                error=None if parsed is not None else "json_parse_failed",
                prompt_version=prompt_version,
                extra={
                    "eval_count": resp.get("eval_count"),
                    "prompt_eval_count": resp.get("prompt_eval_count"),
                },
            )
        except urllib.error.HTTPError as e:
            return LLMResult(
                text="", parsed=None,
                model_name=self.model_name,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=False,
                error=f"HTTP {e.code}: {e.reason}",
                prompt_version=prompt_version,
            )
        except Exception as e:
            return LLMResult(
                text="", parsed=None,
                model_name=self.model_name,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=False,
                error=str(e),
                prompt_version=prompt_version,
            )
