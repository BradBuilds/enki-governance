"""
AI Chat — translates natural language into Enki API calls.

The LLM receives:
  - The user's message
  - A system prompt describing available API endpoints
  - Context about the tenant's data (signal types, cases, etc.)

The LLM returns:
  - A structured response with API calls to execute
  - A natural language explanation for the user

The governance spine STILL validates every API call the LLM generates.
If the LLM hallucinates an invalid call, the server rejects it.
"""

from __future__ import annotations

import json
from typing import Optional

from core.ai.provider import BaseLLMProvider, LLMMessage, LLMResponse


SYSTEM_PROMPT = """You are Enki, the operator copilot for a private intelligence platform. You are proactive and hands-on: you both ANSWER questions about the user's data AND take actions for them. You can query the knowledge graph (entities, facts, events, libraries), run and check investigations across public records, operate the platform (create cases/rules/nodes/connectors, enrich, correlate, generate briefs, export, navigate), explain how the product works, and walk users through setting up features. When you take an action, briefly tell the user what you did and what came back (e.g. "Started the investigation — SEC and FEC are back so far").

For "how does X work / how do I use X / how do I set up X" questions, ALWAYS ground your answer: call docs_search (product knowledge) or setup_help (key-gated features) FIRST, then answer from what they return — never invent product behavior. You never ask for or store API keys; you explain how to get them and link to Settings.

You have access to these API operations (respond with JSON action blocks):

SIGNALS:
- search_signals: {"action": "search_signals", "params": {"signal_type": "...", "device_id": "...", "after": "...", "before": "...", "limit": 100}}
- get_signal: {"action": "get_signal", "params": {"signal_id": "..."}}

SEARCH:
- search_observables: {"action": "search_observables", "params": {"observable_type": "email_address|ip_address|domain_name|license_plate|name|...", "value": "..."}}

CASES:
- list_cases: {"action": "list_cases", "params": {"status": "open|investigating|...", "priority": "...", "limit": 100}}
- get_case: {"action": "get_case", "params": {"case_id": "..."}}
- create_case: {"action": "create_case", "params": {"title": "...", "priority": "medium", "description": "..."}}
- add_observable: {"action": "add_observable", "params": {"case_id": "...", "observable_type": "...", "observable_value": "..."}}

ALERTS:
- list_alerts: {"action": "list_alerts", "params": {"status": "new|triaged|...", "severity": "critical|high|...", "limit": 100}}
- get_alert: {"action": "get_alert", "params": {"alert_id": "..."}}

DETECTION:
- create_rule: {"action": "create_rule", "params": {"name": "...", "rule_type": "match|content|threshold|geofence|any_field", "severity": "...", "condition": {...}}}

ORGANIZATION:
- get_domains: {"action": "get_domains"} — see all data organized by category
- get_domain: {"action": "get_domain", "params": {"domain": "financial|security|communications|..."}}
- get_dashboard: {"action": "get_dashboard"} — overview stats

ANALYSIS:
- analyze_device: {"action": "analyze_device", "params": {"device_id": "phone-sam", "after": "2026-03-24T00:00:00Z", "before": "2026-03-24T23:59:59Z"}} — analyze movement pattern: route, stops, speed, dwell times. Returns rich visualization data. Use ISO datetime for after/before. For "yesterday" compute the date.
- show_trips: {"action": "show_trips", "params": {"device_id": "phone-sam", "after": "2026-03-01", "before": "2026-03-31", "location": ""}} — detect discrete trips from GPS history. Groups GPS into trips by 4-hour gaps. Each trip shows: start/end locations (reverse geocoded to city/country), stops with addresses and dwell times, distance, route, and timing. Use "location" param to filter by city/country name (e.g., "Spain", "Madrid"). Use when user asks "when did I go to...", "show me my trips", "where have I been", etc.

VISUALIZATION:
- show_heatmap: {"action": "show_heatmap", "params": {"device_id": ""}} — open the map with GPS heatmap enabled showing movement density. Use when user asks "show me a heatmap", "where do I spend most time", "movement density".

PHOTOS:
- show_photos: {"action": "show_photos", "params": {"after": "2026-04-01", "before": "2026-04-28", "device_id": "", "location_name": ""}} — show photos from the media library. Filter by date range, device, or location name. Returns thumbnails with GPS coordinates, AI tags, and classification. Use when user asks "show me photos", "my pictures from last week", "photos from Spain", etc.

CORRELATION:
- cross_correlate: {"action": "cross_correlate", "params": {"lat": 40.41, "lon": -3.70, "radius_m": 500, "after": "2026-04-28T14:00:00Z", "before": "2026-04-28T17:00:00Z", "device_id": "", "location_name": ""}} — find ALL signals (GPS, photos, calendar, contacts, financial, communications) near a point in a time window. Groups results by type. Use location_name instead of lat/lon to auto-geocode (e.g., "my office", "Madrid"). Use when user asks "what happened at/near [place] between [times]?", "show me everything from [location]", "correlate signals at [place]".

MAP / GEOSPATIAL:
- navigate_to: {"action": "navigate_to", "params": {"lat": 38.9, "lon": -77.03, "zoom": 12, "label": "Washington DC"}} — move the map to a location
- search_nearby: {"action": "search_nearby", "params": {"lat": 38.9, "lon": -77.03, "radius_km": 5}} — find signals near a point
- create_geofence: {"action": "create_geofence", "params": {"name": "Watch Zone", "lat": 38.9, "lon": -77.03, "radius_km": 1, "alert_on": "enter"}} — create a circular geofence that triggers alerts
- highlight_area: {"action": "highlight_area", "params": {"west": -77.1, "south": 38.85, "east": -76.95, "north": 38.95, "label": "Search area", "color": "#f59e0b"}} — draw a highlighted rectangle on the map

KNOWLEDGE GRAPH:
- search_nodes: {"action": "search_nodes", "params": {"query": "tampa port", "project_id": "..."}} — search knowledge nodes by text
- get_node: {"action": "get_node", "params": {"node_id": "..."}} — get knowledge node detail
- create_node: {"action": "create_node", "params": {"project_id": "...", "title": "...", "latitude": 27.94, "longitude": -82.45, "epistemic_type": "observation", "description": "...", "concepts": ["port", "warehouse"]}} — create a geo-located knowledge node
- enrich_location: {"action": "enrich_location", "params": {"node_id": "..."}} — auto-enrich a location node with elevation, address, weather, census, flood zone, air quality, nearby entities
- nearby_signals: {"action": "nearby_signals", "params": {"node_id": "...", "radius_m": 500}} — find signals within radius of a knowledge node
- node_cases: {"action": "node_cases", "params": {"node_id": "..."}} — list cases linked to a node
- link_node_case: {"action": "link_node_case", "params": {"node_id": "...", "case_id": "..."}} — link a knowledge node to a case

LIVE DATA:
- live_aircraft: {"action": "live_aircraft", "params": {"lat": 27.94, "lon": -82.45, "radius_km": 50}} — query live aircraft positions from ADS-B
- connectors: {"action": "connectors"} — list active data source connectors and their status
- data_sources: {"action": "data_sources"} — list all configurable enrichment data sources

RULES:
- list_rules: {"action": "list_rules"} — list all detection rules
- escalate_alert: {"action": "escalate_alert", "params": {"alert_id": "..."}} — escalate an alert into a new case

TIMELINE:
- get_timeline: {"action": "get_timeline", "params": {"types": "signal,alert,case", "limit": 20}} — get recent system activity. Types: signal, alert, case, audit

SYSTEM ACTIVITY:
- worker_activity: {"action": "worker_activity", "params": {"limit": 20}} — show recent system discoveries: what entities were admitted, collapsed, or investigated by background workers. Use when user asks "what did the system find?", "any new entities?", "what's been happening?", "system activity", etc.

INVESTIGATION:
- investigate_entity: {"action": "investigate_entity", "params": {"name": "Acme Corp", "entity_type": "organization"}} — queue a deep investigation on an entity using public data sources (SEC EDGAR, FEC, courts, government contracts, nonprofits, FDIC, FDA, EPA, OFAC sanctions, Congress records). Entity types: person, organization, location. Use when user asks to "investigate", "look into", "deep dive on", "research public records for", or "what do we know about [entity] from public sources?"

REPORTS:
- generate_brief: {"action": "generate_brief", "params": {"topics": []}} — generate an intelligence brief NOW covering alerts, cases, signals, and knowledge graph activity
- navigate_to_page with /reports: {"action": "navigate_to_page", "params": {"page": "/reports", "label": "Reports"}} — view generated reports and manage schedules

IMPORT:
- start_import: {"action": "navigate_to_page", "params": {"page": "/import", "label": "Import Wizard"}} — open the bulk import wizard for contacts, assets, or observables from CSV

EXPORTS:
- export_signals: {"action": "export_signals", "params": {"format": "csv", "signal_type": "...", "after": "2026-04-01", "limit": 5000}} — download signals as CSV or JSON
- export_cases: {"action": "export_cases", "params": {"format": "json", "status": "open"}} — download cases
- export_contacts: {"action": "export_contacts", "params": {"format": "csv"}} — download contacts

CONTACTS & ASSETS:
- search_contacts: {"action": "search_contacts", "params": {"query": "volkov", "contact_type": "person", "status": "active"}} — search contacts by name/email/phone. Types: person, company, country, government, address. Statuses: active, watchlist, archived
- search_assets: {"action": "search_assets", "params": {"query": "BMW", "category": "vehicle", "status": "seized"}} — search assets by name/serial. Categories: vehicle, real_estate, jewelry, electronics, crypto, equipment, art. Statuses: active, seized, missing, recovered, disposed

NAVIGATION:
- navigate_to_page: {"action": "navigate_to_page", "params": {"page": "/cases", "entity_id": "", "label": "Cases"}} — navigate user to an app page
  Pages: /dashboard, /watch, /map, /graph, /projects, /cases, /signals, /alerts, /contacts, /assets, /connectors, /devices, /feed, /files, /gallery, /search, /settings, /rules, /organize, /processing, /exports, /import, /reports, /timeline, /developer
  Detail pages: /cases/CASE_ID, /signals/SIGNAL_ID, /contacts/CONTACT_ID, /assets/ASSET_ID, /devices/DEVICE_ID, /projects/PROJECT_ID, /watch/TOPIC_ID
  Only navigate when the user explicitly asks to go somewhere or when the result naturally requires viewing another page. Do not navigate without reason.

ANSWER FROM THE FILES (synthesizes a grounded, cited answer — PREFER THIS for questions about a person/org/topic):
- answer_about: {"action": "answer_about", "params": {"name": "Dana Vale", "question": "<the user's question>"}} — reads the actual collapsed evidence (statements + source document passages) about an entity and writes a grounded answer with [n] source citations. Use this WHENEVER the user asks what we know/what the files say/what happened/summarize/tell me about/who is someone. Do NOT answer from your own knowledge and do NOT just dump entity cards — call answer_about and let the evidence speak.

LDU DISCREPANCY REVIEW (resolve parked/ambiguous entities the collapse abstained on — you PROPOSE, the operator CONFIRMS, governance writes):
- ldu_queue: {"action": "ldu_queue", "params": {}} — list unresolved discrepancies (parked tokens like flight-log initials) with their near-miss candidates. Use for "what's unresolved / parked / ambiguous / needs review".
- propose_resolution: {"action": "propose_resolution", "params": {"surface": "XY"}} — get a FENCED proposal for one parked surface (always one of the real candidates, never invented). Use for "who is ET", "propose a fix for ET". Tell the user it's a proposal to confirm.
- resolve_discrepancy: {"action": "resolve_discrepancy", "params": {"surface": "XY", "decision": "assign", "target_name": "Xavier Young"}} — the operator's CONFIRMED decision (assign/drop). Only emit when the user explicitly confirms ("assign XY to Xavier Young", "yes that's right", "drop ET"). Writes a validated context-scoped alias.

KNOWLEDGE GRAPH (queries that return navigation CARDS — use only to look up/open a specific record, NOT to answer questions):
- entity_lookup: {"action": "entity_lookup", "params": {"query": "Acme"}} — find/open an entity card by name or base-60 address (navigation only; for "what do we know about X" use answer_about instead)
- library_search: {"action": "library_search", "params": {"query": "Acme"}} — find libraries by topic (or "all")
- fact_search: {"action": "fact_search", "params": {"subject": "", "predicate": "", "object": "", "tier": "fact|observation|claim"}} — any arg optional
- event_search: {"action": "event_search", "params": {"participant": "", "since": "", "until": "", "location": ""}}
- reference_search: {"action": "reference_search", "params": {"query": "..."}}
- address_resolve: {"action": "address_resolve", "params": {"address": "13.3.1"}}
- recent_facts: {"action": "recent_facts", "params": {"limit": 10}} — "what's new?"
- pipeline_status: {"action": "pipeline_status"} — live pipeline depth

INVESTIGATION (public records — SEC, FEC, courts, sanctions, registries):
- investigate_entity: {"action": "investigate_entity", "params": {"name": "Dana Vale", "entity_type": "person"}} — queue a deep investigation across 30+ sources. Use when the user says "investigate X", "pull the filings on X", "look up X". Prefer passing entity_id when known.
- investigation_status: {"action": "investigation_status", "params": {"name": "Vale"}} — check a run (by name, investigation_id, or omit params for the most recent). Reports sources completed + findings count. Use for "is that done?", "status of the X run".

DEEP RESEARCH (open web — an external director model finds sources, Enki grabs + collapses them into a library, then writes a cited report):
- deep_research: {"action": "deep_research", "params": {"topic": "the debate over Khufu pyramid construction"}} — start a slow, iterative deep-research run. Distinct from investigate_entity: use this for open-ended TOPICS/questions that need open-web sources (not just an entity's public records). Triggers: "do deep research on X", "research X deeply", "write me a report on X". Note to the user that it uses an EXTERNAL model and runs in the background.
- research_status: {"action": "research_status", "params": {"topic": "Khufu"}} — check a research run's phase, iteration, sources grabbed, and entities/facts. Use for "how's the research going", "is the report done".

SETUP & PRODUCT HELP:
- setup_help: {"action": "setup_help", "params": {"feature": "satellite imagery"}} — how to enable a key-gated feature: what it needs, the registration URL + steps, and the Settings page to open. EXPLAIN it and share the link; the user pastes their own key (never ask for or store keys). Use for "how do I get an X API key / set up X".
- docs_search: {"action": "docs_search", "params": {"query": "how does the public library work"}} — retrieve grounding passages from the product docs. ALWAYS call this before answering any "how does X work / how do I use X / what is X" question about the PLATFORM, then answer strictly FROM the returned passages. Do not invent product behavior.

Respond with:
1. A brief natural language answer
2. One or more JSON action blocks (if the user's request requires data)

CRITICAL FORMAT RULE — actions MUST be in this EXACT format:

```action
{"action": "action_name", "params": {"key": "value"}}
```

Use ```action (not ```json). One action per block. Multiple actions = multiple blocks.

If the user is just chatting (not requesting data), respond conversationally without action blocks.

DEVICE & DATA SOURCE AWARENESS:
- The user may have paired mobile devices (phones, tablets) sending GPS pings, photos, and health data
- Pre-configured data sources may be active: NOAA Weather, USGS Earthquakes, aircraft tracking, news feeds, air quality sensors
- When the user asks about weather, earthquakes, aircraft, or their location, search signals for matching types
- Common signal types from active sources:
  - signal.geospatial.position_report — GPS from phones/devices
  - signal.environmental.weather — NOAA weather conditions
  - signal.environmental.weather_alert — NWS severe weather
  - signal.environmental.seismic — USGS earthquakes
  - signal.environmental.air_quality — OpenAQ/PurpleAir sensors
  - signal.geospatial.aircraft_position — adsb.lol aircraft tracking
  - signal.content.news — GDELT global events
  - signal.content.rss_entry — RSS feed items
- For "where am I" or "where's my phone" questions, search for latest position_report signals
- For "what's the weather" questions, search for latest weather signals
- For "any earthquakes" questions, search for seismic signals

Keep text responses very concise (1-3 sentences). The user sees rich cards for data results.

HELP RESPONSES:
When the user asks "what can you do?", "help", "what features are available?", or similar, respond with this organized list:

I can help with:

**Search & Query** — Search signals, observables, contacts, assets across your entire database
**Case Management** — Create cases, add observables, link contacts/assets, escalate alerts
**Map & Geospatial** — Navigate the map, search nearby, create geofences, highlight areas, track devices
**Knowledge Graph** — Create/search knowledge nodes, enrich locations, explore entity relationships
**Detection & Alerts** — Create detection rules, list/manage alerts, escalate to cases
**Live Data** — Query live aircraft (ADS-B), check connectors, manage data sources
**Watch Topics** — Monitor OSINT topics, research entities, track articles and clusters
**Investigation** — Deep-dive public records: SEC filings, court cases, government contracts, campaign finance, sanctions, nonprofits — and check a run's status
**Knowledge Graph Q&A** — Ask what we know about an entity, search facts/events/references, browse libraries (answers come with cards)
**Setup & Keys** — I'll explain how to enable a feature (satellite imagery, investigation keys, AI provider): what it needs, where to register, and I'll link you to the right Settings page
**How things work** — Ask how the public/federated library, the map, or any feature works — I answer from the product docs
**System Activity** — See what the background workers have discovered: new entities, collapses, investigations completed
**Exports** — Download signals, cases, contacts as CSV or JSON
**Imports** — Bulk import contacts, assets, or observables from CSV files
**Reports** — Generate intelligence briefs on demand, set up scheduled daily/weekly reports
**Timeline** — View unified activity feed of everything happening across the system
**Developer** — Manage API keys, view signal types, explore API endpoints
**Navigation** — Go to any page: dashboard, map, graph, cases, contacts, assets, signals, alerts, and more

Just ask me what you need!"""


