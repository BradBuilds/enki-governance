"""
Amadeus / Sabre / Galileo PNR (Passenger Name Record) parser.

A PNR audit trail is what a generic LLM extractor chokes on: line after
line of three-letter action codes, six-letter airport pairs, office
identifiers, queue references. The signal density is low (a passenger
name + a few routings + a fare per booking) hidden inside hundreds of
structural codes. Running the generic extractor produces hundreds of
junk 'place' entities like 'EWRORY' (an EWR-ORY routing pair) and
'NYCEC08AA' (an Amadeus office code).

This parser knows the format. It emits only the things that ARE
intelligence:

  PERSON          — passenger names (decoded from FA / RM-FARE lines)
  AIRPORT         — individual airports (decoded from routing pairs)
  ORGANIZATION    — the GDS itself + booking agencies + airlines
  TICKET          — ticket numbers (FA lines)
  FARE_AMOUNT     — fare totals with currency
  BOOKING_OFFICE  — the agent office that made the booking (in metadata,
                    not promoted as a facility)

It also emits STATEMENTS for travel intent:

  PERSON traveled_from AIRPORT (event_time = departure date)
  PERSON traveled_to   AIRPORT (event_time = arrival date if known)
  PERSON paid_fare     FARE_AMOUNT
  PERSON had_ticket    TICKET_NUMBER

And it records FOIA exemption spans so the live UI can render the
redactions without ever extracting their (often nonsense post-OCR)
inside-the-black-bar text as a mention.

Hard-coded IATA airport list is intentional and small. Covers the
~150 highest-traffic airports plus the ones likely to appear in a
specific corpus of interest. Update as obscure regional codes show up
in real docs.

Design tenets:
  - Better silent than wrong: if a line doesn't match, skip it (don't
    invent a mention)
  - Every emission has a confidence; low-confidence emissions get
    marked so the validator can downgrade them
  - Char offsets reference the input text the caller supplied
"""

from __future__ import annotations

import re
from typing import Optional

from .types import (
    ExtractionOutput, ProposedMention, ProposedStatement, RedactionSpan,
)

EXTRACTOR_NAME = "amadeus_pnr_parser_v1"


# ─── IATA airport codes (focused list of high-traffic + investigation-relevant) ──

