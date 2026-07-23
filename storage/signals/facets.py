"""
Faceted Categorization — multi-dimensional signal tagging.

Extracts facets from signal payloads at ingestion time:
  - countries: ISO country codes from gazetteer
  - people: Person entity names from headline/summary NER
  - organizations: Organization names
  - topic: Short topic label from keyword clustering
  - topic_keywords: Keywords for grouping/dedup
  - event_type: military_action, diplomatic, economic, disaster, legal, etc.
  - threat_domain: kinetic, cyber, financial, health, environmental
  - category: High-level category from keyword matching (World News, Tech, etc.)

Two tiers:
  Tier 1 (this module): Fast keyword/regex extraction, <5ms per signal.
  Tier 2 (future): LLM enrichment for normalization and better labels.
"""

from __future__ import annotations

import re
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger("enki.facets")


# ─── Country Extraction ─────────────────────────────────────────────────

# Country names → ISO 2-letter codes + display labels
_COUNTRY_MAP: dict[str, tuple[str, str]] = {
    # Major countries
    "ukraine": ("UA", "Ukraine"), "russia": ("RU", "Russia"),
    "china": ("CN", "China"), "taiwan": ("TW", "Taiwan"),
    "iran": ("IR", "Iran"), "iraq": ("IQ", "Iraq"),
    "israel": ("IL", "Israel"), "palestine": ("PS", "Palestine"),
    "gaza": ("PS", "Gaza"), "syria": ("SY", "Syria"),
    "afghanistan": ("AF", "Afghanistan"), "pakistan": ("PK", "Pakistan"),
    "india": ("IN", "India"), "japan": ("JP", "Japan"),
    "north korea": ("KP", "North Korea"), "south korea": ("KR", "South Korea"),
    "germany": ("DE", "Germany"), "france": ("FR", "France"),
    "united kingdom": ("GB", "United Kingdom"), "italy": ("IT", "Italy"),
    "spain": ("ES", "Spain"), "poland": ("PL", "Poland"),
    "turkey": ("TR", "Turkey"), "saudi arabia": ("SA", "Saudi Arabia"),
    "brazil": ("BR", "Brazil"), "mexico": ("MX", "Mexico"),
    "canada": ("CA", "Canada"), "australia": ("AU", "Australia"),
    "egypt": ("EG", "Egypt"), "nigeria": ("NG", "Nigeria"),
    "south africa": ("ZA", "South Africa"), "ethiopia": ("ET", "Ethiopia"),
    "kenya": ("KE", "Kenya"), "indonesia": ("ID", "Indonesia"),
    "philippines": ("PH", "Philippines"), "vietnam": ("VN", "Vietnam"),
    "thailand": ("TH", "Thailand"), "myanmar": ("MM", "Myanmar"),
    "yemen": ("YE", "Yemen"), "libya": ("LY", "Libya"),
    "sudan": ("SD", "Sudan"), "somalia": ("SO", "Somalia"),
    "venezuela": ("VE", "Venezuela"), "colombia": ("CO", "Colombia"),
    "argentina": ("AR", "Argentina"), "chile": ("CL", "Chile"),
    "cuba": ("CU", "Cuba"), "haiti": ("HT", "Haiti"),
    "united states": ("US", "United States"), "usa": ("US", "United States"),
    "u.s.": ("US", "United States"), "america": ("US", "United States"),
    "uk": ("GB", "United Kingdom"), "britain": ("GB", "United Kingdom"),
    "uae": ("AE", "UAE"), "qatar": ("QA", "Qatar"),
    "singapore": ("SG", "Singapore"), "malaysia": ("MY", "Malaysia"),
    "new zealand": ("NZ", "New Zealand"), "sweden": ("SE", "Sweden"),
    "norway": ("NO", "Norway"), "finland": ("FI", "Finland"),
    "denmark": ("DK", "Denmark"), "netherlands": ("NL", "Netherlands"),
    "belgium": ("BE", "Belgium"), "switzerland": ("CH", "Switzerland"),
    "austria": ("AT", "Austria"), "greece": ("GR", "Greece"),
    "portugal": ("PT", "Portugal"), "czech republic": ("CZ", "Czech Republic"),
    "romania": ("RO", "Romania"), "hungary": ("HU", "Hungary"),
    "ireland": ("IE", "Ireland"), "scotland": ("GB", "United Kingdom"),
    "peru": ("PE", "Peru"), "ecuador": ("EC", "Ecuador"),
    "bolivia": ("BO", "Bolivia"), "uruguay": ("UY", "Uruguay"),
    "paraguay": ("PY", "Paraguay"), "panama": ("PA", "Panama"),
    "guatemala": ("GT", "Guatemala"), "honduras": ("HN", "Honduras"),
    "morocco": ("MA", "Morocco"), "algeria": ("DZ", "Algeria"),
    "tunisia": ("TN", "Tunisia"), "lebanon": ("LB", "Lebanon"),
    "jordan": ("JO", "Jordan"), "oman": ("OM", "Oman"),
    "kuwait": ("KW", "Kuwait"), "bahrain": ("BH", "Bahrain"),
    "bangladesh": ("BD", "Bangladesh"), "sri lanka": ("LK", "Sri Lanka"),
    "nepal": ("NP", "Nepal"), "cambodia": ("KH", "Cambodia"),
    "laos": ("LA", "Laos"),
    # Demonyms
    "ukrainian": ("UA", "Ukraine"), "russian": ("RU", "Russia"),
    "chinese": ("CN", "China"), "iranian": ("IR", "Iran"),
    "israeli": ("IL", "Israel"), "palestinian": ("PS", "Palestine"),
    "american": ("US", "United States"), "british": ("GB", "United Kingdom"),
    "french": ("FR", "France"), "german": ("DE", "Germany"),
    "japanese": ("JP", "Japan"), "korean": ("KR", "South Korea"),
    "indian": ("IN", "India"), "brazilian": ("BR", "Brazil"),
    "mexican": ("MX", "Mexico"), "canadian": ("CA", "Canada"),
    "australian": ("AU", "Australia"), "turkish": ("TR", "Turkey"),
    "saudi": ("SA", "Saudi Arabia"), "egyptian": ("EG", "Egypt"),
    "nigerian": ("NG", "Nigeria"), "colombian": ("CO", "Colombia"),
    "venezuelan": ("VE", "Venezuela"), "syrian": ("SY", "Syria"),
    "afghan": ("AF", "Afghanistan"), "pakistani": ("PK", "Pakistan"),
    "iraqi": ("IQ", "Iraq"), "lebanese": ("LB", "Lebanon"),
    "yemeni": ("YE", "Yemen"), "libyan": ("LY", "Libya"),
    "italian": ("IT", "Italy"), "spanish": ("ES", "Spain"),
    "polish": ("PL", "Poland"), "swedish": ("SE", "Sweden"),
    "dutch": ("NL", "Netherlands"), "swiss": ("CH", "Switzerland"),
    # Major city → country mapping
    "kyiv": ("UA", "Ukraine"), "moscow": ("RU", "Russia"),
    "beijing": ("CN", "China"), "shanghai": ("CN", "China"),
    "tokyo": ("JP", "Japan"), "seoul": ("KR", "South Korea"),
    "london": ("GB", "United Kingdom"), "paris": ("FR", "France"),
    "berlin": ("DE", "Germany"), "washington": ("US", "United States"),
    "new york": ("US", "United States"), "los angeles": ("US", "United States"),
    "baghdad": ("IQ", "Iraq"), "tehran": ("IR", "Iran"),
    "tel aviv": ("IL", "Israel"), "jerusalem": ("IL", "Israel"),
    "cairo": ("EG", "Egypt"), "riyadh": ("SA", "Saudi Arabia"),
    "dubai": ("AE", "UAE"), "istanbul": ("TR", "Turkey"),
    "hong kong": ("HK", "Hong Kong"), "taipei": ("TW", "Taiwan"),
    "singapore": ("SG", "Singapore"), "bangkok": ("TH", "Thailand"),
    "kabul": ("AF", "Afghanistan"), "islamabad": ("PK", "Pakistan"),
    "havana": ("CU", "Cuba"), "caracas": ("VE", "Venezuela"),
    "crimea": ("UA", "Ukraine"), "donbas": ("UA", "Ukraine"),
    "kremlin": ("RU", "Russia"), "pentagon": ("US", "United States"),
    "nato": ("BE", "NATO"), "wall street": ("US", "United States"),
}

