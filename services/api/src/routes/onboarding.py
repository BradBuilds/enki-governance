"""
Onboarding Preflight — systematic subsystem activation and verification.

GET  /onboarding/status    — Full status of every subsystem (one call, complete picture)
POST /onboarding/activate  — Activate a specific subsystem (bring it live)
POST /onboarding/register-api — Register an external API key for a service
GET  /onboarding/env       — Current environment configuration (redacted secrets)

The onboarding process checks and brings each system live in dependency order:

  Phase 1: Foundation (must pass before anything else)
    1. database      — PostgreSQL connection + schema
    2. node_identity — ENKI_NODE_ID set and registered
    3. root_key      — Root signing key loaded
    4. tenant        — Default tenant exists
    5. bootstrap     — Tier K device grant exists

  Phase 2: Governance (must pass before data flows)
    6. gdu           — Active set loaded, signalvocab verified
    7. signal_chain  — Signal chain initialized

  Phase 3: Services (can be activated incrementally)
    8. auth          — Logto OIDC reachable (user login)
    9. ai_local      — Ollama running + model downloaded
    10. ai_cloud     — Cloud AI provider configured (optional)

  Phase 4: Data Sources (all optional, activate as needed)
    11. enrichment   — Geo enrichment APIs reachable
    12. reference    — Reference data synced (airports, ports, satellites)
    13. connectors   — Active connector feeds
    14. osrm         — Route matching (optional)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

logger = logging.getLogger("enki.onboarding")
router = APIRouter(prefix="/onboarding", tags=["onboarding"])


# ─── Status (complete picture) ──────────────────────────────────────────────

@router.get("/status")
async def onboarding_status(
    x_tenant_id: str = Header("default", alias="X-Tenant-ID"),
):
    """
    Complete onboarding status — every subsystem checked in order.

    Returns a dict with each subsystem's status:
    - "live"    — fully operational
    - "degraded" — working but not ideal (e.g., non-canonical GDU)
    - "down"    — not working, needs attention
    - "skipped" — optional and not configured
    - "blocked" — can't check because a dependency is down

    Also returns the overall phase (1-4) and next_action recommendation.
    """
    results = {}
    phase = 0

    # ── Phase 1: Foundation ──────────────────────────────────────────
    results["database"] = _check_database()
    results["node_identity"] = _check_node_identity()
    results["root_key"] = _check_root_key()
    results["tenant"] = _check_tenant(x_tenant_id)
    results["bootstrap"] = _check_bootstrap()

    phase1_ok = all(results[k]["status"] == "live" for k in
                    ["database", "node_identity", "root_key", "tenant", "bootstrap"])
    if phase1_ok:
        phase = 1

    # ── Phase 2: Governance ──────────────────────────────────────────
    if phase1_ok:
        results["gdu"] = _check_gdu()
        results["signal_chain"] = _check_signal_chain(x_tenant_id)
        phase2_ok = all(results[k]["status"] in ("live", "degraded") for k in
                        ["gdu", "signal_chain"])
        if phase2_ok:
            phase = 2
    else:
        results["gdu"] = {"status": "blocked", "detail": "Foundation not ready"}
        results["signal_chain"] = {"status": "blocked", "detail": "Foundation not ready"}

    # ── Phase 3: Services ────────────────────────────────────────────
    if phase >= 2:
        results["auth"] = _check_auth()
        results["ai_local"] = _check_ai_local()
        results["ai_cloud"] = _check_ai_cloud(x_tenant_id)
        phase3_ok = results["auth"]["status"] in ("live", "skipped")
        if phase3_ok:
            phase = 3
    else:
        results["auth"] = {"status": "blocked", "detail": "Governance not ready"}
        results["ai_local"] = {"status": "blocked", "detail": "Governance not ready"}
        results["ai_cloud"] = {"status": "blocked", "detail": "Governance not ready"}

    # ── Phase 4: Data Sources ────────────────────────────────────────
    if phase >= 3:
        results["enrichment"] = _check_enrichment()
        results["reference"] = _check_reference(x_tenant_id)
        results["connectors"] = _check_connectors(x_tenant_id)
        results["osrm"] = _check_osrm()
        phase = 4
    else:
        for k in ["enrichment", "reference", "connectors", "osrm"]:
            results[k] = {"status": "blocked", "detail": "Services not ready"}

    # ── Overall assessment ───────────────────────────────────────────
    live_count = sum(1 for v in results.values() if v["status"] == "live")
    total = len(results)
    degraded = [k for k, v in results.items() if v["status"] == "degraded"]
    down = [k for k, v in results.items() if v["status"] == "down"]
    blocked = [k for k, v in results.items() if v["status"] == "blocked"]

    # Determine next action
    next_action = None
    for k, v in results.items():
        if v["status"] == "down":
            next_action = {"system": k, "action": v.get("action", f"Activate {k}"), "detail": v.get("detail", "")}
            break

    return {
        "phase": phase,
        "live": live_count,
        "total": total,
        "overall": "ready" if not down and not blocked else "setup_needed",
        "degraded": degraded,
        "down": down,
        "blocked": blocked,
        "next_action": next_action,
        "systems": results,
    }


# ─── Activate ───────────────────────────────────────────────────────────────

@router.post("/activate")
async def activate_subsystem(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Activate a specific subsystem. Performs the initialization action needed
    to bring it from 'down' to 'live'.

    Requires authentication — subsystem activation is an admin-level operation.

    Body: {
        "system": "database" | "tenant" | "bootstrap" | "reference" | "ai_local",
        "model": "gemma4:e2b"  // optional, for ai_local
    }
    """
    body = await request.json()
    system = body.get("system", "")

    # Sync activators
    sync_activators = {
        "database": lambda: _activate_database(),
        "node_identity": lambda: _activate_node_identity(),
        "tenant": lambda: _activate_tenant(auth.tenant_id),
        "bootstrap": lambda: _activate_bootstrap(),
        "ai_local": lambda: _activate_ai_local(body.get("model")),
    }

    # Async activators
    async_activators = {
        "reference": lambda: _activate_reference(auth.tenant_id),
    }

    all_systems = {**sync_activators, **async_activators}

    if system not in all_systems:
        return JSONResponse(status_code=400, content={
            "error": f"Unknown system: {system}",
            "available": sorted(all_systems.keys()),
        })

    try:
        if system in async_activators:
            result = await async_activators[system]()
        else:
            result = sync_activators[system]()
        logger.info("Subsystem %s activated by %s", system, auth.subject_id)
        return {"system": system, "activated": True, **result}
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": str(e), "system": system, "activated": False,
        })


