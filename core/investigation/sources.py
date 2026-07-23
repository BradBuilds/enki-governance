"""
Investigation Data Sources — real API clients for entity investigation.

Each source returns a standardized dict:
  {
    "findings": [{ "type": str, "title": str, "data": dict, "confidence": float }],
    "discovered_entities": [{ "name": str, "type": str, "relationship": str, "detail": dict }],
    "source": str,
    "status": "complete" | "no_data" | "error",
    "raw_count": int
  }

All sources are free, no API keys required (except FEC which uses DEMO_KEY).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import ssl
from typing import Optional

from core.research.credentials import get_credential, has_credential

log = logging.getLogger("enki.investigation.sources")

_SSL_CTX = ssl.create_default_context()
_UA = "Enki/1.0 research@example.com"


# ─── Rate Limiter ─────────────────────────────────────────────────────────────
# Token-bucket rate limiter per source. Prevents getting blocked by APIs.
# Each source has its own bucket with configured max requests per second.

class _RateLimiter:
    """Thread-safe token-bucket rate limiter with dynamic rate updates.

    `set_rate(new_per_second, new_burst)` lets the credentials layer bump the
    ceiling when a key arrives (FEC 30/hr → 1000/hr, SAM 10/day → 1000/day,
    etc.) without restart. Token state is preserved across the rate change.
    """

    def __init__(self, name: str, max_per_second: float, burst: int = 1):
        self.name = name
        self.max_per_second = max_per_second
        self.burst = burst
        self._base_per_second = max_per_second  # remembered for "unkeyed" reset
        self._base_burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._total_calls = 0
        self._total_waits = 0

    def acquire(self):
        """Block until a token is available, then consume one."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.burst, self._tokens + elapsed * self.max_per_second)
            self._last_refill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._total_calls += 1
                return

        # Not enough tokens — wait
        wait_time = (1.0 - self._tokens) / self.max_per_second
        self._total_waits += 1
        time.sleep(wait_time)

        with self._lock:
            self._tokens = 0  # consumed on wake
            self._total_calls += 1

    def set_rate(self, max_per_second: float, burst: Optional[int] = None):
        """Adjust the rate dynamically. Used when a credential lifts the
        ceiling for this source (called once per source, lazily on first hit
        when has_credential() is true)."""
        with self._lock:
            self.max_per_second = max_per_second
            if burst is not None:
                self.burst = burst
            self._last_refill = time.monotonic()

    def reset_to_base(self):
        """Restore the original unkeyed rate. Called when a credential is removed."""
        self.set_rate(self._base_per_second, self._base_burst)

    def stats(self) -> dict:
        return {
            "name": self.name,
            "calls": self._total_calls,
            "waits": self._total_waits,
            "max_per_second": self.max_per_second,
            "base_per_second": self._base_per_second,
            "keyed": self.max_per_second > self._base_per_second,
        }


# Per-source rate limiters
_LIMITERS = {
    "sec_edgar":      _RateLimiter("sec_edgar", max_per_second=8, burst=3),     # SEC: 10/s limit
    "fec":            _RateLimiter("fec", max_per_second=0.25, burst=2),         # 1000/hr = ~0.28/s
    "usaspending":    _RateLimiter("usaspending", max_per_second=2, burst=3),    # generous
    "propublica":     _RateLimiter("propublica", max_per_second=2, burst=3),     # generous
    "courtlistener":  _RateLimiter("courtlistener", max_per_second=1, burst=2),  # 5000/hr
    "fdic":           _RateLimiter("fdic", max_per_second=2, burst=3),           # generous
    "google_books":   _RateLimiter("google_books", max_per_second=0.5, burst=2), # 1000/day
    "fda":            _RateLimiter("fda", max_per_second=3, burst=5),            # 240/min with key
    "epa":            _RateLimiter("epa", max_per_second=1, burst=2),            # generous but polite
    "congress":       _RateLimiter("congress", max_per_second=1, burst=2),       # 5000/hr
    "loc":            _RateLimiter("loc", max_per_second=0.5, burst=2),          # polite, no published limit
    "sam_gov":        _RateLimiter("sam_gov", max_per_second=0.01, burst=1),     # 10/day public tier
    # Phase 8b.6.1 (May 10 2026) — researchpack 1.1.0 official sources
    "cia_factbook":     _RateLimiter("cia_factbook", max_per_second=2, burst=3),       # static GitHub raw, polite
    "geonames":         _RateLimiter("geonames", max_per_second=1, burst=2),            # 1000/hr free tier
    "rest_countries":   _RateLimiter("rest_countries", max_per_second=2, burst=3),     # generous
    "world_bank":       _RateLimiter("world_bank", max_per_second=2, burst=3),         # generous
    "opencorporates":   _RateLimiter("opencorporates", max_per_second=0.5, burst=2),   # 500/day free
    "faa_aircraft":     _RateLimiter("faa_aircraft", max_per_second=1, burst=2),       # registry inquiry, polite
    "un_sanctions":     _RateLimiter("un_sanctions", max_per_second=1, burst=2),       # static XML, polite
    "federal_register": _RateLimiter("federal_register", max_per_second=2, burst=3),   # 1000/hr published
}


def get_rate_limiter_stats() -> list[dict]:
    """Return stats for all rate limiters."""
    return [limiter.stats() for limiter in _LIMITERS.values()]


# Rate-limit deltas applied when a credential is present. Each entry maps a
# source -> (credential_name, keyed_per_second, keyed_burst). Read by
# `_apply_tier_for_source()` on every acquire so a key added mid-process takes
# effect without restart.
_KEYED_RATE_BUMPS: dict[str, tuple[str, float, int]] = {
    # data.gov family — single key, three sources
    "fec":              ("DATA_GOV_API_KEY", 1000 / 3600.0, 5),   # 1000/hr
    "congress":         ("DATA_GOV_API_KEY", 1000 / 3600.0, 3),   # 1000/hr
    "fda":              ("DATA_GOV_API_KEY", 240 / 60.0,    8),   # 240/min
    # SAM.gov: 10/day → 1000/day with a system account key
    "sam_gov":          ("SAM_GOV_API_KEY",  1000 / 86400.0, 2),  # 1000/day
    # GeoNames: 1000/hr default; bump only if operator confirms premium tier
    "geonames":         ("GEONAMES_USERNAME", 1000 / 3600.0, 3),
    # OpenCorporates: free 500/day → paid effectively 5+ req/s
    "opencorporates":   ("OPENCORPORATES_API_TOKEN", 5.0,    5),
    # OpenSanctions: paid per-name lookup
    "opensanctions":    ("OPENSANCTIONS_API_KEY", 2.0,       3),
}

# Per-source page caps. When a keyed tier is active, the source can ask for
# more results per query. Source handlers read this via `source_page_cap()`.
_PAGE_CAPS: dict[str, dict[str, int]] = {
    "fec":              {"free": 5,  "keyed": 20},
    "congress":         {"free": 25, "keyed": 100},
    "opencorporates":   {"free": 25, "keyed": 100},
    "geonames":         {"free": 3,  "keyed": 10},
}

_TIER_APPLIED: set[str] = set()  # source names already bumped this process


def _apply_tier_for_source(source: str):
    """If the credential for `source` is configured, lift the rate limiter
    once. Idempotent — only the first hit per process does the bump. If the
    credential is later removed (notify_credential_change clears cache), we
    do NOT downgrade automatically; admin can call reset_to_base manually."""
    bump = _KEYED_RATE_BUMPS.get(source)
    if not bump or source in _TIER_APPLIED:
        return
    cred_name, keyed_rate, keyed_burst = bump
    if has_credential(cred_name):
        limiter = _LIMITERS.get(source)
        if limiter:
            limiter.set_rate(keyed_rate, keyed_burst)
            log.info("Source %s keyed tier active (cred=%s, rate=%.4f/s)",
                     source, cred_name, keyed_rate)
            _TIER_APPLIED.add(source)


def source_page_cap(source: str) -> int:
    """Return per-page result cap for `source` based on current credential state."""
    caps = _PAGE_CAPS.get(source)
    if not caps:
        return 25
    bump = _KEYED_RATE_BUMPS.get(source)
    if bump and has_credential(bump[0]):
        return caps["keyed"]
    return caps["free"]


def reset_tier_applied() -> None:
    """Test/admin hook — forget that tier bumps were applied so the next
    call re-evaluates. Used when a key is removed."""
    _TIER_APPLIED.clear()
    for limiter in _LIMITERS.values():
        limiter.reset_to_base()


def _rate_limit(source: str):
    """Acquire a rate limit token for the given source. Blocks if needed.
    Also lazily applies a tier bump if the source has a credential."""
    _apply_tier_for_source(source)
    limiter = _LIMITERS.get(source)
    if limiter:
        limiter.acquire()


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _fetch_json(url: str, headers: Optional[dict] = None, timeout: int = 15,
                source: str = "") -> Optional[dict]:
    """GET JSON from a URL. Rate-limited if source is specified."""
    if source:
        _rate_limit(source)
    try:
        req = urllib.request.Request(url, headers=headers or {})
        req.add_header("User-Agent", _UA)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log.debug("HTTP %d for %s: %s", e.code, url[:100], e.reason)
        return None
    except Exception as e:
        log.debug("Fetch failed for %s: %s", url[:100], e)
        return None


def _post_json(url: str, body: dict, timeout: int = 15,
               source: str = "") -> Optional[dict]:
    """POST JSON to a URL. Rate-limited if source is specified."""
    if source:
        _rate_limit(source)
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", _UA)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.debug("POST failed for %s: %s", url[:100], e)
        return None


# ─── Name-token relevance helpers ─────────────────────────────────────────────
# Cheap source-side noise filter. The post-hoc LLM scorer (Phase 7d) catches
# the harder cases, but most noise findings can be killed at the source by
# requiring that the seed name's content tokens actually appear in the result.
# Used by LOC, Federal Register, and similar keyword-search sources where the
# upstream API has no entity-match semantics — it just returns substring hits.

_NAME_STOPWORDS = {
    "the", "of", "a", "an", "and", "or", "in", "on", "for", "to", "at",
    "by", "with", "from", "into", "as", "is", "are", "was", "were",
    "be", "been", "being", "this", "that", "these", "those",
    "mr", "mrs", "ms", "miss", "dr", "sir", "lord", "lady",
    "inc", "llc", "corp", "company", "co", "ltd", "limited",
    "jr", "sr", "ii", "iii", "iv",
}

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def _seed_tokens(name: str) -> list[str]:
    """Lowercased content tokens from a seed name, stopwords removed."""
    if not name:
        return []
    raw = _TOKEN_RE.findall(name.lower())
    return [t for t in raw if t not in _NAME_STOPWORDS and len(t) >= 2]


def _text_mentions_seed(seed: str, text: str, *, require_surname: bool = True) -> bool:
    """True iff `text` plausibly references `seed`. Used by keyword-search
    sources whose API has no entity-match semantics.

    Multi-token seeds: require the LAST content token (surname / most-specific
    noun) plus at least one other token to appear in the text. Single-token
    seeds: require a whole-word match (avoids 'bean' matching 'beanbag')."""
    seed_toks = _seed_tokens(seed)
    if not seed_toks:
        return True
    if not text:
        return False
    t = text.lower()
    matched = [tok for tok in seed_toks
               if re.search(rf"\b{re.escape(tok)}\b", t)]
    if len(seed_toks) == 1:
        return bool(matched)
    if require_surname and seed_toks[-1] not in matched:
        return False
    return len(matched) >= 2


# ─── SEC EDGAR ────────────────────────────────────────────────────────────────

def investigate_sec_edgar(name: str, entity_type: str = "person") -> dict:
    """Search SEC EDGAR for companies, filings, officers, and financials.

    For a person: finds companies where they are officer/director.
    For an org: finds the company's filings and financials directly.
    """
    result = {"findings": [], "discovered_entities": [], "source": "sec_edgar",
              "status": "complete", "raw_count": 0}

    # Step 1: Find matching companies via tickers file
    tickers = _fetch_json("https://www.sec.gov/files/company_tickers.json", source="sec_edgar")
    matched_ciks = []
    if tickers:
        name_lower = name.lower()
        for v in tickers.values():
            if name_lower in v.get("title", "").lower():
                matched_ciks.append({
                    "cik": str(v["cik_str"]),
                    "ticker": v.get("ticker"),
                    "name": v.get("title"),
                })

    # Step 2: For each matched company, pull submissions + XBRL facts
    for company in matched_ciks[:5]:
        cik = company["cik"].zfill(10)
        # Submissions (filing history)
        subs = _fetch_json(f"https://data.sec.gov/submissions/CIK{cik}.json", source="sec_edgar")
        if subs:
            comp_name = subs.get("name", company["name"])
            sic = subs.get("sic", "")
            sic_desc = subs.get("sicDescription", "")
            state = subs.get("stateOfIncorporation", "")
            addr = subs.get("addresses", {}).get("business", {})
            address_str = f"{addr.get('street1', '')}, {addr.get('city', '')}, {addr.get('stateOrCountry', '')} {addr.get('zipCode', '')}"

            recent = subs.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])[:20]
            dates = recent.get("filingDate", [])[:20]
            docs = recent.get("primaryDocument", [])[:20]
            accessions = recent.get("accessionNumber", [])[:20]

            filing_summary = []
            for i in range(min(len(forms), 20)):
                filing_summary.append({
                    "form": forms[i], "date": dates[i],
                    "document": docs[i] if i < len(docs) else "",
                    "accession": accessions[i] if i < len(accessions) else "",
                })

            result["findings"].append({
                "type": "company",
                "title": f"SEC Company: {comp_name}",
                "data": {
                    "company_name": comp_name,
                    "cik": company["cik"],
                    "ticker": company.get("ticker"),
                    "sic": sic, "sic_description": sic_desc,
                    "state_of_incorporation": state,
                    "address": address_str,
                    "tickers": subs.get("tickers", []),
                    "exchanges": subs.get("exchanges", []),
                    "recent_filings": filing_summary[:10],
                },
                "confidence": 0.95,
            })

            # Discover the company as an entity
            result["discovered_entities"].append({
                "name": comp_name, "type": "organization",
                "relationship": "associated_company",
                "detail": {"cik": company["cik"], "ticker": company.get("ticker"),
                           "address": address_str},
            })

            # Step 3: XBRL financial facts
            xbrl = _fetch_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", source="sec_edgar")
            if xbrl:
                facts = xbrl.get("facts", {}).get("us-gaap", {})
                financials = {}
                for concept in ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                                "NetIncomeLoss", "Assets", "StockholdersEquity",
                                "CashAndCashEquivalentsAtCarryingValue", "Liabilities",
                                "EntityNumberOfEmployees"]:
                    vals = facts.get(concept, {}).get("units", {})
                    for unit, entries in vals.items():
                        if entries:
                            latest = sorted(entries, key=lambda x: x.get("end", ""), reverse=True)[0]
                            financials[concept] = {
                                "value": latest.get("val"),
                                "unit": unit,
                                "period_end": latest.get("end"),
                            }
                            break

                if financials:
                    result["findings"].append({
                        "type": "financial",
                        "title": f"Financials: {comp_name}",
                        "data": {"company": comp_name, "cik": company["cik"], **financials},
                        "confidence": 0.95,
                    })

            # Step 4: Parse Form 3/4 XML — officers and directors (no LLM needed)
            officers = _parse_insider_forms(company["cik"], recent)
            for officer in officers:
                result["findings"].append({
                    "type": "officer",
                    "title": f"Officer: {officer['name']} — {officer.get('title', 'Director')}",
                    "data": officer,
                    "confidence": 0.95,  # structured XML, federal filing
                })
                result["discovered_entities"].append({
                    "name": officer["name"],
                    "type": "person",
                    "relationship": "officer_of" if officer.get("is_officer") else "director_of",
                    "detail": {
                        "title": officer.get("title", ""),
                        "company": comp_name,
                        "cik": company["cik"],
                        "shares_owned": officer.get("shares_after"),
                    },
                })

            # Step 5: Build filing document URLs for key filing types
            for i in range(min(len(forms), 20)):
                form_type = forms[i]
                if form_type in ("DEF 14A", "SC 13D", "SC 13G", "10-K"):
                    acc = accessions[i].replace("-", "")
                    doc = docs[i] if i < len(docs) else ""
                    if doc:
                        doc_url = f"https://www.sec.gov/Archives/edgar/data/{company['cik']}/{acc}/{doc}"
                        result["findings"].append({
                            "type": "filing_document",
                            "title": f"Filing: {form_type} ({dates[i]})",
                            "data": {
                                "form_type": form_type,
                                "filing_date": dates[i],
                                "document_url": doc_url,
                                "accession_number": accessions[i],
                                "company": comp_name,
                                "cik": company["cik"],
                                "needs_llm_parsing": form_type in ("DEF 14A", "10-K"),
                            },
                            "confidence": 0.95,
                        })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


