"""
First-run setup wizard API endpoints.

Handles hardware detection, AI model selection, OSRM region management,
and network mode configuration.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from pathlib import Path

# Docker volume that holds the OSRM routing data (override with ENKI_OSRM_VOLUME).
OSRM_VOLUME = os.environ.get("ENKI_OSRM_VOLUME", "enki_osrm_data")

logger = logging.getLogger("enki.setup")
router = APIRouter(prefix="/setup", tags=["setup"])

# Persistent server-level config — survives restarts
ENKI_SERVER_CONFIG = Path(os.environ.get("ENKI_CONFIG_DIR", "/etc/enki")) / "server.env"


# ─── Hardware & Status ────────────────────────────────────────────────────────

@router.get("/status")
async def setup_status(
    x_tenant_id: str = Header("default", alias="X-Tenant-ID"),
):
    """Get current setup state — what's configured and what needs attention."""
    from core.setup.config import get_setup_status, detect_hardware

    status = get_setup_status(x_tenant_id)
    hardware = detect_hardware()

    # Check Ollama status
    ollama_ok = False
    ollama_models = []
    try:
        from core.ai.local_provider import OllamaProvider
        provider = OllamaProvider({"base_url": os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")})
        ollama_ok = provider.is_available()
        ollama_models = provider.list_models()
    except Exception:
        pass

    return {
        **status,
        "hardware": hardware,
        "ollama_available": ollama_ok,
        "ollama_models": ollama_models,
    }


@router.get("/hardware")
async def detect_hw():
    """Detect server hardware for model recommendations."""
    from core.setup.config import detect_hardware
    return detect_hardware()


# ─── AI Model Management ─────────────────────────────────────────────────────

@router.post("/ai/pull")
async def pull_ai_model(
    body: dict,
    x_tenant_id: str = Header("default", alias="X-Tenant-ID"),
):
    """
    Pull an AI model via Ollama. Returns immediately — model downloads in background.
    Check progress via GET /setup/ai/pull-status.
    """
    model_id = body.get("model_id", "")
    if not model_id:
        return JSONResponse(status_code=400, content={"error": "model_id required"})

    try:
        from core.ai.local_provider import OllamaProvider
        provider = OllamaProvider({
            "base_url": os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434"),
            "model": model_id,
        })
        result = provider.pull_model(model_id)
        return {"model_id": model_id, **result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/ai/models")
async def list_local_models():
    """List all models currently downloaded in Ollama."""
    try:
        from core.ai.local_provider import OllamaProvider
        provider = OllamaProvider({"base_url": os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")})
        models = provider.list_models()
        return {"models": models}
    except Exception as e:
        return {"models": [], "error": str(e)}


@router.post("/ai/test")
async def test_ai_model(body: dict):
    """Quick test of a model — classify a sample transaction."""
    model_id = body.get("model_id", "gemma4:e2b")

    try:
        from core.ai.local_provider import OllamaProvider
        from core.ai.provider import LLMMessage
        provider = OllamaProvider({
            "base_url": os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434"),
            "model": model_id,
        })

        result = provider.complete(
            messages=[LLMMessage(
                role="user",
                content='Classify this transaction: "SHELL OIL 04523". Reply with JSON: {"category": "...", "confidence": 0.0-1.0}',
            )],
            max_tokens=100,
            temperature=0.1,
        )

        return {
            "model_id": model_id,
            "response": result.content,
            "error": result.error,
            "success": not result.error,
        }
    except Exception as e:
        return {"model_id": model_id, "success": False, "error": str(e)}


# ─── OSRM Region Management ──────────────────────────────────────────────────

@router.get("/osrm/regions")
async def list_osrm_regions():
    """List available OSRM routing regions with download status."""
    from core.setup.config import get_osrm_regions, detect_hardware

    hw = detect_hardware()
    regions = get_osrm_regions(hw["total_ram_gb"])

    # Check which regions are already downloaded/processed
    # The OSRM volume has processed files — we check via the container
    loaded_regions = _get_loaded_osrm_regions()

    for r in regions:
        r["status"] = "loaded" if r["region_id"] in loaded_regions else "available"

    return {"regions": regions, "loaded": loaded_regions}


@router.post("/osrm/download")
async def download_osrm_region(body: dict):
    """
    Download and process an OSRM region. This is a long-running operation.
    The region will be available for routing after processing completes.
    """
    region_id = body.get("region_id", "")
    if not region_id:
        return JSONResponse(status_code=400, content={"error": "region_id required"})

    from core.setup.config import OSRM_REGIONS

    region = next((r for r in OSRM_REGIONS if r["region_id"] == region_id), None)
    if not region:
        return JSONResponse(status_code=404, content={"error": f"Unknown region: {region_id}"})

    geofabrik_url = f"https://download.geofabrik.de/{region_id}-latest.osm.pbf"
    filename = region_id.replace("/", "-")

    # Run download + processing in background
    try:
        # Step 1: Download PBF
        logger.info(f"Downloading OSRM region: {region_id} from {geofabrik_url}")
        dl_result = subprocess.run(
            ["wget", "-q", "-O", f"/tmp/{filename}.osm.pbf", geofabrik_url],
            capture_output=True, text=True, timeout=600,
        )
        if dl_result.returncode != 0:
            return JSONResponse(status_code=500, content={
                "error": "Download failed", "detail": dl_result.stderr,
            })

        # Step 2: Copy to OSRM volume
        subprocess.run([
            "docker", "run", "--rm",
            "-v", f"{OSRM_VOLUME}:/data",
            "-v", f"/tmp:/input",
            "alpine", "cp", f"/input/{filename}.osm.pbf", "/data/region.osm.pbf",
        ], capture_output=True, timeout=60)

        # Step 3: Extract
        logger.info(f"OSRM extract: {region_id}")
        subprocess.run([
            "docker", "run", "--rm",
            "-v", f"{OSRM_VOLUME}:/data",
            "osrm/osrm-backend:latest",
            "osrm-extract", "-p", "/opt/car.lua", "/data/region.osm.pbf",
        ], capture_output=True, timeout=1800)

        # Step 4: Partition
        logger.info(f"OSRM partition: {region_id}")
        subprocess.run([
            "docker", "run", "--rm",
            "-v", f"{OSRM_VOLUME}:/data",
            "osrm/osrm-backend:latest",
            "osrm-partition", "/data/region.osrm",
        ], capture_output=True, timeout=600)

        # Step 5: Customize
        logger.info(f"OSRM customize: {region_id}")
        subprocess.run([
            "docker", "run", "--rm",
            "-v", f"{OSRM_VOLUME}:/data",
            "osrm/osrm-backend:latest",
            "osrm-customize", "/data/region.osrm",
        ], capture_output=True, timeout=600)

        # Step 6: Restart OSRM container
        subprocess.run(
            ["docker", "compose", "restart", "osrm"],
            capture_output=True, timeout=30,
        )

        # Cleanup
        try:
            os.remove(f"/tmp/{filename}.osm.pbf")
        except OSError:
            pass

        return {"success": True, "region_id": region_id, "status": "processed"}

    except subprocess.TimeoutExpired:
        return JSONResponse(status_code=504, content={
            "error": "Processing timed out. Region may be too large for available RAM.",
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


def _get_loaded_osrm_regions() -> list[str]:
    """Check which OSRM region is currently loaded."""
    try:
        import urllib.request
        osrm_url = os.environ.get("OSRM_URL", "http://localhost:5001")
        # If OSRM responds, we have a loaded region
        req = urllib.request.Request(f"{osrm_url}/nearest/v1/driving/-77.0365,38.8977")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            if data.get("code") == "Ok":
                return ["current"]  # OSRM doesn't expose which file is loaded
    except Exception:
        pass
    return []


# ─── Install Mode ────────────────────────────────────────────────────────────

@router.get("/profile")
async def get_profile():
    """Get the current operating profile.

    Returns the active ENKI_PROFILE plus a summary of what it requires.
    Replaces the legacy /install-mode endpoint."""
    from core.profile import resolve_profile, ProfileResolutionError

    try:
        profile, spec = resolve_profile()
    except ProfileResolutionError as e:
        return JSONResponse(status_code=503, content={
            "error": "profile_not_set",
            "detail": str(e),
        })

    return {
        "profile": profile.value,
        "description": spec.description,
        "requires_logto": spec.requires_logto,
        "requires_hardware_key": spec.requires_hardware_key,
        "requires_luks": spec.requires_luks,
        "requires_signed_active_set": spec.requires_signed_active_set,
        "allowed_device_tiers": sorted(spec.allowed_device_tiers),
    }


@router.post("/profile")
async def set_profile(body: dict):
    """Set the operating profile. Writes /etc/enki/enki.conf and requires
    a server restart to take effect.

    Body: { "profile": "dev" | "cloud" | "hardware" | "hardened" | "hub" }

    Each profile has its own prerequisites — see SETUP.md.
    The profile validator runs at startup and refuses to boot if any
    requirement isn't met."""
    from core.profile import Profile, get_spec

    raw = (body.get("profile") or "").strip().lower()
    valid = [p.value for p in Profile]
    if raw not in valid:
        return JSONResponse(status_code=400, content={
            "error": "invalid_profile",
            "detail": f"profile must be one of {valid}",
        })

    profile = Profile(raw)
    spec = get_spec(profile)

    env_lines = [
        f"# Enki server profile — set by setup wizard",
        f"ENKI_PROFILE={profile.value}",
        f"# Validator requirements for this profile:",
        f"#   logto={spec.requires_logto}",
        f"#   hardware_key={spec.requires_hardware_key}",
        f"#   luks={spec.requires_luks}",
        f"#   signed_active_set={spec.requires_signed_active_set}",
        f"# See SETUP.md.",
    ]

    try:
        ENKI_SERVER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        ENKI_SERVER_CONFIG.write_text("\n".join(env_lines) + "\n")
        logger.info("Profile set to '%s' — written to %s", profile.value, ENKI_SERVER_CONFIG)
    except PermissionError:
        logger.warning("Cannot write to %s — profile set for this session only", ENKI_SERVER_CONFIG)

    return {
        "profile": profile.value,
        "description": spec.description,
        "config_path": str(ENKI_SERVER_CONFIG),
        "restart_required": True,
        "message": (
            f"Profile set to '{profile.value}'. Restart the server. "
            f"The validator will check prerequisites and refuse to boot "
            f"if anything required is missing."
        ),
    }


# ─── Complete Setup ───────────────────────────────────────────────────────────

@router.post("/complete")
async def complete_setup(
    body: dict,
    x_tenant_id: str = Header("default", alias="X-Tenant-ID"),
):
    """
    Save the complete setup configuration and mark setup as done.

    Body: {
        install_mode: "cloud" | "hardware" (optional, sets server auth mode),
        network_mode: "connected" | "offline" | "intermittent",
        ai_model: "gemma4:26b",
        ai_mode: "local" | "cloud" | "hybrid",
        osrm_regions: ["us/florida"],
        cloud_provider: "anthropic" (optional),
        cloud_model: "claude-sonnet-4-20250514" (optional),
        cloud_api_key: "sk-..." (optional),
        tile_source: "remote" | "local" | "cache",
        geocode_mode: "remote" | "local" | "cache",
    }
    """
    from core.setup.config import save_setup_config

    # Set install mode if provided (server-level, persists across restarts)
    if body.get("install_mode"):
        await set_install_mode({"install_mode": body["install_mode"]})

    network_mode = body.get("network_mode", "connected")
    ai_model = body.get("ai_model", "gemma4:e2b")
    ai_mode = body.get("ai_mode", "local")
    osrm_regions = body.get("osrm_regions", [])

    # Derive offline settings
    if network_mode == "offline":
        tile_source = "local"
        geocode_mode = "cache"
        ai_mode = "local"  # Force local in offline mode
    elif network_mode == "intermittent":
        tile_source = body.get("tile_source", "cache")
        geocode_mode = "cache"
    else:
        tile_source = body.get("tile_source", "remote")
        geocode_mode = body.get("geocode_mode", "remote")

    # Save config
    result = save_setup_config(
        tenant_id=x_tenant_id,
        network_mode=network_mode,
        ai_model=ai_model,
        ai_mode=ai_mode,
        osrm_regions=osrm_regions,
        tile_source=tile_source,
        geocode_mode=geocode_mode,
    )

    # Update AI config
    try:
        from core.ai.config import set_ai_config
        set_ai_config(
            tenant_id=x_tenant_id,
            provider="ollama",
            model=ai_model,
            config={"base_url": os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")},
            configured_by="setup_wizard",
        )
    except Exception as e:
        logger.warning(f"Failed to set AI config: {e}")

    # Save cloud config if hybrid
    if ai_mode in ("cloud", "hybrid") and body.get("cloud_provider"):
        try:
            from core.ai.config import set_ai_config
            # Store cloud config separately (the AI system reads both)
            set_ai_config(
                tenant_id=f"{x_tenant_id}:cloud",
                provider=body["cloud_provider"],
                model=body.get("cloud_model", ""),
                config={"api_key": body.get("cloud_api_key", "")},
                configured_by="setup_wizard",
            )
        except Exception:
            pass

    return {**result, "setup_completed": True}
