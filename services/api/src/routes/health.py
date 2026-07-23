"""
Health check endpoints (no auth required).

/health       — liveness: is this process alive? Never checks external deps.
/health/ready — readiness: can this instance serve traffic? Checks all deps.
"""

from __future__ import annotations

import os
import time
import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])
log = logging.getLogger("enki.health")

from storage.config import get_db_dsn
DB_DSN = get_db_dsn()
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
LOGTO_ENDPOINT = os.getenv("LOGTO_ENDPOINT", "http://logto:3001")


def _profile_value() -> str:
    """Resolve the active profile name. Returns 'unknown' on failure
    so health probes never throw."""
    try:
        from core.profile import resolve_profile
        profile, _ = resolve_profile()
        return profile.value
    except Exception:
        return "unknown"


@router.get("/health")
def health() -> dict:
    """Liveness probe. No I/O, always fast. Includes GDU governance status."""
    result = {"status": "ok", "profile": _profile_value()}

    # Hub profile: no GDU governance, just return ok with profile indicator
    if result["profile"] == "hub":
        return result

    # Include GDU governance status if available
    try:
        from core.gdu.signal_vocabulary import is_canonical, get_verification
        verification = get_verification()
        result["governance"] = {
            "canonical": is_canonical(),
            "method": verification.get("method"),
            "active_set_version": verification.get("active_set_version"),
            "pack_hash_verified": verification.get("pack_hash_verified", False),
            "signal_types_count": verification.get("types_count", 0),
        }
        # Classification fingerprint — pack + local overrides. Two federated
        # nodes that share this collapse types identically; a mismatch is the
        # precise pre-flight signal that their graphs will diverge.
        try:
            from core.ldu.subtype_validator import (
                classification_fingerprint, override_count)
            result["governance"]["classification_fingerprint"] = \
                classification_fingerprint()[:16]
            result["governance"]["local_type_overrides"] = override_count()
        except Exception:
            pass
    except Exception:
        result["governance"] = {"canonical": False, "method": "unavailable"}

    return result


@router.get("/health/setup")
def setup_status() -> dict:
    """Check if the server has been bootstrapped.

    Returns setup_complete=True if:
      - At least one device grant exists (Tier K)
      - At least one tenant exists
      - Database is reachable
    """
    from storage.auth.config import GRANTS_DIR
    from pathlib import Path

    checks = {}

    # Check for device grants
    grants_dir = Path(GRANTS_DIR)
    grant_files = list(grants_dir.glob("*.json")) if grants_dir.exists() else []
    checks["grants"] = {
        "exists": len(grant_files) > 0,
        "count": len(grant_files),
    }

    # Check for tenants
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM tenants")
                tenant_count = cur.fetchone()[0]
        checks["tenants"] = {"exists": tenant_count > 0, "count": tenant_count}
    except Exception as e:
        checks["tenants"] = {"exists": False, "error": str(e)}

    # Check for identities (users)
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM identities")
                identity_count = cur.fetchone()[0]
        checks["users"] = {"exists": identity_count > 0, "count": identity_count}
    except Exception:
        checks["users"] = {"exists": False, "count": 0}

    setup_complete = (
        checks["grants"]["exists"]
        and checks.get("tenants", {}).get("exists", False)
    )

    return {
        "setup_complete": setup_complete,
        "checks": checks,
        "action": None if setup_complete else "Run: python scripts/enki-bootstrap.py",
    }


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    """Readiness probe. Checks all dependencies with timeouts."""
    checks = {}

    # Postgres
    checks["postgres"] = await _check_postgres()

    # Hub mode: skip Logto and Ollama (not running on hub)
    if _profile_value() != "hub":
        # Logto
        checks["logto"] = await _check_http("logto", f"{LOGTO_ENDPOINT}/oidc/.well-known/openid-configuration")

        # Ollama (optional — degraded, not down)
        checks["ollama"] = await _check_http("ollama", f"{OLLAMA_URL}/api/tags")

    # Disk space
    checks["disk"] = _check_disk()

    # Overall: postgres down = DOWN, everything else = DEGRADED
    if checks["postgres"]["status"] == "down":
        overall = "down"
        http_code = 503
    elif any(c["status"] != "ok" for c in checks.values()):
        overall = "degraded"
        http_code = 200
    else:
        overall = "ready"
        http_code = 200

    return JSONResponse(
        status_code=http_code,
        content={
            "status": overall,
            "checks": checks,
        },
    )


async def _check_postgres() -> dict:
    """Check database connectivity via the shared connection pool."""
    start = time.monotonic()
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
        return {
            "status": "ok",
            "latency_ms": round((time.monotonic() - start) * 1000, 1),
        }
    except Exception as e:
        return {"status": "down", "error": str(e)}


def _check_disk() -> dict:
    """Check disk space on root filesystem."""
    import shutil
    try:
        usage = shutil.disk_usage("/")
        free_gb = round(usage.free / (1024 ** 3), 1)
        total_gb = round(usage.total / (1024 ** 3), 1)
        used_pct = round((usage.used / usage.total) * 100, 1)

        if free_gb < 0.5:
            status = "critical"
        elif free_gb < 1.0:
            status = "warning"
        else:
            status = "ok"

        return {
            "status": status,
            "free_gb": free_gb,
            "total_gb": total_gb,
            "used_pct": used_pct,
        }
    except Exception as e:
        return {"status": "unknown", "error": str(e)}


async def _check_http(name: str, url: str) -> dict:
    """Check an HTTP dependency."""
    start = time.monotonic()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(url)
            status = "ok" if r.status_code < 400 else "degraded"
            return {
                "status": status,
                "latency_ms": round((time.monotonic() - start) * 1000, 1),
            }
    except Exception as e:
        return {"status": "down", "error": str(e)}
