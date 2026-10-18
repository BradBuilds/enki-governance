"""
Intelligence Setup Wizard — guided walkthrough for data sources, feeds, and topics.

This is the "Phase 4" of onboarding: after infrastructure is running (database,
auth, AI, GDU), this wizard walks the user through selecting interests, curating
RSS sources, toggling free data feeds, entering API keys, and setting location.

Endpoints:
  GET  /intelligence-setup/interests      — available interest bundles
  POST /intelligence-setup/interests      — save selected interests, create watch topics
  GET  /intelligence-setup/sources        — browsable source library grouped by category
  POST /intelligence-setup/sources        — enable/disable selected sources
  GET  /intelligence-setup/feeds          — available free data feeds (recipes)
  POST /intelligence-setup/feeds          — toggle selected feeds
  GET  /intelligence-setup/api-keys       — which API keys are needed and their status
  POST /intelligence-setup/api-keys       — save API keys
  POST /intelligence-setup/location       — set home location for geo-relevant sources
  GET  /intelligence-setup/review         — summary of everything configured
  POST /intelligence-setup/activate       — activate everything and start ingestion
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

logger = logging.getLogger("enki.intelligence_setup")
router = APIRouter(prefix="/intelligence-setup", tags=["intelligence setup"])


# ─── Interest Bundles ────────────────────────────────────────────────────────
# Each bundle maps to: source library categories + watch topic keywords + recipes

INTEREST_BUNDLES = [
    {
        "id": "world_news",
        "title": "World News & Geopolitics",
        "description": "Global events, conflicts, diplomacy, and political developments",
        "icon": "globe",
        "source_categories": ["Global Voices", "Investigative", "Business & Economics"],
        "watch_keywords": ["geopolitics", "diplomacy", "conflict", "international"],
        "recipes": ["gdelt_events"],
        "library_picks": ["rest-of-world", "global-voices", "economist-week", "propublica"],
    },
    {
        "id": "cybersecurity",
        "title": "Cybersecurity & Threats",
        "description": "Vulnerabilities, breaches, threat intelligence, and security research",
        "icon": "shield",
        "source_categories": ["Security"],
        "watch_keywords": ["cybersecurity", "vulnerability", "breach", "malware", "ransomware"],
        "recipes": [],
        "library_picks": ["krebs", "cisa", "schneier", "darknet-diaries"],
    },
    {
        "id": "science_tech",
        "title": "Science & Technology",
        "description": "Research breakthroughs, AI, engineering, and scientific discovery",
        "icon": "microscope",
        "source_categories": ["Math & Science", "Tech & Programming"],
        "watch_keywords": ["research", "discovery", "AI", "engineering", "physics"],
        "recipes": [],
        "library_picks": ["quanta", "nature", "tech-review", "arstechnica", "ieee-spectrum"],
    },
    {
        "id": "finance",
        "title": "Finance & Markets",
        "description": "Markets, economics, crypto, and financial analysis",
        "icon": "chart",
        "source_categories": ["Business & Economics"],
        "watch_keywords": ["markets", "economy", "finance", "trading"],
        "recipes": ["coingecko"],
        "library_picks": ["bbc-business", "economist-week", "planet-money", "markets-matters"],
    },
    {
        "id": "military_defense",
        "title": "Military & Defense",
        "description": "Defense technology, military operations, and strategic analysis",
        "icon": "radar",
        "source_categories": [],
        "watch_keywords": ["military", "defense", "weapons", "intelligence", "NATO"],
        "recipes": ["adsb_aircraft", "ais_vessel_tracker"],
        "library_picks": [],
    },
    {
        "id": "climate_environment",
        "title": "Climate & Environment",
        "description": "Climate change, conservation, natural disasters, and ecology",
        "icon": "leaf",
        "source_categories": ["Nature & Environment"],
        "watch_keywords": ["climate", "environment", "conservation", "wildfire", "ocean"],
        "recipes": ["noaa_weather", "nws_alerts", "usgs_earthquakes", "nasa_eonet"],
        "library_picks": ["nat-geo", "e360", "mongabay", "hakai"],
    },
    {
        "id": "aviation_maritime",
        "title": "Aviation & Maritime",
        "description": "Aircraft tracking, shipping, aviation weather, and transport",
        "icon": "plane",
        "source_categories": [],
        "watch_keywords": ["aviation", "aircraft", "shipping", "maritime"],
        "recipes": ["adsb_aircraft", "ais_vessel_tracker", "aviation_metar", "aviation_taf",
                     "aviation_pirep", "aviation_sigmet"],
        "library_picks": ["flightradar24-blog"],
    },
    {
        "id": "space",
        "title": "Space & Astronomy",
        "description": "Space exploration, satellites, NEOs, and astronomical events",
        "icon": "rocket",
        "source_categories": ["Space & Astronomy"],
        "watch_keywords": ["space", "satellite", "NASA", "astronomy", "orbit"],
        "recipes": [],
        "library_picks": ["nasa", "spacenews", "space-com", "universe-today", "skynightly"],
    },
    {
        "id": "culture_history",
        "title": "Culture, History & Discovery",
        "description": "Archaeology, history, philosophy, art, and unusual stories",
        "icon": "book",
        "source_categories": ["Archaeology & History", "Culture & Discovery", "Weird & Wonderful"],
        "watch_keywords": [],
        "recipes": [],
        "library_picks": ["smithsonian", "atlas-obscura", "aeon", "jstor-daily", "longreads",
                          "futility-closet", "damn-interesting"],
    },
    {
        "id": "health",
        "title": "Health & Medicine",
        "description": "Medical research, public health, disease outbreaks, and health policy",
        "icon": "heart",
        "source_categories": ["Health & Medicine"],
        "watch_keywords": ["health", "medicine", "outbreak", "vaccine", "WHO"],
        "recipes": [],
        "library_picks": ["stat-news", "who-outbreaks", "nejm", "kff-health"],
    },
    {
        "id": "data_journalism",
        "title": "Data & Investigative Journalism",
        "description": "Data-driven reporting, visualizations, and accountability journalism",
        "icon": "bar-chart",
        "source_categories": ["Data Journalism", "Investigative"],
        "watch_keywords": [],
        "recipes": [],
        "library_picks": ["propublica", "markup", "pudding", "owid", "fivethirtyeight"],
    },
]


# ─── Request Models ──────────────────────────────────────────────────────────

class InterestSelection(BaseModel):
    selected_ids: list[str]
    custom_keywords: Optional[list[str]] = None


class SourceSelection(BaseModel):
    enable_ids: list[str] = []
    disable_ids: list[str] = []
    custom_urls: Optional[list[dict]] = None  # [{"url": "...", "label": "..."}]


class FeedSelection(BaseModel):
    enable_recipe_ids: list[str] = []


class ApiKeySubmission(BaseModel):
    keys: dict[str, str]  # {"openweathermap": "abc123", "virustotal": "xyz"}


class LocationSetting(BaseModel):
    latitude: float
    longitude: float
    label: Optional[str] = None  # "Tampa, FL"


# ─── Step 1: Interests ──────────────────────────────────────────────────────

@router.get("/interests")
async def get_interests(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Return available interest bundles for the user to select from."""
    return {
        "bundles": INTEREST_BUNDLES,
        "instruction": (
            "Select the topics you care about. Each selection pre-configures "
            "recommended RSS sources, data feeds, and creates a watch topic "
            "that monitors global news for matching articles."
        ),
    }


