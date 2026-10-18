"""
Secrets registry — named secrets for connectors and AI config.

Secrets are stored in the DB (which is behind the application layer).
They are NEVER returned in API responses. Connectors and AI config
reference secrets by name using the $secret:name syntax.

Resolution happens at use time: when a connector fetches or AI provider
is called, $secret: references are resolved to actual values.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Optional

from storage.postgres.db import get_connection


# ─── Pattern for secret references in config values ──────────────────────────
# Matches: $secret:my-api-key or ${secret:my-api-key}
SECRET_REF_PATTERN = re.compile(r'\$\{?secret:([a-zA-Z0-9_\-]+)\}?')

# Characters allowed in secret names
SECRET_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,253}[a-zA-Z0-9]$')


def create_secret(
    tenant_id: str,
    name: str,
    value: str,
    category: str = "api_key",
    description: str = "",
    created_by: str = "system",
    dsn: Optional[str] = None,
) -> dict:
    """Create or update a named secret."""
    if not SECRET_NAME_PATTERN.match(name):
        raise ValueError(
            f"Invalid secret name '{name}'. Use letters, numbers, hyphens, underscores (2-255 chars)."
        )

    secret_id = f"sec_{uuid.uuid4().hex[:12]}"

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Upsert: update value if name already exists for this tenant
            cur.execute("""
                INSERT INTO secrets (secret_id, tenant_id, name, value, category,
                                     description, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, name)
                DO UPDATE SET value = EXCLUDED.value,
                              category = EXCLUDED.category,
                              description = EXCLUDED.description,
                              updated_at = NOW()
                RETURNING secret_id
            """, (secret_id, tenant_id, name, value, category, description, created_by))
            actual_id = cur.fetchone()[0]
            conn.commit()

    return {
        "secret_id": actual_id,
        "name": name,
        "category": category,
        "description": description,
        "created": True,
    }


def get_secret_value(
    tenant_id: str, name: str, dsn: Optional[str] = None,
) -> Optional[str]:
    """Get a secret's actual value. Internal use only — never expose via API."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM secrets WHERE tenant_id = %s AND name = %s",
                (tenant_id, name),
            )
            r = cur.fetchone()
            return r[0] if r else None


def list_secrets(
    tenant_id: str, category: Optional[str] = None, dsn: Optional[str] = None,
) -> list[dict]:
    """List secrets for a tenant. Values are NEVER included."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if category:
                cur.execute("""
                    SELECT secret_id, name, category, description, used_by,
                           created_at, updated_at
                    FROM secrets
                    WHERE tenant_id = %s AND category = %s
                    ORDER BY name
                """, (tenant_id, category))
            else:
                cur.execute("""
                    SELECT secret_id, name, category, description, used_by,
                           created_at, updated_at
                    FROM secrets
                    WHERE tenant_id = %s
                    ORDER BY name
                """, (tenant_id,))

            return [
                {
                    "secret_id": r[0],
                    "name": r[1],
                    "category": r[2],
                    "description": r[3],
                    "used_by": r[4] or [],
                    "created_at": r[5].isoformat() if r[5] else None,
                    "updated_at": r[6].isoformat() if r[6] else None,
                    "has_value": True,  # confirms a value is set, without revealing it
                }
                for r in cur.fetchall()
            ]


def delete_secret(
    tenant_id: str, name: str, dsn: Optional[str] = None,
) -> bool:
    """Delete a secret by name."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM secrets WHERE tenant_id = %s AND name = %s",
                (tenant_id, name),
            )
            ok = cur.rowcount > 0
            conn.commit()
            return ok


def update_used_by(
    tenant_id: str, name: str, connector_id: str, dsn: Optional[str] = None,
):
    """Track which connector uses this secret."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE secrets
                SET used_by = used_by || %s::jsonb,
                    updated_at = NOW()
                WHERE tenant_id = %s AND name = %s
                  AND NOT used_by @> %s::jsonb
            """, (
                json.dumps([connector_id]),
                tenant_id, name,
                json.dumps([connector_id]),
            ))
            conn.commit()


# ─── Secret Resolution ───────────────────────────────────────────────────────

def resolve_secrets(config: dict, tenant_id: str, dsn: Optional[str] = None) -> dict:
    """
    Walk a config dict and replace all $secret:name references with actual values.

    Works recursively on nested dicts. String values matching $secret:name
    are replaced with the secret's value. If the entire string is a secret
    reference, the raw value is substituted. If it's embedded in a larger
    string, it's interpolated.

    Returns a NEW dict (original is not modified).
    """
    return _resolve_recursive(config, tenant_id, dsn)


def _resolve_recursive(obj, tenant_id: str, dsn: Optional[str]) -> any:
    if isinstance(obj, dict):
        return {k: _resolve_recursive(v, tenant_id, dsn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_recursive(v, tenant_id, dsn) for v in obj]
    if isinstance(obj, str):
        return _resolve_string(obj, tenant_id, dsn)
    return obj


def _resolve_string(value: str, tenant_id: str, dsn: Optional[str]) -> str:
    """Resolve secret references in a string value."""
    matches = list(SECRET_REF_PATTERN.finditer(value))
    if not matches:
        return value

    # If the entire string is a single secret reference, return the raw value
    if len(matches) == 1 and matches[0].group(0) == value.strip():
        secret_name = matches[0].group(1)
        secret_value = get_secret_value(tenant_id, secret_name, dsn)
        if secret_value is not None:
            return secret_value
        return value  # leave unresolved if secret not found

    # Multiple references or embedded — interpolate
    result = value
    for match in reversed(matches):  # reverse to preserve positions
        secret_name = match.group(1)
        secret_value = get_secret_value(tenant_id, secret_name, dsn)
        if secret_value is not None:
            result = result[:match.start()] + secret_value + result[match.end():]

    return result


def mask_secrets_in_config(config: dict) -> dict:
    """
    Replace any $secret:name references with masked placeholders in a config dict.
    Used when returning connector configs via API — never reveal secret names.
    """
    return _mask_recursive(config)


def _mask_recursive(obj) -> any:
    if isinstance(obj, dict):
        return {k: _mask_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_recursive(v) for v in obj]
    if isinstance(obj, str):
        return SECRET_REF_PATTERN.sub("••••••••", obj)
    return obj