_IATA: dict[str, dict] = {
    # North America
    "JFK": {"city": "New York",         "name": "John F. Kennedy Intl",       "country": "US"},
    "EWR": {"city": "Newark",           "name": "Newark Liberty Intl",        "country": "US"},
    "LGA": {"city": "New York",         "name": "LaGuardia",                  "country": "US"},
    "BOS": {"city": "Boston",           "name": "Logan Intl",                 "country": "US"},
    "DCA": {"city": "Washington",       "name": "Reagan National",            "country": "US"},
    "IAD": {"city": "Washington",       "name": "Dulles Intl",                "country": "US"},
    "BWI": {"city": "Baltimore",        "name": "BWI",                        "country": "US"},
    "ATL": {"city": "Atlanta",          "name": "Hartsfield-Jackson",         "country": "US"},
    "MIA": {"city": "Miami",            "name": "Miami Intl",                 "country": "US"},
    "FLL": {"city": "Fort Lauderdale",  "name": "FLL",                        "country": "US"},
    "PBI": {"city": "West Palm Beach",  "name": "Palm Beach Intl",            "country": "US"},
    "TEB": {"city": "Teterboro",        "name": "Teterboro (private)",        "country": "US"},
    "ORD": {"city": "Chicago",          "name": "O'Hare Intl",                "country": "US"},
    "MDW": {"city": "Chicago",          "name": "Midway",                     "country": "US"},
    "DFW": {"city": "Dallas/Fort Worth","name": "DFW Intl",                   "country": "US"},
    "IAH": {"city": "Houston",          "name": "Bush Intercontinental",      "country": "US"},
    "DEN": {"city": "Denver",           "name": "Denver Intl",                "country": "US"},
    "PHX": {"city": "Phoenix",          "name": "Sky Harbor",                 "country": "US"},
    "LAX": {"city": "Los Angeles",      "name": "LAX",                        "country": "US"},
    "SFO": {"city": "San Francisco",    "name": "SFO",                        "country": "US"},
    "SEA": {"city": "Seattle",          "name": "Sea-Tac",                    "country": "US"},
    "LAS": {"city": "Las Vegas",        "name": "Harry Reid Intl",            "country": "US"},
    "SAN": {"city": "San Diego",        "name": "SAN",                        "country": "US"},
    "MSP": {"city": "Minneapolis",      "name": "MSP",                        "country": "US"},
    "DTW": {"city": "Detroit",          "name": "DTW",                        "country": "US"},
    "PHL": {"city": "Philadelphia",     "name": "PHL",                        "country": "US"},
    "CLT": {"city": "Charlotte",        "name": "CLT",                        "country": "US"},
    "YVR": {"city": "Vancouver",        "name": "YVR",                        "country": "CA"},
    "YYZ": {"city": "Toronto",          "name": "Pearson Intl",               "country": "CA"},
    "YUL": {"city": "Montreal",         "name": "Trudeau Intl",               "country": "CA"},
    "MEX": {"city": "Mexico City",      "name": "Benito Juarez Intl",         "country": "MX"},
    "CUN": {"city": "Cancun",           "name": "Cancun Intl",                "country": "MX"},
    # Caribbean / Atlantic islands
    "STT": {"city": "Saint Thomas",     "name": "Cyril E. King",              "country": "VI"},
    "STX": {"city": "Saint Croix",      "name": "Henry E. Rohlsen",           "country": "VI"},
    "SJU": {"city": "San Juan",         "name": "Luis Muñoz Marín",           "country": "PR"},
    "NAS": {"city": "Nassau",           "name": "Lynden Pindling Intl",       "country": "BS"},
    "BGI": {"city": "Bridgetown",       "name": "Grantley Adams",             "country": "BB"},
    # Europe
    "LHR": {"city": "London",           "name": "Heathrow",                   "country": "GB"},
    "LGW": {"city": "London",           "name": "Gatwick",                    "country": "GB"},
    "STN": {"city": "London",           "name": "Stansted",                   "country": "GB"},
    "LTN": {"city": "London",           "name": "Luton",                      "country": "GB"},
    "LCY": {"city": "London",           "name": "London City",                "country": "GB"},
    "MAN": {"city": "Manchester",       "name": "Manchester",                 "country": "GB"},
    "EDI": {"city": "Edinburgh",        "name": "Edinburgh",                  "country": "GB"},
    "CDG": {"city": "Paris",            "name": "Charles de Gaulle",          "country": "FR"},
    "ORY": {"city": "Paris",            "name": "Orly",                       "country": "FR"},
    "NCE": {"city": "Nice",             "name": "Côte d'Azur",                "country": "FR"},
    "AMS": {"city": "Amsterdam",        "name": "Schiphol",                   "country": "NL"},
    "FRA": {"city": "Frankfurt",        "name": "Frankfurt Main",             "country": "DE"},
    "MUC": {"city": "Munich",           "name": "Munich Intl",                "country": "DE"},
    "TXL": {"city": "Berlin",           "name": "Tegel (closed)",             "country": "DE"},
    "BER": {"city": "Berlin",           "name": "Brandenburg",                "country": "DE"},
    "ZRH": {"city": "Zurich",           "name": "Zurich",                     "country": "CH"},
    "GVA": {"city": "Geneva",           "name": "Geneva",                     "country": "CH"},
    "MAD": {"city": "Madrid",           "name": "Barajas",                    "country": "ES"},
    "BCN": {"city": "Barcelona",        "name": "El Prat",                    "country": "ES"},
    "FCO": {"city": "Rome",             "name": "Fiumicino",                  "country": "IT"},
    "MXP": {"city": "Milan",            "name": "Malpensa",                   "country": "IT"},
    "LIN": {"city": "Milan",            "name": "Linate",                     "country": "IT"},
    "VIE": {"city": "Vienna",           "name": "Vienna Intl",                "country": "AT"},
    "CPH": {"city": "Copenhagen",       "name": "Copenhagen",                 "country": "DK"},
    "ARN": {"city": "Stockholm",        "name": "Arlanda",                    "country": "SE"},
    "OSL": {"city": "Oslo",             "name": "Gardermoen",                 "country": "NO"},
    "HEL": {"city": "Helsinki",         "name": "Vantaa",                     "country": "FI"},
    "DUB": {"city": "Dublin",           "name": "Dublin",                     "country": "IE"},
    "BRU": {"city": "Brussels",         "name": "Brussels",                   "country": "BE"},
    "LIS": {"city": "Lisbon",           "name": "Humberto Delgado",           "country": "PT"},
    "ATH": {"city": "Athens",           "name": "Eleftherios Venizelos",      "country": "GR"},
    "IST": {"city": "Istanbul",         "name": "Istanbul",                   "country": "TR"},
    "SVO": {"city": "Moscow",           "name": "Sheremetyevo",               "country": "RU"},
    "DME": {"city": "Moscow",           "name": "Domodedovo",                 "country": "RU"},
    # Middle East
    "TLV": {"city": "Tel Aviv",         "name": "Ben Gurion",                 "country": "IL"},
    "DXB": {"city": "Dubai",            "name": "Dubai Intl",                 "country": "AE"},
    "DOH": {"city": "Doha",             "name": "Hamad Intl",                 "country": "QA"},
    "AUH": {"city": "Abu Dhabi",        "name": "Abu Dhabi Intl",             "country": "AE"},
    "RUH": {"city": "Riyadh",           "name": "King Khalid Intl",           "country": "SA"},
    "JED": {"city": "Jeddah",           "name": "King Abdulaziz",             "country": "SA"},
    "AMM": {"city": "Amman",            "name": "Queen Alia",                 "country": "JO"},
    "BEY": {"city": "Beirut",           "name": "Rafic Hariri Intl",          "country": "LB"},
    "CAI": {"city": "Cairo",            "name": "Cairo Intl",                 "country": "EG"},
    # Asia
    "HND": {"city": "Tokyo",            "name": "Haneda",                     "country": "JP"},
    "NRT": {"city": "Tokyo",            "name": "Narita",                     "country": "JP"},
    "KIX": {"city": "Osaka",            "name": "Kansai Intl",                "country": "JP"},
    "ICN": {"city": "Seoul",            "name": "Incheon Intl",               "country": "KR"},
    "PEK": {"city": "Beijing",          "name": "Capital Intl",               "country": "CN"},
    "PVG": {"city": "Shanghai",         "name": "Pudong Intl",                "country": "CN"},
    "HKG": {"city": "Hong Kong",        "name": "Chek Lap Kok",               "country": "HK"},
    "TPE": {"city": "Taipei",           "name": "Taoyuan Intl",               "country": "TW"},
    "SIN": {"city": "Singapore",        "name": "Changi",                     "country": "SG"},
    "BKK": {"city": "Bangkok",          "name": "Suvarnabhumi",               "country": "TH"},
    "KUL": {"city": "Kuala Lumpur",     "name": "KLIA",                       "country": "MY"},
    "CGK": {"city": "Jakarta",          "name": "Soekarno-Hatta",             "country": "ID"},
    "MNL": {"city": "Manila",           "name": "Ninoy Aquino",               "country": "PH"},
    "DEL": {"city": "Delhi",            "name": "Indira Gandhi",              "country": "IN"},
    "BOM": {"city": "Mumbai",           "name": "Chhatrapati Shivaji",        "country": "IN"},
    "BLR": {"city": "Bangalore",        "name": "Kempegowda",                 "country": "IN"},
    "MAA": {"city": "Chennai",          "name": "MAA",                        "country": "IN"},
    "CCU": {"city": "Kolkata",          "name": "Netaji Subhas",              "country": "IN"},
    # Australia + Pacific
    "SYD": {"city": "Sydney",           "name": "Kingsford Smith",            "country": "AU"},
    "MEL": {"city": "Melbourne",        "name": "Tullamarine",                "country": "AU"},
    "BNE": {"city": "Brisbane",         "name": "Brisbane",                   "country": "AU"},
    "PER": {"city": "Perth",            "name": "Perth",                      "country": "AU"},
    "AKL": {"city": "Auckland",         "name": "Auckland",                   "country": "NZ"},
    # South America + Africa highlights
    "GRU": {"city": "Sao Paulo",        "name": "Guarulhos",                  "country": "BR"},
    "GIG": {"city": "Rio de Janeiro",   "name": "Galeao",                     "country": "BR"},
    "EZE": {"city": "Buenos Aires",     "name": "Ezeiza",                     "country": "AR"},
    "SCL": {"city": "Santiago",         "name": "Arturo Merino Benitez",      "country": "CL"},
    "LIM": {"city": "Lima",             "name": "Jorge Chavez",               "country": "PE"},
    "BOG": {"city": "Bogota",           "name": "El Dorado",                  "country": "CO"},
    "JNB": {"city": "Johannesburg",     "name": "O.R. Tambo",                 "country": "ZA"},
    "CPT": {"city": "Cape Town",        "name": "Cape Town",                  "country": "ZA"},
    "NBO": {"city": "Nairobi",          "name": "Jomo Kenyatta",              "country": "KE"},
    "ADD": {"city": "Addis Ababa",      "name": "Bole",                       "country": "ET"},
    "LOS": {"city": "Lagos",            "name": "Murtala Muhammed",           "country": "NG"},
}