@router.post("/interests")
async def save_interests(
    body: InterestSelection,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Save selected interests. Creates watch topics and pre-selects sources."""
    from storage.postgres.db import get_connection

    selected = [b for b in INTEREST_BUNDLES if b["id"] in body.selected_ids]
    created_topics = []
    recommended_sources = set()
    recommended_recipes = set()

    for bundle in selected:
        # Collect source library picks
        for pick in bundle.get("library_picks", []):
            recommended_sources.add(pick)

        # Collect recipe recommendations
        for recipe_id in bundle.get("recipes", []):
            recommended_recipes.add(recipe_id)

        # Create watch topic if bundle has keywords
        keywords = bundle.get("watch_keywords", [])
        if keywords:
            topic = await _create_watch_topic(
                tenant_id=auth.tenant_id,
                title=bundle["title"],
                keywords=keywords,
                description=bundle["description"],
            )
            if topic:
                created_topics.append(topic)

    # Add custom keywords as a watch topic if provided
    if body.custom_keywords:
        topic = await _create_watch_topic(
            tenant_id=auth.tenant_id,
            title="Custom Interests",
            keywords=body.custom_keywords,
            description="Custom keyword monitoring",
        )
        if topic:
            created_topics.append(topic)

    # Save selection to setup_config for later steps
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO setup_config (tenant_id, network_mode, ai_model, ai_mode,
                        osrm_regions, setup_completed)
                    VALUES (%s, 'connected', 'gemma4:e2b', 'local', '[]'::jsonb, FALSE)
                    ON CONFLICT (tenant_id) DO NOTHING
                """, (auth.tenant_id,))
            conn.commit()
    except Exception:
        pass

    return {
        "saved": True,
        "interests_selected": len(selected),
        "watch_topics_created": len(created_topics),
        "recommended_sources": sorted(recommended_sources),
        "recommended_recipes": sorted(recommended_recipes),
        "topics": created_topics,
    }