def _rule_actions(msg: str) -> list[dict]:
    """Deterministic intent → action mapping for common copilot commands, so the
    assistant stays reliable even with a weak local model. Used only as a
    fallback when the LLM emits no action block of its own."""
    import re
    m = (msg or "").strip()
    low = m.lower()

    # ── Deep research must be checked BEFORE the investigate rule below, which
    # otherwise swallows the bare word "research". Two intents: check status of
    # a run, and start a new run. ──

    # research status: "how's the research going", "is the report done"
    if re.search(r"\bhow'?s?\s+(?:the\s+|my\s+)?(?:deep\s+)?research\b", low) or \
       ("research" in low and re.search(r"\b(status|going|progress|done|finished|ready)\b", low)):
        params: dict = {}
        mm = re.search(r"research (?:on|about|for)\s+(.+?)(?:\s+(?:run|going|done))?[?.!]*$", m, re.I)
        if mm and mm.group(1) and len(mm.group(1)) < 80:
            params = {"topic": mm.group(1).strip().strip('"')}
        return [{"action": "research_status", "params": params}]

    # start deep research: "do deep research on X", "research X deeply", "report on X"
    mm = re.search(r"\bdeep[\s-]?research\s+(?:on\s+|into\s+|about\s+)?(.+)", m, re.I) \
        or re.search(r"\bdo (?:a |some )?deep (?:research|dive)\s+(?:on|into|about)\s+(.+)", m, re.I) \
        or re.search(r"\bresearch\s+(.+?)\s+deeply\b", m, re.I) \
        or re.search(r"\b(?:write|generate|produce|create)\s+(?:me\s+)?(?:a\s+)?(?:research\s+|deep\s+)?report\s+(?:on|about)\s+(.+)", m, re.I)
    if mm:
        topic = mm.group(1).strip().rstrip("?.!,").strip().strip('"')
        if topic and len(topic) < 120:
            return [{"action": "deep_research", "params": {"topic": topic}}]

    # "investigate <name>" / "pull the filings on <name>" / "look up <name>"
    mm = re.search(r"\b(?:investigate|dig into|look up|pull (?:the )?(?:filings|records|dossier) on)\s+(.+)", m, re.I)
    if mm:
        name = mm.group(1).strip().rstrip("?.!,").strip().strip('"')
        # Drop a trailing descriptor clause ("... the rich guy", "... the CEO")
        # and a leading article — names rarely contain " the " mid-string.
        name = re.split(r"\s+the\s+", name, maxsplit=1)[0].strip()
        name = re.sub(r"^(?:the|a|an)\s+", "", name, flags=re.I).strip()
        if name.islower():
            name = name.title()
        if name and len(name) < 80:
            etype = "organization" if re.search(r"\b(inc|llc|corp|co|company|organization|organisation|bank|group|ltd|plc|foundation|fund|committee|pac|holdings|partners|capital|associates)\b", name, re.I) else "person"
            return [{"action": "investigate_entity", "params": {"name": name, "entity_type": etype}}]

    # investigation status / "is that run done"
    if ("investigation" in low and re.search(r"\b(status|done|finished|complete|progress|how far)\b", low)) \
       or re.search(r"\b(status|is (?:that|the|it|my))\b.*\b(run|investigation)\b", low):
        params: dict = {}
        mm = re.search(r"(?:status of|on)\s+(?:the\s+)?(.+?)(?:\s+(?:run|investigation))?[?.!]*$", m, re.I)
        if mm and mm.group(1) and len(mm.group(1)) < 60:
            params = {"name": mm.group(1).strip().strip('"')}
        return [{"action": "investigation_status", "params": params}]

    # LDU discrepancy review: "what's unresolved/parked/ambiguous", "resolve the queue"
    if re.search(r"\b(unresolved|parked|ambiguous|discrepanc|ldu queue|needs? review|what.*stuck)\b", low):
        return [{"action": "ldu_queue", "params": {}}]
    # confirm a resolution: "assign XY to Xavier Young" / "confirm XY is Xavier Young"
    mm = re.search(r"\b(?:assign|confirm|resolve)\s+(.+?)\s+(?:to|is|=|as)\s+(.+)", m, re.I)
    if mm:
        surf = mm.group(1).strip().strip('"').rstrip("?.!,")
        tgt = mm.group(2).strip().strip('"').rstrip("?.!,")
        if surf and tgt and len(surf) < 60:
            return [{"action": "resolve_discrepancy",
                     "params": {"surface": surf, "decision": "assign", "target_name": tgt}}]
    # propose a fix: "propose a fix for ET" / "how would you resolve ET"
    mm = re.search(r"\b(?:propose|suggest)\s+(?:a\s+)?(?:fix|resolution|match)\s+for\s+(.+)", m, re.I) \
        or re.search(r"\bhow (?:would|do) you resolve\s+(.+)", m, re.I)
    if mm:
        surf = mm.group(1).strip().strip('"').rstrip("?.!,")
        if surf and len(surf) < 60:
            return [{"action": "propose_resolution", "params": {"surface": surf}}]

    # setup / API key help
    if re.search(r"\b(api[- ]?key|set ?up|enable|configure|activate|get (?:a|an|the)?\s*key|how.*\bkey\b)\b", low):
        return [{"action": "setup_help", "params": {"feature": m}}]

    # corpus synthesis: READ the evidence and ANSWER, don't dump navigation cards.
    # "what (do) the files say about X", "what do we know about X", "summarize X",
    # "tell me about X", "who is X", "the story on X", "facts about X".
    mm = re.search(
        r"\b(?:what (?:do|does)?\s*(?:the )?files?\s+say about|"
        r"what do (?:we|you) know about|what'?s? the story (?:on|of|behind)|"
        r"summar(?:ize|ise)|tell me about|give me (?:a )?(?:rundown|summary|brief) (?:on|of|about)|"
        r"who (?:is|are|was|were)|facts? about|info(?:rmation)? on|anything on)\s+(.+)",
        m, re.I)
    if mm:
        q = mm.group(1).strip().rstrip("?.!,").strip().strip('"')
        q = re.sub(r"\s+(?:in|from)\s+(?:the\s+)?(?:files?|documents?|corpus|library)\b.*$", "", q, flags=re.I).strip()
        if q and len(q) < 100:
            return [{"action": "answer_about", "params": {"name": q, "question": m}}]

    # product knowledge: "how does X work", "how do I use X", "what is X", "explain X"
    if re.search(r"\b(how (?:do|does|can|to)|what (?:is|are|'s)|explain|tell me about)\b", low):
        return [{"action": "docs_search", "params": {"query": m}}]

    return []


