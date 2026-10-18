"""
LLM client layer for Phase 4 deep extraction.

Generic over backends so the extraction pipeline doesn't depend on
Ollama specifically — Phase 5+ can drop in vLLM, llama.cpp, or a
hosted API by implementing the same Protocol.

The cheap pass continues to handle 95% of extraction work via regex
and gazetteer; LLMs are only invoked when the deep-pass gate fires
(garbage title, thin narrative, designated target, language gap).

Public API:
    from core.extraction.llm import get_default_client, LLMClient

    client = get_default_client()  # reads ENKI_LLM_BACKEND env var
    text = client.complete("Summarize this:", max_tokens=200)
    obj  = client.complete_json("Extract entities", schema=...)
"""

from core.extraction.llm.base import LLMClient, LLMResult
from core.extraction.llm.factory import get_default_client

__all__ = ["LLMClient", "LLMResult", "get_default_client"]