# ─── Step 2: Source Library Browser ──────────────────────────────────────────

@router.get("/sources")
async def browse_sources(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    category: Optional[str] = None,
):
    """Browse the curated source library, grouped by category.
    Shows which sources are already enabled for this tenant."""
    import json as _json
    from storage.postgres.db import get_connection

    # Load library
    lib_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "libraries", "source_library.json")
    try:
        with open(lib_path) as f:
            library = _json.load(f)
    except Exception:
        library = []

    # Get currently active connectors for this tenant
    active_urls = set()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT source_config->>'url'
                    FROM connectors
                    WHERE tenant_id = %s AND status = 'active'
                """, (auth.tenant_id,))
                active_urls = {r[0] for r in cur.fetchall() if r[0]}
    except Exception:
        pass

    # Build grouped response
    groups: dict[str, list] = {}
    for source in library:
        if category and source.get("category") != category:
            continue
        cat = source.get("category", "Other")
        groups.setdefault(cat, [])
        groups[cat].append({
            **source,
            "enabled": source.get("url", "") in active_urls,
        })

    # Sort categories in a logical order
    category_order = [
        "Math & Science", "Space & Astronomy", "Nature & Environment",
        "Health & Medicine", "Tech & Programming", "Security",
        "Business & Economics", "Investigative", "Data Journalism",
        "Global Voices", "Archaeology & History", "Culture & Discovery",
        "Weird & Wonderful", "Audio & Podcasts", "Academic & Papers",
        "Books & Literature", "Design & Creative", "Niche Interests",
    ]
    ordered = []
    for cat in category_order:
        if cat in groups:
            ordered.append({"category": cat, "sources": groups.pop(cat)})
    for cat in sorted(groups):
        ordered.append({"category": cat, "sources": groups[cat]})

    return {
        "categories": ordered,
        "total_sources": len(library),
        "enabled_count": sum(1 for s in library if s.get("url") in active_urls),
        "instruction": (
            "Browse sources by category. Toggle on the ones that interest you. "
            "Each source is an RSS feed that delivers articles to your Intelligence tab. "
            "You can always add or remove sources later."
        ),
    }


@router.post("/sources")
async def toggle_sources(
    body: SourceSelection,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Enable or disable sources from the library. Also accepts custom RSS URLs."""
    import json as _json
    from storage.postgres.db import get_connection

    lib_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "libraries", "source_library.json")
    try:
        with open(lib_path) as f:
            library = {s["id"]: s for s in _json.load(f)}
    except Exception:
        library = {}

    enabled = []
    errors = []

    # Enable selected library sources
    for source_id in body.enable_ids:
        source = library.get(source_id)
        if not source:
            errors.append({"id": source_id, "error": "not found in library"})
            continue

        url = source["url"]
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
        connector_id = f"conn_lib_{url_hash}"
        device_id = f"dev_lib_{url_hash}"

        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO connectors
                            (connector_id, tenant_id, device_id, connector_type, source_type,
                             source_config, poll_interval, status, created_at, updated_at)
                        VALUES (%s, %s, %s, 'pull', 'rss', %s, '15 minutes', 'active', NOW(), NOW())
                        ON CONFLICT (connector_id) DO UPDATE SET status = 'active', updated_at = NOW()
                    """, (
                        connector_id, auth.tenant_id, device_id,
                        _json.dumps({
                            "url": url,
                            "feed_title": source["title"],
                            "label": source["title"],
                            "category": source.get("category", "general"),
                            "region": "global",
                            "max_items_per_fetch": 20,
                            "enrich_articles": True,
                            "added_by": "intelligence_setup",
                        }),
                    ))
                conn.commit()
            enabled.append({"id": source_id, "title": source["title"]})
        except Exception as e:
            errors.append({"id": source_id, "error": str(e)})

    # Disable selected sources
    disabled_count = 0
    if body.disable_ids:
        for source_id in body.disable_ids:
            source = library.get(source_id)
            if not source:
                continue
            url = source["url"]
            url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
            connector_id = f"conn_lib_{url_hash}"
            try:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE connectors SET status = 'inactive', updated_at = NOW()
                            WHERE connector_id = %s AND tenant_id = %s
                        """, (connector_id, auth.tenant_id))
                    conn.commit()
                disabled_count += 1
            except Exception:
                pass

    # Add custom RSS URLs
    custom_added = []
    for custom in (body.custom_urls or []):
        url = custom.get("url", "").strip()
        label = custom.get("label", "")
        if not url:
            continue
        from services.api.src.routes.sources import _is_safe_url
        if not _is_safe_url(url):
            errors.append({"url": url, "error": "blocked — targets private network"})
            continue

        url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
        connector_id = f"conn_user_{url_hash}"
        device_id = f"dev_user_{url_hash}"
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO connectors
                            (connector_id, tenant_id, device_id, connector_type, source_type,
                             source_config, poll_interval, status, created_at, updated_at)
                        VALUES (%s, %s, %s, 'pull', 'rss', %s, '15 minutes', 'active', NOW(), NOW())
                        ON CONFLICT (connector_id) DO UPDATE SET status = 'active', updated_at = NOW()
                    """, (
                        connector_id, auth.tenant_id, device_id,
                        _json.dumps({
                            "url": url,
                            "feed_title": label or url,
                            "label": label or url,
                            "category": "custom",
                            "region": "global",
                            "max_items_per_fetch": 20,
                            "enrich_articles": True,
                            "added_by": "intelligence_setup",
                        }),
                    ))
                conn.commit()
            custom_added.append({"url": url, "label": label or url})
        except Exception as e:
            errors.append({"url": url, "error": str(e)})

    return {
        "enabled": enabled,
        "disabled": disabled_count,
        "custom_added": custom_added,
        "errors": errors,
    }


# ─── Step 3: Free Data Feeds (Recipes) ───────────────────────────────────────

FREE_FEEDS = [
    {"recipe_id": "noaa_weather", "category": "Weather", "title": "NOAA Weather (US)", "description": "Current conditions and forecasts from the National Weather Service", "requires_location": True},
    {"recipe_id": "nws_alerts", "category": "Weather", "title": "NWS Severe Weather Alerts", "description": "Tornado, hurricane, flood warnings — push alerts", "requires_location": True},
    {"recipe_id": "usgs_earthquakes", "category": "Weather", "title": "USGS Earthquakes", "description": "Global seismic events, magnitude 2.5+", "requires_location": False},
    {"recipe_id": "nasa_eonet", "category": "Weather", "title": "NASA Natural Events", "description": "Wildfires, volcanic eruptions, floods, storms worldwide", "requires_location": False},
    {"recipe_id": "noaa_tides", "category": "Weather", "title": "NOAA Tides & Currents", "description": "Tide predictions for US coastal stations", "requires_location": True},
    {"recipe_id": "aviation_metar", "category": "Aviation", "title": "Airport Weather (METAR)", "description": "Real-time weather observations at airports", "requires_location": True},
    {"recipe_id": "aviation_taf", "category": "Aviation", "title": "Terminal Forecasts (TAF)", "description": "24-hour airport weather forecasts", "requires_location": True},
    {"recipe_id": "aviation_pirep", "category": "Aviation", "title": "Pilot Reports (PIREP)", "description": "Turbulence, icing, and weather from pilots in flight", "requires_location": False},
    {"recipe_id": "aviation_sigmet", "category": "Aviation", "title": "SIGMETs", "description": "Significant weather advisories for aviation", "requires_location": False},
    {"recipe_id": "adsb_aircraft", "category": "Tracking", "title": "ADS-B Aircraft Tracking", "description": "Live aircraft positions from adsb.lol (global)", "requires_location": True},
    {"recipe_id": "ais_vessel_tracker", "category": "Tracking", "title": "AIS Ship Tracking", "description": "Vessel positions from digitraffic.fi (Baltic region)", "requires_location": False},
    {"recipe_id": "gdelt_events", "category": "News", "title": "GDELT Global Events", "description": "Conflicts, protests, diplomacy from 100K+ outlets, 65 languages", "requires_location": False},
    {"recipe_id": "coingecko", "category": "Financial", "title": "Cryptocurrency Prices", "description": "Bitcoin, Ethereum, and 10,000+ coin prices", "requires_location": False},
]


@router.get("/feeds")
async def get_available_feeds(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List available free data feeds (recipes) that can be toggled on."""
    from storage.postgres.db import get_connection

    # Check which are already active
    active_recipes = set()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT device_id FROM connectors
                    WHERE tenant_id = %s AND status = 'active'
                      AND device_id LIKE 'recipe_%%'
                """, (auth.tenant_id,))
                for r in cur.fetchall():
                    # device_id format: recipe_{recipe_id}_{tenant_id}
                    parts = r[0].split("_", 2)
                    if len(parts) >= 2:
                        active_recipes.add(parts[1])
    except Exception:
        pass

    feeds = []
    for feed in FREE_FEEDS:
        feeds.append({
            **feed,
            "enabled": feed["recipe_id"] in active_recipes,
            "cost": "free",
        })

    return {
        "feeds": feeds,
        "instruction": (
            "These data feeds are completely free with no API key required. "
            "Toggle on the ones relevant to your work. Location-dependent feeds "
            "will use the coordinates you set in the next step."
        ),
    }


@router.post("/feeds")
async def toggle_feeds(
    body: FeedSelection,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Enable selected free data feeds by instantiating their recipes."""
    from storage.connectors.recipes import get_recipe, instantiate_recipe
    from storage.connectors.manager import create_connector

    enabled = []
    errors = []

    for recipe_id in body.enable_recipe_ids:
        recipe = get_recipe(recipe_id)
        if not recipe:
            errors.append({"recipe_id": recipe_id, "error": "recipe not found"})
            continue

        instance = instantiate_recipe(recipe_id, {})
        if not instance:
            errors.append({"recipe_id": recipe_id, "error": "instantiation failed"})
            continue

        conn_config = instance["connector_config"]
        device_id = f"recipe_{recipe_id}_{auth.tenant_id}"
        source_config = conn_config["source_config"]
        if instance.get("signal_type_hint"):
            source_config["signal_type_hint"] = instance["signal_type_hint"]

        try:
            result = create_connector(
                tenant_id=auth.tenant_id,
                device_id=device_id,
                connector_type=conn_config.get("connector_type", "pull"),
                source_type=conn_config["source_type"],
                source_config=source_config,
                poll_interval=conn_config.get("poll_interval", "5 minutes"),
            )
            enabled.append({"recipe_id": recipe_id, "connector_id": result.get("connector_id")})
        except Exception as e:
            errors.append({"recipe_id": recipe_id, "error": str(e)})

    return {"enabled": enabled, "errors": errors}