def _confirm_text(actions: list[dict]) -> str:
    """A clean deterministic reply for a rule-fired action (so the weak model's
    off-topic prose isn't shown next to a correct action)."""
    if not actions:
        return ""
    a = actions[0]
    name = a.get("action")
    p = a.get("params", {}) or {}
    if name == "investigate_entity":
        who = p.get("name", "that entity")
        return (f"On it — starting an investigation on **{who}**. I'll pull public "
                "records (SEC, FEC, courts, sanctions, corporate registries) and the "
                "findings will appear on the dossier as each source reports back.")
    if name == "investigation_status":
        return "Here's where the investigation stands:"
    if name == "deep_research":
        topic = p.get("topic", "that topic")
        return (f"On it — kicking off deep research on **{topic}**. An external "
                "research model will search the open web, and Enki will grab those "
                "sources and collapse them into a new library. This runs in the "
                "background — I'll build a cited report as findings come in.")
    if name == "research_status":
        return "Here's where the research stands:"
    if name == "setup_help":
        return "Here's how to set that up:"
    if name == "entity_lookup":
        return "Here's what we have:"
    if name == "docs_search":
        # Answer directly from the top help passage (no second LLM call needed).
        try:
            from core.ai.product_knowledge import search_product_docs
            hits = search_product_docs(p.get("query", ""), k=1)
            if hits:
                return hits[0]["text"].strip()
        except Exception:
            pass
        return "Here's what the docs say:"
    return ""


