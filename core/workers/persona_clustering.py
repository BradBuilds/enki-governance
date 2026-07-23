"""
Persona clustering for the entity dossier.

The problem:
  One entity_id can accumulate findings from several distinct real-world
  referents that happen to share a surface form. The textbook case is
  one entity — "Sam Rowan" — which today holds findings about
  Simon Lowe (UK writer), Sam Franklin Rowan (US
  politician), Sam Rowan Realty Inc (FL company), and a Sam
  Rowan in an unrelated US divorce case. The name-shape disambiguator
  pushes low-match rows to "Possible matches" but doesn't propose how
  they GROUP — the operator sees a flat list and has to manually cluster.

What this module does:
  Cluster a single entity's findings into candidate personas by
  attribute-similarity (not just name shape). Each cluster maps 1:1 onto
  a base-60 substrate row (form_nodes → concept_nodes → form_concept_
  bindings per migration 093), so the operator-approved split also
  becomes the canonical resolution event for that form.

What it does NOT do:
  - LLM call (deterministic features only; LLM is a Phase 2 quality bump)
  - Write to DB (pure function; the route + split endpoint do the writes)
  - Auto-execute splits (operator confirms each cluster in the UI)

Output schema (PersonaCluster) is binding-ready: every field maps onto a
concept_nodes or form_concept_bindings column so the split endpoint can
write the substrate without re-deriving.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional


# ── Source → jurisdiction map ─────────────────────────────────────────────

_US_SOURCES = frozenset([
    "fec", "sec_edgar", "congress", "courtlistener", "usaspending",
    "ny_dos", "ca_sos", "tx_sos", "de_corp", "sunbiz_fl", "fdic",
    "fda", "epa_echo", "nonprofits", "propublica",
])

_UK_SOURCES = frozenset([
    "uk_companies_house", "uk_charity_commission", "uk_hansard",
    "uk_caselaw",
])

# Sources that don't pin jurisdiction by themselves — must read data fields.
_GLOBAL_SOURCES = frozenset([
    "wikipedia", "wikidata", "ofac", "un_sanctions", "opensanctions",
    "opencorporates", "loc", "google_books", "github", "reddit",
    "wayback", "duckduckgo",
])


# ── Finding-type → entity_type hint map ───────────────────────────────────
# None means "this finding_type doesn't pin entity_type by itself — fall
# back to other features".

_TYPE_HINT_BY_FINDING_TYPE: dict[str, Optional[str]] = {
    "encyclopedic_summary": None,
    "structured_profile": None,
    "candidate": "person",
    "committee": "organization",
    "donation": "person",
    "court_case": None,
    "company": "organization",
    "filing": "organization",
    "officer": "person",
    "fl_business_affiliation": "organization",
    "loc_record": None,
    "sanctions": "person",
    "ofac_sanction": "person",
    "trademark": "organization",
    "fdic": "organization",
    "nonprofit": "organization",
    "patent": None,
}


_CORP_SUFFIX_TOKENS = frozenset([
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited",
    "corp", "corporation", "co", "company", "group", "holdings",
    "realty", "properties", "associates", "partners", "ventures",
    "enterprises", "industries", "estate", "trust",
    # Political / nonprofit org names (FEC committees, PACs, 990 nonprofits) —
    # these ARE the entity's own registered name, not a namesake of a person.
    "committee", "pac", "fund", "foundation", "campaign", "association",
    "coalition", "nonprofit", "society", "institute", "council",
    "federation", "alliance", "league",
])

# Strong org identifiers — a member carrying one of these is a DISTINCT registered
# entity, so it keeps its own registered name (identifier-decisive), not the seed's.
_ORG_ID_KEYS = ("committee_id", "ein", "cik", "fec_id", "uei", "lei", "duns",
                "cage", "company_number")

# Tokens that look like middle names — used to detect divergent identity
# inside a shared surface form. "FRANKLIN" between "SAM" and "ROWAN"
# is a stronger signal that we're looking at a different real-world
# referent than a corporate suffix.
_GENERATIONAL_TOKENS = frozenset([
    "jr", "sr", "ii", "iii", "iv", "junior", "senior",
])

# Tokens that look name-shaped but aren't real personal-name middle
# tokens — connectors, common English, demonyms, and verb fragments
# we've seen surface from court-case + government-data titles.
_LABEL_NOISE_TOKENS = frozenset({
    "for", "and", "of", "the", "v", "vs", "et", "al",
    "case", "no", "in", "on", "to", "at", "by",
    "not", "yes", "or", "is", "was", "be", "are", "an", "as",
    "off", "out", "up", "do", "did", "if", "it", "its",
    "sdn", "list", "all", "any", "via", "see", "from",
    "with", "but", "nor", "so", "yet", "than", "then",
    "this", "that", "these", "those", "such", "search",
    "results", "result", "re",
    # Demonyms — describe origin, not name.
    "british", "english", "scottish", "welsh", "irish",
    "american", "canadian", "australian", "french",
    "german", "italian", "spanish", "russian", "ukrainian",
    "chinese", "japanese", "korean", "indian", "israeli",
    "iranian", "saudi",
})

# Tokens that come from URL paths, page metadata, or social-media tags —
# they describe the SOURCE FORMAT, not the entity itself. Surfaced as
# name_extras when source-bridge titles include them (e.g. "Alex
# Acme - twitter profile (archived)" → extras {twitter, profile,
# archived}). They were fragmenting the Acme dossier into 28
# noise-clusters (twitter/instagram/wikipedia/archived all spawning
# their own personas). Strip them from extras so persona similarity
# isn't driven by which social network the source crawled.
# Sources whose title field describes the topic/bill/post the entity
# was MENTIONED IN, not the entity itself. For these, title-derived
# name_extras are pure noise (every bill has different bill prose).
# Clustering for findings from these sources should rely on
# jurisdiction / region / identifiers / corp-shape, not extras.
_TOPIC_TITLE_SOURCES = frozenset({
    "loc",                # Library of Congress: bill titles
    "federal_register",   # rule titles
    "courtlistener",      # case captions like "Doe v. Estate of X"
    "google_books",       # book titles
    "reddit",             # post titles
    "wayback",            # archived page titles (vary by snapshot)
    "duckduckgo",         # related-topic snippets
    "github",             # repo/PR titles
    "epa_echo",           # facility names rather than persons
    "usaspending",        # award descriptions
    "propublica_nonprofits",  # 990-form titles
})


_SOURCE_FORMAT_NOISE = frozenset({
    # Source / platform names
    "wikipedia", "wikidata", "twitter", "instagram", "facebook",
    "linkedin", "youtube", "reddit", "github", "tiktok", "x",
    "telegram", "tumblr", "medium", "substack", "snapchat",
    # Page/URL artifacts
    "profile", "page", "post", "feed", "url", "link", "homepage",
    "archived", "archive", "snapshot", "cached", "mirror",
    "preview", "thumbnail", "image", "photo", "photos", "video",
    "videos", "audio", "embed", "iframe",
    # Sloppy verbs from URL slugs / breadcrumbs
    "linking", "knows", "files", "names", "named",
    "humility", "clear",
})


# Maps common demonyms found in Wikipedia descriptions to ISO country codes.
# Used to derive jurisdiction from a Wikipedia summary when wikidata's
# country_of_citizenship field isn't populated.
_DEMONYM_COUNTRY = {
    "british": "UK", "english": "UK", "scottish": "UK", "welsh": "UK",
    "american": "US", "u.s.": "US", "u.s.a.": "US",
    "canadian": "CA", "australian": "AU", "irish": "IE",
    "french": "FR", "german": "DE", "italian": "IT", "spanish": "ES",
    "russian": "RU", "ukrainian": "UA",
    "chinese": "CN", "japanese": "JP", "korean": "KR", "indian": "IN",
    "israeli": "IL", "iranian": "IR", "saudi": "SA",
}


# ── Feature extraction ────────────────────────────────────────────────────

@dataclass
class FindingFeatures:
    """Per-finding feature vector used for clustering. Pure data —
    no side effects. Common fields work across all profile types;
    type-specific fields (coords, event_year, founded_year) only
    populate when the relevant data is in the finding."""

    finding_id: int
    source: str
    finding_type: str
    title: str
    confidence: float

    # Name shape against the entity's canonical (seed) name
    name_match_score: float
    name_extras: frozenset[str]              # title tokens beyond seed
    has_corp_suffix: bool

    # Identity attributes derived from finding.data + source.
    # Applicable to all types: every concept lives somewhere & some-when.
    jurisdiction: Optional[str]              # 'US', 'UK', ...
    region: Optional[str]                    # state / admin region
    entity_type_hint: Optional[str]          # 'person' | 'organization' | 'place' | 'event'
    identifiers: dict                        # QID, CIK, candidate_id, GeoNameID, ISBN, ...

    # Parenthetical qualifier — strong disambiguation signal when a
    # source titles a result like "Sam Rowan (footballer)" or
    # "Sam Rowan (musician)". The qualifier names a different
    # referent sharing the same surface form. Two findings with
    # DIFFERENT parenthetical_qualifiers are hard-veto distinct.
    parenthetical_qualifier: Optional[str] = None

    # Person-specific
    dob_year: Optional[int] = None

    # Place-specific — disambiguates "Springfield, IL" from "Springfield, MO".
    # Coords stored as (lat_bucket, lon_bucket) in ~10km buckets so near
    # coordinates compare equal without floating-point fuss.
    coords_bucket: Optional[tuple[int, int]] = None

    # Event-specific — disambiguates "the 1953 incident" by year.
    event_year: Optional[int] = None

    # Organization-specific — disambiguates "Apple Inc" from "Apple Corps".
    founded_year: Optional[int] = None
    industry: Optional[str] = None

    # Raw finding (so a cluster can return the operator-facing rows
    # without the API layer re-querying).
    raw: dict = field(default_factory=dict)


def _norm_tokens(s: str) -> list[str]:
    """Match the tokenizer used by _score_name_match so name_extras
    aligns with the disambiguator's behaviour."""
    if ":" in s:
        s = s.split(":", 1)[1]
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    return [t for t in s.split() if t]