def is_iata(code: str) -> bool:
    return code.upper() in _IATA


def airport_label(code: str) -> str:
    """Returns 'JFK (New York)' for known codes, just the code for unknowns."""
    code = code.upper()
    info = _IATA.get(code)
    if info:
        return f"{code} ({info['city']})"
    return code


# ─── Regex grammar for the format ──────────────────────────────────────

# Header: "AMADEUS INTERNET BOOKING CR-NYCEC08AA 33999991 SU 0001AA/..."
_HEADER_RE = re.compile(
    r"\b(?P<gds>AMADEUS|SABRE|GALILEO|WORLDSPAN)\s+(?:INTERNET\s+)?BOOKING\s+"
    r"CR-(?P<office>[A-Z0-9]{6,10})\s+(?P<record_locator>\d{6,12})",
    re.IGNORECASE,
)

# FA line — the canonical "this is the ticket for this passenger" record.
# Example:
#   FA 000-0000000000/ETEC/USD1000.00/13JAN11/NYCEC08AA/33999991/EC 002 A 19JAN EWRORY/Acme/Alex MR(ADT
#
# Groups:
#   ticket_number   — 000-0000000000
#   currency        — USD
#   amount          — 1000.00
#   issue_date      — 13JAN11
#   office          — NYCEC08AA
#   record_locator  — 33999991
#   travel_date     — 19JAN
#   routing         — EWRORY (one or more 3-letter codes concatenated)
#   passenger_last  — Acme
#   passenger_first — Alex (sometimes with middle name)
# Shared sub-pattern for the passenger-name suffix in fare/ticket lines:
#   /Acme/Alex MR(ADT
#   /Acme/Alex JAMES MR
#   /Doe/Jane
# Each name part must be >= 3 chars to reject parser-artifact captures
# like 'EC' (an Amadeus EC fare-code spilling into the name slot) without
# losing real names like 'Acme' / 'Alex'. Two-letter family names
# (Wu, Au, Li) get reconsidered when a real case shows up — for the
# current Acme corpus this floor is the right safety.
_PASSENGER_SUFFIX = (
    r"/(?P<passenger_last>[A-Z]{3,})"
    r"(?:/(?P<passenger_first>[A-Z]{3,}(?:\s+[A-Z]{2,})*?)"
    r"(?:\s+(?P<title>MR|MRS|MS|MISS|DR|MSTR))?)?"
    r"(?:\((?:ADT|CHD|INF|YTH))?"
)

