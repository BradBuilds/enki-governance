"""
Personal Contacts Worker — extracts person entities from contact batch signals.

Processes:
  - signal.personal.contacts_bulk (phone contacts)
  - signal.personal.contacts_email_bulk (email contacts)

For each contact, creates entity enrichments with identifiers (phone, email)
that feed into the entity resolution pipeline for dedup and graph linking.

Purely structural — no LLM needed.
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

log = logging.getLogger("enki.workers.personal_contacts")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "signals_processed": 0,
    "contacts_extracted": 0,
    "entities_created": 0,
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
        name="personal-contacts-worker", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Personal contacts worker started")
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
            log.warning("Personal contacts worker error: %s", e)
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
    """Find unprocessed contacts signals and extract person entities."""

    signals = _find_unprocessed_signals(tenant_id, limit=5)
    if not signals:
        return 0

    _STATUS["batch_num"] += 1
    processed = 0

    for sig in signals:
        try:
            count = _process_contacts_signal(sig, tenant_id)
            _STATUS["signals_processed"] += 1
            _STATUS["contacts_extracted"] += count
            processed += 1
        except Exception as e:
            log.warning("Failed to process contacts signal %d: %s", sig["rid"], e)
            _STATUS["errors"] += 1

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    log.info("Contacts batch %d: processed %d signals", _STATUS["batch_num"], processed)
    return processed


def _find_unprocessed_signals(tenant_id: str, limit: int) -> list:
    """Find contacts_bulk and contacts_email_bulk signals without enrichments."""
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
                          'signal.personal.contacts_bulk',
                          'signal.personal.contacts_email_bulk'
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
        log.debug("Failed to find contacts signals: %s", e)
        return []


def _process_contacts_signal(sig: dict, tenant_id: str) -> int:
    """Extract person entities from a contacts batch signal."""

    payload = sig["payload"]
    signal_type = sig["signal_type"]

    # Extract contacts list from payload
    contacts = payload.get("contacts", payload.get("items", []))
    if not isinstance(contacts, list):
        contacts = []

    entities = []
    relationships = []

    for contact in contacts:
        name = _extract_name(contact)
        if not name or len(name.strip()) < 2:
            continue

        # Build entity with identifiers for resolution
        entity = {
            "name": name.strip(),
            "type": "person",
            "role": "contact",
            "significance": "mentioned",
        }

        # Extract phone numbers
        phones = _extract_phones(contact)
        if phones:
            entity["phone_numbers"] = phones

        # Extract email addresses
        emails = _extract_emails(contact)
        if emails:
            entity["email_addresses"] = emails

        # Extract organization if available
        org = contact.get("organization", contact.get("company", ""))
        if org:
            entity["organization"] = org

        entities.append(entity)
        _STATUS["entities_created"] += 1

    # Write enrichment record
    with get_connection() as conn:
        with conn.cursor() as cur:
            enrichment_data = json.dumps({
                "signal_rid": sig["rid"],
                "entities": entities,
                "contact_count": len(contacts),
            }, sort_keys=True)
            enrichment_hash = hashlib.sha256(
                ("genesis" + enrichment_data).encode()
            ).hexdigest()

            # Keywords: extract unique organization names
            org_names = list(set(
                e.get("organization", "") for e in entities if e.get("organization")
            ))[:10]
            keywords = ["contacts", signal_type.split(".")[-1]] + org_names

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
                json.dumps(entities),
                json.dumps(relationships),
                "contact_sync",
                1,
                keywords,
                f"Extracted {len(entities)} contacts from {len(contacts)} entries",
                enrichment_hash,
            ))

        conn.commit()

    log.info("Signal %d: extracted %d contact entities from %d entries",
             sig["rid"], len(entities), len(contacts))
    return len(entities)


def _extract_name(contact: dict) -> str:
    """Extract display name from contact record."""
    # Try display_name first
    name = contact.get("display_name", "")
    if name:
        return name

    # Try first + last
    first = contact.get("first_name", contact.get("given_name", ""))
    last = contact.get("last_name", contact.get("family_name", ""))
    if first or last:
        return f"{first} {last}".strip()

    # Try name field
    return contact.get("name", "")


def _extract_phones(contact: dict) -> list:
    """Extract phone numbers from contact record."""
    phones = []

    # Direct phone field
    phone = contact.get("phone", contact.get("phone_number", ""))
    if phone:
        phones.append(phone)

    # Phone numbers array
    phone_list = contact.get("phone_numbers", contact.get("phones", []))
    if isinstance(phone_list, list):
        for p in phone_list:
            if isinstance(p, str):
                phones.append(p)
            elif isinstance(p, dict):
                num = p.get("number", p.get("value", ""))
                if num:
                    phones.append(num)

    return phones[:5]  # cap at 5


def _extract_emails(contact: dict) -> list:
    """Extract email addresses from contact record."""
    emails = []

    # Direct email field
    email = contact.get("email", contact.get("email_address", ""))
    if email:
        emails.append(email)

    # Email addresses array
    email_list = contact.get("email_addresses", contact.get("emails", []))
    if isinstance(email_list, list):
        for e in email_list:
            if isinstance(e, str):
                emails.append(e)
            elif isinstance(e, dict):
                addr = e.get("address", e.get("value", ""))
                if addr:
                    emails.append(addr)

    return emails[:5]  # cap at 5
