"""
AI configuration and chat API endpoints.

POST /ai/config        Set LLM provider (Tier K admin only)
GET  /ai/config        Current AI configuration
POST /ai/config/test   Test AI connection
POST /ai/chat          Send message, get AI response with actions
GET  /ai/models        List available models
POST /ai/models/pull   Download a model (Ollama only)
GET  /ai/providers     List available provider types
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

from core.ai.config import get_ai_config, set_ai_config, get_tenant_provider, test_ai_connection
from core.ai.chat import process_chat_message
from core.ai.executor import execute_actions
from core.ai.provider import list_providers, LLMMessage

router = APIRouter(prefix="/ai", tags=["ai"])


class ConfigRequest(BaseModel):
    provider: str               # "ollama", "openai", "anthropic"
    model: str                  # "llama3.2:3b", "gpt-4o-mini", "claude-sonnet-4-20250514"
    api_key: Optional[str] = None
    base_url: Optional[str] = None


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


class PullModelRequest(BaseModel):
    model_name: str             # "llama3.2:3b", "mistral:7b", etc.


@router.get("/providers")
async def list_provider_types():
    """List all available LLM providers. Add API key and go."""
    return {
        "providers": [
            {"name": "ollama", "type": "local", "description": "Local models via Ollama. Data never leaves server.",
             "setup": "Install: curl -fsSL https://ollama.com/install.sh | sh",
             "models": ["gemma4:e2b", "gemma4:e4b", "gemma4:26b", "llama3.2:3b", "mistral:7b", "llama3.1:8b", "qwen2.5:7b"]},
            {"name": "openai", "type": "cloud", "description": "OpenAI (GPT-4o, o3-mini)",
             "setup": "Get key from platform.openai.com",
             "models": ["gpt-4o", "gpt-4o-mini", "o3-mini"]},
            {"name": "anthropic", "type": "cloud", "description": "Anthropic (Claude Opus, Sonnet, Haiku)",
             "setup": "Get key from console.anthropic.com",
             "models": ["claude-opus-4-20250514", "claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"]},
            {"name": "gemini", "type": "cloud", "description": "Google Gemini (2.5 Pro, 2.0 Flash)",
             "setup": "Get key from aistudio.google.com",
             "models": ["gemini-2.5-pro-preview-06-05", "gemini-2.0-flash", "gemini-2.0-flash-lite"]},
            {"name": "grok", "type": "cloud", "description": "xAI Grok (Grok-3)",
             "setup": "Get key from console.x.ai",
             "models": ["grok-3", "grok-3-mini"]},
            {"name": "groq", "type": "cloud", "description": "Groq (fast inference, Llama/Mixtral)",
             "setup": "Get key from console.groq.com",
             "models": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"]},
            {"name": "together", "type": "cloud", "description": "Together AI (open source models)",
             "setup": "Get key from api.together.xyz",
             "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"]},
            {"name": "mistral", "type": "cloud", "description": "Mistral AI (Mistral Large/Small)",
             "setup": "Get key from console.mistral.ai",
             "models": ["mistral-large-latest", "mistral-small-latest"]},
            {"name": "deepseek", "type": "cloud", "description": "DeepSeek (DeepSeek-V3, R1)",
             "setup": "Get key from platform.deepseek.com",
             "models": ["deepseek-chat", "deepseek-reasoner"]},
            {"name": "perplexity", "type": "cloud", "description": "Perplexity (search-augmented AI)",
             "setup": "Get key from perplexity.ai/settings",
             "models": ["sonar-pro", "sonar"]},
            {"name": "cohere", "type": "cloud", "description": "Cohere (Command R+)",
             "setup": "Get key from dashboard.cohere.com",
             "models": ["command-r-plus", "command-r"]},
            {"name": "fireworks", "type": "cloud", "description": "Fireworks AI (fast open source)",
             "setup": "Get key from fireworks.ai",
             "models": ["accounts/fireworks/models/llama-v3p3-70b-instruct"]},
            {"name": "custom", "type": "custom", "description": "Any OpenAI-compatible endpoint (vLLM, LiteLLM, etc.)",
             "setup": "Provide base_url and optional api_key",
             "models": []},
        ]
    }


@router.post("/config")
async def configure_ai(
    body: ConfigRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    x_device_id: str = Header("unknown", alias="X-Device-ID"),
):
    """Configure AI provider for this tenant. Syncs to both legacy ai_config and new ai_providers."""
    config = {}
    if body.api_key:
        config["api_key"] = body.api_key
    if body.base_url:
        config["base_url"] = body.base_url

    # Write to legacy table
    result = set_ai_config(
        tenant_id=auth.tenant_id,
        provider=body.provider,
        model=body.model,
        config=config,
        configured_by=x_device_id,
    )

    # Sync to new ai_providers table (upsert as default)
    try:
        import json as _json
        import uuid
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Check if provider already exists for this tenant + provider + model
                cur.execute("""
                    SELECT provider_id FROM ai_providers
                    WHERE tenant_id = %s AND provider = %s AND model = %s
                    LIMIT 1
                """, (auth.tenant_id, body.provider, body.model))
                existing = cur.fetchone()
                if existing:
                    cur.execute("""
                        UPDATE ai_providers SET config = %s, is_default = TRUE, updated_at = NOW()
                        WHERE provider_id = %s
                    """, (_json.dumps(config), existing[0]))
                else:
                    # Clear other defaults
                    cur.execute("UPDATE ai_providers SET is_default = FALSE WHERE tenant_id = %s", (auth.tenant_id,))
                    cur.execute("""
                        INSERT INTO ai_providers
                            (provider_id, tenant_id, provider, model, display_name,
                             config, is_default, configured_by)
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s)
                    """, (f"prov-{uuid.uuid4().hex[:12]}", auth.tenant_id, body.provider,
                          body.model, f"{body.provider}/{body.model}",
                          _json.dumps(config), x_device_id))
            conn.commit()
    except Exception:
        pass  # best-effort sync — legacy table is still authoritative

    return JSONResponse(status_code=201, content=result)


@router.get("/config")
async def get_config(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Get current AI configuration (API keys masked)."""
    config = get_ai_config(auth.tenant_id)
    if not config:
        return {"configured": False, "provider": None, "model": None}

    # Mask API key and secret references in response
    safe_config = dict(config)
    if isinstance(safe_config.get("config"), dict):
        from storage.secrets import mask_secrets_in_config
        safe_config["config"] = mask_secrets_in_config(safe_config["config"])
        if "api_key" in safe_config["config"]:
            key = safe_config["config"]["api_key"]
            if key and not key.startswith("•"):  # not already masked by secret ref
                safe_config["config"]["api_key"] = key[:8] + "..." if len(key) > 8 else "***"

    return {"configured": True, **safe_config}


