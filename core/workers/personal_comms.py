"""
Personal Communications Worker — extracts entities and relationships from SMS/call logs.

Processes:
  - signal.personal.sms_bulk — uses LLM (Gemma) to analyze message content
  - signal.personal.call_log_bulk — structural only (frequency/duration analysis)

For SMS: extracts sender/receiver entities, sends message bodies to Gemma for
topic extraction, relationship classification, and importance assessment.

For calls: structural extraction of communication patterns (frequency, duration,
relationship strength inference).

Runs as a daemon thread following the standard worker pattern.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.personal_comms")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "signals_processed": 0,
    "sms_analyzed": 0,
    "calls_analyzed": 0,
    "entities_found": 0,
    "relationships_found": 0,
    "llm_calls": 0,
    "errors": 0,
    "batch_num": 0,
}

CHECK_INTERVAL = 15
IDLE_BACKOFF = 60
MAX_BACKOFF = 120


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="personal-comms-worker", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Personal comms worker started")
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
                backoff = min(backoff + 20, IDLE_BACKOFF)
                _STATUS["phase"] = "idle — caught up"
        except Exception as e:
            log.warning("Personal comms worker error: %s", e)
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
    """Find unprocessed SMS/call signals and extract communications intelligence."""

    signals = _find_unprocessed_signals(tenant_id, limit=3)
    if not signals:
        return 0

    _STATUS["batch_num"] += 1
    processed = 0

    for sig in signals:
        try:
            if sig["signal_type"] == "signal.personal.sms_bulk":
                _process_sms_signal(sig, tenant_id)
                _STATUS["sms_analyzed"] += 1
            elif sig["signal_type"] == "signal.personal.call_log_bulk":
                _process_call_signal(sig, tenant_id)
                _STATUS["calls_analyzed"] += 1

            _STATUS["signals_processed"] += 1
            processed += 1
        except Exception as e:
            log.warning("Failed to process comms signal %d: %s", sig["rid"], e)
            _STATUS["errors"] += 1

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    log.info("Comms batch %d: processed %d signals", _STATUS["batch_num"], processed)
    return processed


def _find_unprocessed_signals(tenant_id: str, limit: int) -> list:
    """Find SMS and call_log signals without enrichments."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.rid, s.signal_id, s.signal_type, s.payload, s.timestamp
                    FROM signals s
                    LEFT JOIN signal_enrichments se
                        ON se.signal_rid = s.rid AND se.tier >= 2
                    WHERE s.tenant_id = %s
                      AND s.signal_type IN (
                          'signal.personal.sms_bulk',
                          'signal.personal.call_log_bulk'
                      )
                      AND se.enrichment_id IS NULL
                    ORDER BY s.timestamp DESC
                    LIMIT %s
                """, (tenant_id, limit))

                results = []
                for r in cur.fetchall():
                    results.append({
                        "rid": r[0],
                        "signal_id": r[1],
                        "signal_type": r[2],
                        "payload": r[3] or {},
                        "timestamp": r[4],
                    })
                return results
    except Exception as e:
        log.debug("Failed to find comms signals: %s", e)
        return []


def _process_sms_signal(sig: dict, tenant_id: str):
    """Process SMS batch: extract participants and analyze content with LLM."""

    payload = sig["payload"]
    messages = payload.get("messages", payload.get("items", []))
    if not isinstance(messages, list):
        messages = []

    # Extract participant entities (structural)
    entities = []
    relationships = []
    seen_contacts = set()

    owner_name = payload.get("owner_name", "Owner")

    for msg in messages:
        contact = msg.get("contact_name", msg.get("address", msg.get("number", "")))
        if not contact or contact in seen_contacts:
            continue
        seen_contacts.add(contact)

        entity = {
            "name": contact,
            "type": "person",
            "role": "communicant",
            "significance": "mentioned",
        }

        # Add phone number as identifier
        number = msg.get("number", msg.get("address", ""))
        if number:
            entity["phone_number"] = number

        entities.append(entity)

    # Build message bodies for LLM analysis (cap at 20 messages, 200 chars each)
    message_texts = []
    for msg in messages[:20]:
        body = msg.get("body", msg.get("text", msg.get("message", "")))
        if body and len(body.strip()) > 5:
            direction = "sent" if msg.get("type") == "sent" or msg.get("is_outgoing") else "received"
            contact = msg.get("contact_name", msg.get("address", "Unknown"))
            message_texts.append({
                "from": owner_name if direction == "sent" else contact,
                "to": contact if direction == "sent" else owner_name,
                "body": body[:200],
            })

    # LLM analysis of message content
    llm_result = None
    if message_texts:
        llm_result = _analyze_messages_with_llm(message_texts, list(seen_contacts))

    # Merge LLM results
    importance = 1
    keywords = ["sms", "communication"]
    summary_parts = [f"SMS batch: {len(messages)} messages with {len(seen_contacts)} contacts"]

    if llm_result:
        # Add LLM-extracted topics
        topics = llm_result.get("topics", [])
        if topics:
            keywords.extend(topics[:5])

        # Add relationship classifications
        for rel in llm_result.get("relationships", []):
            source = rel.get("source", "")
            target = rel.get("target", "")
            rel_type = rel.get("type", "communicates_with")
            confidence = rel.get("confidence", 0.7)
            if source and target:
                relationships.append({
                    "source": source,
                    "target": target,
                    "type": rel_type,
                    "confidence": confidence,
                })

        # Use LLM importance if provided (convert text to int)
        imp_map = {"low": 1, "medium": 2, "high": 3, "critical": 5}
        if llm_result.get("importance"):
            importance = imp_map.get(llm_result["importance"], 2)

        # Add facts/claims from LLM
        if llm_result.get("key_facts"):
            summary_parts.append("Key facts: " + "; ".join(llm_result["key_facts"][:3]))

        if llm_result.get("summary"):
            summary_parts.append(llm_result["summary"])

        _STATUS["relationships_found"] += len(relationships)

    _STATUS["entities_found"] += len(entities)

    # Write enrichment
    _write_enrichment(
        sig, tenant_id, entities, relationships,
        event_type="communication",
        importance=importance,
        keywords=keywords,
        summary=" | ".join(summary_parts),
        method="llm" if llm_result else "structural",
        model="gemma4:e4b" if llm_result else "rule_based",
    )

    log.info("Signal %d: %d SMS messages, %d contacts, %d relationships",
             sig["rid"], len(messages), len(entities), len(relationships))


def _process_call_signal(sig: dict, tenant_id: str):
    """Process call log batch: structural extraction of communication patterns."""

    payload = sig["payload"]
    calls = payload.get("calls", payload.get("items", []))
    if not isinstance(calls, list):
        calls = []

    entities = []
    relationships = []
    contact_stats: dict[str, dict] = {}  # contact → {count, total_duration}

    owner_name = payload.get("owner_name", "Owner")

    for call in calls:
        contact = call.get("contact_name", call.get("number", call.get("address", "")))
        if not contact:
            continue

        # Aggregate call stats per contact
        if contact not in contact_stats:
            contact_stats[contact] = {"count": 0, "total_duration": 0, "number": ""}

        contact_stats[contact]["count"] += 1
        duration = call.get("duration", call.get("duration_seconds", 0))
        if isinstance(duration, (int, float)):
            contact_stats[contact]["total_duration"] += duration

        number = call.get("number", call.get("address", ""))
        if number:
            contact_stats[contact]["number"] = number

    # Create entities with communication frequency metadata
    for contact, stats in contact_stats.items():
        if len(contact.strip()) < 2:
            continue

        # Infer relationship strength from frequency
        call_count = stats["count"]
        if call_count >= 10:
            significance = "central"
        elif call_count >= 5:
            significance = "supporting"
        else:
            significance = "peripheral"

        entity = {
            "name": contact,
            "type": "person",
            "role": "communicant",
            "significance": significance,
            "call_count": call_count,
            "total_duration_seconds": stats["total_duration"],
        }
        if stats["number"]:
            entity["phone_number"] = stats["number"]

        entities.append(entity)

        # Create relationship: owner communicates_with contact
        # Confidence based on frequency
        confidence = min(0.5 + (call_count * 0.05), 0.95)
        relationships.append({
            "source": owner_name,
            "target": contact,
            "type": "communicates_with",
            "confidence": round(confidence, 2),
        })

    _STATUS["entities_found"] += len(entities)
    _STATUS["relationships_found"] += len(relationships)

    # Determine importance based on volume
    total_calls = len(calls)
    if total_calls > 50:
        importance = 2
    else:
        importance = 1

    keywords = ["calls", "communication", "phone"]
    summary = (
        f"Call log: {total_calls} calls with {len(contact_stats)} contacts. "
        f"Most frequent: {_top_contacts(contact_stats, 3)}"
    )

    _write_enrichment(
        sig, tenant_id, entities, relationships,
        event_type="communication",
        importance=importance,
        keywords=keywords,
        summary=summary,
        method="structural",
        model="rule_based",
    )

    log.info("Signal %d: %d calls, %d contacts, %d relationships",
             sig["rid"], total_calls, len(entities), len(relationships))


def _analyze_messages_with_llm(messages: list[dict], contacts: list[str]) -> Optional[dict]:
    """Send SMS message bodies to Gemma for intelligence extraction."""

    provider = _get_provider()
    if not provider:
        return None

    # Acquire LLM slot
    try:
        from core.workers.reconciliation import acquire_llm_slot, release_llm_slot
        if not acquire_llm_slot(timeout=30):
            return None
    except ImportError:
        pass

    # Build conversation text
    conversation = "\n".join(
        f"[{m['from']} → {m['to']}]: {m['body']}"
        for m in messages
    )

    prompt = f"""Analyze these SMS messages and extract intelligence.