_COUNTRY_PATTERNS = sorted(_COUNTRY_MAP.keys(), key=len, reverse=True)


def extract_countries(headline: str, summary: str) -> list[dict]:
    """Extract countries mentioned in text with confidence scores.
    Returns list of {code, label, confidence}.

    Ambiguous single-word matches (Jordan, Chad, Turkey) get lower confidence.
    """
    text = f" {headline} {summary} ".lower()
    # Ambiguous names that could be people or other things
    ambiguous = {"jordan", "chad", "turkey", "georgia", "china", "india"}
    # Words only in heading/title get lower weight than body mentions
    headline_lower = f" {headline} ".lower()

    matches: dict[str, dict] = {}

    for name in _COUNTRY_PATTERNS:
        if f" {name} " in text or f" {name}." in text or f" {name}," in text or f" {name}'" in text:
            code, label = _COUNTRY_MAP[name]

            confidence = 0.8
            evidence_parts = []

            # Multi-word match = higher confidence
            if " " in name:
                confidence = 0.95
                evidence_parts.append(f"full name: {name}")
            # Demonym (ends in -ian, -ese, -ish, etc.) = high confidence
            elif name.endswith(("ian", "ese", "ish", "ese", "ese")):
                confidence = 0.9
                evidence_parts.append(f"demonym: {name}")
            # Ambiguous single word = low confidence unless context confirms
            elif name in ambiguous:
                confidence = 0.4
                evidence_parts.append(f"ambiguous: {name}")
            else:
                evidence_parts.append(f"name: {name}")

            # Boost if multiple patterns for same country match
            if code in matches:
                matches[code]["confidence"] = min(matches[code]["confidence"] + 0.15, 0.98)
                matches[code]["evidence"] += f" + {name}"
            else:
                matches[code] = {
                    "code": code,
                    "label": label,
                    "confidence": round(confidence, 2),
                    "evidence": ", ".join(evidence_parts),
                }

    return list(matches.values())


