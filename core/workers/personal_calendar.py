"""
Personal Calendar Worker — extracts event and attendee entities from calendar batches.

Processes: signal.personal.calendar_bulk

For each calendar event:
  - Creates event enrichment with title, time, location
  - Extracts attendee names as person entities
  - Location text stored for potential future geocoding

Purely structural — no LLM needed (can add later for description parsing).
Runs as a daemon thread following the standard worker pattern.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.personal_calendar")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "signals_processed": 0,
    "events_extracted": 0,
    "attendees_found": 0,
    "errors": 0,
    "batch_num": 0,
}

CHECK_INTERVAL = 10
IDLE_BACKOFF = 45
MAX_BACKOFF = 90


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="personal-calendar-worker", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Personal calendar worker started")
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"
    return {"status": "stopped"}


def _worker_loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = CHECK_INTERVAL

    while not _STOP_EVENT.is_set():
        try:
            processed = _process_batch(tenant_id)
            if processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = "processing"
            else:
                backoff = min(backoff + 15, IDLE_BACKOFF)
                _STATUS["phase"] = "idle — caught up"
        except Exception as e:
            log.warning("Personal calendar worker error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, MAX_BACKOFF)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _process_batch(tenant_id: str) -> int:
    """Find unprocessed calendar signals and extract event/attendee entities."""

    signals = _find_unprocessed_signals(tenant_id, limit=5)
    if not signals:
        return 0

    _STATUS["batch_num"] += 1
    processed = 0

    for sig in signals:
        try:
            _process_calendar_signal(sig, tenant_id)
            _STATUS["signals_processed"] += 1
            processed += 1
        except Exception as e:
            log.warning("Failed to process calendar signal %d: %s", sig["rid"], e)
            _STATUS["errors"] += 1

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    log.info("Calendar batch %d: processed %d signals", _STATUS["batch_num"], processed)
    return processed


def _find_unprocessed_signals(tenant_id: str, limit: int) -> list:
    """Find calendar_bulk signals without enrichments."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.rid, s.signal_id, s.payload, s.timestamp
                    FROM signals s
                    LEFT JOIN signal_enrichments se
                        ON se.signal_rid = s.rid AND se.tier >= 2
                    WHERE s.tenant_id = %s
                      AND s.signal_type = 'signal.personal.calendar_bulk'
                      AND se.enrichment_id IS NULL
                    ORDER BY s.timestamp DESC
                    LIMIT %s
                """, (tenant_id, limit))

                results = []
                for r in cur.fetchall():
                    results.append({
                        "rid": r[0],
                        "signal_id": r[1],
                        "payload": r[2] or {},
                        "timestamp": r[3],
                    })
                return results
    except Exception as e:
        log.debug("Failed to find calendar signals: %s", e)
        return []


def _process_calendar_signal(sig: dict, tenant_id: str):
    """Extract events and attendees from a calendar batch signal."""

    payload = sig["payload"]
    events = payload.get("events", payload.get("items", []))
    if not isinstance(events, list):
        events = []

    entities = []
    relationships = []
    keywords = ["calendar", "schedule"]
    locations = []

    owner_name = payload.get("owner_name", "Owner")

    for event in events:
        title = event.get("title", event.get("summary", ""))
        if not title or len(title.strip()) < 3:
            continue

        # Create event entity
        event_entity = {
            "name": title.strip()[:100],
            "type": "event",
            "subtype": "calendar_event",
            "date": event.get("start_time", event.get("dtstart", "")),
            "end_date": event.get("end_time", event.get("dtend", "")),
        }

        # Extract location
        location = event.get("location", event.get("event_location", ""))
        if location:
            event_entity["location"] = location
            locations.append(location)

        entities.append(event_entity)
        _STATUS["events_extracted"] += 1

        # Extract attendees
        attendees = _extract_attendees(event)
        for attendee in attendees:
            if len(attendee.strip()) < 2:
                continue

            # Add attendee as person entity
            attendee_entity = {
                "name": attendee.strip(),
                "type": "person",
                "role": "attendee",
                "significance": "mentioned",
            }
            entities.append(attendee_entity)
            _STATUS["attendees_found"] += 1

            # Relationship: owner meets_with attendee (at this event)
            relationships.append({
                "source": owner_name,
                "target": attendee.strip(),
                "type": "meets_with",
                "confidence": 0.8,
            })

    # Add unique locations as keywords
    unique_locations = list(set(loc for loc in locations if loc))[:5]
    keywords.extend(unique_locations)

    # Determine importance based on volume and upcoming events
    if len(events) > 20:
        importance = 2
    else:
        importance = 1

    summary = f"Calendar sync: {len(events)} events, {_STATUS['attendees_found']} attendees"
    if unique_locations:
        summary += f". Locations: {', '.join(unique_locations[:3])}"

    # Write enrichment
    enrichment_data = json.dumps({
        "signal_rid": sig["rid"],
        "entities": entities,
        "relationships": relationships,
    }, sort_keys=True)
    enrichment_hash = hashlib.sha256(
        ("genesis" + enrichment_data).encode()
    ).hexdigest()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signal_enrichments
                    (signal_rid, tenant_id, tier, method, model,
                     entities, relationships, event_type, importance,
                     keywords, summary,
                     enrichment_hash, previous_hash, extracted_at)
                VALUES (%s, %s, 2, 'structural', 'rule_based',
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, NULL, NOW())
            """, (
                sig["rid"], tenant_id,
                json.dumps(entities), json.dumps(relationships),
                "calendar_sync", importance,
                keywords, summary[:500],
                enrichment_hash,
            ))
        conn.commit()

    log.info("Signal %d: extracted %d calendar events, %d attendees",
             sig["rid"], len(events), len([e for e in entities if e["type"] == "person"]))


def _extract_attendees(event: dict) -> list[str]:
    """Extract attendee names from a calendar event."""
    attendees = []

    # Direct attendees field
    attendee_list = event.get("attendees", event.get("participants", []))
    if isinstance(attendee_list, list):
        for a in attendee_list:
            if isinstance(a, str):
                # Could be "Name <email>" format
                name = _parse_attendee_string(a)
                if name:
                    attendees.append(name)
            elif isinstance(a, dict):
                name = a.get("name", a.get("displayName", a.get("cn", "")))
                if not name:
                    # Try email prefix as name
                    email = a.get("email", a.get("uri", ""))
                    if email and "@" in email:
                        name = email.split("@")[0].replace(".", " ").title()
                if name:
                    attendees.append(name)

    # Try organizer field
    organizer = event.get("organizer", "")
    if isinstance(organizer, str) and organizer:
        name = _parse_attendee_string(organizer)
        if name:
            attendees.append(name)
    elif isinstance(organizer, dict):
        name = organizer.get("name", organizer.get("displayName", ""))
        if name:
            attendees.append(name)

    return list(set(attendees))[:10]  # deduplicate, cap at 10


def _parse_attendee_string(s: str) -> str:
    """Parse 'Name <email>' or plain name/email string into a name."""
    s = s.strip()
    if not s:
        return ""

    # "John Smith <john@example.com>" format
    if "<" in s and ">" in s:
        name_part = s.split("<")[0].strip()
        if name_part and len(name_part) > 1:
            return name_part
        # Fall back to email prefix
        email = s.split("<")[1].split(">")[0]
        if "@" in email:
            return email.split("@")[0].replace(".", " ").title()

    # Plain email
    if "@" in s:
        return s.split("@")[0].replace(".", " ").title()

    # Plain name
    return s
