#!/usr/bin/env python3
"""Configure the default tenant's AI provider to the local Ollama model (from env),
so extraction works out of the box on a fresh install. Idempotent."""
import os
from core.ai.config import set_ai_config

model = os.environ.get("OLLAMA_MODEL", "gemma4:e2b")
base = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
set_ai_config("default", "ollama", model, {"base_url": base, "host": base})
print(f"configured default AI provider: ollama / {model} @ {base}")