# ─── Register API Key ──────────────────────────────────────────────────────

@router.post("/register-api")
async def register_api_key(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Register an API key for an external service.

    Requires authentication — API key registration is a sensitive operation.

    Body: {
        "service": "openai" | "anthropic" | "nasa" | "openaq" | ...,
        "api_key": "sk-...",
        "config": {}  // optional extra config
    }
    """
    body = await request.json()
    service = body.get("service", "")
    api_key = body.get("api_key", "")
    x_tenant_id = auth.tenant_id

    if not service or not api_key:
        return JSONResponse(status_code=400, content={"error": "service and api_key required"})

    # AI providers go to ai_config table
    ai_providers = {
        "openai", "anthropic", "gemini", "groq", "together",
        "mistral", "deepseek", "perplexity", "fireworks", "xai", "cohere",
    }

    if service in ai_providers:
        try:
            from core.ai.config import set_ai_config
            set_ai_config(
                tenant_id=x_tenant_id,
                provider=service,
                model=body.get("model", ""),
                config={"api_key": api_key, **(body.get("config") or {})},
                configured_by="onboarding",
            )
            return {"service": service, "registered": True, "type": "ai_provider"}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    # Data source API keys go to data_source_config
    data_source_keys = {
        "openaq": "openaq",
        "abuseipdb": "abuseipdb",
        "openweathermap": "openweathermap",
        "virustotal": "virustotal",
        "shodan": "shodan",
        "haveibeenpwned": "haveibeenpwned",
        "nasa": "nasa",
    }

    if service in data_source_keys:
        try:
            from storage.postgres.db import get_connection
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE data_source_config
                        SET api_key = %s, enabled = TRUE, updated_at = NOW()
                        WHERE source_id = %s AND tenant_id = %s
                    """, (api_key, data_source_keys[service], x_tenant_id))
                    if cur.rowcount == 0:
                        cur.execute("""
                            INSERT INTO data_source_config (source_id, tenant_id, name, api_key, enabled)
                            VALUES (%s, %s, %s, %s, TRUE)
                            ON CONFLICT (source_id, tenant_id) DO UPDATE SET
                                api_key = EXCLUDED.api_key, enabled = TRUE
                        """, (data_source_keys[service], x_tenant_id, service, api_key))
                conn.commit()
            return {"service": service, "registered": True, "type": "data_source"}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    return JSONResponse(status_code=400, content={
        "error": f"Unknown service: {service}",
        "available_ai": sorted(ai_providers),
        "available_data": sorted(data_source_keys.keys()),
    })