def _extract_features(seed_name: str, finding: dict) -> FindingFeatures:
    # Lazy import — investigation.py pulls in storage.postgres → psycopg2,
    # which we don't want to require at module-import time (this module
    # is itself pure).
    from core.workers.investigation import _score_name_match

    title = finding.get("title") or ""
    source = (finding.get("source") or "").lower()
    finding_type = (finding.get("finding_type") or finding.get("type")
                    or "").lower()
    data = finding.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    # Name shape ------------------------------------------------------------
    name_match = _score_name_match(seed_name, title)
    seed_tokens = set(_norm_tokens(seed_name))
    title_tokens = set(_norm_tokens(title))
    raw_extras = title_tokens - seed_tokens
    # Corp-suffix detection uses raw extras so the "Inc/LLC/Realty"
    # org-shape signal survives the name-shape filter below.
    has_corp_suffix = bool(raw_extras & _CORP_SUFFIX_TOKENS)
    # Topic-titled sources (LOC bills, Federal Register, Reddit/news
    # posts) put the BILL or POST text in the title, not the entity's
    # disambiguating qualifier. "S.Res.608 expressing sense of Senate"
    # has nothing to do with which Alex Acme — every Acme
    # mention in any bill would produce different extras and shatter
    # into one-cluster-per-bill. Zero out extras for these sources so
    # clustering relies on jurisdiction / region / corp-shape / shared
    # identifiers instead.
    if source in _TOPIC_TITLE_SOURCES:
        extras = set()
    else:
        # Name-shape filter — extras drives persona similarity, so only
        # tokens that look like NAME parts should count. Bill numbers
        # (608, 119th), congress-session digits, source-format tokens
        # (twitter, archived), URL slugs (linking, knows), and connectors
        # were all surfacing as extras and shattering same-name clusters
        # into one-finding-per-source noise.
        extras = {
            t for t in raw_extras
            if t.isalpha() and len(t) >= 3
            and t not in _GENERATIONAL_TOKENS
            and t not in _SOURCE_FORMAT_NOISE
            and t not in _LABEL_NOISE_TOKENS
            and t not in _CORP_SUFFIX_TOKENS
        }

    # Jurisdiction ----------------------------------------------------------
    jurisdiction: Optional[str] = None
    if source in _US_SOURCES:
        jurisdiction = "US"
    elif source in _UK_SOURCES:
        jurisdiction = "UK"

    if jurisdiction is None:
        # Wikidata: country_of_citizenship is the strongest signal.
        coc = data.get("country_of_citizenship")
        if isinstance(coc, str) and coc.strip():
            jurisdiction = _country_code_from_freeform(coc)
        elif isinstance(coc, list) and coc:
            jurisdiction = _country_code_from_freeform(str(coc[0]))

    if jurisdiction is None:
        # Wikipedia/Wikidata: parse description for a demonym.
        text = " ".join(str(data.get(k) or "") for k in
                        ("description", "summary", "extract"))
        if text:
            lc = text.lower()
            for d, cc in _DEMONYM_COUNTRY.items():
                if re.search(rf"\b{re.escape(d)}\b", lc):
                    jurisdiction = cc
                    break

    # Region ----------------------------------------------------------------
    region = (data.get("state") or data.get("region") or data.get("city")
              or data.get("country") or None)
    if isinstance(region, str):
        region = region.strip() or None
    else:
        region = None

    # Entity-type hint ------------------------------------------------------
    hint = _TYPE_HINT_BY_FINDING_TYPE.get(finding_type)
    if hint is None and has_corp_suffix:
        # Corporate suffix on the surface form trumps everything except a
        # type hint already in the finding.
        hint = "organization"

    # Year fields — pulled separately per profile type below.
    def _parse_year(*candidates) -> Optional[int]:
        for c in candidates:
            if not c:
                continue
            m = re.search(r"(\d{4})", str(c))
            if m:
                y = int(m.group(1))
                if 1700 <= y <= 2099:
                    return y
        return None

    # DOB (person) ----------------------------------------------------------
    dob_year = _parse_year(
        data.get("dob"), data.get("birth_date"), data.get("birth_year"),
        data.get("date_of_birth"),
    )

    # Founded year (organization) ------------------------------------------
    founded_year = _parse_year(
        data.get("founded"), data.get("inception"), data.get("founding_date"),
        data.get("incorporation_date"), data.get("first_file_date"),
    )

    # Event year ------------------------------------------------------------
    event_year = _parse_year(
        data.get("event_date"), data.get("date"), data.get("start_date"),
        data.get("point_in_time"),
    )

    # Parenthetical qualifier (e.g. "Sam Rowan (footballer)") ---------
    # Search the title and any source URL for a "(qualifier)" pattern. Only
    # capture qualifiers that are SHORT and look like an occupation/role
    # word — skip QIDs like "(Q12345)" and dates.
    parenthetical_qualifier: Optional[str] = None
    candidates_for_qualifier = [title]
    url_field = data.get("source_url") or data.get("url") or data.get("page_url") or ""
    if isinstance(url_field, str):
        # URL paths often include "_(footballer)" style
        import urllib.parse
        decoded = urllib.parse.unquote(url_field)
        candidates_for_qualifier.append(decoded.replace("_", " "))
    for cand in candidates_for_qualifier:
        m = re.search(r"\(([^)]{2,30})\)", cand)
        if not m:
            continue
        q = m.group(1).strip().lower()
        # Skip QIDs (Q123), dates (1982), single-character qualifiers
        if not q or q.startswith("q") and q[1:].isdigit():
            continue
        if q.isdigit():
            continue
        # Likely a role/occupation if it's mostly letters and 1-2 words.
        if re.fullmatch(r"[a-z][a-z\s\-]{1,28}", q):
            parenthetical_qualifier = q
            break

    # Industry / sector (org) ----------------------------------------------
    industry: Optional[str] = None
    ind = data.get("industry") or data.get("sector") or data.get("sic_code")
    if isinstance(ind, str) and ind.strip():
        industry = ind.strip().lower()

    # Coordinates (place) — bucket to ~0.1° (~10km) so near values match.
    coords_bucket: Optional[tuple[int, int]] = None
    lat = data.get("lat") or data.get("latitude")
    lon = data.get("lon") or data.get("lng") or data.get("longitude")
    try:
        if lat is not None and lon is not None:
            lat_f = float(lat)
            lon_f = float(lon)
            if -90 <= lat_f <= 90 and -180 <= lon_f <= 180:
                coords_bucket = (int(round(lat_f * 10)), int(round(lon_f * 10)))
    except (TypeError, ValueError):
        pass

    # Identifiers -----------------------------------------------------------
    identifiers: dict = {}
    for k in ("wikidata_id", "qid", "cik", "ein", "candidate_id",
              "committee_id", "fec_id", "uei", "cage", "company_number",
              "duns", "doi", "isbn", "lei", "geonames_id", "osm_id", "imo",
              "icao", "iata", "ticker", "permid", "wikidata_qid"):
        v = data.get(k)
        if v:
            identifiers[k] = str(v)
    # Fold the three Wikidata-id spellings into a single canonical `qid` so a
    # QID conflict is detected regardless of which source key carried it.
    for _alias in ("wikidata_id", "wikidata_qid"):
        if _alias in identifiers:
            identifiers.setdefault("qid", identifiers.pop(_alias))
        identifiers.pop(_alias, None)

    return FindingFeatures(
        finding_id=int(finding.get("finding_id") or 0),
        source=source,
        finding_type=finding_type,
        title=title,
        confidence=float(finding.get("confidence") or 0.0),
        name_match_score=name_match,
        name_extras=frozenset(extras),
        has_corp_suffix=has_corp_suffix,
        jurisdiction=jurisdiction,
        region=region,
        entity_type_hint=hint,
        identifiers=identifiers,
        parenthetical_qualifier=parenthetical_qualifier,
        dob_year=dob_year,
        coords_bucket=coords_bucket,
        event_year=event_year,
        founded_year=founded_year,
        industry=industry,
        raw=finding,
    )