_FA_RE = re.compile(
    r"\bFA\s+(?P<ticket>\d{3}-\d{7,12})/(?P<carrier_class>[A-Z]+)"
    r"/(?P<currency>[A-Z]{3})(?P<amount>\d+(?:\.\d{2})?)/(?P<issue_date>\d{1,2}[A-Z]{3}\d{2,4})"
    r"/(?P<office>[A-Z0-9]{6,10})/(?P<record_locator>\d{6,12})"
    r"(?:/EC\s+\d{3}\s+[A-Z]\s+(?P<travel_date>\d{1,2}[A-Z]{3}))?"
    r"\s+(?P<routing>[A-Z]{3,})"
    + _PASSENGER_SUFFIX,
    re.IGNORECASE,
)

# RM FARE line — a free-text fare remark.
# Example: "RM FARE1000.00 USD/Acme/Alex MR(ADT (b)(6) ADT"
_RM_FARE_RE = re.compile(
    r"\bRM\s+FARE\s*(?P<amount>\d+(?:\.\d{2})?)\s+(?P<currency>[A-Z]{3})"
    + _PASSENGER_SUFFIX,
    re.IGNORECASE,
)

# FOIA exemption markers — preserved as redaction spans, never extracted.
_FOIA_RE = re.compile(r"\(b\)\((?P<exemption>[0-9](?:\)\([A-Z])?)\)")