def _parse_insider_forms(cik: str, recent_filings: dict) -> list[dict]:
    """Parse Form 3/4 XML filings to extract officers and directors.

    Form 3 = initial statement of ownership (when officer/director appointed)
    Form 4 = change in ownership (stock transactions)

    Both are structured XML — no LLM needed. Returns list of:
    {name, title, is_officer, is_director, shares_after, filing_date, form_type}
    """
    import re

    officers: dict[str, dict] = {}  # name → latest data (dedup by name)
    forms = recent_filings.get("form", [])
    dates = recent_filings.get("filingDate", [])
    accessions = recent_filings.get("accessionNumber", [])

    form_count = 0
    for i in range(len(forms)):
        if forms[i] not in ("3", "4", "3/A", "4/A"):
            continue
        if form_count >= 10:  # cap to avoid rate limiting
            break

        acc = accessions[i].replace("-", "")
        # Fetch filing index to find the XML document
        try:
            index = _fetch_json(
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json",
                source="sec_edgar",
            )
            if not index:
                continue

            items = index.get("directory", {}).get("item", [])
            xml_name = None
            for item in items:
                fname = item.get("name", "")
                # The ownership XML (not the XSLT-rendered form)
                if fname.endswith(".xml") and "form" not in fname.lower():
                    xml_name = fname
                    break

            if not xml_name:
                continue

            xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{xml_name}"
            xml = _fetch_text(xml_url)
            if not xml:
                continue

            form_count += 1

            # Parse XML fields
            owner = _xml_tag(xml, "rptOwnerName")
            title = _xml_tag(xml, "officerTitle")
            is_dir = _xml_tag(xml, "isDirector") == "1"
            is_off = _xml_tag(xml, "isOfficer") == "1"

            # Get shares owned after transaction
            shares_matches = re.findall(
                r"<sharesOwnedFollowingTransaction>.*?<value>([^<]+)</value>",
                xml, re.DOTALL,
            )
            shares_after = shares_matches[-1] if shares_matches else None

            if owner and owner.strip():
                clean_name = _normalize_form4_name(owner.strip())
                # Keep the most recent data per person
                if clean_name not in officers or dates[i] > officers[clean_name].get("filing_date", ""):
                    officers[clean_name] = {
                        "name": clean_name,
                        "title": html.unescape(title) if title else ("Director" if is_dir else ""),
                        "is_officer": is_off,
                        "is_director": is_dir,
                        "shares_after": shares_after,
                        "filing_date": dates[i],
                        "form_type": forms[i],
                        "cik": cik,
                    }

        except Exception as e:
            log.debug("Failed to parse Form %s for CIK %s: %s", forms[i], cik, e)

    return list(officers.values())


# Common multi-token surname prefixes (case-insensitive). When present at
# the start of a Form 4 name, treat the next token as part of the surname:
# "ST JOHN FRANK A" → surname = "St. John", first = "Frank", → "Frank St. John"
_MULTI_TOKEN_SURNAME_PREFIXES = {
    "st", "st.", "saint",
    "van", "von", "der", "den", "de", "del", "della", "di",
    "la", "le", "el",
    "mc", "mac", "o'", "o",
    "al", "el",
    "ben", "bin", "abu", "ibn",
}

_NAME_SUFFIXES_TO_DROP = {"JR", "JR.", "SR", "SR.", "II", "III", "IV", "V"}


def _normalize_form4_name(name: str) -> str:
    """Reorder SEC Form 4 'LASTNAME FIRSTNAME M JR' → 'Firstname Lastname'.

    SEC filings put the reporter's name in surname-first order with optional
    middle initial and generational suffix. Downstream sources (FEC, courts,
    Congress) expect natural order. This function:

      1. HTML-unescape (for & and other entities passing through).
      2. Drop generational suffixes (JR/SR/III/etc).
      3. Detect multi-token surname prefixes (St., Van, De La, Mc, Bin, ...) —
         keep them attached to the surname token.
      4. Reorder LAST FIRST [M] → First Last (or First M Last with initial).
      5. Title-case the result.

    Examples:
      'DOE JAMES D JR'   → 'James D Doe'
      'St John Frank A'      → 'Frank St. John'
      'Roe Vicki A.'      → 'Vicki Roe'
      'VAN BERGEN ROBERT'    → 'Robert Van Bergen'
      'Joseph Danforth'       → 'Joseph Danforth'  (already natural order)
    """
    import html as _html
    import re
    if not name:
        return name

    s = _html.unescape(name).strip()
    parts = s.split()
    if not parts:
        return s

    # Drop trailing generational suffixes
    while parts and parts[-1].upper().rstrip(".") in _NAME_SUFFIXES_TO_DROP:
        parts.pop()
    if len(parts) < 2:
        return " ".join(p.title() for p in parts)

    # Detect: is first token already a natural-order first name, or surname-first?
    # Heuristic: if the LAST token looks like a real surname (capitalized word),
    # AND the first token is also capitalized, we can't tell from case alone in
    # mixed-case input. Use these signals:
    #   - All-caps input → almost certainly Form 4 LAST-FIRST format.
    #   - Mixed-case with first token containing a "St"/"Van"/"De" prefix → LAST-FIRST.
    #   - Mixed-case with last token being a single initial / period → LAST-FIRST.
    is_all_caps = s == s.upper()
    last_is_initial = re.match(r"^[A-Z]\.?$", parts[-1]) is not None
    first_token_is_prefix = parts[0].lower().rstrip(".") in _MULTI_TOKEN_SURNAME_PREFIXES

    surname_first = is_all_caps or last_is_initial or first_token_is_prefix

    if not surname_first:
        # Already natural order — just title-case.
        return " ".join(p.title() if p.lower() not in _MULTI_TOKEN_SURNAME_PREFIXES else p.title() for p in parts)

    # Surname-first: figure out where the surname ends.
    surname_tokens = [parts[0]]
    rest_start = 1
    if first_token_is_prefix and len(parts) >= 3:
        # "ST JOHN FRANK A" → surname = ST JOHN, rest = FRANK A
        surname_tokens.append(parts[1])
        rest_start = 2

    rest = parts[rest_start:]
    if not rest:
        return " ".join(p.title() for p in surname_tokens)

    # rest = [first, [middle/initial...]] — middle initials become single letter.
    # Drop any trailing single-letter initial (it's noise for downstream search).
    while rest and re.match(r"^[A-Za-z]\.?$", rest[-1]):
        rest.pop()
    if not rest:
        # Only initials in rest — fall back to "Surname" alone (rare).
        return " ".join(p.title() for p in surname_tokens)

    # Title-case surname; "St" → "St." for readability.
    surname = " ".join(
        (p.title() + ".") if p.lower().rstrip(".") == "st" else p.title()
        for p in surname_tokens
    )
    given = " ".join(p.title() for p in rest)
    return f"{given} {surname}"