def _country_code_from_freeform(s: str) -> Optional[str]:
    """Best-effort ISO-2 code from a free-form country string."""
    lc = s.lower().strip()
    direct = {
        "united states": "US", "united states of america": "US",
        "usa": "US", "us": "US",
        "united kingdom": "UK", "uk": "UK", "great britain": "UK",
        "england": "UK", "scotland": "UK", "wales": "UK",
        "canada": "CA", "australia": "AU", "ireland": "IE",
        "france": "FR", "germany": "DE", "italy": "IT", "spain": "ES",
        "russia": "RU", "ukraine": "UA", "china": "CN", "japan": "JP",
        "south korea": "KR", "korea": "KR", "india": "IN",
        "israel": "IL", "iran": "IR", "saudi arabia": "SA",
    }
    return direct.get(lc) or _DEMONYM_COUNTRY.get(lc)


# ── Cluster output schema ─────────────────────────────────────────────────

@dataclass
class PersonaCluster:
    """A proposed persona — maps 1:1 onto base-60 substrate.

    On approval the split endpoint writes:
      * form_nodes (the seed name; usually already exists)
      * concept_nodes:
          - canonical_name      = label
          - canonical_subtype   = canonical_subtype
          - subtype_path        = subtype_path
          - identifiers         = identifiers
          - type_evidence       = dominant_attrs
          - evidence_tier       = evidence_tier
      * form_concept_bindings:
          - form_id  → concept_id
          - binding_confidence  = confidence
          - binding_source      = 'persona_clustering_v1'
          - binding_evidence    = dominant_attrs
    """
    cluster_id: str
    label: str
    anchor: bool
    finding_ids: list[int]
    findings: list[dict]            # full finding rows for the UI
    confidence: float

    # Base-60 binding-ready fields
    canonical_subtype: str
    subtype_path: str
    identifiers: dict
    dominant_attrs: dict
    evidence_tier: str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Similarity ────────────────────────────────────────────────────────────

