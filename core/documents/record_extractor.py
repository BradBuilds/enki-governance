"""
Record extractor — parse structured single-line records into events.

Source records look like:
  January 4, 2006 F-2006-00385 LEAVING S. VIETNAM FOR N. CAMBODIA
  January 5, 2006 F-2006-00395 IGNAZIO SILONE
  June 20, 2011 Guwahati, India UFO appears in Manipur!
  June 26, 1956 KC-97 accident were autopsied at the Ballard

Each is a date + identifier + subject. Trying to extract S-P-O facts
from these produces garbage (the prose LLM choked on them at 96%
failure rate). The right thing to do is parse them as record fields
and emit an `event` row plus entity-mention links — no LLM needed,
fully regex-driven.

Output written:
  events                 (event_time, event_type='record_entry', title, ...)
  document_mentions      (one per identified name in the subject)
  statements             (one optional "X appeared_in <case_id>" if a
                          named entity AND a case_id both surface)

Public surface:
  extract_records(document_id) -> dict
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.record_extractor")

# Patterns ---------------------------------------------------------------

# Standalone month-name + day + year (allow OCR slop in year)
_DATE_PATTERN = re.compile(
    r"^(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+"
    r"(?P<day>\d{1,2}),?\s+"
    r"(?P<year>\d{4})\b",
    re.IGNORECASE,
)

# Case identifiers — FOIA / declassification / FBI-vault style:
#   F-2006-00385  F_2006_00385  F_2oo6_oo385 (OCR)  DOC-2011-001
_CASE_ID_PATTERN = re.compile(
    r"\b([A-Z]{1,4}[-_][\dOo]{2,5}[-_][\dOo]{2,6})\b",
)

_MONTH_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_date(text: str) -> Optional[datetime]:
    m = _DATE_PATTERN.match(text)
    if not m:
        return None
    try:
        mo = _MONTH_NUM[m.group("month").lower()]
        d = int(m.group("day"))
        y = int(m.group("year"))
        if y < 1900 or y > 2100:
            return None
        return datetime(y, mo, d, tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None


def _extract_case_ids(text: str) -> list[str]:
    """OCR-tolerant case ID matcher. Normalizes oOoO→0000 style swaps."""
    raw = _CASE_ID_PATTERN.findall(text)
    out = []
    for r in raw:
        # Normalize OCR'd "O" / "o" to "0" inside the numeric segments
        parts = re.split(r"[-_]", r)
        if len(parts) >= 2:
            try:
                normalized = parts[0]
                for p in parts[1:]:
                    normalized += "-" + p.translate(str.maketrans("Oo", "00"))
                out.append(normalized)
            except Exception:
                out.append(r)
        else:
            out.append(r)
    return list(dict.fromkeys(out))  # dedupe preserve order


# Connector / range words that surface as standalone "titles" when a
# paragraph holds a compound-date phrase like "APRIL 1, 1950 THROUGH JUNE 30,
# 1950". The chunk between the two dates is just the connector — we don't
# want it to become its own record.
_CONNECTOR_TITLES = {
    "and", "or", "to", "through", "thru", "thru.", "until",
    "till", "via", "—", "-", "–", "&",
}


def _extract_subject(text: str, after_date_pos: int) -> str:
    """Get the subject — text after the date prefix, with case-IDs stripped."""
    s = text[after_date_pos:].strip()
    # Strip leading case IDs
    s = _CASE_ID_PATTERN.sub("", s).strip()
    # Drop leading connector words/punctuation that bleed in from compound
    # date phrases ("APRIL 1, 1950 THROUGH JUNE 30, 1950 ..." → first chunk
    # is "THROUGH ..." which yields subject="THROUGH").
    while True:
        head = s.split(maxsplit=1)
        if not head or head[0].lower().strip(".,;:—-") not in _CONNECTOR_TITLES:
            break
        s = head[1] if len(head) > 1 else ""
    # Collapse leftover whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_real_subject(subject: str) -> bool:
    """A subject is real when it's more than a stray connector after the
    date strip. Filters 'AND', 'THROUGH', 'OR', '—', etc. and runs ≥ 4 chars."""
    if not subject:
        return False
    s = subject.strip().strip(".,;:—-").lower()
    if len(s) < 4:
        return False
    if s in _CONNECTOR_TITLES:
        return False
    return True


_DATE_ANYWHERE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+"
    r"(?P<day>\d{1,2}),?\s+"
    r"(?P<year>\d{4})\b",
    re.IGNORECASE,
)


def _extract_records_from_paragraph(text: str) -> list[dict]:
    """Multiple records may live on one line — e.g.
    'January 5, 2006 F-2006-00395 IGNAZIO SILONE January 5, 2006 F-2006-00000 JOHN EXAMPLE'
    Returns list of records, each {date, case_ids, subject, source_text}."""
    records: list[dict] = []
    # Use unanchored pattern so we find every date in the line, including
    # the second/third date when multiple records share a paragraph.
    date_positions = [m.start() for m in re.finditer(_DATE_ANYWHERE, text)]
    if not date_positions:
        return records
    # Slice text between dates
    for i, start in enumerate(date_positions):
        end = date_positions[i + 1] if i + 1 < len(date_positions) else len(text)
        chunk = text[start:end]
        d = _parse_date(chunk)
        if not d:
            continue
        # Skip past the date string to extract subject
        m = _DATE_PATTERN.match(chunk)
        after = m.end() if m else 0
        case_ids = _extract_case_ids(chunk[after:])
        subject = _extract_subject(chunk, after)
        # Require either a real subject or a case_id; otherwise this is
        # the "connector chunk" between two dates in a compound-date phrase.
        if not _is_real_subject(subject) and not case_ids:
            continue
        records.append({
            "date": d,
            "case_ids": case_ids,
            "subject": subject,
            "source_text": chunk.strip(),
        })
    return records


# Flight manifest parsing -------------------------------------------------
#
# Passenger-flight logs (e.g. the Acme aircraft logs) are structured rows,
# not prose. We parse them deterministically into `flight` events + person
# mentions — no LLM. The 5,001-row flight log produced ZERO noise this way,
# vs. ~96% garbage when the same rows were fed to the prose extractor.

# A row: [seq#] M/D/Y [tail#] ... AAA BBB ... <passengers>
_FLIGHT_ROW = re.compile(
    r"^\s*(?:\d{1,4}\s+)?"                         # optional leading sequence number
    r"(?P<mo>\d{1,2})[/-](?P<da>\d{1,2})[/-](?P<yr>\d{2,4})\b"   # date
    r"(?P<rest>.*)$"
)
# Departure/arrival airport-code pair anywhere in the remainder.
# Accepts 3-letter IATA (PBI, TEB) and 4-letter ICAO (LFPB, EGLL) codes.
_ROUTE = re.compile(r"\b([A-Z]{3,4})[ \-/]+([A-Z]{3,4})\b")
# Common airport codes / non-name all-caps tokens to keep out of mentions.
_NOT_A_NAME = frozenset({
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
    "NOV", "DEC", "USA", "INC", "LLC", "INT", "DEP", "ARR", "ETA", "PIC",
})


def _parse_flight_date(mo: str, da: str, yr: str) -> Optional[datetime]:
    try:
        m, d, y = int(mo), int(da), int(yr)
        if y < 100:                       # 2-digit year: 95→1995, 06→2006
            y = 2000 + y if y < 50 else 1900 + y
        if not (1 <= m <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100):
            return None
        return datetime(y, m, d, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# Denormalized flight-log EXPORTS put ONE passenger per row, with the name in a
# tail segment after a record-id token like "35020-G-1159B-N123AA-CMH-PBI-779-Pass",
# followed by "<n> First Last [comma/initial alias forms] Yes Flight Log". The city
# names sit BETWEEN the route codes and that tail (which is why a naive scan from the
# route grabs "Columbus"/"West Palm Beach"). Anchor on the record-id to skip them.
_FLIGHT_RECID = re.compile(r"\d+-[A-Z0-9./-]+-Pass\b", re.IGNORECASE)
_FLIGHT_TRAILER = re.compile(r"\b(?:Yes|No)\s+Flight\s+Log.*$", re.IGNORECASE)
_FULL_NAME = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)\b")


def _flight_passengers(rest: str) -> list[str]:
    """Passenger name(s) from the segment after the route. Handles the
    denormalized export (name after a '…-Pass' record id, one per row) and
    falls back to a capitalized-token scan for simple "DEP ARR Surname…" rows.
    No hardcoded cast — the resolver collapses these to canonical people."""
    m = _FLIGHT_RECID.search(rest)
    if m:
        seg = _FLIGHT_TRAILER.sub("", rest[m.end():])
        nm = _FULL_NAME.search(seg)
        return [nm.group(1)] if nm else []
    # Fallback for simple manifest rows (no record-id, no embedded cities).
    out: list[str] = []
    for tok in re.findall(r"[A-Z][a-z]{3,}", rest):
        if tok.upper() in _NOT_A_NAME:
            continue
        out.append(tok)
    return list(dict.fromkeys(out))


def _parse_flight_row(text: str) -> Optional[dict]:
    """Parse one flight-manifest line → {date, dep, arr, passengers, source_text}
    or None if it isn't a flight row."""
    m = _FLIGHT_ROW.match(text or "")
    if not m:
        return None
    d = _parse_flight_date(m.group("mo"), m.group("da"), m.group("yr"))
    if not d:
        return None
    rest = m.group("rest") or ""
    rt = _ROUTE.search(rest)
    if not rt:
        return None  # no airport pair → not confidently a flight row
    dep, arr = rt.group(1), rt.group(2)
    # passenger segment = text after the route
    pax_seg = rest[rt.end():]
    return {
        "date": d, "dep": dep, "arr": arr,
        "passengers": _flight_passengers(pax_seg),
        "source_text": text.strip(),
    }