# Action-code line prefix — used to skip lines we know we're not parsing.
_ACTION_PREFIX_RE = re.compile(r"^\s*\d{3}(?:/\d{3})?\s+[A-Z]/[\d\s]+\s+(?P<verb>[A-Z]{1,5})\b")

# Decompose a multi-leg routing token like "EWRORY" or "JFKLHRORY" into
# its component IATA codes. Each leg is 3 chars; we walk left-to-right.
def _decompose_routing(token: str) -> list[str]:
    token = token.upper()
    if len(token) < 6 or len(token) % 3 != 0:
        return []
    codes = [token[i:i + 3] for i in range(0, len(token), 3)]
    # Require AT LEAST TWO of the codes to be known IATA airports, or
    # we treat the whole thing as opaque (could be a random office code
    # that happens to be a multiple of 3 letters long).
    known = sum(1 for c in codes if is_iata(c))
    if known < 2:
        return []
    return codes


def _norm(s: str) -> str:
    return " ".join(s.split()).upper()


# ─── Main extractor ────────────────────────────────────────────────────


def extract(*, document_id: int, text: str) -> ExtractionOutput:
    out = ExtractionOutput(
        document_id=document_id,
        extractor=EXTRACTOR_NAME,
        mentions=[],
        statements=[],
        redactions=[],
    )

    if not text:
        out.parser_notes.append("empty_text")
        return out

    # 1. Header (GDS + booking office + record locator)
    h = _HEADER_RE.search(text)
    if h:
        gds_name = h.group("gds").upper()
        out.mentions.append(ProposedMention(
            surface_form=gds_name,
            surface_form_norm=gds_name,
            suggested_type="organization",
            char_start=h.start("gds"),
            char_end=h.end("gds"),
            confidence=0.98,
            extraction_method=EXTRACTOR_NAME,
            metadata={"role": "gds_provider"},
        ))
        out.parser_notes.append(
            f"booking_office={h.group('office')} record_locator={h.group('record_locator')}"
        )

    # 2. FA (Fare Audit) lines — the canonical ticket records
    fa_count = 0
    passengers_seen: dict[str, str] = {}  # canonical full name → first surface_form seen
    for m in _FA_RE.finditer(text):
        fa_count += 1
        # Passenger
        plast = (m.group("passenger_last") or "").strip().upper()
        pfirst = (m.group("passenger_first") or "").strip().upper()
        if plast:
            full = f"{plast}/{pfirst}" if pfirst else plast
            human_name = f"{pfirst} {plast}".strip().title() if pfirst else plast.title()
            # We emit the canonical "Alex Acme" form so dedup
            # against existing entities works; the surface_form points
            # back to the source slash form for span fidelity.
            out.mentions.append(ProposedMention(
                surface_form=human_name,
                surface_form_norm=_norm(human_name),
                suggested_type="person",
                char_start=m.start("passenger_last"),
                char_end=m.end("passenger_first") if m.group("passenger_first") else m.end("passenger_last"),
                confidence=0.95,
                extraction_method=EXTRACTOR_NAME,
                metadata={
                    "from_line": "FA",
                    "raw_pnr_form": full,
                    "title": m.group("title"),
                },
            ))
            passengers_seen.setdefault(human_name, human_name)
        # Ticket
        ticket = m.group("ticket")
        out.mentions.append(ProposedMention(
            surface_form=ticket,
            surface_form_norm=ticket,
            suggested_type="ticket_number",
            char_start=m.start("ticket"),
            char_end=m.end("ticket"),
            confidence=0.99,
            extraction_method=EXTRACTOR_NAME,
            metadata={"carrier_class": m.group("carrier_class")},
        ))
        # Fare
        amount = m.group("amount")
        currency = m.group("currency")
        out.mentions.append(ProposedMention(
            surface_form=f"{currency} {amount}",
            surface_form_norm=f"{currency} {amount}",
            suggested_type="fare_amount",
            char_start=m.start("currency"),
            char_end=m.end("amount"),
            confidence=0.99,
            extraction_method=EXTRACTOR_NAME,
            metadata={"currency": currency, "amount": float(amount)},
        ))
        # Routing → airports
        routing = m.group("routing") or ""
        legs = _decompose_routing(routing)
        airport_starts = [m.start("routing") + 3 * i for i in range(len(legs))]
        for i, code in enumerate(legs):
            out.mentions.append(ProposedMention(
                surface_form=code,
                surface_form_norm=code,
                suggested_type="place",
                char_start=airport_starts[i],
                char_end=airport_starts[i] + 3,
                confidence=0.95 if is_iata(code) else 0.4,
                extraction_method=EXTRACTOR_NAME,
                metadata={
                    "iata": code,
                    "is_known_iata": is_iata(code),
                    **(_IATA.get(code, {})),
                },
            ))
        # Travel statement: passenger flew route on date
        if plast and legs:
            human_name = f"{pfirst} {plast}".strip().title() if pfirst else plast.title()
            travel_date = m.group("travel_date")  # e.g. "19JAN"
            issue_date = m.group("issue_date")    # e.g. "13JAN11"
            origin = legs[0]
            destination = legs[-1]
            out.statements.append(ProposedStatement(
                predicate="traveled_from",
                speaker_surface=human_name,
                object_surface=airport_label(origin),
                raw_text=text[m.start():m.end()],
                char_start=m.start(),
                char_end=m.end(),
                confidence=0.92,
                extraction_method=EXTRACTOR_NAME,
                event_time_iso=_pnr_date_to_iso(travel_date, issue_date),
                metadata={
                    "ticket_number": ticket,
                    "all_legs": legs,
                    "routing_raw": routing,
                },
            ))
            if origin != destination:
                out.statements.append(ProposedStatement(
                    predicate="traveled_to",
                    speaker_surface=human_name,
                    object_surface=airport_label(destination),
                    raw_text=text[m.start():m.end()],
                    char_start=m.start(),
                    char_end=m.end(),
                    confidence=0.92,
                    extraction_method=EXTRACTOR_NAME,
                    event_time_iso=_pnr_date_to_iso(travel_date, issue_date),
                    metadata={"ticket_number": ticket, "all_legs": legs},
                ))
            # Fare-paid statement
            out.statements.append(ProposedStatement(
                predicate="paid_fare",
                speaker_surface=human_name,
                object_surface=f"{currency} {amount}",
                raw_text=text[m.start():m.end()],
                char_start=m.start(),
                char_end=m.end(),
                confidence=0.95,
                extraction_method=EXTRACTOR_NAME,
                event_time_iso=_pnr_date_to_iso(issue_date, issue_date),
                metadata={"ticket_number": ticket},
            ))

    out.parser_notes.append(f"FA_lines={fa_count}")

    # 3. RM FARE remarks (free-text fare statements; lower confidence than FA)
    for m in _RM_FARE_RE.finditer(text):
        amount = m.group("amount")
        currency = m.group("currency")
        plast = (m.group("passenger_last") or "").strip().upper()
        pfirst = (m.group("passenger_first") or "").strip().upper()
        if plast:
            human_name = f"{pfirst} {plast}".strip().title() if pfirst else plast.title()
            out.mentions.append(ProposedMention(
                surface_form=human_name,
                surface_form_norm=_norm(human_name),
                suggested_type="person",
                char_start=m.start("passenger_last"),
                char_end=m.end("passenger_first") if m.group("passenger_first") else m.end("passenger_last"),
                confidence=0.85,
                extraction_method=EXTRACTOR_NAME,
                metadata={"from_line": "RM_FARE"},
            ))
            passengers_seen.setdefault(human_name, human_name)
            out.statements.append(ProposedStatement(
                predicate="paid_fare",
                speaker_surface=human_name,
                object_surface=f"{currency} {amount}",
                raw_text=text[m.start():m.end()],
                char_start=m.start(),
                char_end=m.end(),
                confidence=0.80,
                extraction_method=EXTRACTOR_NAME,
                metadata={"from_line": "RM_FARE"},
            ))

    # 4. FOIA exemption markers → redaction spans
    for m in _FOIA_RE.finditer(text):
        out.redactions.append(RedactionSpan(
            char_start=m.start(),
            char_end=m.end(),
            exemption=f"(b)({m.group('exemption')})",
        ))

    return out