def _attr_similarity(a: FindingFeatures, b: FindingFeatures) -> float:
    """0..1 attribute-similarity between two findings.

    Hard vetoes (return 0.0 immediately):
      * Both have entity_type_hint and they differ.
      * Both have jurisdiction and they differ.
      * Both have dob_year and they differ by more than 2 years.

    Identifier match (return 1.0): any shared identifier value (QID, CIK,
    candidate_id) means the sources themselves already agree these are
    the same referent.

    Otherwise weighted average over name_extras / region / source_class.
    """

    # Identifier short-circuit — a SHARED identifier value means same referent.
    for k, v in a.identifiers.items():
        if v and b.identifiers.get(k) == v:
            return 1.0

    # Identifier CONFLICT veto — the inverse. Identifier-decisive: never merge on
    # name/region when the identifiers say distinct. (1) Same key, different value
    # (committee_id C00123 ≠ C00456) → different registered entities. (2) Each
    # carries a strong ORG identifier and they share NONE (a committee_id vs an
    # ein) → distinct registered orgs (a PAC is not a 501c). This is what stops N
    # different FEC committees + the Foundation fusing under one "Dana Vale".
    for k in a.identifiers.keys() & b.identifiers.keys():
        if a.identifiers[k] and b.identifiers[k] and a.identifiers[k] != b.identifiers[k]:
            return 0.0
    a_org = {a.identifiers[k] for k in _ORG_ID_KEYS if a.identifiers.get(k)}
    b_org = {b.identifiers[k] for k in _ORG_ID_KEYS if b.identifiers.get(k)}
    if a_org and b_org and not (a_org & b_org):
        return 0.0

    # Hard vetoes — applicable to all profile types

    # Parenthetical qualifier veto — "Sam Rowan (footballer)" vs
    # "Sam Rowan (musician)" vs "Sam Rowan (author)" each
    # explicitly names a DIFFERENT referent. The source itself is
    # disambiguating; we must respect that.
    if (a.parenthetical_qualifier and b.parenthetical_qualifier
            and a.parenthetical_qualifier != b.parenthetical_qualifier):
        return 0.0

    if (a.entity_type_hint and b.entity_type_hint and
            a.entity_type_hint != b.entity_type_hint):
        return 0.0
    if (a.jurisdiction and b.jurisdiction and
            a.jurisdiction != b.jurisdiction):
        return 0.0
    if a.has_corp_suffix != b.has_corp_suffix:
        # Sam Rowan (person) vs Sam Rowan Realty Inc (org).
        # Distinct concept even if the surface form overlaps.
        return 0.0

    # Person-specific
    if (a.dob_year and b.dob_year and abs(a.dob_year - b.dob_year) > 2):
        return 0.0

    # Place-specific — two Springfields >~30km apart are different cities.
    # Buckets are ~0.1° (~10km); we tolerate 3 buckets of drift.
    if a.coords_bucket and b.coords_bucket:
        dlat = abs(a.coords_bucket[0] - b.coords_bucket[0])
        dlon = abs(a.coords_bucket[1] - b.coords_bucket[1])
        if dlat > 3 or dlon > 3:
            return 0.0

    # Event-specific — events more than a year apart are different events.
    if a.event_year and b.event_year and abs(a.event_year - b.event_year) > 1:
        return 0.0

    # Org-specific — founding years more than 5 years apart are different
    # entities. Tolerance: incorporation paperwork can lag founding.
    if (a.founded_year and b.founded_year and
            abs(a.founded_year - b.founded_year) > 5):
        return 0.0
    if (a.industry and b.industry and a.industry != b.industry):
        # Two same-named orgs in totally different industries — distinct.
        return 0.0

    # Soft features ---------------------------------------------------------
    score = 0.0
    weight = 0.0

    # Name extras (middle names, qualifiers): high signal when present.
    if a.name_extras and b.name_extras:
        inter = a.name_extras & b.name_extras
        union = a.name_extras | b.name_extras
        jacc = len(inter) / len(union) if union else 0.0
        score += 0.45 * jacc
        weight += 0.45
    elif not a.name_extras and not b.name_extras:
        # Both are bare seed-form titles — no signal either way, mild boost.
        score += 0.15
        weight += 0.20
    else:
        # One has extras the other lacks — slight penalty.
        score += 0.05
        weight += 0.20

    # Jurisdiction agreement (only one known is mildly positive).
    if a.jurisdiction and b.jurisdiction:
        score += 0.20  # already passed the veto, so they agree
        weight += 0.20
    elif a.jurisdiction or b.jurisdiction:
        score += 0.05
        weight += 0.10

    # Region (state/city)
    if a.region and b.region:
        if a.region.lower() == b.region.lower():
            score += 0.15
        weight += 0.15

    # Source diversity bonus: same source for both means we likely have
    # near-duplicates from one feed — don't over-credit.
    if a.source != b.source:
        score += 0.10
        weight += 0.10
    else:
        weight += 0.10

    return score / weight if weight > 0 else 0.0


