"""
Cloud LLM providers — plug-in services. Add API key and go.

Most providers use the OpenAI chat completions format (or close to it).
The OpenAICompatibleProvider handles: OpenAI, Groq, Together, Mistral,
DeepSeek, Perplexity, Fireworks, and any OpenAI-compatible endpoint.

Providers with unique APIs get their own class: Anthropic, Google Gemini, xAI Grok.

Usage:
  POST /ai/config {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-..."}
  POST /ai/config {"provider": "anthropic", "model": "claude-sonnet-4-20250514", "api_key": "sk-ant-..."}
  POST /ai/config {"provider": "gemini", "model": "gemini-2.0-flash", "api_key": "AIza..."}
  POST /ai/config {"provider": "grok", "model": "grok-3-mini", "api_key": "xai-..."}
  POST /ai/config {"provider": "groq", "model": "llama-3.3-70b-versatile", "api_key": "gsk_..."}
  POST /ai/config {"provider": "together", "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "api_key": "..."}
  POST /ai/config {"provider": "mistral", "model": "mistral-small-latest", "api_key": "..."}
  POST /ai/config {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-..."}
  POST /ai/config {"provider": "perplexity", "model": "sonar-pro", "api_key": "pplx-..."}
  POST /ai/config {"provider": "cohere", "model": "command-r-plus", "api_key": "..."}
  POST /ai/config {"provider": "fireworks", "model": "accounts/fireworks/models/llama-v3p3-70b-instruct", "api_key": "..."}
  POST /ai/config {"provider": "custom", "model": "my-model", "api_key": "...", "base_url": "https://my-server/v1"}
"""

from __future__ import annotations

import json
from typing import Optional
from urllib.request import urlopen, Request

from core.ai.provider import (
    BaseLLMProvider, LLMMessage, LLMResponse, WebSearchResult, register_provider,
)


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 90) -> dict:
    """POST JSON and return the parsed response (raises on transport error)."""
    req = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ─── OpenAI-Compatible Base (handles most providers) ──────────────────────────

class OpenAICompatibleProvider(BaseLLMProvider):
    """
    Generic provider for any API using OpenAI's chat completions format.
    Covers: OpenAI, Groq, Together, Mistral, DeepSeek, Perplexity,
    Fireworks, and any custom OpenAI-compatible endpoint.
    """

    provider_name = "openai_compatible"
    default_base_url = "https://api.openai.com/v1"
    default_models: list[str] = []
    auth_header = "Authorization"
    auth_prefix = "Bearer"

    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "")
        self.base_url = config.get("base_url", self.default_base_url).rstrip("/")

    def complete(self, messages, max_tokens=1000, temperature=0.1):
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers[self.auth_header] = f"{self.auth_prefix} {self.api_key}"

        try:
            req = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            return LLMResponse(
                content=data["choices"][0]["message"]["content"],
                model=self.model, provider=self.provider_name,
                usage=data.get("usage", {}),
            )
        except Exception as e:
            return LLMResponse(content="", model=self.model, provider=self.provider_name, error=str(e))

    def is_available(self):
        return bool(self.api_key)

    def list_models(self):
        return self.default_models or [self.model]


# ─── OpenAI ───────────────────────────────────────────────────────────────────