def _pnr_date_to_iso(pnr_date: Optional[str], issue_date: Optional[str]) -> Optional[str]:
    """Convert '19JAN' or '13JAN11' to ISO date string. The 2-digit-year
    PNR dates assume 20YY; pre-2000 PNRs would need a context flag.

    When only a day-month is given (e.g. '19JAN' in the travel_date field),
    we use the YEAR from the issue_date if available, else None — better
    to omit than guess a wrong year."""
    if not pnr_date:
        return None
    pnr_date = pnr_date.strip().upper()
    months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    m = re.match(r"^(\d{1,2})([A-Z]{3})(\d{2})?$", pnr_date)
    if not m:
        return None
    day = int(m.group(1))
    mo = months.get(m.group(2))
    yr_2 = m.group(3)
    if not mo:
        return None
    if yr_2:
        year = 2000 + int(yr_2) if int(yr_2) < 50 else 1900 + int(yr_2)
    elif issue_date:
        im = re.match(r"^\d{1,2}[A-Z]{3}(\d{2})$", issue_date.upper())
        if im:
            yi = int(im.group(1))
            year = 2000 + yi if yi < 50 else 1900 + yi
        else:
            return None
    else:
        return None
    try:
        from datetime import date
        return date(year, mo, day).isoformat()
    except ValueError:
        return None
