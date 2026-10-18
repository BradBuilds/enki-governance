"""
Research credential resolver — single bridge from the `secrets` table to the
investigation source handlers.

The investigation worker calls `set_tenant_context(tenant_id)` at the start of
each run; source handlers call `get_credential("DATA_GOV_API_KEY")` to fetch the
value. Resolution order:

  1. Secrets table for the current tenant (UI-managed, hot-reloadable)
  2. Process environment (`os.environ`) for ops-provisioned defaults
  3. Empty string

Returning "" instead of None keeps the call sites simple — they already use
truthiness checks (`if not api_key: skip`).

This is the bridge that makes wizard step 6 ("Optional API Keys") functionally
matter. Without it, every paid/free key the operator pastes into the UI is
written to the DB but never read by the source handlers.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("enki.research.credentials")

# Per-investigation tenant context. The worker sets this so the source layer
# can resolve secrets without threading tenant_id through every signature.
_TENANT_CTX: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "enki_research_tenant", default=None,
)

# Small in-process cache so per-source calls don't hammer the secrets table.
# Invalidated on save (see notify_credential_change).
_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_CACHE_TTL = 30.0  # seconds — short, so UI changes propagate fast


def set_tenant_context(tenant_id: Optional[str]) -> None:
    """Set the tenant scope for credential lookups in the current task/thread."""
    _TENANT_CTX.set(tenant_id)


def get_tenant_context() -> Optional[str]:
    return _TENANT_CTX.get()


def get_credential(name: str, tenant_id: Optional[str] = None) -> str:
    """Resolve a credential by name. Secrets table first, env fallback, "" if missing.

    `name` is the canonical credential key (e.g. "DATA_GOV_API_KEY"). The same
    name is used for the env var and for the secret-table row, so operators can
    pick whichever they prefer.
    """
    tenant = tenant_id or _TENANT_CTX.get()
    cache_key = (tenant or "_env_only", name)
    now = time.monotonic()

    cached = _CACHE.get(cache_key)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    value = ""
    if tenant:
        try:
            from storage.secrets import get_secret_value
            v = get_secret_value(tenant, name)
            if v:
                value = v
        except Exception as e:
            log.debug("Secrets table lookup failed for %s/%s: %s", tenant, name, e)

    if not value:
        value = os.environ.get(name, "") or ""

    _CACHE[cache_key] = (value, now)
    return value


def has_credential(name: str, tenant_id: Optional[str] = None) -> bool:
    """True if `name` resolves to a non-empty value."""
    return bool(get_credential(name, tenant_id))


def notify_credential_change(name: Optional[str] = None) -> None:
    """Invalidate the cache. Call from the secrets POST/DELETE handler so the
    next investigation picks up the new value within seconds instead of waiting
    for TTL expiry. Pass `name=None` to flush everything."""
    if name is None:
        _CACHE.clear()
        return
    for key in list(_CACHE.keys()):
        if key[1] == name:
            _CACHE.pop(key, None)


# ─── Catalog loading ─────────────────────────────────────────────────────────

_CATALOG_PATH = Path(__file__).resolve().parents[2] / "libraries" / "researchpack" / "definitions" / "researchpack.credentials.json"
_CATALOG_CACHE: Optional[dict] = None


def load_catalog() -> dict:
    """Load the credentials catalog JSON. Defines every credential the system
    knows how to use, what data each unlocks, signup URLs, and free/keyed
    rate-limit deltas. The wizard and status endpoints both read from this."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE
    try:
        with _CATALOG_PATH.open("r", encoding="utf-8") as f:
            _CATALOG_CACHE = json.load(f)
    except Exception as e:
        log.warning("Failed to load credentials catalog at %s: %s", _CATALOG_PATH, e)
        _CATALOG_CACHE = {"credentials": []}
    return _CATALOG_CACHE


def reset_catalog_cache() -> None:
    global _CATALOG_CACHE
    _CATALOG_CACHE = None


def credential_status(tenant_id: Optional[str] = None) -> list[dict]:
    """Return per-credential status for the wizard UI. Never includes values."""
    catalog = load_catalog()
    out = []
    for cred in catalog.get("credentials", []):
        name = cred.get("name", "")
        out.append({
            **cred,
            "configured": has_credential(name, tenant_id),
        })
    return out