def _persist_flight(
    conn, *, tenant_id: str, document_id: int, paragraph_id: int,
    page_id: Optional[int], flight: dict
) -> Optional[int]:
    """Insert a flight as an event + one person-mention per passenger.
    Deterministic node_id (date+route) so the same flight from multiple
    docs collapses to ONE event."""
    import hashlib
    from core.documents.mentions import persist_mention
    route = f"{flight['dep']}→{flight['arr']}"
    title = f"Flight {route}"
    fingerprint = hashlib.sha1(
        f"{flight['date'].isoformat()}|{flight['dep']}|{flight['arr']}".encode()
    ).hexdigest()[:16]
    node_id = f"event:flt_{fingerprint}"
    ev_id = None
    with conn.cursor() as cur:
        try:
            # place_text is an ARRAY column — the route lives in the title;
            # we pass the dep/arr codes as the place_text array.
            cur.execute(
                """
                INSERT INTO events
                    (tenant_id, node_id, event_type, title, place_text,
                     event_time, temporal_precision, confidence)
                VALUES (%s, %s, 'flight', %s, %s, %s, 'day', 0.9)
                ON CONFLICT DO NOTHING
                RETURNING event_id
                """,
                (tenant_id, node_id, title, [flight["dep"], flight["arr"]],
                 flight["date"]),
            )
            r = cur.fetchone()
            ev_id = r[0] if r else None
        except Exception as e:
            log.debug("flight event insert failed: %s", e)
            return None
    # Emit passenger mentions (resolver collapses surnames → entities; the
    # noise filter already rejects airport-pair routings on its own).
    for name in flight["passengers"]:
        try:
            persist_mention(
                tenant_id=tenant_id, document_id=document_id,
                page_id=page_id, paragraph_id=paragraph_id,
                char_start=None, char_end=None, surface_form=name,
                suggested_type="person", extraction_method="flight_manifest",
            )
        except Exception as e:
            log.debug("flight passenger mention failed (%s): %s", name, e)
    return ev_id