class OpenAIProvider(OpenAICompatibleProvider):
    provider_name = "openai"
    default_base_url = "https://api.openai.com/v1"
    default_models = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo", "o3-mini"]
    supports_web_search = True

    def web_search(self, query: str, max_tokens: int = 2000) -> WebSearchResult:
        """Native web search via the Responses API `web_search` tool.

        Parses the output text + `url_citation` annotations. The tool type has
        shipped under two names ("web_search" and "web_search_preview"); we try
        both so this keeps working across API revisions.
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers[self.auth_header] = f"{self.auth_prefix} {self.api_key}"
        url = f"{self.base_url}/responses"

        last_err = None
        for tool_type in ("web_search", "web_search_preview"):
            payload = {
                "model": self.model or "gpt-4o",
                "tools": [{"type": tool_type}],
                "input": query,
                "max_output_tokens": max_tokens,
            }
            try:
                data = _post_json(url, payload, headers)
            except Exception as e:  # HTTP 400 on an unknown tool type → try the next
                last_err = str(e)
                continue
            return self._parse_responses(data)

        return WebSearchResult(answer="", citations=[], model=self.model,
                               provider=self.provider_name, error=last_err or "web_search_failed")

    def _parse_responses(self, data: dict) -> WebSearchResult:
        text_parts: list[str] = []
        citations: list[dict] = []
        seen: set[str] = set()
        for item in data.get("output", []) or []:
            if item.get("type") != "message":
                continue
            for block in item.get("content", []) or []:
                if block.get("type") in ("output_text", "text"):
                    if block.get("text"):
                        text_parts.append(block["text"])
                    for ann in block.get("annotations", []) or []:
                        if ann.get("type") == "url_citation" and ann.get("url"):
                            u = ann["url"]
                            if u in seen:
                                continue
                            seen.add(u)
                            citations.append({"url": u, "title": ann.get("title", ""),
                                              "snippet": ""})
        # Fallback: some revisions expose a flat output_text convenience field.
        if not text_parts and data.get("output_text"):
            text_parts.append(data["output_text"])
        return WebSearchResult(
            answer="\n".join(text_parts).strip(), citations=citations,
            model=self.model, provider=self.provider_name,
            usage=data.get("usage", {}),
        )


# ─── Groq ─────────────────────────────────────────────────────────────────────

class GroqProvider(OpenAICompatibleProvider):
    provider_name = "groq"
    default_base_url = "https://api.groq.com/openai/v1"
    default_models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768", "gemma2-9b-it"]


# ─── Together ─────────────────────────────────────────────────────────────────

class TogetherProvider(OpenAICompatibleProvider):
    provider_name = "together"
    default_base_url = "https://api.together.xyz/v1"
    default_models = [
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "Qwen/Qwen2.5-72B-Instruct-Turbo",
    ]


# ─── Mistral ──────────────────────────────────────────────────────────────────

class MistralProvider(OpenAICompatibleProvider):
    provider_name = "mistral"
    default_base_url = "https://api.mistral.ai/v1"
    default_models = ["mistral-large-latest", "mistral-small-latest", "mistral-medium-latest", "open-mistral-nemo"]


# ─── DeepSeek ─────────────────────────────────────────────────────────────────

class DeepSeekProvider(OpenAICompatibleProvider):
    provider_name = "deepseek"
    default_base_url = "https://api.deepseek.com/v1"
    default_models = ["deepseek-chat", "deepseek-reasoner"]


# ─── Perplexity ───────────────────────────────────────────────────────────────

class PerplexityProvider(OpenAICompatibleProvider):
    provider_name = "perplexity"
    default_base_url = "https://api.perplexity.ai"
    default_models = ["sonar-pro", "sonar", "sonar-reasoning-pro"]
    supports_web_search = True

    def web_search(self, query: str, max_tokens: int = 2000) -> WebSearchResult:
        """Perplexity `sonar` models search the web natively; the answer's
        sources come back as `search_results` (titled) and/or `citations` (URLs).
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers[self.auth_header] = f"{self.auth_prefix} {self.api_key}"
        payload = {
            "model": self.model or "sonar-pro",
            "messages": [{"role": "user", "content": query}],
            "max_tokens": max_tokens,
        }
        try:
            data = _post_json(f"{self.base_url}/chat/completions", payload, headers)
        except Exception as e:
            return WebSearchResult(answer="", citations=[], model=self.model,
                                   provider="perplexity", error=str(e))

        answer = ""
        try:
            answer = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            pass
        citations: list[dict] = []
        seen: set[str] = set()
        for sr in data.get("search_results", []) or []:
            u = sr.get("url")
            if u and u not in seen:
                seen.add(u)
                citations.append({"url": u, "title": sr.get("title", ""), "snippet": ""})
        for u in data.get("citations", []) or []:
            if isinstance(u, str) and u not in seen:
                seen.add(u)
                citations.append({"url": u, "title": "", "snippet": ""})
        return WebSearchResult(answer=answer, citations=citations, model=self.model,
                               provider="perplexity", usage=data.get("usage", {}))


# ─── Fireworks ────────────────────────────────────────────────────────────────

class FireworksProvider(OpenAICompatibleProvider):
    provider_name = "fireworks"
    default_base_url = "https://api.fireworks.ai/inference/v1"
    default_models = [
        "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "accounts/fireworks/models/mixtral-8x22b-instruct",
    ]


# ─── xAI Grok ─────────────────────────────────────────────────────────────────

class GrokProvider(OpenAICompatibleProvider):
    provider_name = "grok"
    default_base_url = "https://api.x.ai/v1"
    default_models = ["grok-3-mini", "grok-3"]


# ─── Anthropic (unique API format) ────────────────────────────────────────────