@router.post("/config/test")
async def test_config(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Test if the configured AI provider is working."""
    result = test_ai_connection(auth.tenant_id)
    status = 200 if result["success"] else 503
    return JSONResponse(status_code=status, content=result)


@router.post("/chat")
async def chat(
    body: ChatRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Send a message to Enki AI. Returns natural language response
    plus structured API actions to execute.

    The frontend executes the actions and renders results as cards.
    """
    provider = get_tenant_provider(auth.tenant_id, role="chat")
    if not provider:
        return JSONResponse(status_code=200, content={
            "response": "AI is not configured. Go to Settings → AI to set up a language model. "
                        "You can use a local model (Ollama) or cloud API (OpenAI, Anthropic).",
            "actions": [],
            "configured": False,
        })

    result = process_chat_message(
        user_message=body.message,
        provider=provider,
        tenant_context={"tenant_id": auth.tenant_id},
    )

    # Execute any actions the AI generated — server-side, not client-side
    if result.get("actions"):
        action_results = execute_actions(
            actions=result["actions"],
            tenant_id=auth.tenant_id,
        )
        result["action_results"] = action_results
    else:
        result["action_results"] = []

    return result


# ─── Connect Wizard ──────────────────────────────────────────────────────────

class ConnectWizardRequest(BaseModel):
    message: str
    conversation_history: Optional[list[dict]] = None


@router.post("/connect-wizard")
async def connect_wizard(
    body: ConnectWizardRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    AI-guided connector wizard. Helps users connect any device or data
    source through conversational guidance and code generation.
    """
    import os
    provider = get_tenant_provider(auth.tenant_id)
    if not provider:
        return JSONResponse(status_code=200, content={
            "response": "AI is not configured. Go to Settings → AI to set up a language model.",
            "actions": [],
            "action_results": [],
            "configured": False,
        })

    from core.ai.connect_wizard import process_wizard_message
    server_url = os.environ.get("ENKI_PUBLIC_URL", "https://example.com")

    result = process_wizard_message(
        user_message=body.message,
        provider=provider,
        server_url=server_url,
        tenant_id=auth.tenant_id,
        conversation_history=body.conversation_history,
    )

    # Execute wizard actions server-side
    if result.get("actions"):
        action_results = execute_actions(
            actions=result["actions"],
            tenant_id=auth.tenant_id,
        )
        result["action_results"] = action_results
    else:
        result["action_results"] = []

    return result


@router.get("/models")
async def list_models(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List available models for the configured provider."""
    provider = get_tenant_provider(auth.tenant_id)
    if not provider:
        return {"models": [], "provider": None, "configured": False}

    models = provider.list_models()
    return {
        "models": models,
        "provider": provider.provider_name,
        "current_model": getattr(provider, "model", None),
    }


@router.post("/models/pull")
async def pull_model(
    body: PullModelRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Download a model (Ollama only). This can take several minutes."""
    provider = get_tenant_provider(auth.tenant_id)
    if not provider:
        return JSONResponse(status_code=400, content={"error": "No AI provider configured"})

    if provider.provider_name != "ollama":
        return JSONResponse(status_code=400, content={
            "error": "Model download only supported for Ollama (local models)"
        })

    result = provider.pull_model(body.model_name)
    status = 200 if result.get("success") else 500
    return JSONResponse(status_code=status, content=result)


@router.post("/setup/ollama")
async def setup_ollama(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    x_device_id: str = Header("unknown", alias="X-Device-ID"),
):
    """
    One-click Ollama setup: configure provider, pull chat + embedding models.

    This sets up the full offline AI stack:
      - Chat model: llama3.2:3b (2GB, runs on 4GB RAM)
      - Embedding model: nomic-embed-text (274MB, 768-dim vectors)
    """
    import os
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

    # Configure Ollama as the provider
    config = {"base_url": base_url}
    set_ai_config(
        tenant_id=auth.tenant_id,
        provider="ollama",
        model="llama3.2:3b",
        config=config,
        configured_by=x_device_id,
    )

    # Get provider and ensure models are available
    provider = get_tenant_provider(auth.tenant_id)
    if not provider:
        return JSONResponse(status_code=500, content={"error": "Failed to create Ollama provider"})

    if not hasattr(provider, "ensure_models"):
        return JSONResponse(status_code=400, content={"error": "Provider does not support model management"})

    models_result = provider.ensure_models()

    # Test connection
    test_result = test_ai_connection(auth.tenant_id)

    return {
        "provider": "ollama",
        "base_url": base_url,
        "chat_model": "llama3.2:3b",
        "embed_model": "nomic-embed-text",
        "models": models_result,
        "test": test_result,
    }
