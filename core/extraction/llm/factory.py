"""
LLM client factory for the extraction pipeline.

Precedence (highest first):

  1. ENKI_LLM_BACKEND=none      — disabled, extraction skips deep passes
  2. ENKI_LLM_BACKEND=ollama    — force legacy local Ollama (Gemma CPU)
  3. ai_config (any tenant)     — use whatever cloud provider the admin
                                  configured via Settings UI / /ai/config
                                  (Anthropic, OpenAI, Gemini, Groq, …).
                                  This is the default now. Keeps "set an
                                  API key and it works" behavior.
  4. legacy Ollama fallback     — if no tenant provider is configured AND
                                  Ollama is reachable, use it.

Cached singleton per process; call reset_cache() in tests after
mutating configuration.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("enki.extraction.llm.factory")

_CACHED_CLIENT = None
_CACHE_KEY: Optional[str] = None


def _tenant_scope() -> str:
    """Default to the single-tenant 'default' scope so workers don't
    need to thread tenant_id through every call. Multi-tenant servers
    can override per-call via get_tenant_client(tenant_id)."""
    return os.environ.get("ENKI_LLM_TENANT", "default")


def get_tenant_client(tenant_id: str):
    """Resolve a per-tenant LLMClient. Bypasses the process-wide cache
    so cloud-provider swaps take effect without restart. Returns None
    if no provider is configured for this tenant."""
    from core.ai.resolver import get_effective_provider
    prov = get_effective_provider(tenant_id)
    if prov is None:
        return None
    from core.extraction.llm.tenant_client import TenantProviderClient
    return TenantProviderClient(prov)


def get_default_client():
    """Return the configured LLM client, or None if disabled / unreachable.

    Set ENKI_LLM_BACKEND=none to skip LLM calls entirely (deep pass
    becomes a no-op). Any other value is treated as a hint but the
    actual selection follows the precedence rules at the top of this
    file."""
    global _CACHED_CLIENT, _CACHE_KEY

    backend_override = os.environ.get("ENKI_LLM_BACKEND", "").lower().strip()
    tenant = _tenant_scope()
    cache_key = f"{backend_override}:{tenant}"

    if _CACHED_CLIENT is not None and _CACHE_KEY == cache_key:
        return _CACHED_CLIENT

    # Hard-off switch
    if backend_override == "none":
        _CACHED_CLIENT = None
        _CACHE_KEY = cache_key
        return None

    # Explicit Ollama override (useful for air-gap installs where we
    # DO NOT want to fall through to a cloud provider even if one is
    # configured).
    if backend_override == "ollama":
        client = _try_ollama()
        _CACHED_CLIENT = client
        _CACHE_KEY = cache_key
        return client

    # Default path: prefer tenant-configured provider, fall back to Ollama.
    client = get_tenant_client(tenant)
    if client is None:
        client = _try_ollama()
    else:
        log.info("LLM client: tenant provider (%s)", client.model_version)

    _CACHED_CLIENT = client
    _CACHE_KEY = cache_key
    return client


def _try_ollama():
    """Best-effort Ollama client — returns None if unreachable."""
    try:
        from core.extraction.llm.ollama import OllamaClient
        client = OllamaClient()
        if not client.health():
            log.warning("Ollama not reachable and no tenant provider configured — LLM disabled")
            return None
        log.info("LLM client: ollama fallback (%s model_version=%s)",
                 client.model_name, client.model_version)
        return client
    except Exception as e:
        log.warning("Ollama fallback failed: %s", e)
        return None


def reset_cache():
    """For tests or post-config-change flushes."""
    global _CACHED_CLIENT, _CACHE_KEY
    _CACHED_CLIENT = None
    _CACHE_KEY = None
