"""
Structural classification of documents at ingest.

Different document types have wildly different signal-to-noise ratios when
fed to a generic LLM extractor. A GDS PNR (Amadeus / Sabre booking record)
is 90% structural codes and 10% intelligence; running the generic
mention-extraction pipeline on it produces hundreds of garbage mentions
like 'EWRORY' (a Newark-Paris airport pair extracted as if it were a
single place name) or 'NYCEC08AA' (an Amadeus office code extracted as a
facility).

This module classifies each document by structural signals BEFORE the
extraction pipeline runs. The class drives which extractor to use:

  gds_booking_record → PNR parser (extracts passenger names, routings,
                       fares, ticket numbers; ignores the rest)
  court_filing       → citation/party/docket parser
  news_article       → generic LLM (works well here)
  government_record  → generic LLM + FOIA-redaction-marker awareness
  research_paper     → generic LLM with section awareness
  email_thread       → header + body + reply chain split
  tabular            → row-by-row, not paragraph-by-paragraph
  image_with_text    → OCR'd; needs higher confidence thresholds
  unknown            → fallback: generic LLM, low expectations

Classification is HEURISTIC — no LLM call. Each class has a small set of
regex / token-frequency rules. We aggregate "evidence signals" per class
and return the highest-scoring one. Confidence < 0.5 means "couldn't
decide"; downstream code falls back to generic extraction and the doc
gets re-classified later if better signal becomes available (e.g. after
OCR runs).

The rules are intentionally narrow — we'd rather mis-classify as
'unknown' (and use generic extraction) than mis-classify into a
domain-specific recipe and emit nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ─── Class definitions ──────────────────────────────────────────────────

CLASS_GDS_BOOKING       = "gds_booking_record"
CLASS_COURT_FILING      = "court_filing"
CLASS_NEWS_ARTICLE      = "news_article"
CLASS_GOVERNMENT_RECORD = "government_record"
CLASS_RESEARCH_PAPER    = "research_paper"
CLASS_EMAIL_THREAD      = "email_thread"
CLASS_TABULAR           = "tabular"
CLASS_IMAGE_WITH_TEXT   = "image_with_text"
CLASS_UNKNOWN           = "unknown"

KNOWN_CLASSES = {
    CLASS_GDS_BOOKING, CLASS_COURT_FILING, CLASS_NEWS_ARTICLE,
    CLASS_GOVERNMENT_RECORD, CLASS_RESEARCH_PAPER, CLASS_EMAIL_THREAD,
    CLASS_TABULAR, CLASS_IMAGE_WITH_TEXT, CLASS_UNKNOWN,
}


@dataclass
class ClassificationResult:
    document_class: str
    confidence: float           # [0, 1]
    matched_signals: list[str]  # which rules fired
    rejected_signals: list[str] # rules that almost-fired but didn't win
    # raw scores for transparency — caller can see why the winner won
    per_class_scores: dict[str, float]


# ─── GDS booking record (Amadeus / Sabre / Galileo PNR) ────────────────
#
# Strong signals:
#   - Header like "AMADEUS INTERNET BOOKING CR-NYCEC08AA"
#   - PNR action-code lines: "001 A/7 RM ..." or "003 P/ QE/..."
#   - FA (fare audit) line: "FA 000-0000000000/ETEC/USD1000.00/..."
#   - Airport-pair routing tokens: EWRORY, ORYEWR, CDGJFK (6-letter pairs)
#   - Frequent-flyer codes: FF:BEDFFUS, FF:AAxxxxxxx
#   - Amadeus office codes: NYCEC08AA pattern (3 letters + 2 letters + 2 digits + 2 letters)

_GDS_HEADER_RE = re.compile(
    r"\b(AMADEUS|SABRE|GALILEO|WORLDSPAN)\s+(INTERNET\s+)?BOOKING\b",
    re.IGNORECASE,
)
_GDS_ACTION_RE = re.compile(
    # "001 A/7 RM ..." or "002 Z/  ..."
    r"^\s*\d{3}\s+[A-Z]/(?:[0-9]+|\s)\s+(?:RM|AP|FA|FP|FF|TKXL|TKOK|QE|FH)\b",
    re.MULTILINE,
)
_GDS_FA_LINE_RE = re.compile(
    # FA = fare audit. Ticket number + price + segments + passenger.
    r"\bFA\s+\d{3}-\d{7,}/[A-Z]+/USD\d+\.\d{2}/",
)
_GDS_AIRPORT_PAIR_RE = re.compile(
    # 6 contiguous capitals where each triple is in a known airport set.
    # Doc-level signal: count occurrences of pairs that are *likely* IATA
    # codes. We don't have the full list here; we use the known-pair
    # density as a heuristic via the OFFICE code regex too.
    r"\b[A-Z]{6}(?:[A-Z]{3})*\b"
)
_GDS_OFFICE_RE = re.compile(
    r"\b[A-Z]{3}EC\d{2}[A-Z]{2}\b"  # e.g. NYCEC08AA — Amadeus office
)
_GDS_FOIA_RE = re.compile(r"\(b\)\([0-9]\)")  # FOIA exemption marker


def _score_gds(text: str) -> tuple[float, list[str]]:
    sig: list[str] = []
    score = 0.0

    if _GDS_HEADER_RE.search(text):
        sig.append("gds_header")
        score += 0.45
    action_hits = len(_GDS_ACTION_RE.findall(text))
    if action_hits >= 5:
        sig.append(f"pnr_action_codes:{action_hits}")
        score += min(0.30, 0.05 * (action_hits // 5))
    fa_hits = len(_GDS_FA_LINE_RE.findall(text))
    if fa_hits >= 1:
        sig.append(f"fa_lines:{fa_hits}")
        score += 0.15
    office_hits = len(_GDS_OFFICE_RE.findall(text))
    if office_hits >= 2:
        sig.append(f"amadeus_office_codes:{office_hits}")
        score += 0.10
    if _GDS_FOIA_RE.search(text):
        # Government-redacted PNR — common in FOIA releases.
        sig.append("foia_redaction_present")
        score += 0.05

    return min(score, 1.0), sig


# ─── Court filing ──────────────────────────────────────────────────────
#
# Strong signals:
#   - Case caption: "UNITED STATES OF AMERICA v. SOMEONE"
#   - Docket number: "Case No. 1:23-cr-00456"
#   - "Plaintiff", "Defendant", "Petitioner", "Respondent" usage density
#   - Bluebook citation pattern (limited; only a couple variants here)
#   - "DOCKET" or "FILED" header

_COURT_CAPTION_RE = re.compile(
    r"\b(?:UNITED STATES|STATE|COMMONWEALTH|PEOPLE)\s+OF\s+[A-Z][A-Z ]+\s+(?:v\.|vs\.|VERSUS)\s+",
    re.IGNORECASE,
)
_DOCKET_RE = re.compile(
    r"\b(?:Case|Docket|Civil Action)\s+(?:No\.?|Number)?\s*[:\-]?\s*\d{1,2}:\d{2}-[a-z]{2,4}-\d{3,6}",
    re.IGNORECASE,
)
_PARTY_LABELS_RE = re.compile(
    r"\b(?:Plaintiffs?|Defendants?|Petitioners?|Respondents?|Appellants?|Appellees?)\b"
)
_FILED_HEADER_RE = re.compile(r"\bFILED\b\s+\d{1,2}/\d{1,2}/\d{2,4}")


def _score_court(text: str) -> tuple[float, list[str]]:
    sig: list[str] = []
    score = 0.0
    if _COURT_CAPTION_RE.search(text):
        sig.append("case_caption")
        score += 0.40
    if _DOCKET_RE.search(text):
        sig.append("docket_number")
        score += 0.30
    party_hits = len(_PARTY_LABELS_RE.findall(text))
    if party_hits >= 5:
        sig.append(f"party_labels:{party_hits}")
        score += min(0.20, 0.04 * (party_hits // 5))
    if _FILED_HEADER_RE.search(text):
        sig.append("filed_header")
        score += 0.10
    return min(score, 1.0), sig


# ─── Government record ─────────────────────────────────────────────────
#
# Signals:
#   - FOIA exemption markers '(b)(6)', '(b)(7)(C)', etc.
#   - Agency seal text: "DEPARTMENT OF ..."
#   - Classification banners: "CONFIDENTIAL", "SECRET", "UNCLASSIFIED"
#   - "Bates" stamps on each page
#
# Note: GDS booking records *can* also be FOIA-released government
# records. We let the GDS classifier win on those since the extraction
# recipe needs to be PNR-aware, but classification_evidence preserves
# the fact that government markers were present.

_FOIA_EXEMPTION_RE = re.compile(
    r"\(b\)\((?:[0-9]|[0-9]\)\([A-Z])\)"  # (b)(6), (b)(7)(C)
)
_AGENCY_HEADER_RE = re.compile(
    r"\b(?:DEPARTMENT|BUREAU|OFFICE|AGENCY)\s+OF\s+(?:THE\s+)?[A-Z]{2,}",
)
_CLASSIFICATION_BANNER_RE = re.compile(
    r"\b(?:UNCLASSIFIED|SECRET|CONFIDENTIAL|TOP SECRET|NOFORN)\b//"
)
_BATES_RE = re.compile(r"\b[A-Z]{2,4}[-_]?\d{6,10}\b")


def _score_government(text: str) -> tuple[float, list[str]]:
    sig: list[str] = []
    score = 0.0
    foia_hits = len(_FOIA_EXEMPTION_RE.findall(text))
    if foia_hits >= 1:
        sig.append(f"foia_exemption_markers:{foia_hits}")
        score += min(0.35, 0.10 * foia_hits)
    if _AGENCY_HEADER_RE.search(text):
        sig.append("agency_header")
        score += 0.20
    if _CLASSIFICATION_BANNER_RE.search(text):
        sig.append("classification_banner")
        score += 0.30
    bates_hits = len(_BATES_RE.findall(text))
    if bates_hits >= 3:
        sig.append(f"bates_stamps:{bates_hits}")
        score += 0.15
    return min(score, 1.0), sig


# ─── News article ──────────────────────────────────────────────────────
#
# Signals:
#   - Byline pattern: "By {NAME}\nUpdated {date}" or "By {NAME}, {Outlet}"
#   - Dateline: "WASHINGTON — " at paragraph start
#   - Outlet boilerplate: "All rights reserved", "subscribe to"
#
# News is also often the LOWEST evidence — once we have the structural
# signals we set a moderate confidence; the generic LLM extractor
# handles it well enough that we don't need a domain extractor.

_BYLINE_RE = re.compile(r"\bBy\s+[A-Z][a-z]+\s+[A-Z][a-z]+", re.MULTILINE)
_DATELINE_RE = re.compile(r"^[A-Z][A-Z]+(?:,\s+[A-Z][a-z]+)?\s+[—–-]\s+", re.MULTILINE)
_OUTLET_FOOTER_RE = re.compile(
    r"\b(?:All rights reserved|Subscribe (?:to|for)|copyright\s+©|©)\b",
    re.IGNORECASE,
)


def _score_news(text: str) -> tuple[float, list[str]]:
    sig: list[str] = []
    score = 0.0
    if _BYLINE_RE.search(text):
        sig.append("byline")
        score += 0.30
    if _DATELINE_RE.search(text):
        sig.append("dateline")
        score += 0.25
    if _OUTLET_FOOTER_RE.search(text):
        sig.append("outlet_boilerplate")
        score += 0.15
    # Paragraph-density heuristic: news pieces have many newline-separated
    # paragraphs of 200-800 chars. We use it only as a weak tiebreaker.
    paras = [p for p in text.split("\n\n") if 100 < len(p) < 2000]
    if len(paras) >= 5:
        sig.append(f"paragraph_blocks:{len(paras)}")
        score += 0.10
    return min(score, 1.0), sig


# ─── Research paper ────────────────────────────────────────────────────

_ABSTRACT_RE = re.compile(r"^\s*Abstract\s*[:\n]", re.IGNORECASE | re.MULTILINE)
_REFERENCES_RE = re.compile(r"^\s*References\s*$", re.IGNORECASE | re.MULTILINE)
_SECTION_HEADER_RE = re.compile(
    r"^\s*\d+\.\s+[A-Z][A-Za-z][A-Za-z ]+\s*$",
    re.MULTILINE,
)
_DOI_RE = re.compile(r"\bdoi\.org/10\.\d{4,9}/", re.IGNORECASE)


def _score_research(text: str) -> tuple[float, list[str]]:
    sig: list[str] = []
    score = 0.0
    if _ABSTRACT_RE.search(text):
        sig.append("abstract_section")
        score += 0.30
    if _REFERENCES_RE.search(text):
        sig.append("references_section")
        score += 0.25
    if _DOI_RE.search(text):
        sig.append("doi_link")
        score += 0.20
    sect_hits = len(_SECTION_HEADER_RE.findall(text))
    if sect_hits >= 3:
        sig.append(f"numbered_sections:{sect_hits}")
        score += min(0.20, 0.05 * (sect_hits // 3))
    return min(score, 1.0), sig


# ─── Email thread ──────────────────────────────────────────────────────

_EMAIL_HEADER_RE = re.compile(
    r"^(?:From|To|Cc|Bcc|Subject|Sent|Date):\s*",
    re.MULTILINE,
)
_EMAIL_REPLY_RE = re.compile(
    r"^-+\s*(?:Original Message|Forwarded message)\s*-+",
    re.IGNORECASE | re.MULTILINE,
)


def _score_email(text: str) -> tuple[float, list[str]]:
    sig: list[str] = []
    score = 0.0
    header_hits = len(_EMAIL_HEADER_RE.findall(text))
    if header_hits >= 4:
        sig.append(f"email_headers:{header_hits}")
        score += min(0.45, 0.10 * (header_hits // 4))
    if _EMAIL_REPLY_RE.search(text):
        sig.append("reply_chain_separator")
        score += 0.20
    return min(score, 1.0), sig


# ─── Tabular ───────────────────────────────────────────────────────────

# Tabular detection is mostly mime-driven (CSV/XLS/XLSX); the classifier
# accepts a hint via the caller. For text-only inputs we don't try to
# detect tables from layout — that's a different problem.

# ─── Public API ────────────────────────────────────────────────────────


_CLASS_SCORERS = [
    (CLASS_GDS_BOOKING,       _score_gds,        0.40),
    (CLASS_COURT_FILING,      _score_court,      0.50),
    (CLASS_GOVERNMENT_RECORD, _score_government, 0.35),
    (CLASS_NEWS_ARTICLE,      _score_news,       0.45),
    (CLASS_RESEARCH_PAPER,    _score_research,   0.45),
    (CLASS_EMAIL_THREAD,      _score_email,      0.45),
]
# (class, scorer_fn, min_confidence_to_claim).


def classify(text: str, mime_type: Optional[str] = None) -> ClassificationResult:
    """Classify a document by its text body. Returns ClassificationResult.

    `mime_type` is used as a hint for tabular / image cases where the
    binary format already tells us the class. Pass it when you have it.
    """
    if not text or not isinstance(text, str):
        return ClassificationResult(
            document_class=CLASS_UNKNOWN,
            confidence=0.0,
            matched_signals=[],
            rejected_signals=["empty_text"],
            per_class_scores={},
        )

    # mime-driven short-circuits
    if mime_type:
        if mime_type in (
            "text/csv",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ):
            return ClassificationResult(
                document_class=CLASS_TABULAR,
                confidence=0.95,
                matched_signals=[f"mime:{mime_type}"],
                rejected_signals=[],
                per_class_scores={CLASS_TABULAR: 0.95},
            )
        if mime_type.startswith("image/"):
            return ClassificationResult(
                document_class=CLASS_IMAGE_WITH_TEXT,
                confidence=0.95,
                matched_signals=[f"mime:{mime_type}"],
                rejected_signals=[],
                per_class_scores={CLASS_IMAGE_WITH_TEXT: 0.95},
            )

    # Score every class
    per_class: dict[str, float] = {}
    sigs_by_class: dict[str, list[str]] = {}
    for cls, scorer, _floor in _CLASS_SCORERS:
        score, sigs = scorer(text)
        per_class[cls] = score
        sigs_by_class[cls] = sigs

    # Pick the highest scorer that clears its own floor
    winner: Optional[str] = None
    winner_score = 0.0
    for cls, _scorer, floor in _CLASS_SCORERS:
        if per_class[cls] >= floor and per_class[cls] > winner_score:
            winner = cls
            winner_score = per_class[cls]

    if winner is None:
        return ClassificationResult(
            document_class=CLASS_UNKNOWN,
            confidence=max(per_class.values()) if per_class else 0.0,
            matched_signals=[],
            rejected_signals=[
                f"{cls}:{score:.2f}<{floor}"
                for cls, _, floor in _CLASS_SCORERS
                for score in [per_class[cls]] if score < floor
            ],
            per_class_scores=per_class,
        )

    rejected = [
        f"{cls}:{per_class[cls]:.2f}"
        for cls in per_class
        if cls != winner and per_class[cls] > 0
    ]
    return ClassificationResult(
        document_class=winner,
        confidence=winner_score,
        matched_signals=sigs_by_class[winner],
        rejected_signals=rejected,
        per_class_scores=per_class,
    )