# ─── Step 4: API Keys ────────────────────────────────────────────────────────

API_KEY_SOURCES = [
    {
        "service": "openweathermap",
        "title": "OpenWeatherMap",
        "description": "Global weather data — current conditions, forecasts, historical",
        "register_url": "https://openweathermap.org/appid",
        "cost": "Free (60 calls/min)",
        "key_format": "32-character hex string",
        "recipe_id": "openweathermap",
    },
    {
        "service": "openaq",
        "title": "OpenAQ",
        "description": "Global air quality measurements from government stations",
        "register_url": "https://explore.openaq.org/register",
        "cost": "Free",
        "key_format": "API key from dashboard",
        "recipe_id": "openaq",
    },
    {
        "service": "abuseipdb",
        "title": "AbuseIPDB",
        "description": "IP reputation — check if an IP has been reported for abuse",
        "register_url": "https://www.abuseipdb.com/register",
        "cost": "Free (1,000 checks/day)",
        "key_format": "80-character key",
        "recipe_id": "abuseipdb",
    },
    {
        "service": "virustotal",
        "title": "VirusTotal",
        "description": "File and URL scanning against 70+ antivirus engines",
        "register_url": "https://www.virustotal.com/gui/join-us",
        "cost": "Free (4 lookups/min)",
        "key_format": "64-character hex string",
        "recipe_id": "virustotal",
    },
    {
        "service": "shodan",
        "title": "Shodan",
        "description": "Internet-wide port scanning — find exposed services",
        "register_url": "https://account.shodan.io/register",
        "cost": "Free (limited), paid plans available",
        "key_format": "Alphanumeric API key",
        "recipe_id": "shodan_monitor",
    },
]