def _persist_record(
    conn, *, tenant_id: str, document_id: int, paragraph_id: int,
    page_id: Optional[int], record: dict
) -> Optional[int]:
    """Insert one record into events. Returns event_id (or None on conflict)."""
    import hashlib
    title = record["subject"][:300] if record["subject"] else (
        record["case_ids"][0] if record["case_ids"] else "(no subject)"
    )
    # Unique node_id per (date, normalized_title). Same crash mentioned in
    # five paragraphs is ONE event, not five. Same crash referenced from
    # multiple docs is also ONE event — the ON CONFLICT clause will hit
    # `events_tenant_node_unique` and skip the insert. Previously this hash
    # included document_id+paragraph_id, which is why a 1985-12-12 "CRASH
    # OF ARROW AIRLINES FLIGHT 1285R" showed up 5× in the Roswell library.
    norm_title = re.sub(r"\s+", " ", title.lower()).strip()
    fingerprint = hashlib.sha1(
        f"{record['date'].isoformat()}|{norm_title}".encode("utf-8")
    ).hexdigest()[:16]
    node_id = f"event:rec_{fingerprint}"
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO events
                    (tenant_id, node_id, event_type, title, event_time,
                     temporal_precision, confidence)
                VALUES (%s, %s, 'record_entry', %s, %s, 'day', 0.85)
                ON CONFLICT DO NOTHING
                RETURNING event_id
                """,
                (tenant_id, node_id, title, record["date"]),
            )
            r = cur.fetchone()
            return r[0] if r else None
        except Exception as e:
            log.debug("event insert failed: %s", e)
            return None


def extract_records(document_id: int, tenant_id: str = "default") -> dict:
    """Walk all record-typed paragraphs in a doc; emit one event per
    detected record."""
    stats = {"document_id": document_id, "paragraphs_scanned": 0,
             "records_extracted": 0, "events_created": 0,
             "flights_extracted": 0, "flight_events_created": 0,
             "skipped_no_date": 0}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT paragraph_id, page_id, raw_text
                  FROM document_paragraphs
                 WHERE document_id = %s
                   AND paragraph_type = 'record'
                """,
                (document_id,),
            )
            rows = cur.fetchall()
        for pid, page_id, text in rows:
            stats["paragraphs_scanned"] += 1
            # Flight manifests first — structured passenger rows → flight
            # events + person mentions, fully deterministic.
            flight = _parse_flight_row(text or "")
            if flight:
                stats["flights_extracted"] += 1
                ev_id = _persist_flight(
                    conn, tenant_id=tenant_id, document_id=document_id,
                    paragraph_id=pid, page_id=page_id, flight=flight,
                )
                if ev_id:
                    stats["flight_events_created"] += 1
                continue
            records = _extract_records_from_paragraph(text or "")
            if not records:
                stats["skipped_no_date"] += 1
                continue
            stats["records_extracted"] += len(records)
            for r in records:
                ev_id = _persist_record(
                    conn, tenant_id=tenant_id,
                    document_id=document_id, paragraph_id=pid,
                    page_id=page_id, record=r,
                )
                if ev_id:
                    stats["events_created"] += 1
        conn.commit()
    return stats