def _cluster_features(cluster_features: list[FindingFeatures],
                      candidate: FindingFeatures) -> float:
    """Similarity of a candidate to an existing cluster.

    Cluster-level vetoes FIRST — looking at the cluster's dominant
    profile (most common jurisdiction / entity_type / corp-status), not
    just any single member. Without this, the cluster grows
    heterogeneous: one weak-jurisdiction member admits the next
    borderline candidate, which admits the next, etc. We saw this on
    Sam Rowan — 48 findings spanning UK + two US states all
    in one anchor cluster.

    Then MAX pairwise similarity for the rest. Max (not mean) because
    legitimate evidence chains can have one strong tie-in even if
    other members are weaker.
    """
    from collections import Counter

    # Cluster-level parenthetical qualifier veto
    cluster_qualifiers = Counter(m.parenthetical_qualifier
                                  for m in cluster_features if m.parenthetical_qualifier)
    if cluster_qualifiers and candidate.parenthetical_qualifier:
        dom_q = cluster_qualifiers.most_common(1)[0][0]
        if dom_q != candidate.parenthetical_qualifier:
            return 0.0

    # Asymmetric qualifier check — cluster has Wikipedia/Wikidata
    # anchor text but no parenthetical qualifier of its own. If a
    # candidate brings a qualifier (DDG "footballer") that's NOT
    # mentioned anywhere in the anchor's text, veto. Catches the case
    # where Wikipedia about the activist seeded the cluster and DDG
    # footballer tries to join.
    if candidate.parenthetical_qualifier and not cluster_qualifiers:
        anchor_text_blobs: list[str] = []
        for m in cluster_features:
            if m.source not in ("wikipedia", "wikidata"):
                continue
            data = m.raw.get("data") or {}
            if not isinstance(data, dict):
                continue
            for k in ("summary", "description", "extract", "snippet"):
                v = data.get(k)
                if isinstance(v, str):
                    anchor_text_blobs.append(v.lower())
        if anchor_text_blobs:
            combined = " ".join(anchor_text_blobs)
            if candidate.parenthetical_qualifier.lower() not in combined:
                # The qualifier is a different referent the anchor
                # doesn't describe. Different person.
                return 0.0

    # Dominant-attribute vetoes
    jurs = Counter(m.jurisdiction for m in cluster_features if m.jurisdiction)
    if jurs and candidate.jurisdiction:
        dom_jur = jurs.most_common(1)[0][0]
        if dom_jur != candidate.jurisdiction:
            return 0.0

    types = Counter(m.entity_type_hint for m in cluster_features
                    if m.entity_type_hint)
    if types and candidate.entity_type_hint:
        dom_type = types.most_common(1)[0][0]
        if dom_type != candidate.entity_type_hint:
            return 0.0

    if cluster_features:
        corp_count = sum(1 for m in cluster_features if m.has_corp_suffix)
        cluster_corp = corp_count > len(cluster_features) / 2
        if cluster_corp != candidate.has_corp_suffix:
            return 0.0

    # DOB veto — if the cluster has any dob_year and candidate has one
    # that's > 2 years off, veto. Birth years are stable identity.
    cluster_dobs = [m.dob_year for m in cluster_features if m.dob_year]
    if cluster_dobs and candidate.dob_year:
        if min(abs(d - candidate.dob_year) for d in cluster_dobs) > 2:
            return 0.0

    # Place coordinate veto at cluster level — if cluster has any
    # coords and candidate is far from all of them, veto.
    cluster_coords = [m.coords_bucket for m in cluster_features if m.coords_bucket]
    if cluster_coords and candidate.coords_bucket:
        far_from_all = all(
            abs(c[0] - candidate.coords_bucket[0]) > 3
            or abs(c[1] - candidate.coords_bucket[1]) > 3
            for c in cluster_coords
        )
        if far_from_all:
            return 0.0

    # Event-year veto at cluster level.
    cluster_years = [m.event_year for m in cluster_features if m.event_year]
    if cluster_years and candidate.event_year:
        if min(abs(y - candidate.event_year) for y in cluster_years) > 1:
            return 0.0

    # Founded-year veto at cluster level.
    cluster_founded = [m.founded_year for m in cluster_features if m.founded_year]
    if cluster_founded and candidate.founded_year:
        if min(abs(f - candidate.founded_year) for f in cluster_founded) > 5:
            return 0.0

    # Industry veto — same-named org in different industries is distinct.
    cluster_industries = Counter(m.industry for m in cluster_features if m.industry)
    if cluster_industries and candidate.industry:
        dom_industry = cluster_industries.most_common(1)[0][0]
        if dom_industry != candidate.industry:
            return 0.0

    # Identifier-decisive veto at cluster level. A strong identifier (FEC
    # committee_id, EIN, CIK, UEI, LEI, DUNS, CAGE, company_number, QID) is
    # the primary key of an entity: same value → same referent, different
    # value → NEVER the same. The pairwise `max` below can't enforce this —
    # it picks the best-matching member and would let a candidate carrying a
    # conflicting id in on the strength of one non-conflicting member. So the
    # veto must live here, over the whole cluster:
    #   (a) candidate shares an id KEY with any member but a DIFFERENT value
    #       → hard conflict → veto.
    #   (b) candidate carries a strong org id and the cluster carries strong
    #       org ids, and none overlap → two distinct registered orgs → veto.
    # (A shared value is handled by _attr_similarity's short-circuit to 1.0.)
    cand_ids = candidate.identifiers or {}
    if cand_ids:
        for m in cluster_features:
            m_ids = m.identifiers or {}
            for k in cand_ids.keys() & m_ids.keys():
                if cand_ids[k] and m_ids[k] and cand_ids[k] != m_ids[k]:
                    return 0.0
        cand_org = {cand_ids[k] for k in _ORG_ID_KEYS if cand_ids.get(k)}
        if cand_org:
            cluster_org = {m.identifiers[k]
                           for m in cluster_features
                           for k in _ORG_ID_KEYS
                           if (m.identifiers or {}).get(k)}
            if cluster_org and not (cand_org & cluster_org):
                return 0.0

    return max(
        (_attr_similarity(m, candidate) for m in cluster_features),
        default=0.0,
    )