@router.get("/api-keys")
async def get_api_key_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Show which API keys are available to configure and their current status."""
    from storage.secrets import list_secrets

    # Check which keys are already stored
    stored = {}
    try:
        secrets = list_secrets(tenant_id=auth.tenant_id, category="api_key")
        stored = {s["name"]: True for s in secrets}
    except Exception:
        pass

    sources = []
    for src in API_KEY_SOURCES:
        sources.append({
            **src,
            "configured": src["service"] in stored,
        })

    return {
        "api_keys": sources,
        "configured_count": sum(1 for s in sources if s["configured"]),
        "instruction": (
            "These services offer free API keys that unlock additional data sources. "
            "Click the registration link, create a free account, copy your API key, "
            "and paste it here. You can skip any source and add keys later."
        ),
    }


@router.post("/api-keys")
async def save_api_keys(
    body: ApiKeySubmission,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Save API keys and optionally enable their associated recipes."""
    from storage.secrets import create_secret
    from storage.connectors.recipes import get_recipe, instantiate_recipe
    from storage.connectors.manager import create_connector

    saved = []
    enabled = []
    errors = []

    for service, key in body.keys.items():
        key = key.strip()
        if not key:
            continue

        # Save as a secret
        try:
            create_secret(
                tenant_id=auth.tenant_id,
                name=service,
                value=key,
                category="api_key",
                description=f"API key for {service}",
                created_by=auth.subject_id,
            )
            saved.append(service)
        except Exception as e:
            errors.append({"service": service, "error": str(e)})
            continue

        # Find matching recipe and enable it
        src = next((s for s in API_KEY_SOURCES if s["service"] == service), None)
        if src and src.get("recipe_id"):
            recipe = get_recipe(src["recipe_id"])
            if recipe:
                instance = instantiate_recipe(src["recipe_id"], {"api_key": f"$secret:{service}"})
                if instance:
                    conn_config = instance["connector_config"]
                    device_id = f"recipe_{src['recipe_id']}_{auth.tenant_id}"
                    source_config = conn_config["source_config"]
                    if instance.get("signal_type_hint"):
                        source_config["signal_type_hint"] = instance["signal_type_hint"]
                    try:
                        result = create_connector(
                            tenant_id=auth.tenant_id,
                            device_id=device_id,
                            connector_type=conn_config.get("connector_type", "pull"),
                            source_type=conn_config["source_type"],
                            source_config=source_config,
                            poll_interval=conn_config.get("poll_interval", "5 minutes"),
                        )
                        enabled.append({"service": service, "recipe_id": src["recipe_id"]})
                    except Exception as e:
                        errors.append({"service": service, "error": f"recipe enable failed: {e}"})

    return {"saved": saved, "enabled": enabled, "errors": errors}


