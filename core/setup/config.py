"""
First-Run Setup Configuration.

Manages the setup wizard state: network mode, AI model selection,
OSRM region selection, and offline configuration.

Stored in setup_config table. Read at startup to configure services.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
from typing import Optional

from storage.postgres.db import get_connection

logger = logging.getLogger("enki.setup")


# ─── Hardware Detection ───────────────────────────────────────────────────────

def _detect_gpu() -> dict:
    """Detect NVIDIA GPU via nvidia-smi. Returns empty dict if no GPU."""
    import subprocess

    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,driver_version,compute_cap",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return {}
        line = r.stdout.strip().split("\n")[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            return {}
        vram_mb = int(parts[1])
        return {
            "gpu_name": parts[0],
            "gpu_vram_mb": vram_mb,
            "gpu_vram_gb": round(vram_mb / 1024, 1),
            "gpu_driver": parts[2],
            "gpu_compute_cap": parts[3],
            "gpu_available": True,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return {}


def detect_hardware() -> dict:
    """Detect server hardware (CPU, RAM, disk, GPU) for model recommendations."""
    import psutil

    mem = psutil.virtual_memory()
    disk = shutil.disk_usage("/")

    total_ram_gb = round(mem.total / (1024 ** 3), 1)
    available_ram_gb = round(mem.available / (1024 ** 3), 1)
    total_disk_gb = round(disk.total / (1024 ** 3), 1)
    free_disk_gb = round(disk.free / (1024 ** 3), 1)
    cpu_count = os.cpu_count() or 1

    # GPU detection
    gpu = _detect_gpu()
    gpu_vram_gb = gpu.get("gpu_vram_gb", 0)

    # Estimate RAM available for AI model (total minus OS and services)
    service_overhead_gb = 8  # postgres, api, ui, logto, osrm, traccar, ollama runtime
    model_budget_gb = round(max(total_ram_gb - service_overhead_gb, 0), 1)

    # Recommend model based on budget — GPU VRAM accelerates but RAM is the gate
    if model_budget_gb >= 20:
        recommended_model = "gemma4:26b"
        recommended_label = "Gemma 4 26B MoE (Best)"
        model_ram = 12
    elif model_budget_gb >= 10:
        recommended_model = "gemma4:e4b"
        recommended_label = "Gemma 4 E4B (Very Good)"
        model_ram = 7
    elif model_budget_gb >= 5:
        recommended_model = "gemma4:e2b"
        recommended_label = "Gemma 4 E2B (Good)"
        model_ram = 5
    else:
        recommended_model = "llama3.2:3b"
        recommended_label = "Llama 3.2 3B (Lightweight)"
        model_ram = 3

    # GPU-aware speed estimate for each model
    models = _get_model_options(model_budget_gb, gpu_vram_gb)

    result = {
        "total_ram_gb": total_ram_gb,
        "available_ram_gb": available_ram_gb,
        "model_budget_gb": model_budget_gb,
        "total_disk_gb": total_disk_gb,
        "free_disk_gb": free_disk_gb,
        "cpu_count": cpu_count,
        "platform": platform.system(),
        "arch": platform.machine(),
        "recommended_model": recommended_model,
        "recommended_label": recommended_label,
        "recommended_model_ram": model_ram,
        "models": models,
    }
    result.update(gpu)
    return result


def _estimate_gpu_fit(model_ram_gb: float, gpu_vram_gb: float) -> dict:
    """Estimate how a model uses the GPU given available VRAM."""
    if gpu_vram_gb <= 0:
        return {"gpu_mode": "cpu_only", "est_tok_s": "3-8"}

    # Rough VRAM needed for model weights (Q4 quantization)
    vram_for_weights = {12: 8, 18: 14, 7: 5, 5: 3.5, 3: 2}
    needed = vram_for_weights.get(int(model_ram_gb), model_ram_gb * 0.7)

    if needed <= gpu_vram_gb * 0.85:
        return {"gpu_mode": "full_gpu", "est_tok_s": "30-50"}
    elif needed <= gpu_vram_gb * 1.5:
        return {"gpu_mode": "mostly_gpu", "est_tok_s": "15-25"}
    elif needed <= gpu_vram_gb * 3:
        return {"gpu_mode": "hybrid", "est_tok_s": "8-15"}
    else:
        return {"gpu_mode": "mostly_cpu", "est_tok_s": "3-8"}


def _get_model_options(budget_gb: float, gpu_vram_gb: float = 0) -> list[dict]:
    """Return available model options annotated with fit and GPU status."""
    models = [
        {
            "model_id": "gemma4:26b",
            "name": "Gemma 4 26B MoE",
            "tier": "best",
            "description": "Near-cloud quality, runs locally. Only activates 3.8B params per token.",
            "quality_notes": "Accurate entity extraction with correct typing and roles. "
                             "Sharp headlines, faithful multi-source narratives. "
                             "Entity graph is trustworthy for analyst decisions.",
            "ram_gb": 12,
            "disk_gb": 18,
            "params": "26B total / 3.8B active",
        },
        {
            "model_id": "gemma4:31b",
            "name": "Gemma 4 31B",
            "tier": "maximum",
            "description": "Highest quality local model. Slower inference.",
            "quality_notes": "Maximum accuracy. Best for air-gapped deployments "
                             "that cannot fall back to cloud AI.",
            "ram_gb": 18,
            "disk_gb": 20,
            "params": "31B",
        },
        {
            "model_id": "gemma4:e4b",
            "name": "Gemma 4 E4B",
            "tier": "standard",
            "description": "Fast responses, good quality. Native JSON output.",
            "quality_notes": "Gets most entities with correct types. Good headlines. "
                             "Occasional missed attribution in narratives.",
            "ram_gb": 7,
            "disk_gb": 10,
            "params": "4.5B",
        },
        {
            "model_id": "gemma4:e2b",
            "name": "Gemma 4 E2B",
            "tier": "lightweight",
            "description": "Minimal resources, basic quality. Good for low-RAM servers.",
            "quality_notes": "Gets obvious entities but misses secondary ones. "
                             "Frequently misclassifies entity types. Shallow narratives. "
                             "Suitable for pipeline testing, not operational use.",
            "ram_gb": 5,
            "disk_gb": 7,
            "params": "2.3B",
        },
        {
            "model_id": "llama3.2:3b",
            "name": "Llama 3.2 3B",
            "tier": "minimal",
            "description": "Legacy option. Works on very limited hardware.",
            "quality_notes": "Basic entity extraction only. JSON output unreliable. "
                             "Use only when no other option fits.",
            "ram_gb": 3,
            "disk_gb": 2,
            "params": "3B",
        },
    ]

    for m in models:
        m["fits"] = m["ram_gb"] <= budget_gb
        m["disk_fits"] = True  # checked separately
        gpu_info = _estimate_gpu_fit(m["ram_gb"], gpu_vram_gb)
        m.update(gpu_info)

    return models


# ─── OSRM Regions ─────────────────────────────────────────────────────────────

OSRM_REGIONS = [
    # US States (popular)
    {"region_id": "us/florida", "name": "Florida", "country": "US", "size_mb": 616, "process_ram_gb": 6},
    {"region_id": "us/texas", "name": "Texas", "country": "US", "size_mb": 900, "process_ram_gb": 8},
    {"region_id": "us/california", "name": "California", "country": "US", "size_mb": 850, "process_ram_gb": 8},
    {"region_id": "us/new-york", "name": "New York", "country": "US", "size_mb": 350, "process_ram_gb": 4},
    {"region_id": "us/pennsylvania", "name": "Pennsylvania", "country": "US", "size_mb": 300, "process_ram_gb": 4},
    {"region_id": "us/ohio", "name": "Ohio", "country": "US", "size_mb": 280, "process_ram_gb": 3},
    {"region_id": "us/georgia", "name": "Georgia", "country": "US", "size_mb": 250, "process_ram_gb": 3},
    {"region_id": "us/north-carolina", "name": "North Carolina", "country": "US", "size_mb": 250, "process_ram_gb": 3},
    {"region_id": "us/virginia", "name": "Virginia", "country": "US", "size_mb": 220, "process_ram_gb": 3},
    {"region_id": "us/district-of-columbia", "name": "Washington DC", "country": "US", "size_mb": 20, "process_ram_gb": 1},
    {"region_id": "us/maryland", "name": "Maryland", "country": "US", "size_mb": 150, "process_ram_gb": 2},
    {"region_id": "us/illinois", "name": "Illinois", "country": "US", "size_mb": 310, "process_ram_gb": 4},
    {"region_id": "us/michigan", "name": "Michigan", "country": "US", "size_mb": 260, "process_ram_gb": 3},
    {"region_id": "us/arizona", "name": "Arizona", "country": "US", "size_mb": 200, "process_ram_gb": 3},
    {"region_id": "us/colorado", "name": "Colorado", "country": "US", "size_mb": 200, "process_ram_gb": 3},
    {"region_id": "us/washington", "name": "Washington State", "country": "US", "size_mb": 230, "process_ram_gb": 3},
    {"region_id": "us/oregon", "name": "Oregon", "country": "US", "size_mb": 170, "process_ram_gb": 2},
    {"region_id": "us/massachusetts", "name": "Massachusetts", "country": "US", "size_mb": 160, "process_ram_gb": 2},
    # US Regions
    {"region_id": "us/us-south", "name": "US South (all)", "country": "US", "size_mb": 3200, "process_ram_gb": 16},
    {"region_id": "us/us-northeast", "name": "US Northeast (all)", "country": "US", "size_mb": 2100, "process_ram_gb": 12},
    {"region_id": "us/us-midwest", "name": "US Midwest (all)", "country": "US", "size_mb": 2400, "process_ram_gb": 14},
    {"region_id": "us/us-west", "name": "US West (all)", "country": "US", "size_mb": 3000, "process_ram_gb": 16},
    # International
    {"region_id": "europe/germany", "name": "Germany", "country": "DE", "size_mb": 3800, "process_ram_gb": 16},
    {"region_id": "europe/great-britain", "name": "United Kingdom", "country": "GB", "size_mb": 1100, "process_ram_gb": 8},
    {"region_id": "europe/france", "name": "France", "country": "FR", "size_mb": 3800, "process_ram_gb": 16},
    {"region_id": "asia/japan", "name": "Japan", "country": "JP", "size_mb": 1800, "process_ram_gb": 10},
    {"region_id": "central-america/mexico", "name": "Mexico", "country": "MX", "size_mb": 500, "process_ram_gb": 5},
    {"region_id": "north-america/canada", "name": "Canada", "country": "CA", "size_mb": 2800, "process_ram_gb": 14},
]


def get_osrm_regions(ram_gb: float = 32) -> list[dict]:
    """Return available OSRM regions annotated with processability."""
    regions = []
    for r in OSRM_REGIONS:
        region = {**r}
        region["can_process"] = r["process_ram_gb"] <= ram_gb
        region["geofabrik_url"] = f"https://download.geofabrik.de/{r['region_id']}-latest.osm.pbf"
        regions.append(region)
    return regions


# ─── Setup State ──────────────────────────────────────────────────────────────

def get_setup_status(tenant_id: str = "default", dsn: Optional[str] = None) -> dict:
    """Get current setup state — what's configured and what's not."""
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT network_mode, ai_model, ai_mode, osrm_regions,
                           tile_source, geocode_mode, setup_completed, completed_at
                    FROM setup_config WHERE tenant_id = %s
                """, (tenant_id,))
                row = cur.fetchone()
                if row:
                    return {
                        "configured": True,
                        "network_mode": row[0],
                        "ai_model": row[1],
                        "ai_mode": row[2],
                        "osrm_regions": row[3] or [],
                        "tile_source": row[4],
                        "geocode_mode": row[5],
                        "setup_completed": row[6],
                        "completed_at": str(row[7]) if row[7] else None,
                    }
    except Exception:
        pass

    return {
        "configured": False,
        "network_mode": None,
        "ai_model": None,
        "ai_mode": None,
        "osrm_regions": [],
        "tile_source": None,
        "geocode_mode": None,
        "setup_completed": False,
    }