# ─── Environment ────────────────────────────────────────────────────────────

@router.get("/env")
async def show_env(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Show current environment configuration (secrets redacted).

    Requires authentication — exposes server configuration details.
    """
    def _redact(val):
        if not val:
            return None
        if len(val) <= 8:
            return "***"
        return val[:4] + "..." + val[-4:]

    env_map = {
        "ENKI_NODE_ID": os.getenv("ENKI_NODE_ID"),
        "ENKI_PROFILE": os.getenv("ENKI_PROFILE", "(unset)"),
        "ENKI_DB_DSN": _redact(os.getenv("ENKI_DB_DSN")),
        "ENKI_ROOT_PASSWORD": "***SET***" if os.getenv("ENKI_ROOT_PASSWORD") else "NOT SET",
        "ENKI_ROOT_KEY_PATH": os.getenv("ENKI_ROOT_KEY_PATH", "storage/keys/root_key.enc"),
        "ENKI_GRANTS_DIR": os.getenv("ENKI_GRANTS_DIR", "storage/grants"),
        "OLLAMA_BASE_URL": os.getenv("OLLAMA_BASE_URL", "http://ollama:11434"),
        "OLLAMA_MODEL": os.getenv("OLLAMA_MODEL"),
        "OSRM_URL": os.getenv("OSRM_URL"),
        "LOGTO_ENDPOINT": os.getenv("LOGTO_ENDPOINT", "http://localhost:3001"),
        "ENKI_ORIGIN": os.getenv("ENKI_ORIGIN", "*"),
        "ENKI_NETWORK_MODE": os.getenv("ENKI_NETWORK_MODE"),
    }

    # Resolve current profile if possible
    try:
        from core.profile import resolve_profile
        profile, spec = resolve_profile()
        profile_info = {
            "profile": profile.value,
            "requires_hardware_key": spec.requires_hardware_key,
            "requires_logto": spec.requires_logto,
            "requires_luks": spec.requires_luks,
        }
    except Exception:
        profile_info = {"profile": None, "error": "ENKI_PROFILE not set or invalid"}

    # Classify each var
    required = ["ENKI_NODE_ID", "ENKI_PROFILE", "ENKI_DB_DSN"]
    missing = [k for k in required if not os.getenv(k)]

    return {
        "env": env_map,
        "missing_required": missing,
        "profile": profile_info,
    }


# ─── Connector Validation ──────────────────────────────────────────────────

@router.post("/validate-connectors")
async def validate_connectors(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Test every active connector by triggering a fetch.

    Returns per-connector results showing whether data actually flows.
    This is the "prove it works" check for the install process.
    """
    from storage.connectors.manager import list_connectors, trigger_connector

    connectors = list_connectors(auth.tenant_id)
    if not connectors:
        return {"validated": 0, "results": [], "detail": "No connectors configured. Run bootstrap or enable recipes."}

    results = []
    for conn in connectors:
        cid = conn.get("connector_id", "?")
        try:
            result = trigger_connector(cid, auth.tenant_id)
            results.append({
                "connector_id": cid,
                "device_id": conn.get("device_id"),
                "source_type": conn.get("source_type"),
                "status": "ok" if result.items_fetched > 0 else "empty",
                "items_fetched": result.items_fetched,
                "items_ingested": result.items_ingested,
                "errors": result.errors,
            })
        except Exception as e:
            results.append({
                "connector_id": cid,
                "device_id": conn.get("device_id"),
                "source_type": conn.get("source_type"),
                "status": "error",
                "items_fetched": 0,
                "items_ingested": 0,
                "errors": [str(e)],
            })

    ok = sum(1 for r in results if r["status"] == "ok")
    return {
        "validated": len(results),
        "flowing": ok,
        "empty": sum(1 for r in results if r["status"] == "empty"),
        "errored": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }


# ─── External API Registry ─────────────────────────────────────────────────

@router.get("/apis")
async def list_external_apis():
    """List all external APIs, which need keys, and their registration URLs."""
    return {"apis": _get_api_registry()}


def _get_api_registry() -> list[dict]:
    """Complete registry of external APIs and their requirements."""
    return [
        # ── Free APIs (no key needed) ──
        {"service": "nominatim", "name": "OpenStreetMap Geocoding", "requires_key": False, "category": "enrichment"},
        {"service": "usgs_elevation", "name": "USGS Elevation", "requires_key": False, "category": "enrichment"},
        {"service": "census", "name": "US Census Geocoder", "requires_key": False, "category": "enrichment"},
        {"service": "nws", "name": "National Weather Service", "requires_key": False, "category": "enrichment"},
        {"service": "fema_flood", "name": "FEMA Flood Zones", "requires_key": False, "category": "enrichment"},
        {"service": "fcc", "name": "FCC Area Lookup", "requires_key": False, "category": "enrichment"},
        {"service": "open_elevation", "name": "Open Elevation (Global)", "requires_key": False, "category": "enrichment"},
        {"service": "open_meteo", "name": "Open-Meteo Weather", "requires_key": False, "category": "enrichment"},
        {"service": "rest_countries", "name": "REST Countries", "requires_key": False, "category": "enrichment"},
        {"service": "wikidata", "name": "Wikidata SPARQL", "requires_key": False, "category": "enrichment"},
        {"service": "gdacs", "name": "Global Disaster Alerts", "requires_key": False, "category": "enrichment"},
        {"service": "sec_edgar", "name": "SEC EDGAR Company Data", "requires_key": False, "category": "entity"},
        {"service": "gleif", "name": "GLEIF Legal Entity Lookup", "requires_key": False, "category": "entity"},
        {"service": "usaspending", "name": "USAspending.gov", "requires_key": False, "category": "entity"},
        {"service": "courtlistener", "name": "CourtListener", "requires_key": False, "category": "entity"},
        {"service": "propublica", "name": "ProPublica Nonprofits", "requires_key": False, "category": "entity"},
        {"service": "finra", "name": "FINRA BrokerCheck", "requires_key": False, "category": "entity"},
        {"service": "celestrak", "name": "CelesTrak Satellite TLE", "requires_key": False, "category": "reference"},
        {"service": "ourairports", "name": "OurAirports (75K airports)", "requires_key": False, "category": "reference"},
        {"service": "nga_ports", "name": "NGA World Port Index", "requires_key": False, "category": "reference"},
        # ── APIs with free keys ──
        {"service": "nasa", "name": "NASA APIs (NEO, DONKI, Fireballs)", "requires_key": True,
         "free_tier": True, "register_url": "https://api.nasa.gov/", "category": "reference",
         "note": "DEMO_KEY works but is rate-limited. Free registration gives 1000 req/hr."},
        {"service": "openaq", "name": "OpenAQ Air Quality", "requires_key": True,
         "free_tier": True, "register_url": "https://docs.openaq.org/", "category": "connector"},
        # ── AI Providers (paid, some free tier) ──
        {"service": "anthropic", "name": "Anthropic (Claude)", "requires_key": True,
         "free_tier": False, "register_url": "https://console.anthropic.com/", "category": "ai"},
        {"service": "openai", "name": "OpenAI (GPT-4)", "requires_key": True,
         "free_tier": False, "register_url": "https://platform.openai.com/api-keys", "category": "ai"},
        {"service": "groq", "name": "Groq (Fast LLM)", "requires_key": True,
         "free_tier": True, "register_url": "https://console.groq.com/keys", "category": "ai",
         "note": "Free tier with generous limits. Good for testing."},
        {"service": "gemini", "name": "Google Gemini", "requires_key": True,
         "free_tier": True, "register_url": "https://aistudio.google.com/apikey", "category": "ai"},
        {"service": "cohere", "name": "Cohere (Command)", "requires_key": True,
         "free_tier": True, "register_url": "https://dashboard.cohere.com/api-keys", "category": "ai"},
        {"service": "mistral", "name": "Mistral AI", "requires_key": True,
         "free_tier": False, "register_url": "https://console.mistral.ai/api-keys/", "category": "ai"},
        {"service": "together", "name": "Together.ai", "requires_key": True,
         "free_tier": False, "register_url": "https://api.together.xyz/settings/api-keys", "category": "ai"},
        {"service": "deepseek", "name": "DeepSeek", "requires_key": True,
         "free_tier": False, "register_url": "https://platform.deepseek.com/api_keys", "category": "ai"},
        {"service": "xai", "name": "xAI (Grok)", "requires_key": True,
         "free_tier": False, "register_url": "https://console.x.ai/", "category": "ai"},
        {"service": "fireworks", "name": "Fireworks AI", "requires_key": True,
         "free_tier": False, "register_url": "https://fireworks.ai/api-keys", "category": "ai"},
        {"service": "perplexity", "name": "Perplexity AI", "requires_key": True,
         "free_tier": False, "register_url": "https://www.perplexity.ai/settings/api", "category": "ai"},
        # ── Security connectors ──
        {"service": "abuseipdb", "name": "AbuseIPDB (IP Reputation)", "requires_key": True,
         "free_tier": True, "register_url": "https://www.abuseipdb.com/register", "category": "connector"},
        {"service": "virustotal", "name": "VirusTotal", "requires_key": True,
         "free_tier": True, "register_url": "https://www.virustotal.com/gui/join-us", "category": "connector"},
        {"service": "shodan", "name": "Shodan", "requires_key": True,
         "free_tier": True, "register_url": "https://account.shodan.io/register", "category": "connector"},
        {"service": "haveibeenpwned", "name": "Have I Been Pwned", "requires_key": True,
         "free_tier": False, "register_url": "https://haveibeenpwned.com/API/Key", "category": "connector"},
    ]


# ─── Subsystem Checkers ─────────────────────────────────────────────────────

def _check_database() -> dict:
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'")
                table_count = cur.fetchone()[0]
        return {"status": "live", "tables": table_count}
    except Exception as e:
        return {"status": "down", "detail": str(e), "action": "Check ENKI_DB_DSN and PostgreSQL"}


def _check_node_identity() -> dict:
    node_id = os.getenv("ENKI_NODE_ID")
    if not node_id:
        return {"status": "down", "detail": "ENKI_NODE_ID not set", "action": "Set ENKI_NODE_ID in environment"}
    return {"status": "live", "node_id": node_id}


def _check_root_key() -> dict:
    from pathlib import Path
    key_path = os.getenv("ENKI_ROOT_KEY_PATH", "storage/keys/root_key.enc")
    password = os.getenv("ENKI_ROOT_PASSWORD")
    if not password:
        return {"status": "down", "detail": "ENKI_ROOT_PASSWORD not set", "action": "Set ENKI_ROOT_PASSWORD"}
    if not Path(key_path).exists():
        return {"status": "down", "detail": f"Root key not found at {key_path}", "action": "Run enki-bootstrap.py"}
    try:
        from storage.auth.root_key import load_root_key
        rk = load_root_key(key_path, password)
        return {"status": "live", "public_key_prefix": rk.public_key_base64()[:16] + "..."}
    except Exception as e:
        return {"status": "down", "detail": str(e), "action": "Check ENKI_ROOT_PASSWORD"}


def _check_tenant(tenant_id: str) -> dict:
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT tenant_id FROM tenants WHERE tenant_id = %s", (tenant_id,))
                if cur.fetchone():
                    return {"status": "live", "tenant_id": tenant_id}
        return {"status": "down", "detail": f"Tenant '{tenant_id}' not found",
                "action": "POST /onboarding/activate {\"system\": \"tenant\"}"}
    except Exception as e:
        return {"status": "down", "detail": str(e)}


def _check_bootstrap() -> dict:
    from pathlib import Path
    grants_dir = Path(os.getenv("ENKI_GRANTS_DIR", "storage/grants"))
    if not grants_dir.exists():
        return {"status": "down", "detail": "Grants directory not found", "action": "Run enki-bootstrap.py"}
    grants = list(grants_dir.glob("*.json"))
    if not grants:
        return {"status": "down", "detail": "No device grants found", "action": "Run enki-bootstrap.py"}
    return {"status": "live", "grant_count": len(grants),
            "devices": [g.stem for g in grants]}


def _check_gdu() -> dict:
    try:
        from core.gdu.signal_vocabulary import is_canonical, get_verification, get_signal_types
        types = get_signal_types()
        verification = get_verification()
        if is_canonical():
            return {"status": "live", "canonical": True,
                    "active_set_version": verification.get("active_set_version"),
                    "types_count": len(types)}
        else:
            return {"status": "degraded", "canonical": False,
                    "method": verification.get("method"),
                    "types_count": len(types),
                    "detail": "GDU loaded via fallback (non-canonical)"}
    except Exception as e:
        return {"status": "down", "detail": str(e), "action": "Check active_set_v5.json exists and verify_pack_set.py reports green"}


def _check_signal_chain(tenant_id: str) -> dict:
    try:
        from storage.signals.chain import get_signal_chain_head
        node_id = os.getenv("ENKI_NODE_ID", "local")
        chain_hash, count = get_signal_chain_head(tenant_id, node_id)
        return {"status": "live", "chain_hash": chain_hash[:16] + "..." if chain_hash else "genesis",
                "signal_count": count}
    except Exception as e:
        return {"status": "down", "detail": str(e)}


def _check_auth() -> dict:
    try:
        from core.profile import resolve_profile
        profile, spec = resolve_profile()
    except Exception as e:
        return {"status": "down", "detail": f"profile not resolved: {e}"}

    if profile.value == "dev":
        return {"status": "live", "profile": "dev", "detail": "Dev profile — header-based auth"}
    if not spec.requires_logto:
        return {"status": "live", "profile": profile.value,
                "detail": f"Profile {profile.value} does not use Logto"}
    try:
        import httpx
        logto = os.getenv("LOGTO_ENDPOINT", "http://localhost:3001")
        r = httpx.get(f"{logto}/oidc/.well-known/openid-configuration", timeout=5)
        if r.status_code == 200:
            return {"status": "live", "profile": profile.value, "logto_endpoint": logto}
        return {"status": "down", "detail": f"Logto returned {r.status_code}"}
    except Exception as e:
        return {"status": "down", "detail": str(e), "action": "Start Logto container"}


def _check_ai_local() -> dict:
    try:
        from core.ai.local_provider import OllamaProvider
        provider = OllamaProvider({"base_url": os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")})
        if not provider.is_available():
            return {"status": "down", "detail": "Ollama not reachable",
                    "action": "Start Ollama or set OLLAMA_BASE_URL"}

        models = provider.list_models()
        # list_models returns either dicts or strings depending on Ollama version
        model_names = []
        for m in models:
            if isinstance(m, dict):
                model_names.append(m.get("name", m.get("model", "?")))
            else:
                model_names.append(str(m))

        # Check if any Gemma 4 model is downloaded
        has_gemma4 = any("gemma4" in str(n).lower() or "gemma-4" in str(n).lower() for n in model_names)

        if not models:
            return {"status": "down", "detail": "Ollama running but no models downloaded",
                    "action": "POST /onboarding/activate {\"system\": \"ai_local\"}",
                    "models": []}

        if not has_gemma4:
            return {"status": "degraded", "detail": "Ollama running but Gemma 4 not downloaded",
                    "action": "POST /onboarding/activate {\"system\": \"ai_local\", \"model\": \"gemma4:e2b\"}",
                    "models": model_names[:5], "model_count": len(models)}

        return {"status": "live", "models": model_names[:5],
                "model_count": len(models), "has_gemma4": True}
    except Exception as e:
        return {"status": "down", "detail": str(e)}


def _check_ai_cloud(tenant_id: str) -> dict:
    try:
        from core.ai.config import get_tenant_provider
        provider = get_tenant_provider(tenant_id)
        if provider:
            return {"status": "live", "provider": type(provider).__name__}
        return {"status": "skipped", "detail": "No cloud AI configured (optional)"}
    except Exception:
        return {"status": "skipped", "detail": "Cloud AI not configured (optional)"}


def _check_enrichment() -> dict:
    """Quick check — hit one free API to verify internet + enrichment works."""
    try:
        import httpx
        r = httpx.get("https://api.open-meteo.com/v1/forecast?latitude=38.9&longitude=-77.0&current_weather=true",
                      timeout=5)
        if r.status_code == 200:
            return {"status": "live", "detail": "External APIs reachable"}
        return {"status": "degraded", "detail": f"API returned {r.status_code}"}
    except Exception:
        mode = os.getenv("ENKI_NETWORK_MODE", "connected")
        if mode == "offline":
            return {"status": "skipped", "detail": "Offline mode — enrichment disabled"}
        return {"status": "down", "detail": "External APIs unreachable",
                "action": "Check internet connection or set ENKI_NETWORK_MODE=offline"}


def _check_reference(tenant_id: str) -> dict:
    try:
        from storage.postgres.db import get_connection
        counts = {}
        with get_connection() as conn:
            with conn.cursor() as cur:
                for table in ["airports", "ports", "reference_points"]:
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM {table}")
                        counts[table] = cur.fetchone()[0]
                    except Exception:
                        counts[table] = 0
                        conn.rollback()
        total = sum(counts.values())
        if total > 0:
            return {"status": "live", "records": total, "breakdown": counts}
        return {"status": "down", "detail": "No reference data synced",
                "action": "POST /onboarding/activate {\"system\": \"reference\"}"}
    except Exception as e:
        return {"status": "down", "detail": str(e)}


def _check_connectors(tenant_id: str) -> dict:
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM connectors WHERE tenant_id = %s AND enabled = TRUE",
                           (tenant_id,))
                count = cur.fetchone()[0]
        if count > 0:
            return {"status": "live", "active_connectors": count}
        return {"status": "skipped", "detail": "No connectors configured (optional)"}
    except Exception:
        return {"status": "skipped", "detail": "Connectors not configured (optional)"}


def _check_osrm() -> dict:
    osrm_url = os.getenv("OSRM_URL")
    if not osrm_url:
        return {"status": "skipped", "detail": "OSRM not configured (optional)"}
    try:
        import httpx
        r = httpx.get(f"{osrm_url}/nearest/v1/driving/-77.0365,38.8977", timeout=3)
        if r.status_code == 200:
            return {"status": "live", "url": osrm_url}
        return {"status": "down", "detail": f"OSRM returned {r.status_code}"}
    except Exception:
        return {"status": "down", "detail": "OSRM unreachable", "action": "Start OSRM container"}


# ─── Subsystem Activators ───────────────────────────────────────────────────

def _activate_database() -> dict:
    from storage.postgres.db import initialize_schema
    initialize_schema()
    return {"detail": "Schema initialized"}


def _activate_node_identity() -> dict:
    node_id = os.getenv("ENKI_NODE_ID")
    if not node_id:
        from storage.node.identity import generate_node_identity
        identity = generate_node_identity()
        os.environ["ENKI_NODE_ID"] = identity.node_id
        return {"node_id": identity.node_id, "detail": "Generated (set ENKI_NODE_ID in env to persist)"}
    return {"node_id": node_id, "detail": "Already set"}


def _activate_tenant(tenant_id: str) -> dict:
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (tenant_id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, tenant_id.title()),
            )
        conn.commit()
    return {"tenant_id": tenant_id}


def _activate_bootstrap() -> dict:
    return {"detail": "Run: python scripts/enki-bootstrap.py",
            "note": "Bootstrap must be run from CLI (root key generation requires terminal)"}


async def _activate_reference(tenant_id: str) -> dict:
    """Sync reference data — airports, ports, satellites."""
    results = {}
    try:
        from core.reference.sync import sync_airports
        airports = await sync_airports()
        results["airports"] = airports
    except Exception as e:
        results["airports"] = {"error": str(e)}

    try:
        from core.reference.sync import sync_ports
        ports = await sync_ports()
        results["ports"] = ports
    except Exception as e:
        results["ports"] = {"error": str(e)}

    try:
        from core.reference.space_sync import sync_celestrak_tle
        sats = await sync_celestrak_tle()
        results["satellites"] = sats
    except Exception as e:
        results["satellites"] = {"error": str(e)}

    return {"synced": results}


def _activate_ai_local(model: Optional[str] = None) -> dict:
    """Check Ollama and pull Gemma 4 model.

    If no model specified, auto-selects based on available RAM:
      >= 20 GB budget → gemma4:26b (best quality, MoE)
      >= 10 GB budget → gemma4:e4b (very good)
      >= 5 GB budget  → gemma4:e2b (good, lightweight)
      < 5 GB budget   → gemma4:e2b (minimum viable)
    """
    try:
        from core.ai.local_provider import OllamaProvider
        provider = OllamaProvider({"base_url": os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")})

        if not provider.is_available():
            return {"detail": "Ollama not reachable — start the service first"}

        # Auto-select model based on hardware if not specified
        if not model:
            try:
                from core.setup.config import detect_hardware
                hw = detect_hardware()
                model = hw.get("recommended_model", "gemma4:e2b")
            except Exception:
                model = "gemma4:e2b"

        # Check if model already downloaded
        existing = provider.list_models()
        existing_names = [m.get("name", m.get("model", "")) for m in existing]
        if any(model in str(n) for n in existing_names):
            return {"detail": f"{model} already downloaded", "model": model,
                    "models": existing_names[:5]}

        # Pull the model (this starts a background download in Ollama)
        logger.info(f"Pulling AI model: {model}")
        result = provider.pull_model(model)
        return {"detail": f"Pulling {model} — this may take several minutes",
                "model": model, **result}
    except Exception as e:
        return {"detail": f"Ollama activation failed: {e}"}