# ─── Person / Organization Extraction ────────────────────────────────────

# Simple proper noun NER: sequences of capitalized words
_TITLE_PREFIXES = {
    "president", "minister", "prime", "king", "queen", "prince", "princess",
    "general", "admiral", "commander", "director", "chairman", "chairwoman",
    "ceo", "cto", "cfo", "senator", "governor", "mayor", "judge",
    "pope", "ayatollah", "sheikh", "sultan", "emperor",
    "dr", "mr", "mrs", "ms", "prof",
}

_KNOWN_ORGS = {
    "nato": "NATO", "un": "United Nations", "united nations": "United Nations",
    "eu": "European Union", "european union": "European Union",
    "who": "WHO", "fda": "FDA", "fbi": "FBI", "cia": "CIA", "nsa": "NSA",
    "pentagon": "Pentagon", "nasa": "NASA", "epa": "EPA", "cdc": "CDC",
    "sec": "SEC", "fed": "Federal Reserve", "federal reserve": "Federal Reserve",
    "imf": "IMF", "world bank": "World Bank",
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
    "microsoft": "Microsoft", "apple": "Apple", "amazon": "Amazon",
    "meta": "Meta", "tesla": "Tesla", "spacex": "SpaceX", "nvidia": "NVIDIA",
    "twitter": "Twitter", "reuters": "Reuters", "bbc": "BBC",
    "cnn": "CNN", "fox news": "Fox News", "al jazeera": "Al Jazeera",
    "hamas": "Hamas", "hezbollah": "Hezbollah", "isis": "ISIS",
    "taliban": "Taliban", "wagner": "Wagner Group",
    "idf": "IDF", "iaea": "IAEA", "opec": "OPEC",
    "cisa": "CISA", "nist": "NIST",
}

# Words that look like proper nouns but aren't people
_NOT_PEOPLE = {
    "the", "this", "that", "said", "says", "new", "how", "why", "what",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "reuters", "associated", "press", "bbc", "cnn", "fox", "news",
    "update", "breaking", "report", "analysis", "opinion", "exclusive",
    "photo", "video", "watch", "live", "latest", "first", "last",
    "north", "south", "east", "west", "united", "states", "kingdom",
    "wall", "street", "silicon", "valley",
}


def extract_people(headline: str, summary: str) -> list[dict]:
    """Extract likely person names WITH confidence scores.
    Returns list of {name, confidence, evidence}."""
    text = f"{headline}. {summary}"
    people: dict[str, dict] = {}

    # Pattern 1: Title + Name (highest confidence — explicit marker)
    for match in re.finditer(
        r'\b(?:' + '|'.join(_TITLE_PREFIXES) + r')\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',
        text, re.IGNORECASE
    ):
        name = match.group(1).strip()
        if len(name) > 2 and name.split()[0].lower() not in _NOT_PEOPLE:
            people[name] = {"name": name, "confidence": 0.85, "evidence": "title prefix"}

    # Pattern 2: Two+ consecutive capitalized words (medium confidence)
    for match in re.finditer(r'(?<=[a-z,;:]\s)([A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', text):
        name = match.group(1).strip()
        words = name.split()
        if all(w.lower() not in _NOT_PEOPLE for w in words):
            if name.lower() not in _KNOWN_ORGS and name.lower() not in _COUNTRY_MAP:
                # Skip if already tagged with higher confidence from title pattern
                if name not in people:
                    # Confidence depends on context
                    confidence = 0.55
                    # Boost if mentioned multiple times
                    mentions = text.count(name)
                    if mentions >= 3:
                        confidence += 0.15
                    elif mentions >= 2:
                        confidence += 0.08
                    # Penalize if any word is a geography/place name
                    if any(w.lower() in _COUNTRY_MAP for w in words):
                        confidence -= 0.3
                    people[name] = {
                        "name": name,
                        "confidence": round(min(max(confidence, 0.0), 1.0), 2),
                        "evidence": f"proper noun ({mentions}x)",
                    }

    result = sorted(people.values(), key=lambda p: -p["confidence"])[:10]
    return result


def extract_people_names(headline: str, summary: str) -> list[str]:
    """Legacy API — return just names (only high-confidence)."""
    people = extract_people(headline, summary)
    return [p["name"] for p in people if p["confidence"] >= 0.6]