MESSAGES:
{conversation}

KNOWN CONTACTS: {', '.join(contacts[:20])}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "topics": ["topic1", "topic2"],
  "key_facts": ["fact1", "fact2"],
  "relationships": [
    {{"source": "Person A", "target": "Person B", "type": "friend|business|family|romantic|acquaintance", "confidence": 0.8}}
  ],
  "importance": "low|medium|high|critical",
  "summary": "One sentence summary of communication patterns"
}}

Rules:
- topics: 2-5 main discussion topics
- key_facts: specific actionable information mentioned (dates, amounts, plans)
- relationships: classify the relationship type between communicants
- importance: low=routine, medium=noteworthy plans/info, high=urgent/sensitive, critical=emergency
- Use actual contact names from the messages"""

    try:
        from core.ai.provider import LLMMessage
        response = provider.complete(
            [LLMMessage(role="user", content=prompt)],
            max_tokens=1500, temperature=0.0,
        )

        try:
            from core.workers.reconciliation import release_llm_slot
            release_llm_slot()
        except Exception:
            pass

        _STATUS["llm_calls"] += 1

        if not response.content or response.error:
            return None

        # Parse JSON response
        return _parse_llm_json(response.content)

    except Exception as e:
        log.warning("LLM analysis failed for SMS: %s", e)
        try:
            from core.workers.reconciliation import release_llm_slot
            release_llm_slot()
        except Exception:
            pass
        return None


def _parse_llm_json(content: str) -> Optional[dict]:
    """Parse LLM JSON response, handling markdown fences and thinking blocks."""
    if not content:
        return None

    cleaned = re.sub(r'```(?:json)?\s*', '', content)
    cleaned = re.sub(r'<thinking>[\s\S]*?</thinking>', '', cleaned)
    cleaned = cleaned.strip()

    try:
        match = re.search(r'\{[\s\S]*\}', cleaned)
        if match:
            return json.loads(match.group())
    except json.JSONDecodeError:
        pass

    log.debug("Failed to parse LLM response for SMS analysis")
    return None


def _get_provider():
    """Get the LLM provider (same pattern as feed_enrichment)."""
    try:
        from core.ai.resolver import get_effective_provider
        return get_effective_provider()
    except Exception:
        return None


def _write_enrichment(sig: dict, tenant_id: str, entities: list, relationships: list,
                      event_type: str, importance: str, keywords: list,
                      summary: str, method: str, model: str):
    """Write enrichment record to signal_enrichments table."""

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
                VALUES (%s, %s, 2, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, NULL, NOW())
            """, (
                sig["rid"], tenant_id, method, model,
                json.dumps(entities), json.dumps(relationships),
                event_type, importance,
                keywords, summary[:500],
                enrichment_hash,
            ))
        conn.commit()


def _top_contacts(stats: dict, n: int) -> str:
    """Return top N contacts by call count as a string."""
    sorted_contacts = sorted(stats.items(), key=lambda x: x[1]["count"], reverse=True)
    parts = [f"{name} ({s['count']} calls)" for name, s in sorted_contacts[:n]]
    return ", ".join(parts) if parts else "none"