# ── Cluster post-processing ───────────────────────────────────────────────

_SOURCE_PRIORITY = {
    "wikipedia": 10, "wikidata": 11,
    "courtlistener": 20, "ofac": 21, "un_sanctions": 22, "opensanctions": 23,
    "fec": 30, "congress": 31, "loc": 32,
    "opencorporates": 40, "epa_echo": 41, "usaspending": 42,
    "uk_companies_house": 45,
    "ny_dos": 50, "ca_sos": 51, "sunbiz_fl": 52,
    "google_books": 60,
    "duckduckgo": 70, "wayback": 71,
    "github": 80, "reddit": 81,
}


def _derive_subtype(members: list[FindingFeatures]) -> str:
    """Pick the cluster's canonical_subtype from its members."""
    hints = [m.entity_type_hint for m in members if m.entity_type_hint]
    if hints:
        # Mode
        from collections import Counter
        return Counter(hints).most_common(1)[0][0]
    if any(m.has_corp_suffix for m in members):
        return "organization"
    if any(m.identifiers.get(k) for m in members for k in _ORG_ID_KEYS):
        return "organization"   # committee_id / EIN / CIK → a registered org
    return "person"


def _derive_label(members: list[FindingFeatures], seed_name: str,
                  canonical_subtype: str) -> str:
    """Cluster label — keep all namesakes balanced on the shared form.

    The point of the namesake panel is to show that the same SURFACE
    FORM ("Sam Rowan") binds to multiple distinct referents. The
    label should reinforce that shared-form signal, not invent
    disambiguation strings like "Sam Melinda Rowan" or
    "Sam Rowan v. Initech Inc." Those go in attribute
    chips, not in the label.

    Three paths:

      1. CORPORATE — a member has a corp suffix (Inc, LLC, REALTY).
         Use the member's title verbatim because that IS the concept's
         own legal name, not a namesake of the seed.

      2. PARENTHETICAL QUALIFIER — sources sometimes disambiguate
         themselves: "Sam Rowan (footballer)", "Sam Rowan
         (musician)", "Sam Rowan (author)". Use that.

      3. DEFAULT — just the seed name unchanged. The dominant_attrs
         chips (jurisdiction, region, qualifier, name_extras,
         identifiers) carry the disambiguation.
    """
    members_sorted = sorted(
        members,
        key=lambda m: (_SOURCE_PRIORITY.get(m.source, 99), -m.confidence),
    )

    # 1. Corporate / registered org — use the member's OWN name verbatim (that IS
    #    the concept's legal name, not a namesake of the seed). Fire on a corp/org
    #    suffix OR a strong org identifier (committee_id, ein, cik…): a distinct
    #    registered entity keeps its registered name. This is what stops N distinct
    #    FEC committees / 990 nonprofits from all collapsing to the seed person name.
    # Prefer the member that actually CARRIES the org identifier — its title is the
    # registered name (the FEC committee row), over e.g. a court-case title in the
    # same cluster that merely happens to contain an org word.
    id_members = [m for m in members_sorted
                  if any(m.identifiers.get(k) for k in _ORG_ID_KEYS)]
    for m in (id_members + members_sorted):
        has_org_id = any(m.identifiers.get(k) for k in _ORG_ID_KEYS)
        if m.has_corp_suffix or has_org_id:
            t = m.title
            if ":" in t:
                t = t.split(":", 1)[1].strip()
            t = t.strip()
            # skip a litigation-style title ("X v. Y") — not the registered name
            if t and t.lower() != seed_name.lower() and " v. " not in f" {t.lower()} " \
                    and " v " not in f" {t.lower()} ":
                return t

    # 2. Wikipedia / Wikidata occupation — when the cluster has an
    #    encyclopedic anchor, use its structured occupation/position
    #    rather than a noisy DDG qualifier. "Sam Rowan (travel
    #    writer)" reads better than "Sam Rowan (footballer)"
    #    when the cluster is actually the activist.
    for m in members_sorted:
        if m.source not in ("wikipedia", "wikidata"):
            continue
        data = m.raw.get("data") or {}
        if not isinstance(data, dict):
            continue
        occ = data.get("occupation") or data.get("position_held")
        if isinstance(occ, str) and occ.strip():
            return f"{seed_name} ({occ.strip()})"
        if isinstance(occ, list) and occ:
            return f"{seed_name} ({occ[0]})"

    # 3. Parenthetical qualifier from the source itself (DDG etc.)
    qualifiers = [m.parenthetical_qualifier
                  for m in members_sorted if m.parenthetical_qualifier]
    if qualifiers:
        return f"{seed_name} ({qualifiers[0]})"

    # 4. Default — keep the shared form
    return seed_name