def extract_organizations_with_confidence(headline: str, summary: str) -> list[dict]:
    """Extract organization names WITH confidence scores."""
    text = f" {headline} {summary} "
    text_lower = text.lower()
    orgs: dict[str, dict] = {}

    # Pattern 1: Known organizations (highest confidence)
    for key, canonical in _KNOWN_ORGS.items():
        if f" {key} " in text_lower or f" {key}." in text_lower or f" {key}," in text_lower:
            orgs[canonical] = {"name": canonical, "confidence": 0.95, "evidence": "known org"}

    # Pattern 2: ALL-CAPS acronyms (3-6 letters, likely org)
    for match in re.finditer(r'\b([A-Z]{3,6})\b', text):
        acronym = match.group(1)
        if acronym in ("USA", "UK", "USSR", "EU", "UN"):
            continue  # These are handled as countries/known
        if acronym not in orgs:
            orgs[acronym] = {"name": acronym, "confidence": 0.6, "evidence": "ALL-CAPS acronym"}

    # Pattern 3: Corporate suffixes (Inc, Corp, Ltd, Co)
    for match in re.finditer(r'\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\s+(Inc\.?|Corp\.?|Ltd\.?|Co\.?|LLC|GmbH|AG|SA)\b', text):
        name = match.group(1).strip()
        if name not in orgs:
            orgs[name] = {"name": name, "confidence": 0.85, "evidence": "corporate suffix"}

    return sorted(orgs.values(), key=lambda o: -o["confidence"])[:10]


def extract_organizations(headline: str, summary: str) -> list[str]:
    """Legacy API — return just high-confidence org names."""
    orgs = extract_organizations_with_confidence(headline, summary)
    return [o["name"] for o in orgs if o["confidence"] >= 0.6]


def _extract_organizations_legacy(headline: str, summary: str) -> list[str]:
    """Old keyword-only extractor (kept for reference)."""
    text = f" {headline} {summary} ".lower()
    orgs = []
    seen = set()

    # Check known organizations
    for key, canonical in _KNOWN_ORGS.items():
        if f" {key} " in text or f" {key}." in text or f" {key}," in text or f" {key}'" in text:
            if canonical not in seen:
                orgs.append(canonical)
                seen.add(canonical)

    return orgs[:10]


# ─── Event Type Classification ───────────────────────────────────────────

_EVENT_TYPE_KEYWORDS: dict[str, set[str]] = {
    "military_action": {
        "missile", "airstrike", "strike", "troops", "deployment", "artillery",
        "combat", "drone", "bombing", "shelling", "offensive", "counter-offensive",
        "invasion", "assault", "intercept", "shoot down", "air defense",
    },
    "diplomatic": {
        "sanctions", "treaty", "summit", "talks", "negotiations", "ceasefire",
        "peace", "ambassador", "diplomat", "bilateral", "multilateral",
        "alliance", "agreement", "accord",
    },
    "political": {
        "election", "vote", "president", "parliament", "legislation", "bill",
        "law", "policy", "campaign", "party", "opposition", "protest",
        "coup", "impeachment", "resignation",
    },
    "economic": {
        "market", "stock", "economy", "inflation", "gdp", "trade", "tariff",
        "earnings", "recession", "interest rate", "fed", "central bank",
        "investment", "ipo", "bankruptcy",
    },
    "disaster": {
        "earthquake", "hurricane", "flood", "tornado", "wildfire", "tsunami",
        "eruption", "landslide", "drought", "famine", "disaster", "emergency",
        "evacuation", "casualties",
    },
    "cyber": {
        "hack", "breach", "malware", "ransomware", "vulnerability", "exploit",
        "phishing", "cve", "zero-day", "backdoor", "botnet", "ddos",
        "data leak", "cybersecurity",
    },
    "legal": {
        "court", "trial", "verdict", "lawsuit", "indictment", "arrest",
        "charged", "convicted", "acquitted", "sentence", "ruling",
        "supreme court", "prosecution",
    },
    "scientific": {
        "research", "study", "discovery", "experiment", "clinical trial",
        "peer review", "breakthrough", "vaccine", "genome", "quantum",
        "launch", "orbit", "nasa", "space",
    },
    "terrorism": {
        "terrorist", "terrorism", "attack", "bombing", "hostage", "kidnap",
        "extremist", "insurgent", "militant", "isis", "al-qaeda",
    },
}


def classify_event_type(headline: str, summary: str) -> str | None:
    """Classify into an event type. Returns best match or None."""
    text = f"{headline} {summary}".lower()
    scores: dict[str, int] = {}

    for event_type, keywords in _EVENT_TYPE_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in text:
                score += 2 if " " in kw else 1
        if score > 0:
            scores[event_type] = score

    if not scores:
        return None
    return max(scores, key=scores.get)  # type: ignore


# ─── Threat Domain ───────────────────────────────────────────────────────

def map_threat_domain(signal_type: str, event_type: str | None) -> str | None:
    """Map to threat domain from signal type and event type."""
    if not signal_type:
        return None

    # From signal type
    if "security" in signal_type:
        return "cyber"
    if "environmental" in signal_type:
        return "environmental"
    if "financial" in signal_type:
        return "financial"

    # From event type
    event_domain = {
        "military_action": "kinetic",
        "terrorism": "kinetic",
        "cyber": "cyber",
        "disaster": "environmental",
        "economic": "financial",
        "scientific": "scientific",
    }
    if event_type and event_type in event_domain:
        return event_domain[event_type]

    return None


