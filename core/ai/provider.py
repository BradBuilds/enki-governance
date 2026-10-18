"""
LLM Provider abstraction.

Every AI operation goes through this interface. The concrete provider
(local Ollama, OpenAI API, Anthropic API, etc.) is configured per tenant.

The provider NEVER has direct database access. It receives context
(signal data, case info) as text and returns text responses. The caller
is responsible for translating LLM output into API calls, and the
governance spine validates those calls.

This means: if the LLM hallucinates an invalid action, the server
rejects it deterministically. The LLM cannot bypass governance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LLMMessage:
    """A single message in a conversation."""
    role: str           # "system", "user", "assistant"
    content: str


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str                    # The generated text
    model: str                      # Which model produced this
    provider: str                   # "ollama", "openai", "anthropic", etc.
    usage: dict = field(default_factory=dict)  # token counts if available
    error: Optional[str] = None     # Error message if failed


@dataclass
class WebSearchResult:
    """Result of a native web search performed by the provider's own tool.

    `answer` is the model's synthesized text; `citations` is the list of
    sources it pointed to — these are the URLs Enki grabs and collapses.
    """
    answer: str                                      # model's synthesized text
    citations: list[dict] = field(default_factory=list)  # [{url, title, snippet}]
    model: str = ""
    provider: str = ""
    usage: dict = field(default_factory=dict)
    error: Optional[str] = None                      # e.g. "web_search_unsupported"


class BaseLLMProvider(ABC):
    """Abstract base for all LLM providers."""

    provider_name: str = "base"

    # Providers that can perform native web search override this to True.
    supports_web_search: bool = False

    @abstractmethod
    def complete(
        self,
        messages: list[LLMMessage],
        max_tokens: int = 1000,
        temperature: float = 0.1,
    ) -> LLMResponse:
        """Generate a completion from a list of messages."""
        ...

    def web_search(self, query: str, max_tokens: int = 2000) -> WebSearchResult:
        """Run a native web search and return an answer + source citations.

        Default: not supported. Search-capable providers (OpenAI, Anthropic,
        Perplexity) override this. Callers MUST check `error` — the deep-research
        orchestrator aborts rather than falling back to un-sourced model recall.
        """
        return WebSearchResult(
            answer="", citations=[], model=getattr(self, "model", ""),
            provider=self.provider_name, error="web_search_unsupported",
        )

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the provider is configured and reachable."""
        ...

    @abstractmethod
    def list_models(self) -> list[str]:
        """List available models for this provider."""
        ...


# ─── Provider Registry ─────────────────────────────────────────────────────────

_PROVIDERS: dict[str, type[BaseLLMProvider]] = {}


def register_provider(name: str, cls: type[BaseLLMProvider]):
    _PROVIDERS[name] = cls


def get_provider(name: str, config: dict) -> Optional[BaseLLMProvider]:
    """Get a configured provider instance by name."""
    cls = _PROVIDERS.get(name)
    if not cls:
        return None
    return cls(config)


def list_providers() -> list[str]:
    """List registered provider names."""
    return list(_PROVIDERS.keys())