# ─── Step 5: Location ────────────────────────────────────────────────────────

@router.post("/location")
async def set_location(
    body: LocationSetting,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Set home location for geo-relevant data sources.
    Updates weather stations, tide stations, and airport codes based on coordinates."""
    from storage.postgres.db import get_connection

    lat, lon = body.latitude, body.longitude
    label = body.label or f"{lat:.4f}, {lon:.4f}"

    # Save location preference
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_preferences (tenant_id, identity_id, preference_key, preference_value, updated_at)
                    VALUES (%s, %s, 'home_location', %s, NOW())
                    ON CONFLICT (tenant_id, identity_id, preference_key) DO UPDATE
                    SET preference_value = EXCLUDED.preference_value, updated_at = NOW()
                """, (
                    auth.tenant_id, auth.subject_id,
                    json.dumps({"latitude": lat, "longitude": lon, "label": label}),
                ))
            conn.commit()
    except Exception as e:
        logger.warning("Could not save location preference: %s", e)

    # Find nearest reference points for geo-relevant feeds
    nearest = {}
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Nearest airport (for METAR/TAF)
                cur.execute("""
                    SELECT icao, name, municipality,
                           ST_Distance(geom::geography, ST_MakePoint(%s,%s)::geography) / 1000 AS dist_km
                    FROM airports WHERE icao IS NOT NULL AND type IN ('large_airport','medium_airport')
                    ORDER BY geom <-> ST_MakePoint(%s,%s) LIMIT 1
                """, (lon, lat, lon, lat))
                row = cur.fetchone()
                if row:
                    nearest["airport"] = {"icao": row[0], "name": row[1], "city": row[2], "distance_km": round(row[3], 1)}

                # Nearest port
                cur.execute("""
                    SELECT port_name, country,
                           ST_Distance(geom::geography, ST_MakePoint(%s,%s)::geography) / 1000 AS dist_km
                    FROM ports
                    ORDER BY geom <-> ST_MakePoint(%s,%s) LIMIT 1
                """, (lon, lat, lon, lat))
                row = cur.fetchone()
                if row:
                    nearest["port"] = {"name": row[0], "country": row[1], "distance_km": round(row[2], 1)}
    except Exception as e:
        logger.warning("Nearest reference lookup failed: %s", e)

    return {
        "saved": True,
        "location": {"latitude": lat, "longitude": lon, "label": label},
        "nearest": nearest,
        "instruction": (
            "Your location is set. Weather, aviation, and tracking feeds "
            "will use these coordinates to show relevant local data."
        ),
    }