# ─── Topic / Category (reuses feed_graph logic) ─────────────────────────

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "nor",
    "not", "only", "own", "same", "so", "than", "too", "very", "just",
    "don", "now", "and", "but", "or", "if", "while", "that", "this",
    "what", "which", "who", "whom", "these", "those", "it", "its", "he",
    "she", "they", "them", "his", "her", "their", "my", "your", "our",
    "we", "you", "me", "him", "us", "up", "about", "new", "also", "says",
    "said", "one", "two", "first", "last", "many", "much", "still", "show",
    "shows", "get", "gets", "got", "make", "makes", "made", "take", "takes",
    "come", "comes", "go", "goes", "see", "well", "back", "even", "give",
    "way", "because", "every", "good", "like", "long", "look", "think",
    "know", "time", "year", "years", "people", "right", "day", "world",
    "going", "want", "been", "since", "using", "use", "after", "via",
    "per", "around", "without", "across", "against", "along", "among",
    "story", "article", "report", "update", "source", "news",
}

_CATEGORY_KEYWORDS: dict[str, set[str]] = {
    "World News": {
        "war", "election", "president", "nato", "treaty", "sanctions", "diplomacy",
        "united nations", "geopolitics", "conflict", "ceasefire", "peace talks",
        "parliament", "prime minister", "coup", "protest", "refugee",
        "embassy", "foreign policy", "summit", "bilateral", "humanitarian",
        "ukraine", "russia", "china", "iran", "israel", "gaza", "palestine",
        "korea", "taiwan", "syria", "afghanistan", "terrorism", "insurgency",
    },
    "Cybersecurity": {
        "vulnerability", "breach", "hack", "malware", "ransomware", "exploit",
        "cve", "phishing", "zero-day", "backdoor", "botnet", "cybersecurity",
        "infosec", "cisa", "apt", "data leak", "encryption", "intrusion",
    },
    "Tech": {
        "software", "startup", "developer", "code", "open source", "github",
        "programming", "api", "framework", "database", "cloud", "saas",
        "chip", "semiconductor", "linux", "browser", "platform",
    },
    "AI & ML": {
        "artificial intelligence", "llm", "gpt", "claude", "chatgpt", "gemini",
        "openai", "anthropic", "language model", "transformer", "diffusion",
        "generative", "copilot", "prompt", "ai safety", "alignment",
    },
    "Finance": {
        "stock", "market", "economy", "inflation", "gdp", "earnings",
        "cryptocurrency", "crypto", "bitcoin", "ethereum", "blockchain",
        "fed", "federal reserve", "interest rate", "recession", "banking",
        "investment", "ipo", "trading", "wall street", "tariff", "trade war",
    },
    "Military & Defense": {
        "military", "troops", "missile", "defense", "pentagon", "weapons",
        "army", "navy", "air force", "nuclear", "drone", "strike",
        "deployment", "artillery", "combat", "warfare", "intelligence",
    },
    "Science & Space": {
        "research", "study", "nasa", "space", "discovery", "physics",
        "biology", "quantum", "telescope", "asteroid", "mars", "satellite",
        "launch", "orbit", "climate science",
    },
    "Environment": {
        "wildfire", "earthquake", "hurricane", "flood", "tornado",
        "climate change", "emissions", "carbon", "pollution", "renewable",
        "solar", "drought", "deforestation", "conservation",
    },
    "Health": {
        "health", "disease", "fda", "vaccine", "hospital", "pandemic",
        "medical", "cancer", "treatment", "drug", "clinical trial",
        "virus", "outbreak", "who", "pharmaceutical",
    },
    "Automotive": {
        "car", "vehicle", "autonomous", "self-driving", "tesla",
        "electric vehicle", "charging", "recall", "crash test",
    },
    "Archaeology & History": {
        "archaeology", "archeology", "excavation", "artifact", "artefact",
        "ancient", "ruins", "tomb", "burial", "fossil", "neolithic",
        "bronze age", "iron age", "roman", "medieval", "prehistoric",
        "pharaoh", "pyramid", "temple", "mummy", "hieroglyph",
        "dig site", "heritage", "antiquity", "civilization", "mesopotamia",
        "stone age", "paleolithic", "cave painting", "dinosaur",
        "anthropology", "smithsonian", "museum", "unesco", "world heritage",
        "historical", "discovery", "archaeological",
    },
}


def categorize_article(headline: str, summary: str) -> str:
    """Categorize into one of the predefined categories."""
    text = f"{headline} {summary}".lower()
    scores: dict[str, int] = {}

    for cat, keywords in _CATEGORY_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in text:
                score += 2 if " " in kw else 1
        if score > 0:
            scores[cat] = score

    if not scores:
        return "General"
    return max(scores, key=scores.get)  # type: ignore


def extract_keywords(headline: str, summary: str) -> list[str]:
    """Extract significant keywords for topic clustering."""
    text = f"{headline} {summary}".lower()
    words = re.findall(r'\b[a-z]{3,}\b', text)
    meaningful = [w for w in words if w not in _STOPWORDS]
    counts = Counter(meaningful)

    # Boost proper nouns from original text
    proper = re.findall(r'\b[A-Z][a-z]{2,}\b', f"{headline} {summary}")
    for p in proper:
        pl = p.lower()
        if pl not in _STOPWORDS:
            counts[pl] += 2

    return [word for word, _ in counts.most_common(8)]