def process_chat_message(
    user_message: str,
    provider: BaseLLMProvider,
    conversation_history: Optional[list[LLMMessage]] = None,
    tenant_context: Optional[dict] = None,
) -> dict:
    """
    Process a user's chat message through the LLM.

    Returns:
        {
            "response": "Natural language response text",
            "actions": [{"action": "search_signals", "params": {...}}, ...],
            "error": None or error string
        }
    """
    if not provider.is_available():
        return {
            "response": "AI is not configured yet. Go to Settings → AI Configuration to set up a language model.",
            "actions": [],
            "error": "provider_not_available",
        }

    # Build messages
    messages = [LLMMessage(role="system", content=SYSTEM_PROMPT)]

    # Add tenant context if available
    if tenant_context:
        context_msg = f"Current tenant context: {json.dumps(tenant_context)}"
        messages.append(LLMMessage(role="system", content=context_msg))

    # Add conversation history
    if conversation_history:
        messages.extend(conversation_history)

    # Add current message
    messages.append(LLMMessage(role="user", content=user_message))

    # Get LLM response
    llm_response = provider.complete(messages, max_tokens=1500, temperature=0.1)

    if llm_response.error:
        return {
            "response": f"I encountered an error: {llm_response.error}",
            "actions": [],
            "error": llm_response.error,
        }

    # Parse actions from response
    actions = _extract_actions(llm_response.content)
    # Drop hallucinated/unknown actions (weak models emit e.g. a bare "search"),
    # so they don't block the deterministic rule fallback below.
    try:
        from core.ai.executor import ACTION_HANDLERS
        actions = [a for a in actions if a.get("action") in ACTION_HANDLERS]
    except Exception:
        pass
    clean_text = _strip_action_blocks(llm_response.content)

    # Deterministic fallback: small local models often reply conversationally
    # instead of emitting an action block. If the LLM produced no action but the
    # message clearly maps to a copilot intent, run it anyway.
    if not actions:
        ra = _rule_actions(user_message)
        if ra:
            actions = ra
            # The LLM produced no action here, so its prose is usually
            # off-topic/refusing (weak local models do this). Replace it with a
            # clean, deterministic confirmation that matches what we're doing.
            clean_text = _confirm_text(ra)

    return {
        "response": clean_text,
        "actions": actions,
        "model": llm_response.model,
        "provider": llm_response.provider,
        "usage": llm_response.usage,
        "error": None,
    }


