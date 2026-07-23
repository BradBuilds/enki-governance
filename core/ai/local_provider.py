"""
Local LLM provider — Ollama integration.

Downloads and runs models locally. No data leaves the server.
Required for LE deployments where data cannot go to cloud.

Ollama must be installed separately:
  curl -fsSL https://ollama.com/install.sh | sh

Then pull models:
  ollama pull llama3.2:3b          (2GB, runs on 4GB RAM — chat)
  ollama pull nomic-embed-text     (274MB — embeddings, 768 dims)
  ollama pull mistral:7b           (4GB, runs on 8GB RAM — chat)
  ollama pull llama3.1:8b          (5GB, runs on 8GB RAM — chat)

Or download from HuggingFace and import:
  ollama create mymodel -f Modelfile
"""

from __future__ import annotations

import json
import os
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from core.ai.provider import BaseLLMProvider, LLMMessage, LLMResponse, register_provider

DEFAULT_EMBED_MODEL = "nomic-embed-text"


class OllamaProvider(BaseLLMProvider):
    """Local LLM via Ollama. Data never leaves the server."""

    provider_name = "ollama"

    def __init__(self, config: dict):
        self.base_url = config.get(
            "base_url",
            os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        )
        self.model = config.get("model", "gemma4:e2b")
        self.embed_model = config.get("embed_model", DEFAULT_EMBED_MODEL)
        # Determinism controls (for the two-node A/B). When ENKI_LLM_SEED is set,
        # every generation pins that seed; ENKI_LLM_TEMPERATURE overrides the
        # default sampling temperature (set to 0 for greedy decoding). Both nodes
        # export the SAME values so the only remaining variable is the GPU.
        # Unset → unchanged production behaviour.
        seed_env = config.get("seed", os.environ.get("ENKI_LLM_SEED"))
        self.seed: Optional[int] = int(seed_env) if seed_env not in (None, "") else None
        temp_env = config.get("temperature", os.environ.get("ENKI_LLM_TEMPERATURE"))
        self.temperature_override: Optional[float] = (
            float(temp_env) if temp_env not in (None, "") else None
        )

    def complete(
        self,
        messages: list[LLMMessage],
        max_tokens: int = 1000,
        temperature: float = 0.1,
    ) -> LLMResponse:
        url = f"{self.base_url}/api/chat"
        options = {
            "num_predict": max_tokens,
            "temperature": self.temperature_override if self.temperature_override is not None else temperature,
        }
        if self.seed is not None:
            options["seed"] = self.seed
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "think": False,
            "options": options,
        }

        try:
            req = Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            # Fast-fail: a GPU call returns in seconds; a long timeout lets one
            # hung/slow call (noisy OCR) stall the whole collapse for minutes.
            _to = int(os.environ.get("ENKI_LLM_TIMEOUT", "90"))
            with urlopen(req, timeout=_to) as resp:
                data = json.loads(resp.read())

            content = data.get("message", {}).get("content", "")

            return LLMResponse(
                content=content,
                model=self.model,
                provider=self.provider_name,
                usage={
                    "prompt_tokens": data.get("prompt_eval_count", 0),
                    "completion_tokens": data.get("eval_count", 0),
                },
            )
        except URLError as e:
            return LLMResponse(
                content="",
                model=self.model,
                provider=self.provider_name,
                error=f"Ollama not reachable at {self.base_url}. Is Ollama running? "
                      f"Install: curl -fsSL https://ollama.com/install.sh | sh — Error: {e}",
            )
        except Exception as e:
            return LLMResponse(
                content="", model=self.model, provider=self.provider_name,
                error=str(e),
            )

    def is_available(self) -> bool:
        """Check if Ollama is running and has the model."""
        try:
            req = Request(f"{self.base_url}/api/tags")
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            return self.model in models or any(self.model.split(":")[0] in m for m in models)
        except Exception:
            return False

    def list_models(self) -> list[str]:
        """List models available in local Ollama."""
        try:
            req = Request(f"{self.base_url}/api/tags")
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def embed(self, text: str) -> list[float]:
        """Generate an embedding vector using the embedding model (nomic-embed-text).

        Returns a 768-dimensional float vector.
        Raises on failure so the caller can decide to skip.
        """
        url = f"{self.base_url}/api/embed"
        payload = {"model": self.embed_model, "input": text}

        req = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        embeddings = data.get("embeddings")
        if embeddings and len(embeddings) > 0:
            return embeddings[0]
        raise ValueError(f"No embedding returned from {self.embed_model}")

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in one call."""
        url = f"{self.base_url}/api/embed"
        payload = {"model": self.embed_model, "input": texts}

        req = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())

        return data.get("embeddings", [])

    def pull_model(self, model_name: str) -> dict:
        """Download a model from Ollama registry or HuggingFace."""
        url = f"{self.base_url}/api/pull"
        payload = {"name": model_name, "stream": False}

        try:
            req = Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=600) as resp:  # 10 min timeout for large models
                data = json.loads(resp.read())
            return {"success": True, "status": data.get("status", "success")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def ensure_models(self) -> dict:
        """Ensure both chat and embedding models are available. Pull if missing."""
        models = self.list_models()
        results = {}

        for model_name in [self.model, self.embed_model]:
            has_model = model_name in models or any(
                model_name.split(":")[0] in m for m in models
            )
            if has_model:
                results[model_name] = "ready"
            else:
                pull_result = self.pull_model(model_name)
                results[model_name] = "pulled" if pull_result["success"] else pull_result.get("error", "failed")

        return results


# Register
register_provider("ollama", OllamaProvider)


def get_default_provider() -> Optional[OllamaProvider]:
    """Load the default AI provider from the database config.

    Returns an OllamaProvider configured to connect to wherever
    the admin pointed it (local Ollama, production server, etc.).
    Returns None if no provider is configured or unreachable.
    """
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT provider, config FROM ai_providers
                    WHERE tenant_id = 'default' AND is_default = true AND enabled = true
                    LIMIT 1
                """)
                row = cur.fetchone()
                if not row:
                    return None

                provider_name = row[0]
                config = row[1] if isinstance(row[1], dict) else json.loads(row[1]) if row[1] else {}

                if provider_name == "ollama":
                    provider = OllamaProvider(config)
                    if provider.is_available():
                        return provider

    except Exception as e:
        import logging
        logging.getLogger("enki.ai").debug("Failed to load default provider: %s", e)

    return None