def _derive_evidence_tier(members: list[FindingFeatures]) -> str:
    """A through G per the evidence model (lower letter = stronger)."""
    if any(m.identifiers.get("wikidata_id") or m.identifiers.get("qid")
           for m in members):
        return "B"  # Wikidata-anchored
    if any(m.source in ("wikipedia", "ofac", "un_sanctions")
           for m in members):
        return "C"
    if any(m.source in ("fec", "sec_edgar", "congress",
                         "uk_companies_house", "courtlistener",
                         "opencorporates")
           for m in members):
        return "D"
    return "E"


def _derive_dominant_attrs(members: list[FindingFeatures]) -> dict:
    """The cluster's distinguishing attributes — what the operator sees,
    and what gets stored as binding_evidence."""
    from collections import Counter

    attrs: dict = {}
    jur = Counter(m.jurisdiction for m in members if m.jurisdiction)
    if jur:
        attrs["jurisdiction"] = jur.most_common(1)[0][0]
    regions = Counter(m.region for m in members if m.region)
    if regions:
        attrs["region"] = regions.most_common(1)[0][0]
    extras = Counter()
    for m in members:
        for tok in m.name_extras:
            if (tok.isalpha() and len(tok) >= 3
                    and tok not in _CORP_SUFFIX_TOKENS
                    and tok not in _LABEL_NOISE_TOKENS
                    and tok not in _GENERATIONAL_TOKENS
                    and tok not in _SOURCE_FORMAT_NOISE):
                extras[tok] += 1
    if extras:
        common = [t for t, _ in extras.most_common(3)]
        if common:
            attrs["name_extras"] = common
    if any(m.has_corp_suffix for m in members):
        attrs["is_corporate"] = True
    dobs = [m.dob_year for m in members if m.dob_year]
    if dobs:
        attrs["dob_year"] = dobs[0]
    # Place
    coords = [m.coords_bucket for m in members if m.coords_bucket]
    if coords:
        # Mean of buckets, back to degrees.
        lat = sum(c[0] for c in coords) / len(coords) / 10
        lon = sum(c[1] for c in coords) / len(coords) / 10
        attrs["coords"] = {"lat": round(lat, 2), "lon": round(lon, 2)}
    # Event
    eyears = [m.event_year for m in members if m.event_year]
    if eyears:
        attrs["event_year"] = eyears[0]
    # Org
    fyears = [m.founded_year for m in members if m.founded_year]
    if fyears:
        attrs["founded_year"] = fyears[0]
    industries = Counter(m.industry for m in members if m.industry)
    if industries:
        attrs["industry"] = industries.most_common(1)[0][0]
    # Parenthetical qualifier — most distinguishing single signal when
    # the source itself disambiguated (footballer / hooligan / activist).
    qualifiers = Counter(m.parenthetical_qualifier
                          for m in members if m.parenthetical_qualifier)
    if qualifiers:
        attrs["qualifier"] = qualifiers.most_common(1)[0][0]
    return attrs


def _collect_identifiers(members: list[FindingFeatures]) -> dict:
    out: dict = {}
    for m in members:
        for k, v in m.identifiers.items():
            if k not in out and v:
                out[k] = v
    return out


def _cluster_confidence(members: list[FindingFeatures]) -> float:
    """How tight is the cluster?

    Penalises clusters with low intra-similarity. A single-member
    cluster gets 0.7 — we know it's distinct (it didn't fit anything
    else) but we don't know much about it.
    """
    if len(members) == 1:
        return 0.7
    sims = []
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            sims.append(_attr_similarity(a, b))
    if not sims:
        return 0.7
    return round(sum(sims) / len(sims), 2)


# ── Public API ────────────────────────────────────────────────────────────

# Attach to existing cluster if any member-similarity ≥ this threshold.
# Below this we spawn a new cluster. Tuned for "different middle name +
# different jurisdiction reliably splits" without fragmenting the
# Wikipedia/Wikidata pair (which lands near 0.95).
_CLUSTER_THRESHOLD = 0.55


def extract_meta_findings(findings: list[dict]) -> list[dict]:
    """Return only the meta (non-referent) findings — search aggregates,
    sanctions negatives, etc. These are facts ABOUT the entity, not
    different referents sharing the surface form. Callers route them to
    the anchor's properties instead of letting them form clusters."""
    return [f for f in findings if _is_meta_finding(f)]


# Sources whose results are name-search-driven — if the title shares
# NO token with the seed name, the result was a fuzzy-search false
# positive (e.g. CourtListener returning "Globex Industrial Company" for a
# "Sam Rowan" search). Identifier-anchored sources (Wikipedia,
# Wikidata) are exempt because they may legitimately return findings
# with the entity's real name (Simon Lowe) which doesn't
# share tokens with the seed name (Sam Rowan) but is correct.
_NAME_MATCH_REQUIRED_SOURCES = frozenset({
    "courtlistener", "duckduckgo", "google_books",
    "sunbiz_fl", "ny_dos", "ca_sos", "tx_sos",
    "reddit", "github",
})


