"""Wikidata validation provider.

Uses the Wikidata REST search API (wbsearchentities) followed by a
targeted entity fetch (wbgetentities) to get canonical name + aliases +
type claims. Picks the best-scoring hit from the top 5, where score
combines type fit + temporal plausibility + context overlap. (Old
behavior: first-valid-type wins, which lost Alex-the-financier to
Jacob-the-painter.)

Wikidata's type system is too broad for us — we map their Q-typed
"instance of" claims to our internal taxonomy (person, organization,
aircraft, vessel, place, …). The mapping table is below and lives as a
constant so we can govern it through a future pack.

Rate limits: wbsearchentities is ~1 req/sec burst, wbgetentities is
similar. The entity_validator worker runs under the governor's REVIEW
priority + 10/min limit so we comfortably stay under Wikidata's ceiling.
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import Optional

import requests

from core.entity_validation.provider import Provider
from core.entity_validation.types import Candidate, Match

log = logging.getLogger("enki.validation.wikidata")


# ── Scoring helpers ──────────────────────────────────────────────────

def _now_year() -> int:
    return datetime.datetime.now(datetime.timezone.utc).year


def _year_from_claim(entity: dict, prop: str) -> Optional[int]:
    """Extract the year from a Wikidata time-typed claim (P569/P570/etc).
    Returns None if the claim is absent or unparseable.
    """
    claims = (entity.get("claims") or {}).get(prop) or []
    for claim in claims:
        try:
            time_str = claim["mainsnak"]["datavalue"]["value"]["time"]
            # Wikidata times look like "+1953-01-20T00:00:00Z" or
            # "-0050-08-15T00:00:00Z" for BCE. We just need the year.
            m = re.match(r"([+-]?)(\d+)-", time_str)
            if not m:
                continue
            sign, year = m.group(1), int(m.group(2))
            if sign == "-":
                year = -year
            return year
        except (KeyError, TypeError, ValueError):
            continue
    return None


_HISTORICAL_HINT_RX = re.compile(
    r"\b(historic|historical|century|ancient|medieval|renaissance|"
    r"victorian|edwardian|colonial|antiquity|18\d{2}|19[0-3]\d)\b",
    re.IGNORECASE,
)


def _context_has_historical_hint(context: Optional[str]) -> bool:
    """Quick heuristic — does the surrounding text reference a historical
    period? Lets us NOT penalise long-dead candidates when the article
    is clearly about them (biography, art history, etc.)."""
    if not context:
        return False
    return bool(_HISTORICAL_HINT_RX.search(context))


# Description tokens that signal a derivative / disqualified entity.
# These are GENERIC quality cues, not entity-specific knowledge:
# - "alleged" → unconfirmed / rumored figures
# - "fictional" / "hoax" / "fake" → not-real referents
# - "duplicate article" → Wikidata-side merge candidate, should not win
# - "wikimedia disambiguation page" → meta page, not the referent
_DESCRIPTION_PENALTIES = (
    "alleged",
    "fictional",
    "fictitious",
    "hoax",
    "fake",
    "rumored",
    "speculated",
    "duplicate article",
    "disambiguation page",
    "namesake of",
    "named after",
    "given name",
    "surname",
)


_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for",
    "from", "by", "with", "as", "is", "was", "are", "were", "be", "been",
    "being", "has", "had", "have", "this", "that", "these", "those",
    "it", "its", "his", "her", "their", "our", "us", "we", "you", "he",
    "she", "they", "them", "him", "who", "what", "which", "when",
    "where", "why", "how", "but", "if", "not", "no", "so", "do", "did",
    "does", "would", "could", "should", "will", "can", "may", "might",
    "must", "than", "then", "into", "out", "up", "down", "over", "under",
    "about", "after", "before", "between", "during", "through",
})


def _significant_words(text: str) -> set[str]:
    return {
        w.lower() for w in re.findall(r"\b[a-zA-Z][a-zA-Z\-]{2,}\b", text or "")
        if w.lower() not in _STOPWORDS
    }


def _lexical_overlap(
    description: str, context: str, exclude: Optional[str] = None
) -> tuple[float, list[str]]:
    """Score lexical overlap between description and context. Each shared
    significant word is worth 0.3, capped at 1.5. Returns (score, words_used).

    The candidate's own name and the input surface form are typically
    substrings of both — circular matches that signal nothing about
    semantic fit. Pass the input raw_text via `exclude` to drop those
    tokens before scoring (e.g. for input "Dana Vale", "vale" must
    not count toward overlap because every Vale-descended Wikidata
    entry has "Vale" in its description).
    """
    if not description or not context:
        return (0.0, [])
    d = _significant_words(description)
    c = _significant_words(context)
    common = d & c
    if exclude:
        common = common - _significant_words(exclude)
    if not common:
        return (0.0, [])
    common_sorted = sorted(common)
    score = min(0.3 * len(common_sorted), 1.5)
    return (score, common_sorted[:8])


# Compatible-but-not-identical mapping (mostly for the lossy hint_type
# coming from the upstream extractor). Keeping a SMALL table here is
# justified — these are taxonomy aliases, not entity-specific knowledge,
# and they govern only the scoring nudge.
_COMPATIBLE_HINTS = {
    "person": {"human", "people"},
    "organization": {"org", "company", "corporation", "agency", "ngo"},
    "government_agency": {"agency", "organization", "ministry"},
    "city": {"place", "town", "municipality"},
    "country": {"place", "nation", "state"},
    "region": {"place", "area"},
}


def _hint_is_compatible(hint: str, mapped_type: str) -> bool:
    return hint in _COMPATIBLE_HINTS.get(mapped_type, set())

_SEARCH_URL = "https://www.wikidata.org/w/api.php"
_ENTITY_URL = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
_USER_AGENT = "Enki/1.0 entity-validator (contact: ops@example.com)"


# Map Wikidata "instance of" Q-IDs to our canonical type taxonomy.
# Mapping is intentionally inclusive — subclasses are walked server-side
# by Wikidata's claims; we just need a sensible bucket for the UI badge.
_QID_TO_TYPE: dict[str, str] = {
    # People
    "Q5": "person",                         # human
    "Q14623683": "fictional_person",        # fictional human

    # Organizations
    "Q43229": "organization",               # organization
    "Q4830453": "organization",             # business
    "Q6881511": "organization",             # enterprise
    "Q783794": "organization",              # company
    "Q891723": "organization",              # public company
    "Q786820": "organization",              # motor vehicle manufacturer
    "Q431289": "organization",              # brand
    "Q1616075": "organization",             # telecommunications company
    "Q7210356": "organization",             # political organization
    "Q7278": "organization",                # political party
    "Q163740": "organization",              # nonprofit organization
    "Q484652": "organization",              # international organization
    "Q38819":  "organization",              # intergovernmental organization
    "Q748019": "organization",              # social movement
    "Q484170": "organization",              # municipality
    "Q6498903": "organization",             # aerospace manufacturer
    "Q18388277": "organization",            # technology company
    "Q327333": "government_agency",         # government agency
    "Q2659904": "government_agency",        # government organization
    "Q7188":   "government_agency",         # government
    "Q8187769":"government_agency",         # ministry
    "Q3918":   "university",                # university
    "Q11033":  "mass_media",                # mass media
    "Q27686":  "research_institute",        # research institute
    "Q204873": "military_unit",             # military unit
    "Q176799": "military_unit",             # military organization
    "Q4508":   "military_unit",             # military formation
    "Q3018230":"military_unit",             # military alliance  (NATO, etc.)
    "Q2945919":"military_unit",             # branch of military
    "Q1934170":"military_unit",             # armed force
    "Q2992826":"military_unit",             # defense force

    # Places — political
    "Q515":    "city",                      # city
    "Q1549591":"city",                      # big city
    "Q1637706":"city",                      # city with millions of inhabitants
    "Q484170": "city",                      # commune (FR)
    "Q6256":   "country",                   # country
    "Q3624078":"country",                   # sovereign state
    "Q7275":   "country",                   # state
    "Q1763527":"country",                   # constitutional state
    "Q107390": "region",                    # federated state
    "Q1048835":"region",                    # political territorial entity
    "Q82794":  "region",                    # geographic region
    "Q35145263":"region",                   # subregion
    "Q3336843":"region",                    # statistical territorial entity

    # Places — natural features (was missing — Strait of Hormuz lives here)
    "Q34942":  "strait",                    # strait
    "Q1172599":"bay",                       # bay
    "Q3323775":"gulf",                      # gulf
    "Q12284":  "canal",                     # canal
    "Q47521":  "peninsula",                 # peninsula
    "Q23442":  "island",                    # island
    "Q33837":  "archipelago",               # archipelago
    "Q46831":  "mountain_range",            # mountain range
    "Q8502":   "mountain",                  # mountain
    "Q35509":  "desert",                    # desert
    "Q4022":   "river",                     # river
    "Q23397":  "lake",                      # lake
    "Q165":    "sea",                       # sea
    "Q9430":   "ocean",                     # ocean
    "Q201404": "gulf",                      # gulf (alt)
    "Q39594":  "reef",                      # reef
    "Q25325":  "island_group",              # island group
    "Q5107":   "continent",                 # continent

    # Infrastructure
    "Q1248784":"airport",                   # airport
    "Q644371": "airport",                   # international airport
    "Q283202": "seaport",                   # port
    "Q124282": "railway_station",           # railway station
    "Q159719": "pipeline",                  # pipeline
    "Q158438": "bridge",                    # bridge
    "Q12323":  "dam",                       # dam
    "Q134447": "power_plant",               # power station
    "Q81623":  "refinery",                  # oil refinery

    # Equipment / vehicles / vessels / aircraft
    "Q11436":  "aircraft",                  # aircraft
    "Q197":    "aircraft",                  # airplane
    "Q34486":  "aircraft",                  # airplane (alt)
    "Q34442":  "aircraft",                  # fighter aircraft
    "Q208187": "aircraft",                  # attack aircraft
    "Q744620": "aircraft",                  # bomber
    "Q744193": "aircraft",                  # military transport aircraft
    "Q3041792":"aircraft",                  # transport aircraft
    "Q5961005":"aircraft",                  # trainer aircraft
    "Q1184757":"aircraft",                  # cargo aircraft
    "Q124072": "helicopter",                # rotorcraft
    "Q34564":  "helicopter",                # helicopter
    "Q901486": "helicopter",                # military helicopter
    "Q46970":  "airliner",                  # airliner
    "Q11438":  "unmanned_aerial_vehicle",   # UAV
    "Q2241046":"unmanned_aerial_vehicle",   # combat drone
    "Q2875704":"unmanned_aerial_vehicle",   # unmanned aerial vehicle
    "Q11446":  "vessel",                    # ship
    "Q1318029":"vessel",                    # merchant vessel
    "Q1172902":"vessel",                    # naval ship
    "Q1261534":"vessel",                    # warship
    "Q170382": "vehicle",                   # motor vehicle
    "Q3609473":"vehicle",                   # armored fighting vehicle
    "Q223269": "vehicle",                   # tank
    "Q40598":  "weapon",                    # weapon
    "Q2223648":"weapon",                    # weapon system
    "Q171208": "weapon",                    # missile
    "Q40605":  "weapon",                    # ballistic missile
    "Q180079": "weapon",                    # cruise missile
    "Q47941":  "weapon",                    # bomb
    "Q2488":   "spacecraft",                # spacecraft
    "Q149918": "spacecraft",                # artificial satellite

    # Events
    "Q1656682":"event",                     # event
    "Q645883": "event",                     # military operation
    "Q178561": "event",                     # battle
    "Q198":    "event",                     # war
    "Q189290": "event",                     # armed conflict
    "Q27020041":"event",                    # natural disaster
    "Q8065":   "event",                     # election
}


# Description-keyword fallback. Ordered: first match wins. Matched with
# word-boundary regex so "manufactured by" doesn't trigger "manufacturer"
# and send a helicopter to the organization bucket. Lowercase phrases only.
_DESC_KEYWORDS: tuple[tuple[str, str], ...] = (
    # Most-specific first (geographic features before cities/countries,
    # equipment subclasses before "aircraft", etc.)
    # Natural features — put before organizations so "X strait" wins over
    # any accidental "organization" catch elsewhere.
    ("strait",                      "strait"),
    ("canal",                       "canal"),
    ("bay",                         "bay"),
    ("gulf",                        "gulf"),
    ("peninsula",                   "peninsula"),
    ("archipelago",                 "archipelago"),
    ("mountain range",              "mountain_range"),
    ("mountain",                    "mountain"),
    ("volcano",                     "mountain"),
    ("glacier",                     "mountain"),
    ("desert",                      "desert"),
    ("waterway",                    "canal"),
    ("river",                       "river"),
    ("lake",                        "lake"),
    ("sea",                         "sea"),
    ("ocean",                       "ocean"),
    ("reef",                        "reef"),
    ("island group",                "island_group"),
    ("island",                      "island"),
    ("continent",                   "continent"),
    # Infrastructure
    ("airport",                     "airport"),
    ("international airport",       "airport"),
    ("seaport",                     "seaport"),
    ("port",                        "seaport"),
    ("harbor",                      "seaport"),
    ("harbour",                     "seaport"),
    ("railway station",             "railway_station"),
    ("pipeline",                    "pipeline"),
    ("bridge",                      "bridge"),
    ("dam",                         "dam"),
    ("power station",               "power_plant"),
    ("power plant",                 "power_plant"),
    ("refinery",                    "refinery"),
    # Orgs — specific kinds first, then "manufacturer" guarded against
    # being inside "manufactured by" via word boundary.
    ("manufacturer",                "organization"),
    ("company",                     "organization"),
    ("corporation",                 "organization"),
    ("conglomerate",                "organization"),
    ("publisher",                   "organization"),
    ("broadcaster",                 "organization"),
    ("publishing",                  "organization"),
    ("news organization",           "organization"),
    ("newspaper",                   "organization"),
    ("international organization",  "organization"),
    ("intergovernmental organization","organization"),
    ("military alliance",           "military_unit"),
    ("treaty organization",         "organization"),
    ("political party",             "organization"),
    ("nonprofit",                   "organization"),
    ("non-profit",                  "organization"),
    ("ngo",                         "organization"),
    ("think tank",                  "organization"),
    ("agency",                      "government_agency"),
    ("ministry",                    "government_agency"),
    ("bureau",                      "government_agency"),
    ("commission",                  "government_agency"),
    ("department of",               "government_agency"),
    # Military
    ("military unit",               "military_unit"),
    ("regiment",                    "military_unit"),
    ("division",                    "military_unit"),
    ("brigade",                     "military_unit"),
    ("battalion",                   "military_unit"),
    ("army",                        "military_unit"),
    ("air force",                   "military_unit"),
    ("navy",                        "military_unit"),
    ("marine corps",                "military_unit"),
    ("coast guard",                 "military_unit"),
    # Aircraft (most specific first, "aircraft" generic last)
    ("airliner",                    "aircraft"),
    ("bomber",                      "aircraft"),
    ("fighter aircraft",            "aircraft"),
    ("military aircraft",           "aircraft"),
    ("transport aircraft",          "aircraft"),
    ("reconnaissance aircraft",     "aircraft"),
    ("trainer aircraft",            "aircraft"),
    ("jet aircraft",                "aircraft"),
    ("cargo aircraft",              "aircraft"),
    ("aircraft",                    "aircraft"),
    ("helicopter",                  "helicopter"),
    ("rotorcraft",                  "helicopter"),
    ("unmanned aerial vehicle",     "unmanned_aerial_vehicle"),
    ("uav",                         "unmanned_aerial_vehicle"),
    ("drone",                       "unmanned_aerial_vehicle"),
    # Vessels + vehicles + weapons
    ("warship",                     "vessel"),
    ("submarine",                   "vessel"),
    ("destroyer",                   "vessel"),
    ("frigate",                     "vessel"),
    ("cruiser",                     "vessel"),
    ("aircraft carrier",            "vessel"),
    ("cargo ship",                  "vessel"),
    ("merchant vessel",             "vessel"),
    ("naval ship",                  "vessel"),
    ("ship",                        "vessel"),
    ("tank",                        "vehicle"),
    ("armored fighting vehicle",    "vehicle"),
    ("armoured fighting vehicle",   "vehicle"),
    ("infantry fighting vehicle",   "vehicle"),
    ("ballistic missile",           "weapon"),
    ("cruise missile",              "weapon"),
    ("missile",                     "weapon"),
    ("anti-tank",                   "weapon"),
    ("artillery",                   "weapon"),
    ("rifle",                       "weapon"),
    ("spacecraft",                  "spacecraft"),
    ("satellite",                   "spacecraft"),
    # People (fallback — Wikidata usually has Q5 direct)
    ("politician",                  "person"),
    ("journalist",                  "person"),
    ("engineer",                    "person"),
    ("businessman",                 "person"),
    ("businesswoman",               "person"),
    # Places
    ("capital city",                "city"),
    ("capital of",                  "city"),
    ("municipality",                "city"),
    ("town",                        "city"),
    ("city in",                     "city"),
    ("country in",                  "country"),
    ("sovereign state",             "country"),
    ("oblast",                      "region"),
    ("province",                    "region"),
    ("state in",                    "region"),
    ("region of",                   "region"),
)

# Compile keyword phrases to word-boundary regexes once at import.
# `manufacturer` won't match inside `manufactured by` this way.
import re as _re
_DESC_PATTERNS: tuple[tuple["_re.Pattern", str], ...] = tuple(
    (_re.compile(r"\b" + _re.escape(phrase) + r"\b", _re.IGNORECASE), mapped)
    for phrase, mapped in _DESC_KEYWORDS
)


def _type_from_description(desc: str) -> Optional[str]:
    for pattern, mapped in _DESC_PATTERNS:
        if pattern.search(desc):
            return mapped
    return None


class WikidataProvider(Provider):
    name = "wikidata"
    priority = 10
    timeout_seconds = 10.0
    supports_kinds = ("entity", "place", "topic")

    # Per-Q-ID resolution cache — once we walk P279 for Q856317 and discover
    # it ultimately subclasses from Q11436 (aircraft), we cache the mapping.
    # Every future sighting of that Q-ID is a dict lookup, zero network calls.
    # Process-scope; repopulated across restarts. A persistent cache table
    # comes in Phase 5D.
    _qid_type_cache: dict[str, Optional[str]] = {}

    # Cap how far the subclass walk goes. P279 chains are usually 3-6 hops
    # from a specific concept up to a general one; 10 is a safe ceiling
    # against a runaway Wikidata graph.
    _P279_MAX_DEPTH = 8

    def __init__(self, timeout_seconds: float = 10.0):
        self.timeout_seconds = timeout_seconds
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})

    def validate(self, candidate: Candidate) -> Optional[Match]:
        # 1. Search — find QIDs whose label or aliases match the candidate.
        hits = self._search(candidate.raw_text)
        if not hits:
            return None

        # 2. Fetch the top-N hits' full entity records, score each by
        #    type fit + temporal plausibility + context overlap, and
        #    pick the highest-scoring candidate. The old logic took the
        #    first hit whose mapped_type was non-None, which lost
        #    Alex Acme (Q2222222) to Jacob Acme
        #    (painter, Q3333333) because the search returned the dead
        #    painter first and the loop never looked further. Scoring
        #    is corpus-grounded — no hardcoded entity → QID mappings.
        scored = []
        for hit in hits[:5]:
            qid = hit["id"]
            entity = self._fetch_entity(qid)
            if entity is None:
                continue

            mapped_type = self._map_type(entity)
            if mapped_type is None:
                continue

            # Kind-level sanity: a candidate marked as "place" shouldn't
            # match a person/org/aircraft/etc. (entity → place direction
            # remains allowed — see comment above the score loop below.)
            if candidate.kind == "place" and mapped_type not in {
                "city", "country", "region", "river", "lake", "sea",
            }:
                continue

            score, score_breakdown = self._score_hit(hit, entity, candidate, mapped_type)
            scored.append({
                "hit": hit,
                "qid": qid,
                "entity": entity,
                "mapped_type": mapped_type,
                "score": score,
                "breakdown": score_breakdown,
            })

        if not scored:
            return None

        scored.sort(key=lambda x: x["score"], reverse=True)
        best = scored[0]

        # Confidence reflects how decisive the win was. If the runner-up
        # is within 0.2 of the winner, we have an ambiguous case — keep
        # confidence modest so the dossier UI surfaces alternatives.
        margin = (
            best["score"] - scored[1]["score"] if len(scored) > 1 else 1.0
        )
        confidence = max(0.5, min(0.99, 0.65 + 0.30 * min(margin, 1.0)))

        entity = best["entity"]
        qid = best["qid"]
        labels = entity.get("labels", {})
        aliases_obj = entity.get("aliases", {})
        en_label = labels.get("en", {}).get("value") or candidate.raw_text
        en_aliases = [a.get("value") for a in aliases_obj.get("en", []) if a.get("value")]
        descr = entity.get("descriptions", {}).get("en", {}).get("value")

        log.debug(
            "wikidata %r → %s (%s, score=%.2f margin=%.2f breakdown=%s)",
            candidate.raw_text, qid, best["mapped_type"], best["score"],
            margin, best["breakdown"],
        )

        return Match(
            source="wikidata",
            source_id=qid,
            source_url=f"https://www.wikidata.org/wiki/{qid}",
            canonical_name=en_label,
            canonical_type=best["mapped_type"],
            aliases=en_aliases,
            identifiers={"wikidata_qid": qid},
            confidence=confidence,
            description=descr,
            extra={
                "wikidata_qid": qid,
                "instance_of_qids": self._instance_of(entity),
                "score": best["score"],
                "score_margin": margin,
                "score_breakdown": best["breakdown"],
                "candidates_considered": [
                    {"qid": s["qid"], "score": s["score"],
                     "type": s["mapped_type"]}
                    for s in scored[:5]
                ],
            },
        )

    # ── scoring ──────────────────────────────────────────────────────

    def _score_hit(
        self, hit: dict, entity: dict, candidate: Candidate,
        mapped_type: str,
    ) -> tuple[float, dict]:
        """Score a single Wikidata candidate for fit against the input
        candidate. Higher is better. Returns (score, breakdown_dict)
        so the breakdown can be logged + persisted as extra evidence.

        Components (each contributes to a total in [0, 5]):
          - type_match (1.0): always 1.0 if mapped_type passed our taxonomy
          - hint_alignment (0..1.0): description / mapped_type vs caller hint_type
          - lifespan_fit (-1.0..1.0): living/recent vs ancient-dead;
            penalises matching modern news to a long-dead namesake
          - context_overlap (0..1.5): words from entity description that
            also appear in candidate.context (the surrounding sentence)
          - label_quality (0..0.5): exact label match vs alias-only match
        """
        breakdown: dict = {}
        score = 0.0

        # type_match — always 1.0 once we reach scoring (the loop
        # already filtered unmappable hits)
        score += 1.0
        breakdown["type_match"] = 1.0

        # hint_alignment
        hint = (candidate.hint_type or "").lower().strip()
        hint_score = 0.0
        if hint:
            if hint == mapped_type:
                hint_score = 1.0
            elif _hint_is_compatible(hint, mapped_type):
                hint_score = 0.5
        score += hint_score
        breakdown["hint_alignment"] = hint_score

        # lifespan_fit — DOD long ago + no historical hint in context = penalty
        dod_year = _year_from_claim(entity, "P570")  # date of death
        dob_year = _year_from_claim(entity, "P569")  # date of birth
        current_year = _now_year()
        lifespan_score = 0.0
        if dod_year is not None:
            years_since_death = current_year - dod_year
            ctx_has_historical = _context_has_historical_hint(candidate.context)
            if years_since_death > 50 and not ctx_has_historical:
                lifespan_score = -1.0
            elif years_since_death > 20 and not ctx_has_historical:
                lifespan_score = -0.5
        elif dob_year is not None and dob_year > current_year - 110:
            # Living, plausible age range — small positive signal
            lifespan_score = 0.3
        score += lifespan_score
        breakdown["lifespan_fit"] = lifespan_score
        if dod_year is not None:
            breakdown["dod_year"] = dod_year
        if dob_year is not None:
            breakdown["dob_year"] = dob_year

        # context_overlap — exclude the candidate's own name so we
        # don't get circular matches ("Vale" in context vs "Vale" in
        # "son of Dana Vale Jr." description is meaningless signal).
        descr = ((entity.get("descriptions") or {}).get("en") or {}).get("value", "")
        ctx = (candidate.context or "")
        overlap_score, overlap_words = _lexical_overlap(
            descr, ctx, exclude=candidate.raw_text,
        )
        score += overlap_score
        breakdown["context_overlap"] = overlap_score
        if overlap_words:
            breakdown["overlap_words"] = overlap_words

        # label_quality — exact label match boosts confidence over alias match
        label_quality = 0.5 if hit.get("match", {}).get("type") == "label" else 0.2
        score += label_quality
        breakdown["label_quality"] = label_quality

        # description_quality — penalise descriptions that explicitly
        # mark the candidate as derivative/disqualified ("alleged",
        # "fictional", "duplicate article", etc.), bonus for richer
        # descriptions (the real famous referent typically has the
        # longest, most informative description in Wikidata). Generic
        # heuristics, no entity-specific knowledge.
        descr_lower = descr.lower() if descr else ""
        desc_quality = 0.0
        penalty_hits = []
        for needle in _DESCRIPTION_PENALTIES:
            if needle in descr_lower:
                desc_quality -= 0.8
                penalty_hits.append(needle)
                # One penalty per candidate is enough; multiple matches
                # for the same disqualifier shouldn't double-count.
                break
        # Length bonus: tops out at ~200 chars (well-developed bio /
        # description). Caps at +0.3 so it can't dominate the other
        # axes; the goal is to break ties, not override real signal.
        if descr_lower:
            desc_quality += min(0.3, len(descr_lower) / 700.0)
        score += desc_quality
        breakdown["description_quality"] = desc_quality
        if penalty_hits:
            breakdown["description_penalty_hits"] = penalty_hits

        return (score, breakdown)

    # ── internals ─────────────────────────────────────────────────────

    def _search(self, text: str) -> list[dict]:
        params = {
            "action": "wbsearchentities",
            "search": text,
            "language": "en",
            "format": "json",
            "limit": 5,
            "origin": "*",
        }
        try:
            r = self._session.get(_SEARCH_URL, params=params,
                                  timeout=self.timeout_seconds)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.debug("wikidata search %r failed: %s", text, e)
            return []
        return data.get("search", []) or []

    def _fetch_entity(self, qid: str) -> Optional[dict]:
        url = _ENTITY_URL.format(qid=qid)
        try:
            r = self._session.get(url, timeout=self.timeout_seconds)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.debug("wikidata entity fetch %s failed: %s", qid, e)
            return None
        return (data.get("entities") or {}).get(qid)

    def _instance_of(self, entity: dict) -> list[str]:
        claims = entity.get("claims", {}) or {}
        p31 = claims.get("P31") or []
        out = []
        for claim in p31:
            try:
                qid = claim["mainsnak"]["datavalue"]["value"]["id"]
                out.append(qid)
            except (KeyError, TypeError):
                continue
        return out

    def _map_type(self, entity: dict) -> Optional[str]:
        # Pass 1 — exact Q-ID match against our curated taxonomy (fastest)
        instance_qids = self._instance_of(entity)
        for qid in instance_qids:
            if qid in _QID_TO_TYPE:
                return _QID_TO_TYPE[qid]

        # Pass 2 — description-keyword fallback using word-boundary regex.
        # Catches the long tail where Wikidata's Q-ID is too specific for
        # our hand-curated map (public company, heavy-lift helicopter, etc).
        desc = (
            (entity.get("descriptions") or {}).get("en") or {}
        ).get("value", "")
        if desc:
            mapped = _type_from_description(desc)
            if mapped:
                return mapped

        # Pass 3 — subclass walk. For each instance-of Q-ID, climb P279
        # ("subclass of") edges until we hit something in our map. Most
        # of Wikidata's long tail resolves this way — e.g. Q856317
        # (Sikorsky CH-53E) → Q34564 (helicopter, in our map).
        for qid in instance_qids:
            climbed = self._climb_subclass_tree(qid)
            if climbed:
                return climbed

        return None

    def _climb_subclass_tree(self, start_qid: str, depth: int = 0) -> Optional[str]:
        """BFS up the P279 subclass graph until a mapped ancestor is found.

        Uses the in-process _qid_type_cache for fast lookups within a
        single validator process. A persistent wikidata_qid_cache table
        exists (migration 018) — the governor-hosted worker reads/writes
        that table directly via core.entity_validation.cache; this
        provider stays lightweight and process-local."""
        if start_qid in self._qid_type_cache:
            return self._qid_type_cache[start_qid]

        if depth >= self._P279_MAX_DEPTH:
            self._qid_type_cache[start_qid] = None
            return None

        entity = self._fetch_entity(start_qid)
        if entity is None:
            self._qid_type_cache[start_qid] = None
            return None

        claims = entity.get("claims", {}) or {}
        p279 = claims.get("P279") or []
        parent_qids: list[str] = []
        for claim in p279:
            try:
                pqid = claim["mainsnak"]["datavalue"]["value"]["id"]
                parent_qids.append(pqid)
            except (KeyError, TypeError):
                continue

        for pqid in parent_qids:
            if pqid in _QID_TO_TYPE:
                result = _QID_TO_TYPE[pqid]
                self._qid_type_cache[start_qid] = result
                return result

        for pqid in parent_qids:
            result = self._climb_subclass_tree(pqid, depth=depth + 1)
            if result is not None:
                self._qid_type_cache[start_qid] = result
                return result

        self._qid_type_cache[start_qid] = None
        return None