def save_setup_config(
    tenant_id: str,
    network_mode: str,
    ai_model: str,
    ai_mode: str,
    osrm_regions: list[str],
    tile_source: str = "remote",
    geocode_mode: str = "remote",
    dsn: Optional[str] = None,
) -> dict:
    """Save the complete setup configuration."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO setup_config
                    (tenant_id, network_mode, ai_model, ai_mode, osrm_regions,
                     tile_source, geocode_mode, setup_completed, completed_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, TRUE, NOW())
                ON CONFLICT (tenant_id) DO UPDATE SET
                    network_mode = EXCLUDED.network_mode,
                    ai_model = EXCLUDED.ai_model,
                    ai_mode = EXCLUDED.ai_mode,
                    osrm_regions = EXCLUDED.osrm_regions,
                    tile_source = EXCLUDED.tile_source,
                    geocode_mode = EXCLUDED.geocode_mode,
                    setup_completed = TRUE,
                    completed_at = NOW(),
                    updated_at = NOW()
                RETURNING *
            """, (
                tenant_id, network_mode, ai_model, ai_mode,
                json.dumps(osrm_regions), tile_source, geocode_mode,
            ))
        conn.commit()

    return {
        "saved": True,
        "network_mode": network_mode,
        "ai_model": ai_model,
        "ai_mode": ai_mode,
        "osrm_regions": osrm_regions,
        "tile_source": tile_source,
        "geocode_mode": geocode_mode,
    }