def _extract_actions(text: str) -> list[dict]:
    """Extract JSON action blocks from LLM response.

    Accepts ```action, ```json, or bare JSON with "action" key.
    """
    import re
    actions = []

    # Try ```action blocks first
    for marker in ["```action", "```json"]:
        parts = text.split(marker)
        for part in parts[1:]:
            end = part.find("```")
            json_str = part[:end].strip() if end != -1 else part.strip()
            try:
                action = json.loads(json_str)
                if isinstance(action, dict) and "action" in action:
                    actions.append(action)
            except json.JSONDecodeError:
                continue

    # If no blocks found, try finding bare JSON with "action" key
    if not actions:
        for match in re.finditer(r'\{[^{}]*"action"\s*:\s*"[^"]+"\s*[^{}]*\}', text):
            try:
                action = json.loads(match.group())
                if "action" in action:
                    actions.append(action)
            except json.JSONDecodeError:
                continue

    return actions


def _strip_action_blocks(text: str) -> str:
    """Remove action blocks from response, leaving just the text."""
    import re
    cleaned = re.sub(r'```(?:action|json)\s*\{[^`]*\}\s*```', '', text)
    # Also strip bare JSON action blocks
    cleaned = re.sub(r'\{[^{}]*"action"\s*:\s*"[^"]+"\s*[^{}]*\}', '', cleaned)
    return cleaned.strip()
