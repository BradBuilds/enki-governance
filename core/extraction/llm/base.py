"""
LLMClient protocol — common interface every LLM backend implements.

Why a Protocol instead of an ABC: extraction workers should be able to
mock this trivially in tests, and the LLMClient role is data-shape-only
(complete + complete_json), not implementation-bearing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class LLMResult:
    """Structured result from an LLM call.

    text:       the raw model output (always present)
    parsed:     parsed JSON if complete_json was used, else None
    model_name: the backend model id (e.g. 'gemma4:e2b')
    duration_ms: wall time in milliseconds
    success:    True if the call completed without error
    error:      error message if success=False
    prompt_version: identifier for the prompt template used (for audit)
    """
    text: str = ""
    parsed: Any = None
    model_name: str = ""
    model_version: Optional[str] = None
    duration_ms: int = 0
    success: bool = True
    error: Optional[str] = None
    prompt_version: Optional[str] = None
    extra: dict = field(default_factory=dict)


@runtime_checkable
class LLMClient(Protocol):
    """Protocol every LLM backend must implement.

    The two methods cover the only two interaction patterns the
    extraction pipeline needs:

      complete()       — free-form text → text (narrative synthesis,
                         translation)
      complete_json()  — text → schema-constrained JSON (entity
                         extraction, structured field filling)

    Idempotency requirement: implementations must default temperature=0
    so the same prompt produces the same output across runs. Re-running
    extraction must be deterministic for governance audit.
    """

    model_name: str
    model_version: Optional[str]

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        prompt_version: Optional[str] = None,
        timeout: float = 60.0,
    ) -> LLMResult:
        """Free-form completion. Returns LLMResult with .text populated."""
        ...

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
        """Schema-constrained completion. Returns LLMResult with .parsed
        populated (a dict matching the schema, or None on parse failure)."""
        ...

    def health(self) -> bool:
        """Quick check that the backend is reachable. Should return
        within 5 seconds."""
        ...
