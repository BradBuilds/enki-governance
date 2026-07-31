"""
AI configuration management.

Stores LLM provider settings per tenant:
  - Which provider (ollama, openai, anthropic)
  - API key (encrypted for cloud providers)
  - Model selection
  - Custom endpoint URL (for self-hosted or proxied APIs)
"""

import json
from typing import Optional

from storage.postgres.db import get_connection
from core.ai.provider import get_provider, list_providers, BaseLLMProvider


def get_ai_config(
    tenant_id: str,
    dsn: Optional[str] = None,
    role: str = "default",
) -> Optional[dict]:
    """Get AI configuration for a tenant, optionally for a specific role.

    Roles let chat and bulk extraction point at different providers — see
    migration 135. A role with no row of its own falls back to the tenant's
    'default' row, so callers that don't care about roles are unaffected.
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Prefer the requested role; fall back to 'default'. Ordering by
            # (role = requested) DESC puts the exact match first when both exist.
            cur.execute("""
                SELECT provider, model, config, status, role
                FROM ai_config
                WHERE tenant_id = %s AND role IN (%s, 'default')
                ORDER BY (role = %s) DESC
                LIMIT 1
            """, (tenant_id, role, role))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "provider": row[0],
                "model": row[1],
                "config": row[2],  # API keys masked in response
                "status": row[3],
                "role": row[4],
            }


def set_ai_config(
    tenant_id: str,
    provider: str,
    model: str,
    config: dict,
    configured_by: str = "admin",
    dsn: Optional[str] = None,
    role: str = "default",
) -> dict:
    """Set or update AI configuration for a tenant (optionally per role)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ai_config (tenant_id, role, provider, model, config, configured_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, role) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    model = EXCLUDED.model,
                    config = EXCLUDED.config,
                    configured_by = EXCLUDED.configured_by,
                    updated_at = NOW()
            """, (tenant_id, role, provider, model, json.dumps(config), configured_by))
            conn.commit()

    return {"tenant_id": tenant_id, "role": role, "provider": provider,
            "model": model, "status": "configured"}


def get_tenant_provider(
    tenant_id: str,
    dsn: Optional[str] = None,
    role: str = "default",
) -> Optional[BaseLLMProvider]:
    """Get a configured LLM provider instance for a tenant.

    Pass role="chat" for interactive surfaces so they resolve independently of
    whatever bulk extraction is pointed at. Resolves any $secret:name
    references in the config before creating the provider instance.
    """
    ai_config = get_ai_config(tenant_id, dsn, role=role)
    if not ai_config:
        return None

    provider_name = ai_config["provider"]
    config = ai_config.get("config", {})
    if isinstance(config, str):
        config = json.loads(config)

    # Resolve secret references (e.g., api_key: "$secret:openai-key")
    from storage.secrets import resolve_secrets
    config = resolve_secrets(config, tenant_id, dsn)

    config["model"] = ai_config.get("model", config.get("model", ""))

    return get_provider(provider_name, config)


def test_ai_connection(tenant_id: str, dsn: Optional[str] = None) -> dict:
    """Test if the AI provider is working."""
    provider = get_tenant_provider(tenant_id, dsn)
    if not provider:
        return {"success": False, "error": "No AI provider configured"}

    if not provider.is_available():
        return {"success": False, "error": f"{provider.provider_name} is not reachable or missing API key"}

    # Quick test
    from core.ai.provider import LLMMessage
    response = provider.complete(
        [LLMMessage(role="user", content="Say 'hello' in one word.")],
        max_tokens=10,
    )

    if response.error:
        return {"success": False, "error": response.error}

    return {
        "success": True,
        "provider": provider.provider_name,
        "model": response.model,
        "test_response": response.content[:50],
    }
