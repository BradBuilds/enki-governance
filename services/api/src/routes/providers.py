"""
AI Providers API — multi-provider registry.

GET    /providers              List configured providers for tenant
POST   /providers              Add a new provider
PUT    /providers/:id          Update provider config
DELETE /providers/:id          Remove provider
POST   /providers/:id/test    Test provider connection
POST   /providers/:id/default Set as tenant default
GET    /providers/catalog      List all available provider types (static)
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

import json
import uuid

from storage.postgres.db import get_connection
from core.ai.provider import get_provider, LLMMessage

router = APIRouter(prefix="/providers", tags=["providers"])


class AddProviderRequest(BaseModel):
    provider: str               # ollama, openai, anthropic, gemini, etc.
    model: str
    display_name: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    capabilities: list[str] = []
    is_default: bool = False


class UpdateProviderRequest(BaseModel):
    model: Optional[str] = None
    display_name: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    capabilities: Optional[list[str]] = None
    enabled: Optional[bool] = None


@router.get("")
async def list_providers(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT provider_id, provider, model, display_name, capabilities,
                       is_default, enabled, created_at
                FROM ai_providers WHERE tenant_id = %s
                ORDER BY is_default DESC, created_at ASC
            """, (auth.tenant_id,))
            rows = cur.fetchall()

    providers = [{
        "provider_id": r[0], "provider": r[1], "model": r[2],
        "display_name": r[3], "capabilities": r[4] or [],
        "is_default": r[5], "enabled": r[6],
        "created_at": r[7].isoformat() if r[7] else None,
    } for r in rows]

    return {"providers": providers, "count": len(providers)}


@router.post("")
async def add_provider(
    body: AddProviderRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    x_device_id: str = Header("unknown", alias="X-Device-ID"),
):
    provider_id = f"prov-{uuid.uuid4().hex[:12]}"

    config = {}
    if body.api_key:
        config["api_key"] = body.api_key
    if body.base_url:
        config["base_url"] = body.base_url

    display_name = body.display_name or f"{body.provider}/{body.model}"

    with get_connection() as conn:
        with conn.cursor() as cur:
            # If setting as default, clear other defaults
            if body.is_default:
                cur.execute("""
                    UPDATE ai_providers SET is_default = FALSE
                    WHERE tenant_id = %s AND is_default = TRUE
                """, (auth.tenant_id,))

            # If this is the first provider, make it default
            cur.execute("SELECT COUNT(*) FROM ai_providers WHERE tenant_id = %s", (auth.tenant_id,))
            count = cur.fetchone()[0]
            is_default = body.is_default or count == 0

            cur.execute("""
                INSERT INTO ai_providers
                    (provider_id, tenant_id, provider, model, display_name,
                     config, capabilities, is_default, configured_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                provider_id, auth.tenant_id, body.provider, body.model,
                display_name, json.dumps(config), body.capabilities,
                is_default, x_device_id,
            ))

            # Also sync to legacy ai_config if this is default
            if is_default:
                cur.execute("""
                    INSERT INTO ai_config (tenant_id, provider, model, config, configured_by)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        provider = EXCLUDED.provider, model = EXCLUDED.model,
                        config = EXCLUDED.config, updated_at = NOW()
                """, (auth.tenant_id, body.provider, body.model, json.dumps(config), x_device_id))

        conn.commit()

    return {
        "provider_id": provider_id,
        "provider": body.provider,
        "model": body.model,
        "display_name": display_name,
        "is_default": is_default,
    }


@router.put("/{provider_id}")
async def update_provider(
    provider_id: str,
    body: UpdateProviderRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    sets = []
    params = []

    if body.model is not None:
        sets.append("model = %s")
        params.append(body.model)
    if body.display_name is not None:
        sets.append("display_name = %s")
        params.append(body.display_name)
    if body.enabled is not None:
        sets.append("enabled = %s")
        params.append(body.enabled)
    if body.capabilities is not None:
        sets.append("capabilities = %s")
        params.append(body.capabilities)

    # Handle config updates (merge into existing)
    config_updates = {}
    if body.api_key is not None:
        config_updates["api_key"] = body.api_key
    if body.base_url is not None:
        config_updates["base_url"] = body.base_url

    if config_updates:
        sets.append("config = config || %s")
        params.append(json.dumps(config_updates))

    if not sets:
        return {"updated": False}

    sets.append("updated_at = NOW()")
    params.extend([provider_id, auth.tenant_id])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE ai_providers SET {', '.join(sets)}
                WHERE provider_id = %s AND tenant_id = %s
            """, params)
        conn.commit()

    return {"updated": True, "provider_id": provider_id}