def _has_seed_token_overlap(seed_name: str, title: str) -> bool:
    """True if title contains at least one token from the seed name.

    Uses the same tokenizer as the rest of the module. Stop-words and
    very short tokens are filtered out to avoid false positives on
    common words.
    """
    if not seed_name or not title:
        return False
    seed_tokens = set(_norm_tokens(seed_name))
    # Drop very short tokens (single letters, "et", "al", initials)
    seed_tokens = {t for t in seed_tokens if len(t) >= 3}
    if not seed_tokens:
        return True  # Nothing meaningful to require; pass through
    title_tokens = set(_norm_tokens(title))
    return bool(seed_tokens & title_tokens)


def _is_low_quality_finding(seed_name: str, finding: dict) -> bool:
    """Return True for findings whose source is name-search-driven and
    whose title has no token overlap with the seed name. These are
    upstream search false positives that shouldn't form clusters."""
    source = (finding.get("source") or "").lower()
    if source not in _NAME_MATCH_REQUIRED_SOURCES:
        return False
    title = finding.get("title") or ""
    return not _has_seed_token_overlap(seed_name, title)


def _is_meta_finding(finding: dict) -> bool:
    """Return True for findings that aren't referents — they're search
    aggregates, negative-evidence confirmations, or other meta-data
    about the search rather than data about a specific real-world entity.

    Such findings shouldn't form their own clusters. They get routed to
    the anchor cluster as facts (e.g. 'sanctions: not listed [OFAC]').
    """
    source = (finding.get("source") or "").lower()
    finding_type = (finding.get("finding_type") or finding.get("type") or "").lower()
    data = finding.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    # Sanctions sources with explicit "not listed" payload.
    if source in ("ofac", "un_sanctions", "opensanctions"):
        sanctioned = data.get("sanctioned")
        if sanctioned is False:
            return True

    # CourtListener / similar aggregates that count cases without
    # naming a specific party.
    if finding_type in ("court_summary", "case_summary",
                        "loc_search_summary", "search_summary",
                        "aggregate", "summary_count"):
        return True

    return False


def cluster_findings(
    seed_name: str,
    findings: list[dict],
    *,
    canonical_entity_type: str = "person",
) -> list[PersonaCluster]:
    """Group findings under one entity_id into candidate personas.

    Args:
      seed_name: the entity's canonical_name (used as the form / name-
                 shape disambiguator anchor).
      findings:  list of finding dicts as returned by the dossier query
                 (must include finding_id, source, finding_type, title,
                 data, confidence).
      canonical_entity_type: the parent entity's recorded type, used as
                 a tie-breaker when no member has an entity_type hint.

    Returns:
      List of PersonaCluster ordered: anchor first, then by descending
      member count. Empty list if no findings.

    Note: meta findings (sanctions negatives, search aggregates) are
    filtered out before clustering — call extract_meta_findings(findings)
    separately to get those for routing to anchor facts.
    """
    if not findings:
        return []

    # Skip meta findings — they're not referents (see _is_meta_finding).
    findings = [f for f in findings if not _is_meta_finding(f)]
    # Skip low-quality findings — upstream search false positives
    # (e.g. CourtListener returning "Globex Industrial Company" for a Sam
    # Rowan search). They'd form noise clusters otherwise.
    findings = [f for f in findings if not _is_low_quality_finding(seed_name, f)]
    if not findings:
        return []

    # 1. Extract features, sort by source priority + confidence so the
    #    anchor candidate (usually Wikipedia) seeds cluster A.
    feats = [_extract_features(seed_name, f) for f in findings]
    feats.sort(
        key=lambda f: (_SOURCE_PRIORITY.get(f.source, 99),
                       -f.confidence,
                       -f.name_match_score),
    )

    # 2. Greedy single-pass attach.
    clusters: list[list[FindingFeatures]] = []
    for cand in feats:
        best_idx = -1
        best_sim = 0.0
        for idx, members in enumerate(clusters):
            sim = _cluster_features(members, cand)
            if sim > best_sim:
                best_sim = sim
                best_idx = idx
        if best_sim >= _CLUSTER_THRESHOLD and best_idx >= 0:
            clusters[best_idx].append(cand)
        else:
            clusters.append([cand])

    # 3. Build PersonaCluster output. Anchor = the cluster whose
    #    highest-priority member's name_match_score is best — that's the
    #    cluster closest to the canonical name. Tie-break by member count.
    cluster_objs: list[PersonaCluster] = []
    for idx, members in enumerate(clusters):
        sub = _derive_subtype(members) or canonical_entity_type
        cat = "place" if sub in ("country", "city", "region") else "entity"
        attrs = _derive_dominant_attrs(members)
        cluster_objs.append(PersonaCluster(
            cluster_id=chr(ord("A") + idx),
            label=_derive_label(members, seed_name, sub),
            anchor=False,  # set in the next pass
            finding_ids=[m.finding_id for m in members if m.finding_id],
            findings=[m.raw for m in members],
            confidence=_cluster_confidence(members),
            canonical_subtype=sub,
            subtype_path=f"{cat}.{sub}",
            identifiers=_collect_identifiers(members),
            dominant_attrs=attrs,
            evidence_tier=_derive_evidence_tier(members),
        ))

    # 4. Pick anchor — Wikipedia/Wikidata cluster wins; else most members;
    #    else first.
    def _anchor_score(c_idx: int) -> tuple:
        members = clusters[c_idx]
        has_wiki = any(m.source in ("wikipedia", "wikidata") for m in members)
        max_name_score = max((m.name_match_score for m in members), default=0)
        return (has_wiki, max_name_score, len(members))

    anchor_idx = max(range(len(clusters)), key=_anchor_score)
    cluster_objs[anchor_idx].anchor = True

    # Anchor first, then by member count descending.
    cluster_objs.sort(
        key=lambda c: (0 if c.anchor else 1, -len(c.finding_ids)),
    )
    # Re-letter so anchor is "A" regardless of original index.
    for i, c in enumerate(cluster_objs):
        c.cluster_id = chr(ord("A") + i)

    return cluster_objs
