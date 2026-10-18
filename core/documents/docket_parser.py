"""Parse federal court DOCKET lines into structured events.

Docket entries ("NOTICE ... Document filed by Valerie Vance. (Entered:
01/02/2020)", "ORDER ... (Signed by Judge A. Example on 1/2/2020)") are
rigid METADATA, not prose. The LLM statement extractor mangles them into
"X entered ?" — 38% of court-corpus statements come out degenerate this way.

This parser reads the structure directly: filing type, date, who filed / which
judge signed, and what it responds to — turning docket noise into clean, dated
events. Deterministic, no LLM.
"""
from __future__ import annotations

import re

_ENTERED = re.compile(r"\(Entered:\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I)
_ANYDATE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
_SIGNED = re.compile(r"Signed by (Judge [A-Z][^)0-9]{2,40}?) on (\d{1,2}/\d{1,2}/\d{4})")
_FILED_BY = re.compile(r"[Dd]ocument filed by ([A-Z][^.(]{2,60}?)\s*[.(]")
_RE_TARGET = re.compile(r"\bre:?\s+([A-Za-z][^.(#]{4,60})")
_TYPE = re.compile(
    r"\b(NOTICE|ORDER|OPINION|MEMORANDUM(?: OF LAW)?|MOTION|LETTER|DECLARATION|"
    r"REPLY(?: MEMORANDUM(?: OF LAW)?)?|RESPONSE|SEALED DOCUMENT|OImnion|JUDGMENT|"
    r"MANDATE|STIPULATION|TRANSCRIPT|SUMMONS|COMPLAINT|AFFIDAVIT|BRIEF|SUBPOENA|"
    r"ENDORSED LETTER|SCHEDULING ORDER|MINUTE ENTRY|OPINION & ORDER)\b", re.I)


def _iso(d: str) -> str | None:
    try:
        mm, dd, yy = d.split("/")
        yy = int(yy)
        if yy < 100:
            yy += 2000 if yy < 50 else 1900
        return f"{yy:04d}-{int(mm):02d}-{int(dd):02d}"
    except Exception:
        return None


def parse_docket(text: str) -> dict | None:
    """Return {date, filing_type, filer, judge, summary} for a docket line, or
    None if the text isn't a docket entry."""
    if not text:
        return None
    m_ent = _ENTERED.search(text)
    m_sign = _SIGNED.search(text)
    m_type = _TYPE.search(text)
    m_filer = _FILED_BY.search(text)
    # A docket line is one that carries a docket marker: an Entered stamp, a
    # judge signature, or a filing-type + "document filed by".
    if not (m_ent or m_sign or (m_type and m_filer)):
        return None
    if m_ent:
        date = _iso(m_ent.group(1))
    elif m_sign:
        date = _iso(m_sign.group(2))
    else:
        d = _ANYDATE.search(text)
        date = _iso(d.group(1)) if d else None
    ftype = m_type.group(0).strip().title() if m_type else None
    filer = m_filer.group(1).strip() if m_filer else None
    judge = m_sign.group(1).strip() if m_sign else None
    m_re = _RE_TARGET.search(text)
    target = m_re.group(1).strip() if m_re else None

    bits = [ftype or "Docket entry"]
    if target:
        bits.append(f"re: {target}")
    by = filer or judge
    if by:
        bits.append(f"— {by}")
    return {"date": date, "filing_type": ftype, "filer": filer,
            "judge": judge, "target": target, "summary": " ".join(bits)}