@router.delete("/{provider_id}")
async def remove_provider(
    provider_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM ai_providers WHERE provider_id = %s AND tenant_id = %s
            """, (provider_id, auth.tenant_id))
        conn.commit()
    return {"deleted": True}


@router.post("/{provider_id}/test")
async def test_provider(
    provider_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    from storage.secrets import resolve_secrets

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT provider, model, config FROM ai_providers
                WHERE provider_id = %s AND tenant_id = %s
            """, (provider_id, auth.tenant_id))
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "Provider not found"})

    config = row[2]
    if isinstance(config, memoryview):
        config = json.loads(bytes(config))
    elif isinstance(config, str):
        config = json.loads(config)

    config = resolve_secrets(config, auth.tenant_id)
    config["model"] = row[1]

    instance = get_provider(row[0], config)
    if not instance:
        return {"success": False, "error": f"Unknown provider type: {row[0]}"}

    if not instance.is_available():
        return {"success": False, "error": f"{row[0]} is not reachable"}

    response = instance.complete(
        [LLMMessage(role="user", content="Say 'hello' in one word.")],
        max_tokens=10,
    )

    if response.error:
        return {"success": False, "error": response.error}

    return {
        "success": True,
        "provider": row[0],
        "model": response.model,
        "test_response": response.content[:50],
    }


@router.post("/{provider_id}/default")
async def set_default(
    provider_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ai_providers SET is_default = FALSE
                WHERE tenant_id = %s AND is_default = TRUE
            """, (auth.tenant_id,))
            cur.execute("""
                UPDATE ai_providers SET is_default = TRUE, updated_at = NOW()
                WHERE provider_id = %s AND tenant_id = %s
                RETURNING provider, model, config
            """, (provider_id, auth.tenant_id))
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "Provider not found"})

            # Sync to legacy ai_config
            cur.execute("""
                INSERT INTO ai_config (tenant_id, provider, model, config)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    provider = EXCLUDED.provider, model = EXCLUDED.model,
                    config = EXCLUDED.config, updated_at = NOW()
            """, (auth.tenant_id, row[0], row[1], json.dumps(row[2]) if isinstance(row[2], dict) else row[2]))
        conn.commit()

    return {"default": True, "provider_id": provider_id}


@router.get("/catalog")
async def get_catalog():
    """Static catalog of all supported provider types."""
    return {
        "providers": [
            {"name": "ollama", "type": "local", "label": "Ollama (Local)",
             "models": ["gemma4:e2b", "gemma4:e4b", "gemma4:26b", "llama3.2:3b", "mistral:7b", "qwen2.5:7b"],
             "capabilities": ["local", "fast"]},
            {"name": "openai", "type": "cloud", "label": "OpenAI",
             "models": ["gpt-4o", "gpt-4o-mini", "o3-mini"],
             "capabilities": ["reasoning", "vision"]},
            {"name": "anthropic", "type": "cloud", "label": "Anthropic",
             "models": ["claude-opus-4-20250514", "claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
             "capabilities": ["reasoning", "vision"]},
            {"name": "gemini", "type": "cloud", "label": "Google Gemini",
             "models": ["gemini-2.5-pro-preview-06-05", "gemini-2.0-flash", "gemini-2.0-flash-lite"],
             "capabilities": ["reasoning", "search", "vision"]},
            {"name": "grok", "type": "cloud", "label": "xAI Grok",
             "models": ["grok-3", "grok-3-mini"],
             "capabilities": ["reasoning", "fast"]},
            {"name": "groq", "type": "cloud", "label": "Groq",
             "models": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"],
             "capabilities": ["fast"]},
            {"name": "deepseek", "type": "cloud", "label": "DeepSeek",
             "models": ["deepseek-chat", "deepseek-reasoner"],
             "capabilities": ["reasoning"]},
            {"name": "perplexity", "type": "cloud", "label": "Perplexity",
             "models": ["sonar-pro", "sonar"],
             "capabilities": ["search"]},
            {"name": "mistral", "type": "cloud", "label": "Mistral AI",
             "models": ["mistral-large-latest", "mistral-small-latest"],
             "capabilities": ["reasoning", "fast"]},
            {"name": "together", "type": "cloud", "label": "Together AI",
             "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"],
             "capabilities": ["fast"]},
            {"name": "cohere", "type": "cloud", "label": "Cohere",
             "models": ["command-r-plus", "command-r"],
             "capabilities": ["search"]},
            {"name": "fireworks", "type": "cloud", "label": "Fireworks AI",
             "models": ["accounts/fireworks/models/llama-v3p3-70b-instruct"],
             "capabilities": ["fast"]},
            {"name": "custom", "type": "custom", "label": "Custom (OpenAI-compatible)",
             "models": [], "capabilities": []},
        ]
    }