# ─── Master Extractor ────────────────────────────────────────────────────

def extract_facets(
    signal_type: str,
    payload: dict,
    signal_id: str | None = None,
) -> dict:
    """Extract all facets from a signal payload. Returns facets dict.

    This is the main entry point called during ingestion. Fast (<5ms).
    """
    headline = (
        payload.get("title")
        or payload.get("headline")
        or payload.get("name")
        or ""
    )
    summary = (
        payload.get("summary")
        or payload.get("og_description")
        or payload.get("description")
        or ""
    )[:500]

    # Skip signals without meaningful text content
    if not headline or len(headline) < 10:
        return {"tier": 1}

    facets: dict = {"tier": 1}
    # Confidence threshold — below this, entity is held as candidate, not tagged
    CONFIDENCE_THRESHOLD = 0.6

    try:
        # Countries (filter by confidence)
        countries = extract_countries(headline, summary)
        high_conf_countries = [c for c in countries if c.get("confidence", 0.8) >= CONFIDENCE_THRESHOLD]
        if high_conf_countries:
            facets["countries"] = [c["code"] for c in high_conf_countries]
            facets["country_labels"] = {c["code"]: c["label"] for c in high_conf_countries}
        # Store candidates (low-confidence) for later review
        low_conf_countries = [c for c in countries if c.get("confidence", 0.8) < CONFIDENCE_THRESHOLD]
        if low_conf_countries:
            facets["_candidates_countries"] = [
                {"code": c["code"], "label": c["label"], "confidence": c["confidence"]}
                for c in low_conf_countries
            ]

        # People (with confidence — only tag if ≥ 0.6)
        people_detailed = extract_people(headline, summary)
        high_conf_people = [p for p in people_detailed if p["confidence"] >= CONFIDENCE_THRESHOLD]
        if high_conf_people:
            facets["people"] = [p["name"] for p in high_conf_people]
            facets["people_confidence"] = {p["name"]: p["confidence"] for p in high_conf_people}
        # Candidates for future LLM review
        low_conf_people = [p for p in people_detailed if p["confidence"] < CONFIDENCE_THRESHOLD]
        if low_conf_people:
            facets["_candidates_people"] = low_conf_people

        # Organizations (with confidence)
        orgs_detailed = extract_organizations_with_confidence(headline, summary)
        high_conf_orgs = [o for o in orgs_detailed if o["confidence"] >= CONFIDENCE_THRESHOLD]
        if high_conf_orgs:
            facets["organizations"] = [o["name"] for o in high_conf_orgs]
        low_conf_orgs = [o for o in orgs_detailed if o["confidence"] < CONFIDENCE_THRESHOLD]
        if low_conf_orgs:
            facets["_candidates_organizations"] = low_conf_orgs

        # Category
        category = categorize_article(headline, summary)
        facets["category"] = category

        # Event type
        event_type = classify_event_type(headline, summary)
        if event_type:
            facets["event_type"] = event_type

        # Threat domain
        threat_domain = map_threat_domain(signal_type, event_type)
        if threat_domain:
            facets["threat_domain"] = threat_domain

        # Topic keywords (for grouping)
        keywords = extract_keywords(headline, summary)
        if keywords:
            facets["topic_keywords"] = keywords

    except Exception as e:
        _log.warning("Facet extraction failed for %s: %s", signal_id, e)

    return facets


# ─── Entity Resolution (Learning Layer) ─────────────────────────────────
# Maps extracted facet strings to canonical entity IDs.
# This is what turns the feed into a learning system — every article
# contributes to the canonical entities table.