def _fetch_text(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch raw text from a URL."""
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _UA)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug("Text fetch failed for %s: %s", url, e)
        return None


def _xml_tag(xml: str, tag: str) -> Optional[str]:
    """Extract first occurrence of an XML tag value, HTML-decoded."""
    import re, html
    match = re.search(f"<{tag}>([^<]+)</{tag}>", xml)
    if not match:
        return None
    return html.unescape(match.group(1)).strip()


# ─── SEC Document Parser: DEF 14A Proxy Statement ────────────────────────────

def parse_proxy_statement(document_url: str, company_name: str = "") -> dict:
    """Download and parse a DEF 14A proxy statement.

    Two-stage extraction:
      1. HTML → text → section splitter (regex, no LLM)
      2. Directors/officers section → LLM structured extraction

    Returns dict with board_members, executive_officers, compensation.
    Falls back to regex-only extraction if LLM is unavailable.
    """
    import re
    import html as html_mod

    result = {
        "board_members": [],
        "executive_officers": [],
        "compensation": [],
        "status": "complete",
        "method": "regex",  # upgraded to "llm" if LLM succeeds
    }

    # Fetch the HTML document
    raw_html = _fetch_text(document_url, timeout=30)
    if not raw_html:
        result["status"] = "fetch_failed"
        return result

    # Strip HTML → plain text
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text)

    # ── Stage 1: Section splitting (regex, no LLM) ──────────────────

    # Find the directors section
    directors_section = ""
    for marker in [
        r"ELECTION OF DIRECTORS",
        r"PROPOSAL 1.*?ELECTION",
        r"Director Nominees at a Glance",
        r"Continuing Directors",
    ]:
        match = re.search(marker, text, re.IGNORECASE)
        if match:
            # Grab text from this marker to the next PROPOSAL or TABLE OF CONTENTS
            start = match.start()
            end_match = re.search(
                r"PROPOSAL\s+[2-9]|EXECUTIVE COMPENSATION|TABLE OF CONTENTS",
                text[start + 100:], re.IGNORECASE,
            )
            end = start + 100 + end_match.start() if end_match else start + 8000
            directors_section = text[start:end]
            break

    # Find compensation section
    comp_section = ""
    comp_match = re.search(r"EXECUTIVE COMPENSATION|COMPENSATION DISCUSSION", text, re.IGNORECASE)
    if comp_match:
        start = comp_match.start()
        end_match = re.search(
            r"PROPOSAL\s+[3-9]|SECURITY OWNERSHIP|CERTAIN RELATIONSHIPS",
            text[start + 100:], re.IGNORECASE,
        )
        end = start + 100 + end_match.start() if end_match else start + 6000
        comp_section = text[start:end]

    # ── Stage 2: Try LLM extraction if available ─────────────────────

    llm_result = None
    if directors_section and len(directors_section) > 200:
        llm_result = _llm_extract_directors(directors_section[:6000], company_name)

    if llm_result and llm_result.get("persons"):
        result["board_members"] = llm_result["persons"]
        result["method"] = "llm"
    else:
        # Fallback: regex extraction for common patterns
        # "Name , age, title since year"
        for match in re.finditer(
            r"([A-Z][a-z]+(?:\s+[A-Z]\.?\s*)?[A-Z][a-z]+)\s*,?\s*(\d{2,3})\s*,?\s*"
            r"(?:has been|served as|is|was)\s+(?:a\s+)?([^.]{10,80}?)(?:since|from|in)\s+(\d{4})",
            directors_section,
        ):
            result["board_members"].append({
                "full_name": match.group(1).strip(),
                "age": int(match.group(2)),
                "title": match.group(3).strip(),
                "since": match.group(4),
                "source": "regex_extraction",
            })

    return result


def _llm_extract_directors(section_text: str, company_name: str) -> Optional[dict]:
    """Use the configured LLM to extract structured director/officer data.

    Returns None if LLM is not available or extraction fails.
    """
    try:
        from core.extraction.llm.factory import get_default_client
        client = get_default_client()
        if not client:
            return None

        prompt = f"""Extract all persons mentioned as directors, officers, or nominees from this SEC proxy statement section.

Company: {company_name}

Return ONLY valid JSON matching this exact schema:
{{
  "persons": [
    {{
      "full_name": "string",
      "roles": ["director", "officer", "nominee", "chairman"],
      "title": "string or null (e.g. CEO, CFO, Independent Director)",
      "age": null or integer,
      "since": "year string or null",
      "committees": ["string"],
      "background": "one sentence bio or null"
    }}
  ]
}}

If a field is not mentioned, use null. Do not invent data. Only extract persons explicitly named in the text.

SECTION TEXT:
---
{section_text}
---"""

        result = client.complete_json(
            prompt,
            max_tokens=2048,
            temperature=0.0,
        )

        if result.success and result.text:
            # Parse the JSON from LLM response
            import json as json_mod
            text = result.text.strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                text = re.sub(r"```(?:json)?", "", text).strip()
            parsed = json_mod.loads(text)
            if isinstance(parsed, dict) and "persons" in parsed:
                # Validate: every name must appear in the source text
                validated = []
                for p in parsed["persons"]:
                    name = p.get("full_name", "")
                    # Check last name appears in source
                    last_name = name.split()[-1] if name.split() else ""
                    if last_name and last_name.lower() in section_text.lower():
                        p["source"] = "llm_extraction"
                        validated.append(p)
                return {"persons": validated}

    except Exception as e:
        log.debug("LLM director extraction failed: %s", e)

    return None


# ─── FEC (Federal Election Commission) ───────────────────────────────────────

def investigate_fec(name: str, entity_type: str = "person") -> dict:
    """Search FEC for campaign committees, candidates, and donation patterns.

    With DATA_GOV_API_KEY: 1000/hr ceiling, donor money-trail (step 3) fires,
    per-page cap lifts from 5 to 20. Without: candidate+committee search only,
    DEMO_KEY rate (~30/hr shared)."""
    result = {"findings": [], "discovered_entities": [], "source": "fec",
              "status": "complete", "raw_count": 0}

    api_key = get_credential("DATA_GOV_API_KEY") or "DEMO_KEY"
    keyed = api_key != "DEMO_KEY"
    page_cap = source_page_cap("fec")
    base = "https://api.open.fec.gov/v1"

    # Step 1: Search candidates
    seed_candidate_ids: set[str] = set()   # THIS entity's own FEC candidate_id(s)
    candidates = _fetch_json(
        f"{base}/candidates/?api_key={api_key}&name={urllib.parse.quote_plus(name)}&per_page={page_cap}",
        source="fec",
    )
    if candidates:
        for c in candidates.get("results", [])[:page_cap]:
            if c.get("candidate_id"):
                seed_candidate_ids.add(str(c.get("candidate_id")))
            result["findings"].append({
                "type": "candidate",
                "title": f"FEC Candidate: {c.get('name')}",
                "data": {
                    "candidate_name": c.get("name"),
                    "candidate_id": c.get("candidate_id"),
                    "party": c.get("party_full"),
                    "office": c.get("office_full"),
                    "state": c.get("state"),
                    "election_years": c.get("election_years", []),
                    "status": c.get("candidate_status"),
                    "incumbent_challenge": c.get("incumbent_challenge_full"),
                },
                "confidence": 0.95,
            })
            result["discovered_entities"].append({
                "name": c.get("name", name), "type": "person",
                "relationship": "political_candidate",
                "detail": {"fec_id": c.get("candidate_id"), "party": c.get("party_full")},
            })

    # Step 2: Search committees
    committee_cap = max(10, page_cap)
    committees = _fetch_json(
        f"{base}/committees/?api_key={api_key}&q={urllib.parse.quote_plus(name)}&per_page={committee_cap}",
        source="fec",
    )
    if committees:
        for c in committees.get("results", [])[:committee_cap]:
            result["findings"].append({
                "type": "committee",
                "title": f"FEC Committee: {c.get('name')}",
                "data": {
                    "committee_name": c.get("name"),
                    "committee_id": c.get("committee_id"),
                    "committee_type": c.get("committee_type_full"),
                    "party": c.get("party_full"),
                    "designation": c.get("designation_full"),
                    "treasurer_name": c.get("treasurer_name"),
                    "organization_type": c.get("organization_type_full"),
                    "state": c.get("state"),
                    "filing_frequency": c.get("filing_frequency"),
                    "first_file_date": c.get("first_file_date"),
                    "last_file_date": c.get("last_file_date"),
                },
                "confidence": 0.90,
            })

            # Discover the COMMITTEE ITSELF as its own organization entity, keyed by
            # committee_id — so it collapses by that identifier (never merges with a
            # different committee, never fragments) and links to the person as a
            # distinct org, instead of being stranded as a finding that the persona
            # panel mislabels "also called <person>".
            if c.get("committee_id") and c.get("name"):
                # "Is this the entity's OWN committee?" — decided by FEC facts, not
                # by the name. Designation P (principal campaign committee) / A
                # (authorized by a candidate) means authorized by SOME candidate;
                # to be authorized by THIS entity the committee's candidate_ids
                # must contain the entity's own candidate_id. Otherwise it is an
                # INDEPENDENT committee that merely names them (e.g. "PAC TO DEFEAT
                # …" is the principal committee of a DIFFERENT candidate). We never
                # infer support/oppose from the name.
                _desig = (c.get("designation") or "").strip().upper()
                _cand_ids = {str(x) for x in (c.get("candidate_ids") or []) if x}
                _authorized_by_seed = bool(seed_candidate_ids & _cand_ids)
                affiliation = ("authorized"
                               if (_desig in ("P", "A") and _authorized_by_seed)
                               else "independent")
                result["discovered_entities"].append({
                    "name": c.get("name"), "type": "organization",
                    "relationship": "associated_committee",
                    "detail": {
                        "committee_id": c.get("committee_id"),
                        "committee_type": c.get("committee_type_full"),
                        "designation": _desig or None,
                        "designation_full": c.get("designation_full"),
                        "affiliation": affiliation,
                        "candidate_ids": c.get("candidate_ids"),
                        "party": c.get("party_full"),
                        "state": c.get("state"),
                    },
                })

            # Discover treasurer as entity
            treasurer = c.get("treasurer_name")
            if treasurer and treasurer.lower() != name.lower():
                result["discovered_entities"].append({
                    "name": treasurer, "type": "person",
                    "relationship": "treasurer_of",
                    "detail": {"committee": c.get("name"), "committee_id": c.get("committee_id")},
                })

    # Step 3: Get top individual contributions (Schedule A) for primary committee
    # This is the money trail — who donated, how much, their employer.
    # Only runs with a real DATA_GOV_API_KEY (DEMO_KEY rate-limit makes the
    # extra calls unaffordable).
    if keyed and candidates and candidates.get("results"):
        primary_candidate = candidates["results"][0]
        candidate_id = primary_candidate.get("candidate_id")
        if candidate_id:
            # Find the principal campaign committee
            principal = _fetch_json(
                f"{base}/candidates/{candidate_id}/committees/"
                f"?api_key={api_key}&designation=P&per_page=1",
                source="fec",
            )
            principal_id = None
            if principal and principal.get("results"):
                principal_id = principal["results"][0].get("committee_id")

            if principal_id:
                donations = _fetch_json(
                    f"{base}/schedules/schedule_a/"
                    f"?api_key={api_key}&committee_id={principal_id}"
                    f"&sort=-contribution_receipt_amount&per_page=20",
                    source="fec",
                )
                if donations:
                    for d in donations.get("results", [])[:20]:
                        donor_name = d.get("contributor_name", "")
                        amount = d.get("contribution_receipt_amount", 0)
                        if not donor_name or amount < 100:
                            continue
                        result["findings"].append({
                            "type": "donation",
                            "title": f"Donation: {donor_name} → ${amount:,.0f}",
                            "data": {
                                "donor_name": donor_name,
                                "amount": amount,
                                "employer": d.get("contributor_employer"),
                                "occupation": d.get("contributor_occupation"),
                                "city": d.get("contributor_city"),
                                "state": d.get("contributor_state"),
                                "date": d.get("contribution_receipt_date"),
                                "committee": d.get("committee", {}).get("name", ""),
                                "memo": d.get("memo_text"),
                            },
                            "confidence": 0.95,
                        })
                        # Discover major donors as entities
                        if amount >= 5000 and donor_name:
                            result["discovered_entities"].append({
                                "name": donor_name, "type": "person",
                                "relationship": "donor_to",
                                "detail": {"amount": amount,
                                           "employer": d.get("contributor_employer"),
                                           "committee": d.get("committee", {}).get("name", "")},
                            })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── USAspending (Government Contracts) ─────���────────────────────────────���───

def investigate_usaspending(name: str, entity_type: str = "organization") -> dict:
    """Search USAspending for government contracts awarded to the entity."""
    result = {"findings": [], "discovered_entities": [], "source": "usaspending",
              "status": "complete", "raw_count": 0}

    data = _post_json("https://api.usaspending.gov/api/v2/search/spending_by_award/", {
        "filters": {
            "recipient_search_text": [name],
            "award_type_codes": ["A", "B", "C", "D"],  # contracts
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency",
                    "Description", "Start Date", "End Date", "recipient_id",
                    "Awarding Sub Agency", "Award Type"],
        "limit": 25, "page": 1, "sort": "Award Amount", "order": "desc",
    }, source="usaspending")

    if data:
        total = data.get("page_metadata", {}).get("total", 0)
        for r in data.get("results", [])[:25]:
            recipient = r.get("Recipient Name", "")
            # Filter out false matches (e.g., a longer name that merely contains the search term)
            if name.lower() not in recipient.lower():
                continue
            result["findings"].append({
                "type": "contract",
                "title": f"Contract: {recipient} — ${r.get('Award Amount', 0):,.0f}",
                "data": {
                    "award_id": r.get("Award ID"),
                    "recipient_name": recipient,
                    "award_amount": r.get("Award Amount"),
                    "awarding_agency": r.get("Awarding Agency"),
                    "awarding_sub_agency": r.get("Awarding Sub Agency"),
                    "description": r.get("Description"),
                    "start_date": r.get("Start Date"),
                    "end_date": r.get("End Date"),
                    "award_type": r.get("Award Type"),
                },
                "confidence": 0.90,
            })

            # Discover the awarding agency
            agency = r.get("Awarding Agency")
            if agency:
                result["discovered_entities"].append({
                    "name": agency, "type": "organization",
                    "relationship": "awarded_contract_from",
                    "detail": {"award_amount": r.get("Award Amount"),
                               "award_id": r.get("Award ID")},
                })

    # Also search grants
    grants = _post_json("https://api.usaspending.gov/api/v2/search/spending_by_award/", {
        "filters": {
            "recipient_search_text": [name],
            "award_type_codes": ["02", "03", "04", "05"],  # grants
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency",
                    "Description", "Start Date"],
        "limit": 10, "page": 1, "sort": "Award Amount", "order": "desc",
    }, source="usaspending")

    if grants:
        for r in grants.get("results", [])[:10]:
            recipient = r.get("Recipient Name", "")
            if name.lower() not in recipient.lower():
                continue
            result["findings"].append({
                "type": "grant",
                "title": f"Grant: {recipient} — ${r.get('Award Amount', 0):,.0f}",
                "data": {
                    "award_id": r.get("Award ID"),
                    "recipient_name": recipient,
                    "award_amount": r.get("Award Amount"),
                    "awarding_agency": r.get("Awarding Agency"),
                    "description": r.get("Description"),
                    "start_date": r.get("Start Date"),
                },
                "confidence": 0.90,
            })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── ProPublica Nonprofits (IRS 990) ─���───────────────────────────────────────

def investigate_nonprofits(name: str, entity_type: str = "person") -> dict:
    """Search ProPublica for nonprofit organizations and IRS 990 filings."""
    result = {"findings": [], "discovered_entities": [], "source": "propublica_nonprofits",
              "status": "complete", "raw_count": 0}

    search = _fetch_json(
        f"https://projects.propublica.org/nonprofits/api/v2/search.json?q={urllib.parse.quote_plus(name)}",
        source="propublica",
    )
    if not search:
        result["status"] = "error"
        return result

    for org in search.get("organizations", [])[:10]:
        ein = org.get("ein")
        org_name = org.get("name", "")

        result["findings"].append({
            "type": "nonprofit",
            "title": f"Nonprofit: {org_name}",
            "data": {
                "name": org_name,
                "ein": ein,
                "city": org.get("city"),
                "state": org.get("state"),
                "income": org.get("income_amount"),
                "asset_amount": org.get("asset_amount"),
                "ntee_code": org.get("ntee_code"),
            },
            "confidence": 0.90,
        })

        result["discovered_entities"].append({
            "name": org_name, "type": "organization",
            "relationship": "associated_nonprofit",
            "detail": {"ein": ein, "city": org.get("city"), "state": org.get("state")},
        })

        # Get full 990 data for each org
        if ein:
            full = _fetch_json(
                f"https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json",
                source="propublica",
            )
            if full:
                filings = full.get("filings_with_data", [])[:5]
                for f in filings:
                    result["findings"].append({
                        "type": "990_filing",
                        "title": f"990 Filing: {org_name} ({f.get('tax_prd', '')})",
                        "data": {
                            "organization": org_name,
                            "ein": ein,
                            "tax_period": f.get("tax_prd"),
                            "total_revenue": f.get("totrevenue"),
                            "total_expenses": f.get("totfuncexpns"),
                            "total_assets": f.get("totassetsend"),
                            "total_liabilities": f.get("totliabend"),
                            "net_assets": f.get("totnetassetend"),
                            "tax_period_begin": f.get("tax_prd_yr"),
                            "form_type": f.get("formtype"),
                        },
                        "confidence": 0.95,
                    })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── CourtListener (Federal Courts) ──────���───────────────────────────────────

def investigate_courts(name: str, entity_type: str = "person") -> dict:
    """Search CourtListener for federal court cases."""
    result = {"findings": [], "discovered_entities": [], "source": "courtlistener",
              "status": "complete", "raw_count": 0}

    encoded = urllib.parse.quote_plus(f'"{name}"')
    data = _fetch_json(
        f"https://www.courtlistener.com/api/rest/v4/search/?q={encoded}&type=r&order_by=score+desc",
        source="courtlistener",
    )

    if not data:
        result["status"] = "error"
        return result

    total = data.get("count", 0)
    result["findings"].append({
        "type": "court_summary",
        "title": f"Federal Court Cases: {total} results for {name}",
        "data": {"total_cases": total, "search_term": name},
        "confidence": 0.85,
    })

    for case in data.get("results", [])[:15]:
        case_name = case.get("caseName", case.get("case_name", "Unknown"))
        court = case.get("court", "")
        date_filed = case.get("dateFiled", case.get("date_filed", ""))

        # Quality filter — CourtListener returns fuzzy-search results
        # whose case_name doesn't actually contain the search term
        # (Sam Rowan search returns "Globex Industrial Company" etc.).
        # Drop these at fetch time so they never pollute findings.
        # require_surname=False so single-name searches and "v." cases
        # ("Rowan v. Doe") still pass.
        if not _text_mentions_seed(name, case_name, require_surname=False):
            continue

        # Build the canonical CourtListener URL so the dossier UI can render a
        # clickable link to the actual case page (the disambiguation gap:
        # 5 results for one name, only 1 is the intended person —
        # user needs to click into each case to verify).
        absolute_url = case.get("absolute_url") or case.get("docket_absolute_url") or ""
        source_url = (
            f"https://www.courtlistener.com{absolute_url}"
            if absolute_url else
            f"https://www.courtlistener.com/?type=r&q={urllib.parse.quote_plus(case_name)}"
        )

        result["findings"].append({
            "type": "court_case",
            "title": f"Case: {case_name[:100]}",
            "data": {
                "case_name": case_name,
                "court": court,
                "date_filed": date_filed,
                "docket_number": case.get("docketNumber", case.get("docket_number", "")),
                "status": case.get("status", ""),
                "cause": case.get("cause", ""),
                "nature_of_suit": case.get("suitNature", case.get("nature_of_suit", "")),
                "jury_demand": case.get("juryDemand", case.get("jury_demand", "")),
                "cluster_id": case.get("cluster_id") or case.get("id"),
                "docket_id": case.get("docket_id"),
                "source_url": source_url,
            },
            "confidence": 0.85,
        })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── FDIC BankFind ────────────────────────────────────────────────────────────

def investigate_fdic(name: str, entity_type: str = "organization") -> dict:
    """Search FDIC for banking institutions."""
    result = {"findings": [], "discovered_entities": [], "source": "fdic",
              "status": "complete", "raw_count": 0}

    data = _fetch_json(
        f"https://banks.data.fdic.gov/api/financials?search={urllib.parse.quote_plus(name)}"
        f"&fields=INSTNAME,CERT,CITY,STALP,ASSET,DEP,NETINC,ROA,ROE,OFFICES&limit=10",
        source="fdic",
    )

    if data:
        for item in data.get("data", [])[:10]:
            d = item.get("data", {})
            inst_name = d.get("INSTNAME", "")
            if name.lower() not in inst_name.lower():
                continue
            result["findings"].append({
                "type": "bank",
                "title": f"Bank: {inst_name}",
                "data": {
                    "institution_name": inst_name,
                    "cert": d.get("CERT"),
                    "city": d.get("CITY"),
                    "state": d.get("STALP"),
                    "total_assets": d.get("ASSET"),
                    "total_deposits": d.get("DEP"),
                    "net_income": d.get("NETINC"),
                    "roa": d.get("ROA"),
                    "roe": d.get("ROE"),
                    "offices": d.get("OFFICES"),
                },
                "confidence": 0.90,
            })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── Google Books ─────────────────────────────────────────────────────────────

def investigate_google_books(name: str, entity_type: str = "person") -> dict:
    """Search Google Books for publications by or about the entity.

    No API key required for basic queries. Rate limit ~100/day without key.
    Finds biographies, corporate histories, authored works.
    """
    result = {"findings": [], "discovered_entities": [], "source": "google_books",
              "status": "complete", "raw_count": 0}

    base = "https://www.googleapis.com/books/v1/volumes"

    # Search for books ABOUT the entity
    about = _fetch_json(
        f"{base}?q={urllib.parse.quote_plus(name)}&maxResults=10&orderBy=relevance",
        source="google_books",
    )
    if about:
        for item in about.get("items", [])[:10]:
            vol = item.get("volumeInfo", {})
            book_title = vol.get("title", "")
            authors = vol.get("authors", [])
            description = vol.get("description", "")

            # Skip if not relevant (name doesn't appear in title or description)
            name_parts = name.lower().split()
            last_name = name_parts[-1] if name_parts else ""
            if last_name not in book_title.lower() and last_name not in description.lower():
                continue

            result["findings"].append({
                "type": "book",
                "title": f"Book: {book_title}",
                "data": {
                    "book_title": book_title,
                    "subtitle": vol.get("subtitle"),
                    "authors": authors,
                    "publisher": vol.get("publisher"),
                    "published_date": vol.get("publishedDate"),
                    "description": description[:500],
                    "page_count": vol.get("pageCount"),
                    "categories": vol.get("categories", []),
                    "isbn": next(
                        (i.get("identifier") for i in vol.get("industryIdentifiers", [])
                         if i.get("type") == "ISBN_13"), None,
                    ),
                    "thumbnail": vol.get("imageLinks", {}).get("thumbnail"),
                    "preview_link": vol.get("previewLink"),
                    "average_rating": vol.get("averageRating"),
                },
                "confidence": 0.70,
            })

            # Discover authors as entities
            for author in authors:
                if author.lower() != name.lower():
                    result["discovered_entities"].append({
                        "name": author, "type": "person",
                        "relationship": "author_of",
                        "detail": {"book": book_title, "published": vol.get("publishedDate")},
                    })

    # If person, also search for books BY them
    if entity_type == "person":
        by_author = _fetch_json(
            f"{base}?q=inauthor:{urllib.parse.quote_plus(name)}&maxResults=5",
            source="google_books",
        )
        if by_author:
            for item in by_author.get("items", [])[:5]:
                vol = item.get("volumeInfo", {})
                book_title = vol.get("title", "")
                # Skip duplicates
                if any(f.get("data", {}).get("book_title") == book_title for f in result["findings"]):
                    continue
                result["findings"].append({
                    "type": "authored_book",
                    "title": f"Authored: {book_title}",
                    "data": {
                        "book_title": book_title,
                        "authors": vol.get("authors", []),
                        "publisher": vol.get("publisher"),
                        "published_date": vol.get("publishedDate"),
                        "description": (vol.get("description") or "")[:500],
                        "page_count": vol.get("pageCount"),
                        "categories": vol.get("categories", []),
                        "isbn": next(
                            (i.get("identifier") for i in vol.get("industryIdentifiers", [])
                             if i.get("type") == "ISBN_13"), None,
                        ),
                        "thumbnail": vol.get("imageLinks", {}).get("thumbnail"),
                    },
                    "confidence": 0.85,
                })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── FDA (openFDA) ────────────────────────────────────────────────────────────

def investigate_fda(name: str, entity_type: str = "organization") -> dict:
    """Search FDA for drug/device recalls, enforcement actions, and approvals.

    Uses the openFDA API — free, no key required (api.data.gov key increases rate).
    """
    result = {"findings": [], "discovered_entities": [], "source": "fda",
              "status": "complete", "raw_count": 0}

    api_key = get_credential("DATA_GOV_API_KEY")
    key_param = f"&api_key={api_key}" if api_key else ""

    # Drug recalls/enforcement by company
    recalls = _fetch_json(
        f"https://api.fda.gov/drug/enforcement.json"
        f"?search=recalling_firm:\"{urllib.parse.quote_plus(name)}\""
        f"&limit=10&sort=report_date:desc{key_param}",
    )
    if recalls and recalls.get("results"):
        for r in recalls["results"][:10]:
            result["findings"].append({
                "type": "fda_recall",
                "title": f"FDA Recall: {r.get('product_description', '')[:80]}",
                "data": {
                    "recalling_firm": r.get("recalling_firm"),
                    "product_description": r.get("product_description"),
                    "reason": r.get("reason_for_recall"),
                    "classification": r.get("classification"),
                    "status": r.get("status"),
                    "recall_date": r.get("recall_initiation_date"),
                    "report_date": r.get("report_date"),
                    "city": r.get("city"),
                    "state": r.get("state"),
                    "distribution": r.get("distribution_pattern"),
                },
                "confidence": 0.95,
            })

    # Device recalls
    device_recalls = _fetch_json(
        f"https://api.fda.gov/device/enforcement.json"
        f"?search=recalling_firm:\"{urllib.parse.quote_plus(name)}\""
        f"&limit=5&sort=report_date:desc{key_param}",
    )
    if device_recalls and device_recalls.get("results"):
        for r in device_recalls["results"][:5]:
            result["findings"].append({
                "type": "fda_device_recall",
                "title": f"Device Recall: {r.get('product_description', '')[:80]}",
                "data": {
                    "recalling_firm": r.get("recalling_firm"),
                    "product_description": r.get("product_description"),
                    "reason": r.get("reason_for_recall"),
                    "classification": r.get("classification"),
                    "recall_date": r.get("recall_initiation_date"),
                },
                "confidence": 0.95,
            })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── EPA ECHO (Environmental Compliance) ──────────────────────────────────────

def investigate_epa(name: str, entity_type: str = "organization") -> dict:
    """Search EPA ECHO for environmental violations, facilities, and enforcement.

    Free API, no key needed.
    """
    result = {"findings": [], "discovered_entities": [], "source": "epa_echo",
              "status": "complete", "raw_count": 0}

    # Step 1: Search facilities by name (returns QueryID)
    search = _fetch_json(
        f"https://echodata.epa.gov/echo/cwa_rest_services.get_facilities"
        f"?p_fn={urllib.parse.quote_plus(name)}&output=JSON",
        source="epa",
    )
    qid = search.get("Results", {}).get("QueryID") if search else None
    total_rows = search.get("Results", {}).get("QueryRows", 0) if search else 0

    if qid and total_rows > 0:
        # Step 2: Fetch actual facility data using QueryID
        fac_data = _fetch_json(
            f"https://echodata.epa.gov/echo/cwa_rest_services.get_qid"
            f"?qid={qid}&output=JSON&pageno=1&pagesize=15",
            source="epa",
        )
        facilities = fac_data.get("Results", {}).get("Facilities", []) if fac_data else []

        for fac in facilities[:15]:
            fac_name = fac.get("CWPName", fac.get("FacName", ""))
            lat = fac.get("FacLat", fac.get("CWPLat"))
            lon = fac.get("FacLong", fac.get("CWPLong"))
            street = fac.get("CWPStreet", fac.get("FacStreet", ""))
            city = fac.get("CWPCity", fac.get("FacCity", ""))
            state = fac.get("CWPState", fac.get("FacState", ""))
            zipcode = fac.get("CWPZip", fac.get("FacZip", ""))

            result["findings"].append({
                "type": "epa_facility",
                "title": f"EPA Facility: {fac_name}",
                "data": {
                    "facility_name": fac_name,
                    "registry_id": fac.get("RegistryID", fac.get("CWPPermitNmbr", "")),
                    "address": street,
                    "city": city,
                    "state": state,
                    "zip": zipcode,
                    "latitude": lat,
                    "longitude": lon,
                    "compliance_status": fac.get("CWPCSStatus", fac.get("CWPSNCStatus", "")),
                    "total_facilities_found": total_rows,
                },
                "confidence": 0.90,
            })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── OFAC Sanctions (Treasury SDN List) ───────────────────────────────────────

def investigate_ofac(name: str, entity_type: str = "person") -> dict:
    """Search OFAC Specially Designated Nationals list.

    Local-first: if the `ofac_sdn` refdata pack is synced and fresh, query
    the local PG mirror (sub-millisecond, no network). Otherwise fall back
    to the previous behavior — download the SDN CSV from Treasury and search
    it line-by-line. Live fallback is itself bounded by a downstream
    OpenSanctions check if both fail.
    """
    result = {"findings": [], "discovered_entities": [], "source": "ofac",
              "status": "complete", "raw_count": 0}

    # ─── Local-first via refdata pack ────────────────────────────────────
    try:
        from core.refdata import manager as refdata_manager
        from core.refdata.packs import ofac as ofac_pack
        if refdata_manager.is_fresh("ofac_sdn"):
            hits = ofac_pack.lookup(name, limit=5)
            if hits:
                for m in hits:
                    result["findings"].append({
                        "type": "sanctions_match",
                        "title": f"OFAC SDN: {m['name']} ({m['score']:.0%})",
                        "data": {
                            "matched_name": m["name"],
                            "match_score": m["score"],
                            "sdn_type": m["type"],
                            "program": m["program"],
                            "remarks": m["remarks"],
                            "source_list": "OFAC SDN",
                            "via": "refdata_local",
                            "uid": m["uid"],
                        },
                        "confidence": 0.95 if m["score"] > 0.8 else 0.70,
                    })
            else:
                result["findings"].append({
                    "type": "sanctions_clear",
                    "title": f"OFAC: {name} — not on SDN list",
                    "data": {"sanctioned": False, "via": "refdata_local"},
                    "confidence": 0.90,
                })
            result["raw_count"] = len(result["findings"])
            return result
    except Exception as e:
        log.debug("ofac refdata path failed for %s: %s — falling through to live", name, e)

    # ─── Live fallback (legacy path) ─────────────────────────────────────
    # Download OFAC SDN CSV and search locally (always works, no API dependency)
    sdn_csv = _fetch_text("https://www.treasury.gov/ofac/downloads/sdn.csv", timeout=20)

    if sdn_csv:
        name_upper = name.upper()
        name_parts = name_upper.split()
        last_name = name_parts[-1] if name_parts else name_upper

        matches = []
        for line in sdn_csv.split("\n"):
            if not line.strip():
                continue
            fields = line.split('","')
            if len(fields) < 4:
                continue
            sdn_name = fields[1].strip('"') if len(fields) > 1 else ""
            sdn_type = fields[2].strip('"') if len(fields) > 2 else ""
            sdn_program = fields[3].strip('"') if len(fields) > 3 else ""
            sdn_remarks = fields[4].strip('"') if len(fields) > 4 else ""

            # Match: last name appears in SDN entry name
            if last_name in sdn_name.upper():
                # Score based on how much of the name matches
                score = 0.5
                if name_upper in sdn_name.upper():
                    score = 0.95
                elif all(p in sdn_name.upper() for p in name_parts):
                    score = 0.85

                matches.append({
                    "name": sdn_name,
                    "type": sdn_type,
                    "program": sdn_program,
                    "remarks": sdn_remarks,
                    "score": score,
                })

        if matches:
            matches.sort(key=lambda x: x["score"], reverse=True)
            for m in matches[:5]:
                result["findings"].append({
                    "type": "sanctions_match",
                    "title": f"OFAC SDN: {m['name']} ({m['score']:.0%})",
                    "data": {
                        "matched_name": m["name"],
                        "match_score": m["score"],
                        "sdn_type": m["type"],
                        "program": m["program"],
                        "remarks": m["remarks"],
                        "source_list": "OFAC SDN",
                    },
                    "confidence": 0.95 if m["score"] > 0.8 else 0.70,
                })
        else:
            result["findings"].append({
                "type": "sanctions_clear",
                "title": f"OFAC: {name} — not on SDN list",
                "data": {"sanctioned": False, "sdn_entries_checked": len(sdn_csv.split("\\n"))},
                "confidence": 0.85,
            })

    # Fallback: try existing sanctions_check (uses OpenSanctions if httpx available)
    if not result["findings"]:
        try:
            from core.research.sources import sanctions_check
            schema = "Person" if entity_type == "person" else "Organization"
            sanc = sanctions_check(name, schema)
            if sanc and sanc.get("sanctioned"):
                for m in sanc.get("matches", []):
                    result["findings"].append({
                        "type": "sanctions_match",
                        "title": f"Sanctions: {m.get('name', name)} (score {m.get('score', 0):.0%})",
                        "data": {
                            "matched_name": m.get("name"),
                            "match_score": m.get("score"),
                            "datasets": m.get("datasets", []),
                        },
                        "confidence": 0.90,
                    })
        except Exception:
            pass

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── Congress.gov (Legislative Records) ───────────────────────────────────────

def investigate_congress(name: str, entity_type: str = "person") -> dict:
    """Search Congress.gov for legislators by name.

    The /member endpoint has no free-text query; we fetch all currently-serving
    members (~535) and filter client-side. Two paginated requests cover the
    full set. Uses api.data.gov key (same key as FEC). 5,000 req/hr.
    """
    result = {"findings": [], "discovered_entities": [], "source": "congress",
              "status": "complete", "raw_count": 0}

    api_key = get_credential("DATA_GOV_API_KEY")
    if not api_key:
        result["status"] = "no_api_key"
        return result

    # Fetch all currently-serving members across both pages.
    all_members: list = []
    for offset in (0, 250):
        page = _fetch_json(
            f"https://api.congress.gov/v3/member"
            f"?api_key={api_key}&currentMember=True&limit=250&offset={offset}"
            f"&format=json",
            source="congress",
        )
        if not page:
            break
        members_list = page.get("members") or []
        all_members.extend(members_list)
        if len(members_list) < 250:
            break

    # Match name against member name (Congress.gov returns "Last, First" form)
    name_parts = [p.lower() for p in name.split() if p]
    if not name_parts:
        result["status"] = "no_data"
        return result
    last_name = name_parts[-1]

    for m in all_members:
        member_name = (m.get("name") or "").lower()
        # Member name is "Smith, John" — split on comma
        first_part, _, second_part = member_name.partition(",")
        member_last = first_part.strip()
        member_first = second_part.strip().split()[0] if second_part.strip() else ""

        # Require last-name match; if user gave a first name, require it too.
        # Bidirectional prefix handles common nickname → formal name mismatches
        # (Bernie/Bernard, Bill/William, Bob/Robert, Mike/Michael, etc.).
        if last_name != member_last:
            continue
        if len(name_parts) >= 2:
            user_first = name_parts[0]
            first_match = (
                user_first == member_first
                or (len(user_first) >= 3 and member_first.startswith(user_first[:3]))
                or (len(member_first) >= 3 and user_first.startswith(member_first[:3]))
            )
            if not first_match:
                continue

        terms = m.get("terms", {}).get("item", []) or []
        latest_term = terms[-1] if terms else {}
        display_name = m.get("name") or name

        result["findings"].append({
            "type": "legislator",
            "title": f"Member of Congress: {display_name}",
            "data": {
                "name": display_name,
                "party": m.get("partyName"),
                "state": m.get("state"),
                "chamber": latest_term.get("chamber"),
                "start_year": latest_term.get("startYear"),
                "end_year": latest_term.get("endYear"),
                "bioguide_id": m.get("bioguideId"),
                "district": m.get("district"),
                "url": m.get("url"),
                "depiction": m.get("depiction", {}).get("imageUrl"),
            },
            "confidence": 0.95,
        })
        result["discovered_entities"].append({
            "name": display_name, "type": "person",
            "relationship": "legislator",
            "detail": {"party": m.get("partyName"), "state": m.get("state"),
                       "bioguide_id": m.get("bioguideId")},
        })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── Library of Congress ──────────────────────────────────────────────────────

def investigate_loc(name: str, entity_type: str = "person") -> dict:
    """Search Library of Congress for historical records, photos, newspapers.

    Free API, no key required. Returns manuscripts, photos, web archives,
    newspaper mentions, and other historical records.
    """
    result = {"findings": [], "discovered_entities": [], "source": "loc",
              "status": "complete", "raw_count": 0}

    data = _fetch_json(
        f"https://www.loc.gov/search/?q={urllib.parse.quote_plus(name)}&fo=json&c=15",
    )
    if not data:
        result["status"] = "error"
        return result

    # NOTE: the old `loc_summary` card ("Library of Congress: 53,789 records
    # for Georgia Brook") was always misleading — that count is the raw
    # keyword-hit volume, not a measure of how much LOC has on the entity.
    # It scored high in the UI and crowded out real findings. Dropped.

    for item in data.get("results", [])[:15]:
        title_text = item.get("title", "")
        item_date = item.get("date", "")
        formats = item.get("original_format", [])
        description = ""
        if item.get("description"):
            desc = item["description"]
            description = desc[0] if isinstance(desc, list) else str(desc)
        subjects = item.get("subject", [])[:5]
        contributors = item.get("contributor", [])[:5]

        # Token-overlap gate: title + description + subjects + contributors
        # all count as places the seed name might surface. Drops the bulk of
        # keyword-only noise (the state of Georgia for "Georgia Brook", etc.).
        haystack = " | ".join([
            title_text,
            description,
            " ".join(s for s in subjects if isinstance(s, str)),
            " ".join(c for c in contributors if isinstance(c, str)),
        ])
        if not _text_mentions_seed(name, haystack):
            continue

        # Categorize by format
        finding_type = "loc_record"
        if "photo" in str(formats).lower() or "drawing" in str(formats).lower():
            finding_type = "loc_photo"
        elif "newspaper" in str(formats).lower():
            finding_type = "loc_newspaper"
        elif "web archive" in str(formats).lower():
            finding_type = "loc_web_archive"
        elif "manuscript" in str(formats).lower():
            finding_type = "loc_manuscript"

        result["findings"].append({
            "type": finding_type,
            "title": f"LOC: {title_text[:100]}",
            "data": {
                "title": title_text,
                "date": item_date,
                "formats": formats,
                "description": description[:300],
                "url": item.get("url"),
                "subjects": subjects,
                "contributors": contributors,
                "locations": item.get("location", [])[:3],
            },
            "confidence": 0.75,
        })

        # Discover contributors as entities — only when the contributor name
        # itself contains seed tokens (otherwise we cascade Queen Victoria's
        # biographer into the queue for a "Georgia Brook" investigation).
        for contrib in contributors[:2]:
            if not isinstance(contrib, str) or contrib.lower() == name.lower():
                continue
            if not _text_mentions_seed(name, contrib, require_surname=False):
                continue
            result["discovered_entities"].append({
                "name": contrib, "type": "person",
                "relationship": "associated",
                "detail": {"source": "library_of_congress", "context": title_text[:80]},
            })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── SAM.gov (Federal Contractor Registration) ───────────────────────────────

def investigate_sam(name: str, entity_type: str = "organization") -> dict:
    """Search SAM.gov for federal contractor registrations.

    Requires a SAM.gov-specific API key (separate from api.data.gov).
    Set SAM_GOV_API_KEY in .env. Without it, returns no_api_key status.

    Data: UEI, CAGE code, business type, NAICS, registration status,
    address, exclusion records (debarment).
    """
    result = {"findings": [], "discovered_entities": [], "source": "sam_gov",
              "status": "complete", "raw_count": 0}

    api_key = get_credential("SAM_GOV_API_KEY")
    if not api_key:
        result["status"] = "no_api_key"
        return result

    data = _fetch_json(
        f"https://api.sam.gov/entity-information/v3/entities"
        f"?api_key={api_key}"
        f"&legalBusinessName={urllib.parse.quote_plus(name)}"
        f"&registrationStatus=A"
        f"&includeSections=entityRegistration,coreData",
        source="sam_gov",
    )

    if data and data.get("entityData"):
        for entity in data["entityData"][:10]:
            reg = entity.get("entityRegistration", {})
            core = entity.get("coreData", {})
            phys = core.get("physicalAddress", {})
            biz_name = reg.get("legalBusinessName", "")

            result["findings"].append({
                "type": "sam_registration",
                "title": f"SAM.gov: {biz_name}",
                "data": {
                    "legal_name": biz_name,
                    "dba_name": reg.get("dbaName"),
                    "uei": reg.get("ueiSAM"),
                    "cage_code": reg.get("cageCode"),
                    "registration_status": reg.get("registrationStatus"),
                    "registration_date": reg.get("registrationDate"),
                    "expiration_date": reg.get("registrationExpirationDate"),
                    "entity_structure": core.get("entityStructure"),
                    "entity_type": core.get("entityType"),
                    "address": f"{phys.get('addressLine1', '')}, {phys.get('city', '')}, {phys.get('stateOrProvinceCode', '')} {phys.get('zipCode', '')}",
                    "country": phys.get("countryCode"),
                    "congressional_district": core.get("congressionalDistrict"),
                    "naics_codes": [n.get("naicsCode") for n in core.get("naicsList", {}).get("naicsEntry", [])[:5]],
                },
                "confidence": 0.95,
            })

            result["discovered_entities"].append({
                "name": biz_name, "type": "organization",
                "relationship": "federal_contractor",
                "detail": {"uei": reg.get("ueiSAM"), "cage_code": reg.get("cageCode")},
            })

    result["raw_count"] = len(result["findings"])
    if not result["findings"]:
        result["status"] = "no_data"
    return result


# ─── Master Investigation Runner ──���──────────────────────────────────────────

def investigate_wikipedia(name: str, entity_type: str = "person") -> dict:
    """Bridge: Wikipedia summary lookup wrapped as an investigation source.

    Phase 7b.2 of research-validation plan. Uses
    core/research/sources.py:wikipedia_summary so the same Wikipedia
    integration powers both the legacy `process_enrichment_queue`
    pipeline and the modern investigation worker — single source of
    truth for the API call, two pipelines that consume it.
    """
    result = {"findings": [], "discovered_entities": [], "source": "wikipedia",
              "status": "complete", "raw_count": 0}
    try:
        from core.research.sources import wikipedia_summary
        wiki = wikipedia_summary(name)
        if not wiki:
            result["status"] = "no_data"
            return result
        result["findings"].append({
            "type": "encyclopedic_summary",
            "title": f"Wikipedia: {wiki.get('title', name)}",
            "data": {
                "summary": wiki.get("extract", "")[:2000],
                "page_url": wiki.get("page_url"),
                "thumbnail": wiki.get("thumbnail"),
                "wikidata_id": wiki.get("wikidata_id"),
                "description": wiki.get("description", ""),
                "source_url": wiki.get("page_url"),
            },
            "confidence": 0.90,
        })
        result["raw_count"] = 1
    except Exception as e:
        log.warning("wikipedia bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_wikidata(name: str, entity_type: str = "person") -> dict:
    """Bridge: Wikidata structured-fields lookup. Two-step (Wikipedia →
    QID → Wikidata) since Wikidata's name search is weaker than Wikipedia's.
    Returns biography-grade structured fields (DOB, nationality, position,
    employer, education, social handles, image) when the entity is a real
    Wikidata-known person/org/place.
    """
    result = {"findings": [], "discovered_entities": [], "source": "wikidata",
              "status": "complete", "raw_count": 0}
    try:
        from core.research.sources import wikipedia_summary, wikidata_entity
        wiki = wikipedia_summary(name)
        qid = wiki.get("wikidata_id") if wiki else None
        if not qid:
            result["status"] = "no_data"
            return result
        wd = wikidata_entity(qid)
        if not wd:
            result["status"] = "no_data"
            return result
        result["findings"].append({
            "type": "structured_profile",
            "title": f"Wikidata: {wd.get('label', name)} ({qid})",
            "data": {
                **{k: v for k, v in wd.items() if v},
                "wikidata_id": qid,
                "source_url": f"https://www.wikidata.org/wiki/{qid}",
            },
            "confidence": 0.92,
        })

        # ── Single-value discovered entities ────────────────────────────
        # Per-property type defaults — educated_at / employer / citizenship
        # all point at orgs or countries, not people. position_held is a
        # job title and gets skipped (we want the org, not the title).
        SINGLE_VALUE_RELS = (
            ("employer",                "employed_by", "organization"),
            ("educated_at",             "educated_at", "organization"),
            ("country_of_citizenship",  "citizen_of",  "country"),
        )
        for k, rel, tp in SINGLE_VALUE_RELS:
            v = wd.get(k)
            if v and isinstance(v, str):
                result["discovered_entities"].append({
                    "name": v, "type": tp,
                    "relationship": rel,
                    "detail": {"wikidata_property": k, "source_qid": qid},
                })

        # ── Multi-value discovery: ownership / corporate / family ────────
        # Phase 8b.6 follow-up — extends discovery to the property classes
        # that surface a person's aircraft / property / corp empire / family
        # so the dossier doesn't depend on a hand-seeded corporate spine.
        multi_value_rels = [
            # (wd_key,        relationship,         default_type)
            ("owner_of",       "owns",              None),         # type inferred per item (aircraft/org/place)
            ("owned_by",       "owned_by",          "organization"),
            ("parent_org",     "subsidiary_of",     "organization"),
            ("subsidiary",     "owns",              "organization"),
            ("founded_by",     "founded_by",        "person"),
            ("chairperson",    "chairperson_of",    "person"),
            ("ceo",            "ceo_of",            "person"),
            ("board_member",   "board_member_of",   "person"),
            ("spouse",         "spouse_of",         "person"),
            ("child",          "parent_of",         "person"),
            ("father",         "child_of",          "person"),
            ("mother",         "child_of",          "person"),
            ("sibling",        "sibling_of",        "person"),
            ("hq_location",    "headquartered_in",  "place"),
            ("operator",       "operated_by",       "organization"),
            ("manufacturer",   "manufactured_by",   "organization"),
            ("country_of_origin", "from_country",   "country"),
            ("located_in",     "located_in",        "place"),
        ]
        for wd_key, rel, default_type in multi_value_rels:
            vals = wd.get(wd_key) or []
            if not isinstance(vals, list):
                vals = [vals]
            for v in vals[:25]:  # cap per property — Vale's owner_of list is huge
                if not v or not isinstance(v, str):
                    continue
                # Heuristic type inference for owner_of (which can mix
                # aircraft / org / facility / property). Order matters —
                # check most specific markers first.
                inferred = default_type
                if default_type is None:
                    low = v.lower()
                    # Skip job-title-shaped strings — these are positions,
                    # not entities (e.g. "chief executive officer").
                    if low in {"chief executive officer", "chairperson", "founder",
                               "president", "ceo", "cfo", "coo", "director",
                               "owner", "investor", "businessperson",
                               "business magnate", "real estate developer"}:
                        continue
                    if any(t in low for t in (" 757", " 747", " 727", "boeing",
                                              "sikorsky", "cessna", "gulfstream",
                                              "airbus", "helicopter", "aircraft",
                                              " jet", "boeing 7", "747-", "757-")):
                        inferred = "aircraft"
                    elif any(t in low for t in ("yacht", "vessel", " ship")):
                        inferred = "vessel"
                    elif any(t in low for t in ("tower", "hotel", "club", "resort",
                                                "mansion", "estate", "building",
                                                "complex", "palace", "headquarters",
                                                "ranch", "casino",
                                                "spa", "winery", "vineyard",
                                                "stadium", "arena", "course",
                                                "pavilion", "park ", "park,", "plaza",
                                                "manor", "hall ", "hall,",
                                                "house ", "house,")):
                        inferred = "facility"
                    elif any(t in low for t in (" inc", " corp", " llc", " ltd",
                                                " gmbh", "organization", " co.",
                                                " co ", "company", "trust",
                                                "foundation", "association",
                                                "partners", "holdings", "group")):
                        inferred = "organization"
                    else:
                        inferred = "organization"  # safe default
                result["discovered_entities"].append({
                    "name": v, "type": inferred,
                    "relationship": rel,
                    "detail": {"wikidata_property": wd_key, "source_qid": qid},
                })

        result["raw_count"] = 1
    except Exception as e:
        log.warning("wikidata bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_opensanctions(name: str, entity_type: str = "person") -> dict:
    """Bridge: OpenSanctions match check. Different from the existing
    investigate_ofac handler — OpenSanctions aggregates EU/UK/UN/Treasury
    + many regional lists, not just OFAC SDN. Both handlers can fire
    independently; an entity hit on both gets two findings the user can
    cross-reference.
    """
    result = {"findings": [], "discovered_entities": [], "source": "opensanctions",
              "status": "complete", "raw_count": 0}

    # With OPENSANCTIONS_API_KEY: live /match endpoint with PEP categories,
    # topics (corruption/military/oligarch), and full consolidated dataset.
    # Without: fall back to local cache built from the public bulk download
    # (sanctions_check in core/research/sources.py).
    api_key = get_credential("OPENSANCTIONS_API_KEY")
    if api_key:
        try:
            schema = "Person" if entity_type == "person" else "Organization"
            body = {
                "queries": {
                    "q": {
                        "schema": schema,
                        "properties": {"name": [name]},
                    }
                }
            }
            url = "https://api.opensanctions.org/match/sanctions"
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", _UA)
            req.add_header("Authorization", f"ApiKey {api_key}")
            _rate_limit("opensanctions")
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as resp:
                data = json.loads(resp.read())
            for m in ((data or {}).get("responses", {}).get("q", {}).get("results") or [])[:10]:
                props = m.get("properties") or {}
                result["findings"].append({
                    "type": "sanctions_match",
                    "title": f"OpenSanctions: {(props.get('name') or [name])[0]} ({m.get('schema', '')})",
                    "data": {
                        "id": m.get("id"),
                        "schema": m.get("schema"),
                        "score": m.get("score"),
                        "match_features": m.get("features"),
                        "topics": props.get("topics") or [],
                        "datasets": m.get("datasets") or [],
                        "countries": props.get("country") or [],
                        "first_seen": m.get("first_seen"),
                        "last_seen": m.get("last_seen"),
                        "source_url": f"https://www.opensanctions.org/entities/{m.get('id', '')}/",
                    },
                    "confidence": float(m.get("score") or 0.7),
                })
            result["raw_count"] = len(result["findings"])
            if not result["findings"]:
                result["status"] = "no_data"
            return result
        except Exception as e:
            log.warning("opensanctions live API failed for %s: %s — falling back to cache", name, e)
            # fall through to bulk-cache path

    # Bulk-cache fallback (free)
    try:
        from core.research.sources import sanctions_check
        sanc = sanctions_check(
            name, "Person" if entity_type == "person" else "Organization"
        )
        if not sanc or not sanc.get("matches"):
            result["status"] = "no_data"
            return result
        for m in (sanc.get("matches") or [])[:5]:
            result["findings"].append({
                "type": "sanctions_match",
                "title": f"OpenSanctions: {m.get('name', name)} ({m.get('schema', '')})",
                "data": {
                    **m,
                    "source_url": "https://www.opensanctions.org/",
                },
                "confidence": float(m.get("score") or 0.7),
            })
        result["raw_count"] = len(result["findings"])
    except Exception as e:
        log.warning("opensanctions bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_nominatim(name: str, entity_type: str = "country") -> dict:
    """Bridge: Nominatim geocoding for place entities. Returns one
    finding per match with lat/lon + address breakdown so dossier UI
    renders a map link automatically (via _derive_map_url)."""
    result = {"findings": [], "discovered_entities": [], "source": "nominatim",
              "status": "complete", "raw_count": 0}
    # Skip non-place entities — geocoding a person's name produces nonsense
    # ('Brook Alley, Atlanta' for "Georgia Brook", etc.). Real place
    # entities (country, city, region, place, address) keep their lookup.
    if entity_type not in ("country", "city", "region", "place", "address",
                            "location", "facility", "landmark"):
        result["status"] = "no_data"
        return result
    try:
        from core.research.sources import nominatim_details
        info = nominatim_details(name)
        if not info:
            result["status"] = "no_data"
            return result
        result["findings"].append({
            "type": "geocode",
            "title": f"Nominatim: {info.get('display_name', name)}",
            "data": {
                **info,
                "address": info.get("display_name"),
                "source_url": (
                    f"https://www.openstreetmap.org/?mlat={info.get('lat')}&mlon={info.get('lon')}#map=10/{info.get('lat')}/{info.get('lon')}"
                    if info.get('lat') and info.get('lon') else None
                ),
            },
            "confidence": 0.88,
        })
        result["raw_count"] = 1
    except Exception as e:
        log.warning("nominatim bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


# ─── Phase 8b.6.1 bridges (researchpack 1.1.0) ────────────────────────────────
#
# Eight new official-source bridges: CIA Factbook, GeoNames, REST Countries,
# World Bank, OpenCorporates, FAA Aircraft Registry, UN Sanctions, Federal
# Register. All free / no-key APIs (a couple support optional env-var keys
# for higher rate limits). Each returns the standard
# {findings[], discovered_entities[], source, status, raw_count} dict.

# CIA World Factbook — country profile from the GitHub-mirrored static JSON.
# Project: https://github.com/factbook/factbook.json
_FACTBOOK_REGION_MAP = {
    # Minimal slug map for the most-queried countries; full lookup table
    # would be a separate manifest. Falls back to a generic search by
    # iterating the index file when key isn't found.
    "russia": ("europe", "russia"), "china": ("east-n-southeast-asia", "china"),
    "united states": ("north-america", "united-states"), "iran": ("middle-east", "iran"),
    "iraq": ("middle-east", "iraq"), "syria": ("middle-east", "syria"),
    "israel": ("middle-east", "israel"), "ukraine": ("europe", "ukraine"),
    "germany": ("europe", "germany"), "france": ("europe", "france"),
    "united kingdom": ("europe", "united-kingdom"), "italy": ("europe", "italy"),
    "spain": ("europe", "spain"), "japan": ("east-n-southeast-asia", "japan"),
    "south korea": ("east-n-southeast-asia", "korea-south"),
    "north korea": ("east-n-southeast-asia", "korea-north"),
    "india": ("south-asia", "india"), "pakistan": ("south-asia", "pakistan"),
    "saudi arabia": ("middle-east", "saudi-arabia"), "turkey": ("middle-east", "turkey"),
    "egypt": ("africa", "egypt"), "brazil": ("south-america", "brazil"),
    "mexico": ("north-america", "mexico"), "canada": ("north-america", "canada"),
    "australia": ("australia-oceania", "australia"), "nigeria": ("africa", "nigeria"),
}


def investigate_cia_factbook(name: str, entity_type: str = "country") -> dict:
    """Bridge: CIA World Factbook via the factbook.json GitHub mirror.

    Returns one structured-profile finding with government, economy, military,
    leadership, and demographic fields when the entity is a known country.
    """
    result = {"findings": [], "discovered_entities": [], "source": "cia_factbook",
              "status": "complete", "raw_count": 0}
    if entity_type not in ("country", "place", "region"):
        result["status"] = "no_data"
        return result
    try:
        slug = _FACTBOOK_REGION_MAP.get(name.lower())
        if not slug:
            result["status"] = "no_data"
            return result
        region, country = slug
        url = f"https://raw.githubusercontent.com/factbook/factbook.json/master/{region}/{country}.json"
        data = _fetch_json(url, source="cia_factbook")
        if not data:
            result["status"] = "no_data"
            return result
        # Extract the high-value sections — Factbook JSON is deeply nested
        gov   = (data.get("Government") or {})
        econ  = (data.get("Economy") or {})
        mil   = (data.get("Military and Security") or {})
        intro = (data.get("Introduction") or {}).get("Background", {}).get("text", "")
        leaders = (gov.get("Executive branch") or {}).get("chief of state", {}).get("text", "")
        head_of_govt = (gov.get("Executive branch") or {}).get("head of government", {}).get("text", "")
        capital = (gov.get("Capital") or {}).get("name", {}).get("text", "")
        result["findings"].append({
            "type": "country_profile",
            "title": f"CIA Factbook: {name}",
            "data": {
                "background": (intro or "")[:1500],
                "capital": capital,
                "chief_of_state": leaders,
                "head_of_government": head_of_govt,
                "government_type": (gov.get("Government type") or {}).get("text", ""),
                "gdp_summary": ((econ.get("Real GDP (purchasing power parity)") or {}).get("Real GDP (purchasing power parity) 2023") or {}).get("text", ""),
                "military_summary": ((mil.get("Military and security forces") or {}) or {}).get("text", ""),
                "source_url": f"https://www.cia.gov/the-world-factbook/countries/{country}/",
            },
            "confidence": 0.95,
        })
        # Discovered entity: the head of government / chief of state — links
        # the country to its leader as a person entity for downstream graph.
        if head_of_govt:
            result["discovered_entities"].append({
                "name": head_of_govt.split(",")[0].split("(")[0].strip(),
                "type": "person", "relationship": "leads",
                "detail": {"role": "head_of_government", "country": name},
            })
        if capital:
            result["discovered_entities"].append({
                "name": capital, "type": "city", "relationship": "capital_of",
                "detail": {"country": name, "source": "cia_factbook"},
            })
        result["raw_count"] = 1
    except Exception as e:
        log.warning("cia_factbook bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_geonames(name: str, entity_type: str = "city") -> dict:
    """Bridge: GeoNames search. Returns alternate names, population, admin
    hierarchy, and lat/lon. Catches case-variant collisions at admit time
    by exposing the canonical English form + non-English variants as aliases.

    Requires GEONAMES_USERNAME env var (free registration). Skips silently
    if missing — the bridge logs and returns no_data.
    """
    result = {"findings": [], "discovered_entities": [], "source": "geonames",
              "status": "complete", "raw_count": 0}
    if entity_type not in ("country", "city", "place", "region", "admin_region"):
        result["status"] = "no_data"
        return result
    user = get_credential("GEONAMES_USERNAME").strip()
    if not user:
        log.debug("geonames: GEONAMES_USERNAME not set — skipping")
        result["status"] = "no_api_key"
        return result
    page_cap = source_page_cap("geonames")
    try:
        q = urllib.parse.quote(name)
        url = (f"http://api.geonames.org/searchJSON?q={q}&maxRows={page_cap}"
               f"&style=FULL&username={user}")
        data = _fetch_json(url, source="geonames")
        for hit in (data or {}).get("geonames", [])[:page_cap]:
            alt_names = [an.get("name") for an in (hit.get("alternateNames") or [])
                         if an.get("name") and an.get("name") != hit.get("name")][:20]
            result["findings"].append({
                "type": "geonames_record",
                "title": f"GeoNames: {hit.get('name')} ({hit.get('countryName', '')})",
                "data": {
                    "geoname_id": hit.get("geonameId"),
                    "name": hit.get("name"),
                    "alternate_names": alt_names,
                    "population": hit.get("population"),
                    "country": hit.get("countryName"),
                    "country_code": hit.get("countryCode"),
                    "admin1": hit.get("adminName1"),
                    "admin2": hit.get("adminName2"),
                    "feature_class": hit.get("fclName"),
                    "feature_code": hit.get("fcodeName"),
                    "lat": hit.get("lat"),
                    "lon": hit.get("lng"),
                    "source_url": f"https://www.geonames.org/{hit.get('geonameId')}/",
                },
                "confidence": 0.92,
            })
        result["raw_count"] = len(result["findings"])
        if not result["findings"]:
            result["status"] = "no_data"
    except Exception as e:
        log.warning("geonames bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_rest_countries(name: str, entity_type: str = "country") -> dict:
    """Bridge: REST Countries API. Free, no key. Returns capital/currency/
    languages/timezones/borders/flags/ISO codes."""
    result = {"findings": [], "discovered_entities": [], "source": "rest_countries",
              "status": "complete", "raw_count": 0}
    if entity_type != "country":
        result["status"] = "no_data"
        return result
    try:
        q = urllib.parse.quote(name)
        url = f"https://restcountries.com/v3.1/name/{q}?fullText=false"
        data = _fetch_json(url, source="rest_countries")
        if not data or not isinstance(data, list):
            result["status"] = "no_data"
            return result
        c = data[0]
        result["findings"].append({
            "type": "country_baseline",
            "title": f"REST Countries: {c.get('name', {}).get('common', name)}",
            "data": {
                "official_name": c.get("name", {}).get("official"),
                "common_name": c.get("name", {}).get("common"),
                "capital": (c.get("capital") or [None])[0],
                "region": c.get("region"),
                "subregion": c.get("subregion"),
                "population": c.get("population"),
                "area_km2": c.get("area"),
                "languages": list((c.get("languages") or {}).values()),
                "currencies": list((c.get("currencies") or {}).keys()),
                "timezones": c.get("timezones"),
                "borders": c.get("borders"),
                "iso2": c.get("cca2"),
                "iso3": c.get("cca3"),
                "flag_url": (c.get("flags") or {}).get("png"),
                "lat": (c.get("latlng") or [None, None])[0],
                "lon": (c.get("latlng") or [None, None])[1],
                "source_url": f"https://restcountries.com/v3.1/name/{q}",
            },
            "confidence": 0.93,
        })
        # Discovered entity: capital city
        cap = (c.get("capital") or [None])[0]
        if cap:
            result["discovered_entities"].append({
                "name": cap, "type": "city", "relationship": "capital_of",
                "detail": {"country": name, "source": "rest_countries"},
            })
        result["raw_count"] = 1
    except Exception as e:
        log.warning("rest_countries bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_world_bank(name: str, entity_type: str = "country") -> dict:
    """Bridge: World Bank Indicators API. Free, no key. Pulls headline
    economic indicators (GDP, population, military spend) for a country."""
    result = {"findings": [], "discovered_entities": [], "source": "world_bank",
              "status": "complete", "raw_count": 0}
    if entity_type != "country":
        result["status"] = "no_data"
        return result
    try:
        # Step 1: resolve country name → ISO2 via /country?per_page=400
        # Cached in module scope so we only fetch once per process.
        global _WB_COUNTRY_INDEX
        if _WB_COUNTRY_INDEX is None:
            idx = _fetch_json(
                "https://api.worldbank.org/v2/country?format=json&per_page=400",
                source="world_bank",
            )
            _WB_COUNTRY_INDEX = {}
            for entry in (idx or [[None, []]])[1]:
                if isinstance(entry, dict) and entry.get("name"):
                    _WB_COUNTRY_INDEX[entry["name"].lower()] = entry.get("id")
        iso2 = _WB_COUNTRY_INDEX.get(name.lower())
        if not iso2:
            result["status"] = "no_data"
            return result
        # Step 2: fetch a couple of headline indicators
        indicators = {
            "NY.GDP.MKTP.CD": "GDP (current US$)",
            "SP.POP.TOTL":    "Population, total",
            "MS.MIL.XPND.GD.ZS": "Military expenditure (% of GDP)",
            "NE.EXP.GNFS.CD": "Exports of goods and services (current US$)",
        }
        bundle = {}
        for code, label in indicators.items():
            url = (f"https://api.worldbank.org/v2/country/{iso2}/indicator/{code}"
                   f"?format=json&date=2018:2024&per_page=10")
            data = _fetch_json(url, source="world_bank")
            if not data or len(data) < 2:
                continue
            for point in (data[1] or []):
                if point.get("value") is not None:
                    bundle[label] = {"value": point["value"], "year": point.get("date")}
                    break
        if not bundle:
            result["status"] = "no_data"
            return result
        result["findings"].append({
            "type": "economic_indicators",
            "title": f"World Bank: {name} headline indicators",
            "data": {
                "iso2": iso2,
                "indicators": bundle,
                "source_url": f"https://data.worldbank.org/country/{iso2}",
            },
            "confidence": 0.94,
        })
        result["raw_count"] = 1
    except Exception as e:
        log.warning("world_bank bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


# Module-scoped cache for World Bank country → ISO2 lookup.
_WB_COUNTRY_INDEX: Optional[dict] = None


def investigate_opencorporates(name: str, entity_type: str = "organization") -> dict:
    """Bridge: OpenCorporates company search. Free tier 500/day, no key for
    basic search.

    Person entities → /officers/search → returns every company the person
    is listed as an officer/director of. Each company is emitted as a
    discovered_entity with relationship='officer_of' so the entity graph
    surfaces a person's corporate empire after their investigation runs.

    Organization entities → /companies/search → returns matching companies
    with jurisdiction, address, status. If the response includes officers,
    each is emitted as a discovered_entity with relationship='officer_of'
    so the corporate-officer graph builds in the other direction too.

    Per-finding payload includes the registered address — the geocode
    worker (Phase 8b.6.3) lifts that into entity_addresses + lat/lon.
    """
    result = {"findings": [], "discovered_entities": [], "source": "opencorporates",
              "status": "complete", "raw_count": 0}
    if entity_type not in ("organization", "person", "government_agency"):
        result["status"] = "no_data"
        return result

    token = get_credential("OPENCORPORATES_API_TOKEN")
    token_param = f"&api_token={token}" if token else ""
    page_cap = source_page_cap("opencorporates")

    try:
        q = urllib.parse.quote(name)
        is_person = (entity_type == "person")
        if is_person:
            url = (f"https://api.opencorporates.com/v0.4/officers/search"
                   f"?q={q}&format=json&per_page={page_cap}{token_param}")
        else:
            url = (f"https://api.opencorporates.com/v0.4/companies/search"
                   f"?q={q}&format=json&per_page={page_cap}{token_param}")
        data = _fetch_json(url, source="opencorporates")
        results = (data or {}).get("results", {})
        items = results.get("companies") or results.get("officers") or []

        for it in items[:page_cap]:
            wrap = it.get("officer") if is_person else it.get("company")
            if not wrap:
                continue

            if is_person:
                # `wrap` is an officer record; we want its parent company
                company = wrap.get("company") or {}
                officer_name = wrap.get("name") or name
                position    = wrap.get("position") or ""
                company_name = company.get("name") or ""
                jurisdiction = company.get("jurisdiction_code") or wrap.get("jurisdiction_code") or ""
                address      = company.get("registered_address_in_full") or ""
                company_num  = company.get("company_number")
                oc_url       = wrap.get("opencorporates_url") or company.get("opencorporates_url")
                start_date   = wrap.get("start_date")
                end_date     = wrap.get("end_date")
            else:
                company      = wrap
                officer_name = None
                position     = ""
                company_name = company.get("name") or ""
                jurisdiction = company.get("jurisdiction_code") or ""
                address      = company.get("registered_address_in_full") or company.get("address") or ""
                company_num  = company.get("company_number")
                oc_url       = company.get("opencorporates_url")
                start_date   = None
                end_date     = None

            if not company_name:
                continue

            result["findings"].append({
                "type": "corporate_record",
                "title": (
                    f"OpenCorporates: {officer_name} — {position} of {company_name} ({jurisdiction})"
                    if is_person else
                    f"OpenCorporates: {company_name} ({jurisdiction})"
                ),
                "data": {
                    "name": company_name,
                    "officer_name": officer_name,
                    "officer_position": position,
                    "company_number": company_num,
                    "jurisdiction_code": jurisdiction,
                    "incorporation_date": company.get("incorporation_date"),
                    "company_type": company.get("company_type"),
                    "current_status": company.get("current_status"),
                    "address": address,
                    "officer_start_date": start_date,
                    "officer_end_date": end_date,
                    "source_url": oc_url,
                },
                "confidence": 0.85,
            })

            # Phase 8b.6 follow-up: emit the company as a discovered entity
            # so the entity graph builds the person → company chain.
            # Relationship type is picked by the position string when we
            # have it; falls back to a generic officer/owner edge.
            relationship = _opencorp_relationship_for(position) if is_person else None
            if is_person:
                result["discovered_entities"].append({
                    "name": company_name,
                    "type": "organization",
                    "relationship": relationship or "officer_of",
                    "detail": {
                        "source": "opencorporates",
                        "officer_position": position,
                        "jurisdiction_code": jurisdiction,
                        "company_number": company_num,
                        "address": address,
                        "start_date": start_date, "end_date": end_date,
                    },
                })
            else:
                # When we investigate a company, also pull its disclosed
                # officers as discovered person entities. The bulk-search
                # response includes a few; with an OPENCORPORATES_API_TOKEN
                # we additionally fetch the full per-company /officers list
                # (paid tier — burns extra quota but maps full boards).
                officers_seen = set()
                for off in (company.get("officers") or [])[:10]:
                    off_obj = off.get("officer") or {}
                    off_name = off_obj.get("name")
                    if off_name and off_name not in officers_seen:
                        officers_seen.add(off_name)
                        result["discovered_entities"].append({
                            "name": off_name,
                            "type": "person",
                            "relationship": _opencorp_relationship_for(off_obj.get("position", "")) or "officer_of",
                            "detail": {
                                "source": "opencorporates",
                                "officer_position": off_obj.get("position"),
                                "company": company_name,
                            },
                        })

                if token and company_num and jurisdiction:
                    deep_url = (
                        f"https://api.opencorporates.com/v0.4/companies/"
                        f"{jurisdiction}/{company_num}/officers"
                        f"?format=json&per_page=100{token_param}"
                    )
                    deep = _fetch_json(deep_url, source="opencorporates")
                    for off in ((deep or {}).get("results", {}).get("officers") or [])[:100]:
                        off_obj = off.get("officer") or {}
                        off_name = off_obj.get("name")
                        if off_name and off_name not in officers_seen:
                            officers_seen.add(off_name)
                            result["discovered_entities"].append({
                                "name": off_name,
                                "type": "person",
                                "relationship": _opencorp_relationship_for(off_obj.get("position", "")) or "officer_of",
                                "detail": {
                                    "source": "opencorporates",
                                    "officer_position": off_obj.get("position"),
                                    "company": company_name,
                                    "deep_fetch": True,
                                },
                            })

        result["raw_count"] = len(result["findings"])
        if not result["findings"]:
            result["status"] = "no_data"
    except Exception as e:
        log.warning("opencorporates bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def _opencorp_relationship_for(position: str) -> Optional[str]:
    """Map an OpenCorporates officer position string to the canonical
    relationship string used in entity_relationships. Returns None for
    unknown positions so the caller can fall back to a generic edge."""
    p = (position or "").lower().strip()
    if not p:
        return None
    if "director" in p:
        return "director_of"
    if "officer" in p or "secretary" in p or "treasurer" in p:
        return "officer_of"
    if "ceo" in p or "chief executive" in p:
        return "ceo_of"
    if "cfo" in p or "chief financial" in p:
        return "cfo_of"
    if "coo" in p or "chief operating" in p:
        return "coo_of"
    if "cto" in p or "chief technology" in p:
        return "cto_of"
    if "president" in p:
        return "president_of"
    if "chair" in p:
        return "chairperson_of"
    if "founder" in p:
        return "founded_by"
    if "owner" in p or "shareholder" in p or "beneficial" in p:
        return "beneficial_owner_of"
    if "member" in p or "partner" in p:
        return "member_of"
    return None


_FAA_TAIL_RE = re.compile(r"^N\d{1,5}[A-Z]{0,2}$", re.IGNORECASE)


def investigate_faa_aircraft(name: str, entity_type: str = "aircraft") -> dict:
    """Bridge: FAA Aircraft Registry inquiry. Two modes:

      1. If `name` looks like a US tail number (^N\\d+[A-Z]*$) → fetch the
         registration page directly.
      2. If entity_type is person/organization → no-op for now. A full
         "find all aircraft owned by X" requires parsing the 70MB MASTER
         CSV; deferred until the cache layer lands.
    """
    result = {"findings": [], "discovered_entities": [], "source": "faa_aircraft",
              "status": "complete", "raw_count": 0}
    if not _FAA_TAIL_RE.match((name or "").strip()):
        result["status"] = "no_data"
        return result
    try:
        tail = name.strip().lstrip("Nn")
        # FAA Registry public inquiry endpoint — returns HTML; the JSON-ish
        # equivalent isn't published, so we fetch HTML and extract the few
        # fields we care about via regex.
        url = f"https://registry.faa.gov/AircraftInquiry/Search/NNumberResult?nNumberTxt=N{tail}"
        html = _fetch_text(url)
        if not html or "No records found" in html:
            result["status"] = "no_data"
            return result
        # Cheap field extraction (avoids pulling in BeautifulSoup at this layer).
        def _grab(label: str) -> str:
            m = re.search(rf"{re.escape(label)}\s*</td>\s*<td[^>]*>\s*([^<]+)", html, re.IGNORECASE)
            return (m.group(1).strip() if m else "")
        owner    = _grab("Name")
        addr_st  = _grab("Street")
        addr_c   = _grab("City")
        addr_s   = _grab("State")
        addr_z   = _grab("Zip Code")
        mfr      = _grab("Manufacturer Name")
        model    = _grab("Model")
        ser_no   = _grab("Serial Number")
        result["findings"].append({
            "type": "aircraft_registration",
            "title": f"FAA: N{tail} — {mfr} {model}",
            "data": {
                "tail_number": f"N{tail}",
                "manufacturer": mfr,
                "model": model,
                "serial_number": ser_no,
                "owner_name": owner,
                "address": {
                    "street": addr_st, "city": addr_c,
                    "region": addr_s, "postal_code": addr_z,
                    "country": "United States",
                },
                "source_url": url,
            },
            "confidence": 0.96,
        })
        if owner:
            result["discovered_entities"].append({
                "name": owner, "type": "organization",
                "relationship": "registered_to",
                "detail": {"asset_tail_number": f"N{tail}", "source": "faa_aircraft"},
            })
        result["raw_count"] = 1
    except Exception as e:
        log.warning("faa_aircraft bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


_UN_SANCTIONS_CACHE: Optional[list] = None


def investigate_un_sanctions(name: str, entity_type: str = "person") -> dict:
    """Bridge: UN Consolidated Sanctions list. Downloads + caches the XML
    once per process (changes daily); subsequent calls match in-memory.
    Catches designations OFAC alone might miss (e.g. UN-only designations
    against non-US-relevant actors)."""
    global _UN_SANCTIONS_CACHE
    result = {"findings": [], "discovered_entities": [], "source": "un_sanctions",
              "status": "complete", "raw_count": 0}
    try:
        if _UN_SANCTIONS_CACHE is None:
            url = "https://main.un.org/securitycouncil/sites/www.un.org.securitycouncil/files/consolidated.xml"
            xml = _fetch_text(url, timeout=30)
            if not xml:
                result["status"] = "no_data"
                return result
            # Parse minimally — pull <NAME>...</NAME> + <ENTITY>...</ENTITY>.
            # Real implementation would use stdlib xml.etree, but we keep
            # this regex-based to avoid changing process-wide XML state.
            entries = re.findall(
                r"<(INDIVIDUAL|ENTITY)\b[^>]*>(.*?)</\1>", xml, re.DOTALL,
            )
            cache: list[dict] = []
            for kind, body in entries:
                # Many UN records use FIRST_NAME / SECOND_NAME / NAME segments
                names = re.findall(r"<(?:FIRST|SECOND|THIRD|FOURTH|NAME)[^>]*>([^<]+)</", body)
                full = " ".join(n.strip() for n in names if n and n.strip())
                if not full:
                    continue
                ref = re.search(r"<REFERENCE_NUMBER[^>]*>([^<]+)</", body)
                listed = re.search(r"<LISTED_ON[^>]*>([^<]+)</", body)
                cache.append({
                    "kind": kind.lower(),
                    "name": full[:255],
                    "reference": (ref.group(1).strip() if ref else None),
                    "listed_on": (listed.group(1).strip() if listed else None),
                })
            _UN_SANCTIONS_CACHE = cache
        # Match
        target = name.lower()
        hits = [r for r in _UN_SANCTIONS_CACHE if target in r["name"].lower()][:5]
        for h in hits:
            result["findings"].append({
                "type": "un_sanctions_match",
                "title": f"UN Sanctions: {h['name']} ({h['kind']})",
                "data": {
                    **h,
                    "source_url": "https://main.un.org/securitycouncil/en/sanctions/un-sc-consolidated-list",
                },
                "confidence": 0.91,
            })
        result["raw_count"] = len(result["findings"])
        if not result["findings"]:
            result["status"] = "no_data"
    except Exception as e:
        log.warning("un_sanctions bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_federal_register(name: str, entity_type: str = "organization") -> dict:
    """Bridge: Federal Register API. Searches rules + notices by term.
    For organizations (e.g. 'Boeing', 'Vale Organization') this surfaces
    every rulemaking + notice that mentions the entity over the recent
    history window. For government_agency entities, returns rules
    *issued by* the agency."""
    result = {"findings": [], "discovered_entities": [], "source": "federal_register",
              "status": "complete", "raw_count": 0}
    # Persons no longer query Federal Register: it's regulatory rulemaking
    # text, not biographical. A keyword hit on "Georgia" or "Brook" pulled in
    # endangered species rules and quarterly expatriation lists. If a real
    # public figure shows up in a rule, the org/agency-level investigation
    # will surface it.
    if entity_type not in ("organization", "government_agency"):
        result["status"] = "no_data"
        return result
    try:
        q = urllib.parse.quote(name)
        url = (f"https://www.federalregister.gov/api/v1/documents.json"
               f"?conditions[term]={q}&conditions[publication_date][gte]=2023-01-01"
               f"&per_page=5&order=newest")
        data = _fetch_json(url, source="federal_register")
        for doc in (data or {}).get("results", [])[:5]:
            title = doc.get("title", "") or ""
            abstract = doc.get("abstract", "") or ""
            # Require the org name to actually surface in title or abstract
            # (the API will keyword-match on "Boeing" inside unrelated rule
            # text, which produces noise in the dossier).
            if not _text_mentions_seed(name, f"{title} | {abstract}"):
                continue
            result["findings"].append({
                "type": "federal_register_doc",
                "title": f"Federal Register: {title[:200]}",
                "data": {
                    "document_number": doc.get("document_number"),
                    "title": title,
                    "type": doc.get("type"),
                    "agencies": [a.get("name") for a in (doc.get("agencies") or [])],
                    "publication_date": doc.get("publication_date"),
                    "abstract": abstract[:500],
                    "html_url": doc.get("html_url"),
                    "source_url": doc.get("html_url") or doc.get("public_inspection_pdf_url"),
                },
                "confidence": 0.88,
            })
        result["raw_count"] = len(result["findings"])
        if not result["findings"]:
            result["status"] = "no_data"
    except Exception as e:
        log.warning("federal_register bridge failed for %s: %s", name, e)
        result["status"] = "error"
    return result


# ═════════════════════════════════════════════════════════════════════════════
# Public-data OSINT sources (no API key required, scrape-or-public-API based).
# Designed for the federation model: any node operator can run them without
# vendor lock-in. Adding a new jurisdiction = new function in this section.
# ═════════════════════════════════════════════════════════════════════════════


def investigate_wayback(name: str, entity_type: str = "person") -> dict:
    """Internet Archive Wayback Machine — probe well-known platform URLs
    for archived snapshots of the entity's profile. Captures social/
    professional presence even after original deletion.

    Targets a curated set of slugs because Wayback's CDX search isn't
    name-friendly. Adds an archived snapshot finding when the platform
    has ever captured a URL with the entity's slug.

    No API key. Public availability API.
    """
    result = {"source": "wayback", "findings": [], "discovered_entities": [], "status": "ok"}
    try:
        from urllib.parse import quote_plus
        slug_underscore = name.lower().replace(" ", "_")
        slug_hyphen = name.lower().replace(" ", "-")
        slug_concat = name.lower().replace(" ", "")
        # (platform_url_template, label) — wayback availability check per slug
        platforms = [
            (f"https://en.wikipedia.org/wiki/{slug_underscore.title()}", "wikipedia"),
            (f"https://www.linkedin.com/in/{slug_hyphen}", "linkedin"),
            (f"https://twitter.com/{slug_concat}", "twitter"),
            (f"https://github.com/{slug_concat}", "github"),
            (f"https://www.facebook.com/{slug_concat}", "facebook"),
            (f"https://www.instagram.com/{slug_concat}", "instagram"),
        ]
        for target, platform in platforms:
            _rate_limit("wayback")
            avail = _fetch_json(
                f"https://archive.org/wayback/available?url={quote_plus(target)}"
            )
            if not avail:
                continue
            closest = (avail.get("archived_snapshots") or {}).get("closest")
            if closest and closest.get("available"):
                snap = closest.get("url")
                ts = closest.get("timestamp", "")
                result["findings"].append({
                    "finding_type": "archived_profile",
                    "title": f"Archived {platform} profile",
                    "data": {
                        "url": snap,
                        "original_url": target,
                        "platform": platform,
                        "timestamp": ts,
                    },
                    "confidence": 0.45,  # slug-guess based, not exact match
                    "source_url": snap,
                })
        if not result["findings"]:
            result["status"] = "no_data"
    except Exception as e:
        log.warning("wayback failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_sunbiz_fl(name: str, entity_type: str = "person") -> dict:
    """Florida Sunbiz state business registry. Public search by officer
    name returns business affiliations + registered agent addresses
    (registered agent address is often the officer's home/business address).

    Public, no key. HTML-scraped (Sunbiz has no formal API).
    Pattern source for other states: NY DOS, CA SOS, TX SOS, DE Division
    of Corporations all expose similar public search. Add `investigate_sunbiz_ny`
    etc. following this same pattern.
    """
    result = {"source": "sunbiz_fl", "findings": [], "discovered_entities": [], "status": "ok"}
    if entity_type not in ("person", "organization", "company"):
        result["status"] = "skipped"
        return result
    try:
        _rate_limit("sunbiz_fl")
        from urllib.parse import quote_plus
        # Officer search returns HTML; we extract via a quick regex.
        # If the entity is an org we'd hit entity search instead.
        if entity_type == "person":
            url = (
                "https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults?"
                f"inquirytype=OfficerRegisteredAgentName&searchTerm={quote_plus(name)}"
            )
        else:
            url = (
                "https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults?"
                f"inquirytype=EntityName&searchTerm={quote_plus(name)}"
            )
        html = _fetch_text(url, timeout=20)
        if not html:
            result["status"] = "no_data"
            return result
        # Extract result rows. Sunbiz markup: <a href="/Inquiry/CorporationSearch/SearchResultDetail?...">NAME</a>
        import re
        matches = re.findall(
            r'<a[^>]+href="(/Inquiry/CorporationSearch/SearchResultDetail\?[^"]+)"[^>]*>([^<]+)</a>',
            html,
        )
        # Sunbiz officer search returns everyone whose FIRST name matches the
        # query — "Georgia Brook" returns hundreds of "GEORGIA, *". Filter
        # to results that contain ALL query tokens (case-insensitive) so we
        # only surface real matches.
        query_tokens = [t.lower() for t in name.split() if len(t) >= 2]
        seen: set[str] = set()
        for href, entity_name_raw in matches[:80]:
            entity_name = entity_name_raw.strip()
            if not entity_name or entity_name in seen:
                continue
            seen.add(entity_name)
            entity_lc = entity_name.lower()
            if not all(tok in entity_lc for tok in query_tokens):
                continue  # not a real match
            full_url = "https://search.sunbiz.org" + href
            result["findings"].append({
                "finding_type": "fl_business_affiliation",
                "title": entity_name,
                "data": {
                    "state": "FL",
                    "entity_name": entity_name,
                    "url": full_url,
                    "match_role": "officer" if entity_type == "person" else "entity",
                },
                "confidence": 0.75,
                "source_url": full_url,
            })
            if entity_type == "person":
                result["discovered_entities"].append({
                    "name": entity_name,
                    "entity_type": "organization",
                    "discovered_via": "sunbiz_fl_officer",
                    "evidence_url": full_url,
                })
        if not result["findings"]:
            result["status"] = "no_data"
    except Exception as e:
        log.warning("sunbiz_fl failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_github(name: str, entity_type: str = "person") -> dict:
    """GitHub user/org search. Public API, 60 req/hr unauthenticated.
    Returns matching usernames; if the user has set their location, email,
    or company publicly that's surfaced too. Universal — useful for any
    entity with a tech footprint.
    """
    result = {"source": "github", "findings": [], "discovered_entities": [], "status": "ok"}
    try:
        _rate_limit("github")
        from urllib.parse import quote_plus
        # /search/users supports `fullname:"Name"` plus the bare name.
        # We try fullname-quoted first (precise) then fall back to broad.
        url = (
            f"https://api.github.com/search/users?"
            f"q={quote_plus('fullname:' + name)}&per_page=10"
        )
        data = _fetch_json(
            url,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "Enki/1.0"},
        )
        items = (data or {}).get("items") or []
        # Augment with each user's full profile (location/bio/email).
        for item in items[:8]:
            login = item.get("login")
            api_url = item.get("url")
            if not (login and api_url):
                continue
            _rate_limit("github")
            profile = _fetch_json(
                api_url,
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "Enki/1.0"},
            ) or {}
            full_name = profile.get("name") or login
            # Only keep results whose full name actually matches (case-insens).
            if name.lower() not in (full_name or "").lower() and \
               name.lower() not in (login or "").lower():
                continue
            result["findings"].append({
                "finding_type": "github_profile",
                "title": full_name,
                "data": {
                    "login": login,
                    "name": profile.get("name"),
                    "bio": profile.get("bio"),
                    "company": profile.get("company"),
                    "location": profile.get("location"),
                    "email": profile.get("email"),
                    "blog": profile.get("blog"),
                    "twitter_username": profile.get("twitter_username"),
                    "public_repos": profile.get("public_repos"),
                    "followers": profile.get("followers"),
                    "url": profile.get("html_url"),
                },
                "confidence": 0.70 if profile.get("name") else 0.50,
                "source_url": profile.get("html_url"),
            })
        if not result["findings"]:
            result["status"] = "no_data"
    except Exception as e:
        log.warning("github failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_reddit(name: str, entity_type: str = "person") -> dict:
    """Reddit public search via the .json endpoints. No key, returns posts
    and comments matching the query. Universal — finds public discussion,
    leaked emails in comments, etc.
    """
    result = {"source": "reddit", "findings": [], "discovered_entities": [], "status": "ok"}
    try:
        _rate_limit("reddit")
        from urllib.parse import quote_plus
        # Reddit search across all subreddits, sorted by relevance.
        url = (
            f"https://www.reddit.com/search.json?"
            f"q={quote_plus('\"' + name + '\"')}&sort=relevance&limit=15"
        )
        data = _fetch_json(
            url,
            headers={"User-Agent": "Enki/1.0 (research@example.com)"},
        )
        if not data:
            result["status"] = "no_data"
            return result
        children = ((data.get("data") or {}).get("children")) or []
        for child in children[:15]:
            post = child.get("data") or {}
            title = post.get("title")
            permalink = post.get("permalink")
            if not (title and permalink):
                continue
            result["findings"].append({
                "finding_type": "reddit_post",
                "title": title[:200],
                "data": {
                    "subreddit": post.get("subreddit"),
                    "author": post.get("author"),
                    "url": f"https://reddit.com{permalink}",
                    "score": post.get("score"),
                    "num_comments": post.get("num_comments"),
                    "created_utc": post.get("created_utc"),
                    "selftext_snippet": (post.get("selftext") or "")[:300],
                },
                "confidence": 0.50,
                "source_url": f"https://reddit.com{permalink}",
            })
        if not result["findings"]:
            result["status"] = "no_data"
    except Exception as e:
        log.warning("reddit failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_uk_companies_house(name: str, entity_type: str = "person") -> dict:
    """UK Companies House — directorships, company filings, addresses for
    British entities. First non-US region-specific source (June 4 2026).

    Free public API, HTTP Basic auth with API key as username (empty
    password). Without an API key configured, returns no_data — same
    pattern as FEC without DATA_GOV_API_KEY. Operator gets the key via
    https://developer-specs.company-information.service.gov.uk/ and
    stores it as COMPANIES_HOUSE_API_KEY in /etc/enki/secrets.env or
    the per-tenant credentials store.

    For Sam Rowan (Simon Lowe), this returns his actual
    UK companies, directorships, fellow directors, registered addresses
    — the kind of data the previously-shown US sources had no chance
    of producing.
    """
    result = {"source": "uk_companies_house", "findings": [], "discovered_entities": [], "status": "ok"}

    api_key = get_credential("COMPANIES_HOUSE_API_KEY")
    if not api_key:
        result["status"] = "no_data"  # Quiet no-op until key is configured
        return result

    try:
        import base64
        _rate_limit("uk_companies_house")
        from urllib.parse import quote_plus

        # HTTP Basic auth: API key is the username, password is blank.
        auth_b64 = base64.b64encode(f"{api_key}:".encode()).decode()
        headers = {"Authorization": f"Basic {auth_b64}", "Accept": "application/json"}

        if entity_type in ("person",):
            # Officer search → returns people who are directors of UK
            # registered companies. The match is on name + extra metadata
            # we surface (date of birth, nationality, occupation) so the
            # disambiguator can verify it's the right person.
            data = _fetch_json(
                f"https://api.company-information.service.gov.uk/search/officers?q={quote_plus(name)}&items_per_page=10",
                headers=headers,
            )
            if not data or not data.get("items"):
                result["status"] = "no_data"
                return result
            for officer in (data.get("items") or [])[:10]:
                title = officer.get("title") or officer.get("description") or name
                officer_id = officer.get("links", {}).get("self", "").rsplit("/", 1)[-1]
                result["findings"].append({
                    "type": "uk_director",
                    "title": f"Companies House: {title}",
                    "data": {
                        "officer_name": title,
                        "officer_id": officer_id,
                        "description": officer.get("description"),
                        "appointment_count": officer.get("appointment_count"),
                        "address_snippet": officer.get("address_snippet"),
                        "date_of_birth": officer.get("date_of_birth"),
                        "page_url": f"https://find-and-update.company-information.service.gov.uk/officers/{officer_id}" if officer_id else None,
                        "source_url": f"https://find-and-update.company-information.service.gov.uk/officers/{officer_id}" if officer_id else None,
                    },
                    "confidence": 0.85,
                })

        elif entity_type in ("organization", "company"):
            # Company search.
            data = _fetch_json(
                f"https://api.company-information.service.gov.uk/search/companies?q={quote_plus(name)}&items_per_page=10",
                headers=headers,
            )
            if not data or not data.get("items"):
                result["status"] = "no_data"
                return result
            for company in (data.get("items") or [])[:10]:
                company_number = company.get("company_number")
                result["findings"].append({
                    "type": "uk_company",
                    "title": f"Companies House: {company.get('title') or name}",
                    "data": {
                        "company_name": company.get("title"),
                        "company_number": company_number,
                        "company_status": company.get("company_status"),
                        "company_type": company.get("company_type"),
                        "incorporation_date": company.get("date_of_creation"),
                        "address_snippet": company.get("address_snippet"),
                        "description": company.get("description"),
                        "page_url": f"https://find-and-update.company-information.service.gov.uk/company/{company_number}" if company_number else None,
                        "source_url": f"https://find-and-update.company-information.service.gov.uk/company/{company_number}" if company_number else None,
                    },
                    "confidence": 0.90,
                })

        if not result["findings"]:
            result["status"] = "no_data"

    except Exception as e:
        log.warning("uk_companies_house failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_ny_dos(name: str, entity_type: str = "person") -> dict:
    """New York Department of State business entity search. Public, no key.
    Returns NY business affiliations.
    """
    result = {"source": "ny_dos", "findings": [], "discovered_entities": [], "status": "ok"}
    if entity_type not in ("person", "organization", "company"):
        result["status"] = "skipped"
        return result
    try:
        _rate_limit("ny_dos")
        from urllib.parse import quote_plus
        # NY DOS public corp search returns JSON via their search portal API.
        # We query the entity-name endpoint (officer search is harder there).
        url = (
            "https://apps.dos.ny.gov/publicInquiry/api/business/entitiesSearch?"
            f"name={quote_plus(name)}&type=PARTIAL&start=0&count=20"
        )
        data = _fetch_json(url, timeout=20)
        if not data:
            result["status"] = "no_data"
            return result
        items = data.get("rows") or data.get("results") or []
        query_tokens = [t.lower() for t in name.split() if len(t) >= 2]
        for item in items[:25]:
            ent_name = (item.get("name") or item.get("entityName") or "").strip()
            if not ent_name:
                continue
            entity_lc = ent_name.lower()
            if not all(tok in entity_lc for tok in query_tokens):
                continue
            dos_id = item.get("id") or item.get("dosID")
            ent_status = item.get("status") or item.get("entityStatus")
            ent_type = item.get("type") or item.get("entityType")
            url_detail = (
                f"https://apps.dos.ny.gov/publicInquiry/EntityDisplay?dosID={dos_id}"
                if dos_id else None
            )
            result["findings"].append({
                "finding_type": "ny_business_affiliation",
                "title": ent_name,
                "data": {
                    "state": "NY",
                    "entity_name": ent_name,
                    "dos_id": dos_id,
                    "status": ent_status,
                    "entity_type": ent_type,
                    "url": url_detail,
                },
                "confidence": 0.70,
                "source_url": url_detail,
            })
            if entity_type == "person":
                result["discovered_entities"].append({
                    "name": ent_name,
                    "entity_type": "organization",
                    "discovered_via": "ny_dos",
                    "evidence_url": url_detail,
                })
        if not result["findings"]:
            result["status"] = "no_data"
    except Exception as e:
        log.warning("ny_dos failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_ca_sos(name: str, entity_type: str = "person") -> dict:
    """California Secretary of State business search via CA's bizfileOnline.
    Public, no key. Returns CA business affiliations.
    """
    result = {"source": "ca_sos", "findings": [], "discovered_entities": [], "status": "ok"}
    if entity_type not in ("person", "organization", "company"):
        result["status"] = "skipped"
        return result
    try:
        _rate_limit("ca_sos")
        from urllib.parse import quote_plus
        # CA bizfile has a public JSON search endpoint:
        url = "https://bizfileonline.sos.ca.gov/api/Records/businesssearch"
        body = {
            "SEARCH_VALUE": name,
            "SEARCH_FILTER_TYPE_ID": "0",
            "STARTS_WITH_YN": False,
            "ACTIVE_ONLY_YN": False,
        }
        data = _post_json(url, body, timeout=20)
        if not data:
            result["status"] = "no_data"
            return result
        rows = data.get("rows") or []
        query_tokens = [t.lower() for t in name.split() if len(t) >= 2]
        for row in rows[:25]:
            ent_name = (row.get("TITLE") or row.get("NAME") or "").strip()
            if not ent_name:
                continue
            entity_lc = ent_name.lower()
            if not all(tok in entity_lc for tok in query_tokens):
                continue
            ent_id = row.get("ID")
            url_detail = (
                f"https://bizfileonline.sos.ca.gov/search/business/{ent_id}"
                if ent_id else None
            )
            result["findings"].append({
                "finding_type": "ca_business_affiliation",
                "title": ent_name,
                "data": {
                    "state": "CA",
                    "entity_name": ent_name,
                    "ca_id": ent_id,
                    "status": row.get("STATUS"),
                    "entity_type": row.get("RECORD_TYPE"),
                    "filing_date": row.get("FILING_DATE"),
                    "url": url_detail,
                },
                "confidence": 0.70,
                "source_url": url_detail,
            })
            if entity_type == "person":
                result["discovered_entities"].append({
                    "name": ent_name,
                    "entity_type": "organization",
                    "discovered_via": "ca_sos",
                    "evidence_url": url_detail,
                })
        if not result["findings"]:
            result["status"] = "no_data"
    except Exception as e:
        log.warning("ca_sos failed for %s: %s", name, e)
        result["status"] = "error"
    return result


def investigate_duckduckgo(name: str, entity_type: str = "person") -> dict:
    """DuckDuckGo Instant Answer API. Returns Wikipedia-derived summary +
    disambiguation links for well-known entities. Public, no key, no rate
    limit (gentle use). Universal across entity types.

    Distinct from `investigate_wikipedia` in that DDG handles
    disambiguation natively (returns multiple candidates when ambiguous)
    and surfaces non-Wikipedia summaries (Britannica, etc.) for some entities.
    """
    result = {"source": "duckduckgo", "findings": [], "discovered_entities": [], "status": "ok"}
    try:
        _rate_limit("duckduckgo")
        from urllib.parse import quote_plus
        url = (
            f"https://api.duckduckgo.com/?q={quote_plus(name)}"
            f"&format=json&no_html=1&skip_disambig=0&t=enki"
        )
        data = _fetch_json(url)
        if not data:
            result["status"] = "no_data"
            return result
        abstract = (data.get("Abstract") or "").strip()
        heading = data.get("Heading") or name
        if abstract:
            # Validate the abstract actually concerns the seed name —
            # DDG's instant-answer for an ambiguous query can return
            # the primary disambiguation target's summary even when the
            # operator was after a less-popular sense. We let it through
            # only if heading or abstract contains a seed token.
            haystack = f"{heading} | {abstract}"
            if _text_mentions_seed(name, haystack, require_surname=False):
                result["findings"].append({
                    "finding_type": "summary",
                    "title": heading,
                    "data": {
                        "summary": abstract,
                        "url": data.get("AbstractURL"),
                        "source": data.get("AbstractSource"),
                        "image": data.get("Image") or None,
                    },
                    "confidence": 0.85,
                    "source_url": data.get("AbstractURL"),
                })
        # Related topics surface disambiguation candidates. Validate
        # each — DDG's RelatedTopics for a name search often includes
        # tangentially-related entries that don't actually mention the
        # name (e.g. parent-category pages).
        for rt in (data.get("RelatedTopics") or [])[:10]:
            text = rt.get("Text") or ""
            fu = rt.get("FirstURL") or ""
            if not text or not fu:
                continue
            if not _text_mentions_seed(name, text, require_surname=False):
                continue
            result["findings"].append({
                "finding_type": "related",
                "title": text[:100],
                "data": {"url": fu, "summary": text},
                "confidence": 0.5,
                "source_url": fu,
            })
        if not result["findings"]:
            result["status"] = "no_data"
    except Exception as e:
        log.warning("duckduckgo failed for %s: %s", name, e)
        result["status"] = "error"
    return result


# ─── ALL_SOURCES ──────────────────────────────────────────────────────────────

ALL_SOURCES = {
    "sec_edgar": investigate_sec_edgar,
    "fec": investigate_fec,
    "usaspending": investigate_usaspending,
    "propublica_nonprofits": investigate_nonprofits,
    "courtlistener": investigate_courts,
    "fdic": investigate_fdic,
    "google_books": investigate_google_books,
    "fda": investigate_fda,
    "epa_echo": investigate_epa,
    "ofac": investigate_ofac,
    "congress": investigate_congress,
    "loc": investigate_loc,
    "sam_gov": investigate_sam,
    # Phase 7b.2 bridges into core/research/sources.py
    "wikipedia": investigate_wikipedia,
    "wikidata": investigate_wikidata,
    "opensanctions": investigate_opensanctions,
    "nominatim": investigate_nominatim,
    # Phase 8b.6.1 bridges (researchpack 1.1.0)
    "cia_factbook": investigate_cia_factbook,
    "geonames": investigate_geonames,
    "rest_countries": investigate_rest_countries,
    "world_bank": investigate_world_bank,
    "opencorporates": investigate_opencorporates,
    "faa_aircraft": investigate_faa_aircraft,
    "un_sanctions": investigate_un_sanctions,
    "federal_register": investigate_federal_register,
    # Public-OSINT (no key, public-data, jurisdiction-scoped or universal)
    "wayback": investigate_wayback,
    "sunbiz_fl": investigate_sunbiz_fl,
    "ny_dos":   investigate_ny_dos,
    "ca_sos":   investigate_ca_sos,
    "duckduckgo": investigate_duckduckgo,
    "github":   investigate_github,
    "reddit":   investigate_reddit,
    # UK region (June 4 2026 — first non-US region-specific source)
    "uk_companies_house": investigate_uk_companies_house,
}


# ─── SOURCE JURISDICTION REGISTRY ─────────────────────────────────────────────
#
# Each source declares which ISO 3166 alpha-2 country codes it has meaningful
# data on. Special value "*" means universal (applies to any nationality —
# Wikipedia is in every country, GitHub users come from everywhere, etc.).
#
# When an investigation has detected the entity's country_of_citizenship
# from Wikipedia/Wikidata findings, _run_investigation filters subsequent
# source calls to those whose jurisdictions include either "*" or the
# detected country. Sources that don't match are SKIPPED rather than
# returning false-positive matches (e.g. FEC returning a US person who
# shares the name 'Sam Rowan' when the entity is actually British).
#
# When no nationality has been detected yet, all sources run (current
# behaviour — graceful fallback). Wikipedia and Wikidata always run first
# so the routing has data to filter on by the time other sources execute.
#
# Adding a new region-specific source: define investigate_<source>, register
# it in ALL_SOURCES above, and add an entry here with jurisdictions list.
# See the UK Companies House addition (June 4) as the template.

SOURCE_JURISDICTIONS: dict[str, list[str]] = {
    # Universal (apply to any nationality)
    "wikipedia":               ["*"],   # multi-language available
    "wikidata":                ["*"],
    "google_books":            ["*"],
    "duckduckgo":              ["*"],
    "wayback":                 ["*"],
    "github":                  ["*"],
    "reddit":                  ["*"],
    "opensanctions":           ["*"],   # global sanctions DB
    "un_sanctions":            ["*"],
    "ofac":                    ["*"],   # global by mandate; SDN check is the point
    "nominatim":               ["*"],   # OSM gazetteer
    "opencorporates":          ["*"],   # global corporate DB (US-heavy but covers everywhere)
    "cia_factbook":            ["*"],   # countries dataset
    "geonames":                ["*"],
    "rest_countries":          ["*"],
    "world_bank":              ["*"],
    "faa_aircraft":            ["*"],   # international aircraft registry
    "federal_register":        ["*"],   # mostly US but lists international actions too

    # US-only sources — skip when entity is non-US to stop false-positive
    # 'a US person with the same name' findings from polluting the dossier.
    "sec_edgar":               ["US"],
    "fec":                     ["US"],
    "usaspending":             ["US"],
    "propublica_nonprofits":   ["US"],
    "courtlistener":           ["US"],
    "fdic":                    ["US"],
    "fda":                     ["US"],
    "epa_echo":                ["US"],
    "congress":                ["US"],
    "loc":                     ["US"],
    "sam_gov":                 ["US"],
    "sunbiz_fl":               ["US"],
    "ny_dos":                  ["US"],
    "ca_sos":                  ["US"],

    # UK sources (added June 4 2026 — see investigate_uk_companies_house)
    "uk_companies_house":      ["GB", "UK"],
}


def source_applies_to_country(source_name: str, iso_country: Optional[str]) -> bool:
    """Return True if the source's declared jurisdictions match the entity's
    detected country. Sources without an entry in SOURCE_JURISDICTIONS are
    treated as universal (no metadata → don't gate).
    """
    if not iso_country:
        return True  # No country detected yet — don't filter
    juris = SOURCE_JURISDICTIONS.get(source_name)
    if juris is None:
        return True  # Unknown source — let it run, don't silently drop
    if "*" in juris:
        return True
    iso_country = iso_country.upper()
    # Accept either GB or UK style codes for British entities (different
    # data sources use one or the other; we register both spellings).
    return iso_country in juris


def run_investigation(
    name: str,
    entity_type: str = "person",
    sources: Optional[list[str]] = None,
) -> dict:
    """Run investigation across all (or specified) sources.

    Returns combined results from all sources.
    """
    if sources is None:
        sources = list(ALL_SOURCES.keys())

    combined = {
        "name": name,
        "entity_type": entity_type,
        "sources_queried": sources,
        "sources_completed": [],
        "sources_failed": [],
        "all_findings": [],
        "all_discovered": [],
        "total_findings": 0,
        "total_discovered": 0,
    }

    for source_name in sources:
        handler = ALL_SOURCES.get(source_name)
        if not handler:
            log.warning("Unknown investigation source: %s", source_name)
            continue

        try:
            log.info("Investigating %s via %s...", name, source_name)
            result = handler(name, entity_type)

            for f in result.get("findings", []):
                f["source"] = source_name
            combined["all_findings"].extend(result.get("findings", []))
            combined["all_discovered"].extend(result.get("discovered_entities", []))

            if result.get("status") != "error":
                combined["sources_completed"].append(source_name)
            else:
                combined["sources_failed"].append(source_name)

        except Exception as e:
            log.warning("Investigation source %s failed for %s: %s", source_name, name, e)
            combined["sources_failed"].append(source_name)

    combined["total_findings"] = len(combined["all_findings"])
    combined["total_discovered"] = len(combined["all_discovered"])
    return combined