# ─── Step 6: Review & Activate ───────────────────────────────────────────────

@router.get("/review")
async def review_setup(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Summary of everything configured — shown before final activation."""
    from storage.postgres.db import get_connection

    summary = {
        "sources": 0,
        "watch_topics": 0,
        "recipes": 0,
        "api_keys": 0,
        "location_set": False,
        "details": {},
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Count active RSS connectors
                cur.execute("""
                    SELECT COUNT(*) FROM connectors
                    WHERE tenant_id = %s AND status = 'active' AND source_type = 'rss'
                """, (auth.tenant_id,))
                summary["sources"] = cur.fetchone()[0]

                # Count active recipe connectors
                cur.execute("""
                    SELECT COUNT(*) FROM connectors
                    WHERE tenant_id = %s AND status = 'active' AND device_id LIKE 'recipe_%%'
                """, (auth.tenant_id,))
                summary["recipes"] = cur.fetchone()[0]

                # Count watch topics
                cur.execute("""
                    SELECT COUNT(*) FROM watch_topics
                    WHERE tenant_id = %s AND status = 'active'
                """, (auth.tenant_id,))
                summary["watch_topics"] = cur.fetchone()[0]

                # Count API keys
                cur.execute("""
                    SELECT COUNT(*) FROM secrets
                    WHERE tenant_id = %s AND category = 'api_key'
                """, (auth.tenant_id,))
                summary["api_keys"] = cur.fetchone()[0]

                # Check location
                cur.execute("""
                    SELECT preference_value FROM user_preferences
                    WHERE tenant_id = %s AND preference_key = 'home_location'
                    LIMIT 1
                """, (auth.tenant_id,))
                row = cur.fetchone()
                if row:
                    summary["location_set"] = True
                    summary["location"] = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except Exception as e:
        logger.warning("Review query failed: %s", e)

    return {
        **summary,
        "ready": summary["sources"] > 0 or summary["recipes"] > 0,
        "instruction": (
            "Review your configuration below. When you activate, all enabled "
            "sources will begin polling for data. Articles will appear in your "
            "Intelligence tab within minutes."
        ),
    }


@router.post("/activate")
async def activate_intelligence(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Activate all configured intelligence sources. Starts ingestion."""
    from storage.postgres.db import get_connection

    activated = 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Ensure all active connectors have their next_fetch set
                cur.execute("""
                    UPDATE connectors
                    SET next_fetch_at = NOW(),
                        updated_at = NOW()
                    WHERE tenant_id = %s AND status = 'active' AND next_fetch_at IS NULL
                    RETURNING connector_id
                """, (auth.tenant_id,))
                activated = cur.rowcount
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    logger.info("Intelligence activated for tenant %s — %d connectors queued", auth.tenant_id, activated)

    return {
        "activated": True,
        "connectors_queued": activated,
        "message": (
            f"Intelligence is live. {activated} data sources are now polling. "
            "Articles will appear in your Intelligence tab within a few minutes. "
            "Watch topics will begin backfilling historical data in the background."
        ),
    }


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _create_watch_topic(
    tenant_id: str,
    title: str,
    keywords: list[str],
    description: str,
) -> Optional[dict]:
    """Create a watch topic with the given keywords."""
    from storage.postgres.db import get_connection
    import uuid

    topic_id = f"topic_{uuid.uuid4().hex[:12]}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO watch_topics
                        (topic_id, tenant_id, title, description, keywords, status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 'active', NOW(), NOW())
                    ON CONFLICT DO NOTHING
                    RETURNING topic_id
                """, (
                    topic_id, tenant_id, title, description, keywords,
                ))
                row = cur.fetchone()
            conn.commit()
        if row:
            return {"topic_id": row[0], "title": title, "keywords": keywords}
    except Exception as e:
        logger.warning("Watch topic creation failed: %s", e)
    return None