def resolve_facet_entities(
    facets: dict,
    tenant_id: str = "default",
    dsn: str | None = None,
    source_signal_id: str | None = None,
) -> dict:
    """Resolve raw facet strings to canonical entity IDs.

    Now governance-aware:
    - Validates entity_type against GDU entityvocab
    - Routes unknown types to entity_ldu_p
    - Emits entity audit events for creates/merges
    - Logs alias and type conflicts to governance_conflicts
    """
    if not facets or facets.get("tier") is None:
        return facets

    try:
        from core.gdu.entity_resolution import resolve_entity
        from core.gdu.entity_governance import (
            check_vocab, stage_provisional, emit_entity_event,
            detect_alias_conflict,
        )
    except Exception:
        return facets  # Resolver not available, skip

    resolved_people = []
    resolved_orgs = []
    resolved_locations = []

    def _resolve_with_governance(name: str, etype: str, identifiers: dict | None = None):
        """Wrap resolve_entity with governance checks."""
        if not check_vocab(etype):
            # Type not in GDU vocab → stage provisional
            stage_provisional(
                tenant_id=tenant_id, raw_name=name, suspected_type=etype,
                source_pipeline="feed_facets", source_signal_id=source_signal_id,
                rejection_reason=f"entity_type '{etype}' not in entityvocab",
                dsn=dsn,
            )
            return None
        try:
            result = resolve_entity(
                tenant_id=tenant_id, name=name, entity_type=etype,
                identifiers=identifiers, source_pipeline="feed_facets", dsn=dsn,
            )
            # Emit audit event for new entities
            if result.entity_id and result.is_new:
                emit_entity_event(
                    tenant_id=tenant_id, mutation_type="create",
                    entity_ids=[result.entity_id],
                    payload={"name": name, "type": etype, "match_reason": result.match_reason},
                    source_pipeline="feed_facets", dsn=dsn,
                )
            elif result.entity_id and result.action == "merge" and not result.is_new:
                # Merged into existing — possibly a new alias
                emit_entity_event(
                    tenant_id=tenant_id, mutation_type="alias_add",
                    entity_ids=[result.entity_id],
                    payload={"new_alias": name, "match_reason": result.match_reason,
                             "confidence": result.confidence},
                    source_pipeline="feed_facets", dsn=dsn,
                )
                # Detect alias conflicts (suspicious merges)
                try:
                    from storage.postgres.db import get_connection
                    with get_connection(dsn) as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT canonical_name, aliases FROM entities WHERE entity_id = %s",
                                (result.entity_id,)
                            )
                            row = cur.fetchone()
                            if row:
                                detect_alias_conflict(
                                    entity_id=result.entity_id,
                                    new_alias=name,
                                    existing_aliases=row[1] or [],
                                    canonical_name=row[0],
                                    source_pipeline="feed_facets",
                                    source_signal_id=source_signal_id,
                                    tenant_id=tenant_id, dsn=dsn,
                                )
                except Exception:
                    pass
            return result
        except Exception as e:
            _log.debug("Resolve %s '%s' failed: %s", etype, name, e)
            return None

    # People
    for name in facets.get("people", []):
        if not name or len(name) < 3:
            continue
        result = _resolve_with_governance(name, "person")
        if result and result.entity_id:
            resolved_people.append({
                "entity_id": result.entity_id, "raw": name,
                "action": result.action, "is_new": result.is_new,
            })

    # Organizations
    for name in facets.get("organizations", []):
        if not name or len(name) < 2:
            continue
        result = _resolve_with_governance(name, "organization")
        if result and result.entity_id:
            resolved_orgs.append({
                "entity_id": result.entity_id, "raw": name,
                "action": result.action, "is_new": result.is_new,
            })

    # Countries — resolve by label (ISO code to canonical name)
    country_labels = facets.get("country_labels", {})
    for code in facets.get("countries", []):
        label = country_labels.get(code, code)
        if not label:
            continue
        identifiers = {"iso_3166": code} if len(code) == 2 else None
        result = _resolve_with_governance(label, "location", identifiers)
        if result and result.entity_id:
            resolved_locations.append({
                "entity_id": result.entity_id, "code": code, "label": label,
                "action": result.action, "is_new": result.is_new,
            })

    if resolved_people:
        facets["people_entity_ids"] = [p["entity_id"] for p in resolved_people]
        facets["_resolved_people"] = resolved_people
    if resolved_orgs:
        facets["organizations_entity_ids"] = [o["entity_id"] for o in resolved_orgs]
        facets["_resolved_orgs"] = resolved_orgs
    if resolved_locations:
        facets["locations_entity_ids"] = [l["entity_id"] for l in resolved_locations]
        facets["_resolved_locations"] = resolved_locations

    return facets


def update_entity_mentions(
    facets: dict,
    signal_id: str,
    timestamp: datetime | None = None,
    dsn: str | None = None,
) -> int:
    """Update last_seen and mention tracking on entities mentioned in this signal.

    Links signal to entity via signal_extractions table (if it exists).
    """
    if not facets:
        return 0

    entity_ids = []
    entity_ids.extend(facets.get("people_entity_ids", []))
    entity_ids.extend(facets.get("organizations_entity_ids", []))
    entity_ids.extend(facets.get("locations_entity_ids", []))

    if not entity_ids:
        return 0

    try:
        from storage.postgres.db import get_connection
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Bump last_seen on every mentioned entity
                cur.execute("""
                    UPDATE entities SET
                        last_seen = NOW(),
                        signal_count = signal_count + 1
                    WHERE entity_id = ANY(%s)
                """, (entity_ids,))

                # Try to insert into signal_extractions (if table exists)
                try:
                    for eid in entity_ids:
                        cur.execute("""
                            INSERT INTO signal_extractions
                                (signal_id, entity_id, tenant_id, extraction_source, created_at)
                            VALUES (%s, %s, 'default', 'feed_facets', NOW())
                            ON CONFLICT DO NOTHING
                        """, (signal_id, eid))
                except Exception:
                    # signal_extractions schema might differ, skip
                    pass
            conn.commit()
        return len(entity_ids)
    except Exception as e:
        _log.debug("Entity mention update failed: %s", e)
        return 0


# ─── Entity Candidate Harvesting ─────────────────────────────────────────