class AnthropicProvider(BaseLLMProvider):
    provider_name = "anthropic"
    supports_web_search = True

    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "claude-sonnet-4-20250514")

    def web_search(self, query: str, max_tokens: int = 2000) -> WebSearchResult:
        """Native web search via the Messages API `web_search` server tool.

        Text lives in `text` blocks; sources come from `web_search_tool_result`
        blocks (a list of `web_search_result` items with url/title).
        """
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": query}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        try:
            data = _post_json("https://api.anthropic.com/v1/messages", payload, headers)
        except Exception as e:
            return WebSearchResult(answer="", citations=[], model=self.model,
                                   provider="anthropic", error=str(e))

        text_parts: list[str] = []
        citations: list[dict] = []
        seen: set[str] = set()
        for block in data.get("content", []) or []:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                text_parts.append(block["text"])
                for cit in block.get("citations", []) or []:
                    u = cit.get("url")
                    if u and u not in seen:
                        seen.add(u)
                        citations.append({"url": u, "title": cit.get("title", ""),
                                          "snippet": cit.get("cited_text", "")})
            elif btype == "web_search_tool_result":
                for res in block.get("content", []) or []:
                    if res.get("type") == "web_search_result" and res.get("url"):
                        u = res["url"]
                        if u not in seen:
                            seen.add(u)
                            citations.append({"url": u, "title": res.get("title", ""),
                                              "snippet": ""})
        return WebSearchResult(
            answer="\n".join(text_parts).strip(), citations=citations,
            model=self.model, provider="anthropic", usage=data.get("usage", {}),
        )

    def complete(self, messages, max_tokens=1000, temperature=0.1):
        system_msg = ""
        conv = []
        for m in messages:
            if m.role == "system":
                system_msg = m.content
            else:
                conv.append({"role": m.role, "content": m.content})

        payload = {"model": self.model, "max_tokens": max_tokens, "messages": conv}
        if system_msg:
            payload["system"] = system_msg

        try:
            req = Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(payload).encode(),
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            return LLMResponse(
                content=data["content"][0]["text"],
                model=self.model, provider="anthropic",
                usage=data.get("usage", {}),
            )
        except Exception as e:
            return LLMResponse(content="", model=self.model, provider="anthropic", error=str(e))

    def is_available(self):
        return bool(self.api_key)

    def list_models(self):
        return ["claude-opus-4-20250514", "claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"]


# ─── Google Gemini (unique API format) ────────────────────────────────────────

class GeminiProvider(BaseLLMProvider):
    provider_name = "gemini"

    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "gemini-2.0-flash")

    def complete(self, messages, max_tokens=1000, temperature=0.1):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"

        # Convert messages to Gemini format
        contents = []
        system_instruction = None
        for m in messages:
            if m.role == "system":
                system_instruction = m.content
            else:
                role = "user" if m.role == "user" else "model"
                contents.append({"role": role, "parts": [{"text": m.content}]})

        payload = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        try:
            req = Request(url, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            content = data["candidates"][0]["content"]["parts"][0]["text"]
            return LLMResponse(content=content, model=self.model, provider="gemini",
                               usage=data.get("usageMetadata", {}))
        except Exception as e:
            return LLMResponse(content="", model=self.model, provider="gemini", error=str(e))

    def is_available(self):
        return bool(self.api_key)

    def list_models(self):
        return ["gemini-2.5-pro-preview-06-05", "gemini-2.5-flash-preview-05-20", "gemini-2.0-flash", "gemini-2.0-flash-lite"]


# ─── Cohere (unique API format) ───────────────────────────────────────────────

class CohereProvider(BaseLLMProvider):
    provider_name = "cohere"

    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "command-r-plus")

    def complete(self, messages, max_tokens=1000, temperature=0.1):
        preamble = ""
        chat_history = []
        user_message = ""
        for m in messages:
            if m.role == "system":
                preamble = m.content
            elif m.role == "user":
                user_message = m.content
            elif m.role == "assistant":
                chat_history.append({"role": "CHATBOT", "message": m.content})

        payload = {
            "model": self.model,
            "message": user_message,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if preamble:
            payload["preamble"] = preamble
        if chat_history:
            payload["chat_history"] = chat_history

        try:
            req = Request(
                "https://api.cohere.ai/v1/chat",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            return LLMResponse(content=data.get("text", ""), model=self.model, provider="cohere",
                               usage=data.get("meta", {}).get("tokens", {}))
        except Exception as e:
            return LLMResponse(content="", model=self.model, provider="cohere", error=str(e))

    def is_available(self):
        return bool(self.api_key)

    def list_models(self):
        return ["command-r-plus", "command-r", "command-light"]


# ─── Custom (user provides base URL for any OpenAI-compatible endpoint) ───────

class CustomProvider(OpenAICompatibleProvider):
    provider_name = "custom"
    default_base_url = ""
    default_models = []

    def __init__(self, config: dict):
        super().__init__(config)
        if not self.base_url:
            self.base_url = config.get("base_url", "http://localhost:8080/v1")

    def is_available(self):
        return bool(self.base_url)


# ─── Register all providers ──────────────────────────────────────────────────

register_provider("openai", OpenAIProvider)
register_provider("anthropic", AnthropicProvider)
register_provider("gemini", GeminiProvider)
register_provider("grok", GrokProvider)
register_provider("groq", GroqProvider)
register_provider("together", TogetherProvider)
register_provider("mistral", MistralProvider)
register_provider("deepseek", DeepSeekProvider)
register_provider("perplexity", PerplexityProvider)
register_provider("fireworks", FireworksProvider)
register_provider("cohere", CohereProvider)
register_provider("custom", CustomProvider)
