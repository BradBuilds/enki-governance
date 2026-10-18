"""
Effective-provider resolver.

Two LLM config tables exist for historical reasons:

  ai_config    — modern, per-tenant cloud providers (12 of them:
                 Anthropic, OpenAI, Gemini, Groq, Together, Mistral,
                 DeepSeek, Perplexity, Fireworks, Grok, Cohere, Custom).
                 Populated by the Settings UI and /ai/config endpoints.
  ai_providers — legacy, Ollama-only, default-tenant scoped. Populated
                 by enki-install.sh during first boot.

Extraction and research workers need ONE resolver that tries the
modern cloud config first and falls back to the legacy local config.
This is the single knob that makes "switch to Claude while we wait for
GPU hardware" a one-env-var operation.

Precedence:
  1. core.ai.config.get_tenant_provider(tenant_id)  — reads ai_config
  2. core.ai.local_provider.get_default_provider() — reads ai_providers
  3. None — callers must handle the "no LLM" case by falling back to
           regex / heuristics

Returns a BaseLLMProvider (the core.ai abstraction). For the
core.extraction.llm LLMClient protocol, use the bridge in
core/extraction/llm/tenant_client.py instead.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.ai.provider import BaseLLMProvider

log = logging.getLogger("enki.ai.resolver")


def get_effective_provider(
    tenant_id: str = "default",
    dsn: Optional[str] = None,
    role: str = "default",
) -> Optional[BaseLLMProvider]:
    """Resolve the best-available LLM provider for this tenant.

    Tries modern ai_config first (any cloud provider), then the legacy
    Ollama-only default. Pass role="chat" for interactive surfaces so they
    resolve independently of the bulk-extraction provider. Returns None if
    nothing is configured or everything is unreachable."""
    # 1. Modern cloud config
    try:
        from core.ai.config import get_tenant_provider
        prov = get_tenant_provider(tenant_id, dsn, role=role)
        if prov and prov.is_available():
            return prov
    except Exception as e:
        log.debug("get_tenant_provider failed for %s: %s", tenant_id, e)

    # 2. Legacy local Ollama
    try:
        from core.ai.local_provider import get_default_provider
        prov = get_default_provider()
        if prov and prov.is_available():
            return prov
    except Exception as e:
        log.debug("get_default_provider fallback failed: %s", e)

    return None