def harvest_candidates(
    facets: dict,
    signal_id: str,
    headline: str,
    source: str,
    tenant_id: str = "default",
    dsn: str | None = None,
) -> int:
    """Store low-confidence entity candidates for later LLM classification.

    These are noun phrases we extracted but aren't confident enough to tag.
    When they accumulate across multiple articles/sources, the LLM reviews.
    """
    if not facets:
        return 0

    candidates = []
    # Low-confidence people
    for c in facets.get("_candidates_people", []):
        candidates.append(("person", c["name"], c.get("confidence", 0.3)))
    for c in facets.get("_candidates_organizations", []):
        candidates.append(("organization", c["name"], c.get("confidence", 0.3)))
    for c in facets.get("_candidates_countries", []):
        candidates.append(("location", c["label"], c.get("confidence", 0.3)))

    if not candidates:
        return 0

    try:
        from storage.postgres.db import get_connection
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                for suspected_type, name, confidence in candidates:
                    normalized = name.lower().strip()
                    if len(normalized) < 3:
                        continue

                    # Upsert
                    cur.execute("""
                        INSERT INTO entity_candidates
                            (tenant_id, name, normalized_name, suspected_type,
                             confidence, mention_count, source_count,
                             sample_signal_ids, sample_headlines, sample_sources,
                             first_seen, last_seen)
                        VALUES (%s, %s, %s, %s, %s, 1, 1, %s, %s, %s, NOW(), NOW())
                        ON CONFLICT (tenant_id, normalized_name)
                        DO UPDATE SET
                            mention_count = entity_candidates.mention_count + 1,
                            source_count = (
                                CASE WHEN %s = ANY(entity_candidates.sample_sources)
                                     THEN entity_candidates.source_count
                                     ELSE entity_candidates.source_count + 1
                                END
                            ),
                            sample_signal_ids = (
                                CASE WHEN array_length(entity_candidates.sample_signal_ids, 1) < 5
                                     THEN array_append(entity_candidates.sample_signal_ids, %s)
                                     ELSE entity_candidates.sample_signal_ids
                                END
                            ),
                            sample_headlines = (
                                CASE WHEN array_length(entity_candidates.sample_headlines, 1) < 5
                                     THEN array_append(entity_candidates.sample_headlines, %s)
                                     ELSE entity_candidates.sample_headlines
                                END
                            ),
                            sample_sources = (
                                CASE WHEN %s = ANY(entity_candidates.sample_sources) OR
                                          array_length(entity_candidates.sample_sources, 1) >= 5
                                     THEN entity_candidates.sample_sources
                                     ELSE array_append(entity_candidates.sample_sources, %s)
                                END
                            ),
                            last_seen = NOW(),
                            updated_at = NOW()
                    """, (
                        tenant_id, name, normalized, suspected_type,
                        confidence,
                        [signal_id], [headline[:200]], [source] if source else [],
                        source, signal_id, headline[:200], source, source,
                    ))
            conn.commit()
        return len(candidates)
    except Exception as e:
        _log.debug("Candidate harvest failed: %s", e)
        return 0


# ─── Facet Count Aggregation ─────────────────────────────────────────────

def increment_facet_counts(
    facets: dict,
    signal_id: str,
    tenant_id: str,
    timestamp: datetime | None = None,
    dsn: str | None = None,
) -> None:
    """Increment facet_counts table for each extracted facet.

    Called after signal insert. Best-effort — never blocks ingestion.
    """
    if not facets or facets.get("tier") is None:
        return

    try:
        from storage.postgres.db import get_connection

        now = timestamp or datetime.now(timezone.utc)
        # Truncate to hour for bucketing
        window_start = now.replace(minute=0, second=0, microsecond=0)

        rows = []

        # Countries
        for code in facets.get("countries", []):
            label = facets.get("country_labels", {}).get(code, code)
            rows.append(("country", code, label))

        # People
        for person in facets.get("people", []):
            rows.append(("person", person, person))

        # Organizations
        for org in facets.get("organizations", []):
            rows.append(("organization", org, org))

        # Category
        if facets.get("category"):
            rows.append(("category", facets["category"], facets["category"]))

        # Event type
        if facets.get("event_type"):
            rows.append(("event_type", facets["event_type"],
                         facets["event_type"].replace("_", " ").title()))

        # Threat domain
        if facets.get("threat_domain"):
            rows.append(("threat_domain", facets["threat_domain"],
                         facets["threat_domain"].title()))

        if not rows:
            return

        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                for facet_type, facet_value, facet_label in rows:
                    cur.execute("""
                        INSERT INTO facet_counts
                            (tenant_id, facet_type, facet_value, facet_label,
                             window_start, count, sample_signal_id, updated_at)
                        VALUES (%s, %s, %s, %s, %s, 1, %s, NOW())
                        ON CONFLICT (tenant_id, facet_type, facet_value, window_start)
                        DO UPDATE SET
                            count = facet_counts.count + 1,
                            sample_signal_id = EXCLUDED.sample_signal_id,
                            updated_at = NOW()
                    """, (tenant_id, facet_type, facet_value, facet_label,
                          window_start, signal_id))
            conn.commit()

    except Exception as e:
        _log.debug("Facet count increment failed (non-blocking): %s", e)
