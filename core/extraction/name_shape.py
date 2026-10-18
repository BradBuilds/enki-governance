"""Name-shape classifier — is a label a PERSON name or a TOPIC phrase?

Third leg of the collapse's definitional grounding. Senses (the WordNet
lexicon) and identities (the identity library) each have a home; topic-shaped
labels do not, so they get mis-admitted as junk `person` entities:
"Life of Alex Acme", "Relationship of A and B", "Litigation involving
X", "Remembering Alex Acme", "Acme's Ledger". This routes them away
from the person space and toward the topic library.

Design:
- Deterministic and structural. No model call. No hard dependency on the
  lexicon (the WordNet table is empty on some nodes) — an `is_common_word`
  oracle can be passed in when available, but the structural signals stand on
  their own.
- Precision-first. It ABSTAINS (`uncertain`) on genuinely ambiguous labels —
  titled/royal persons ("Crown Princess of Norway"), lone common-word tokens
  ("Bush"), trailing-possessive variants ("Alex Acme's") — rather than
  risk mistyping a real person or mis-migrating a mergeable variant. Only
  high-confidence `topic` is safe to migrate.
- Auditable. Returns the fired signals and an inferred `topic_kind`.

A real person name is (almost) entirely proper-noun tokens, optionally with
name particles (de/van/von), initials (J.), and suffixes (Jr., III, (spy)).
A topic phrase carries function-word connectives (of/and/involving/between),
a leading abstract noun (Death/Wealth/Litigation), a possessive, a legal-role
prefix, sentence structure, or simply too many tokens to be a name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# Function-word connectives that appear in article titles but essentially never
# inside a personal name. Closed linguistic set.
FUNCTION_WORDS = {
    "of", "and", "the", "a", "an", "or", "for", "in", "on", "at", "to", "with",
    "from", "between", "involving", "among", "amongst", "about", "versus", "vs",
    "into", "onto", "over", "under", "after", "before", "during", "as", "by",
    "amid", "against", "regarding", "concerning", "featuring", "including",
}

# Lowercase tokens that ARE legitimate inside names (nobiliary particles etc.).
# A function-word check must exempt these so "Charles de Gaulle" stays a person.
NAME_PARTICLES = {
    "de", "del", "della", "dela", "van", "von", "der", "den", "di", "da",
    "dos", "das", "du", "la", "le", "bin", "ibn", "al", "el", "ter", "ten",
    "af", "av", "y", "mac", "mc", "st", "ste", "abu", "ap", "ben",
}

# Honorifics / titles. Their presence means "titled person" — such labels can
# legitimately contain "of" ("Crown Princess of Norway"), so a title FLIPS a
# function-word topic signal into abstention rather than a topic verdict.
TITLES = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "professor", "sir", "dame",
    "lord", "lady", "prince", "princess", "king", "queen", "duke", "duchess",
    "earl", "count", "countess", "baron", "baroness", "viscount", "president",
    "vice", "senator", "governor", "judge", "justice", "general", "admiral",
    "colonel", "major", "captain", "lieutenant", "sergeant", "rev", "reverend",
    "pope", "cardinal", "bishop", "sheikh", "emir", "sultan", "chief",
    "secretary", "ambassador", "mayor", "congressman", "congresswoman",
    "representative", "saint", "father", "sister", "imam", "rabbi",
}

# Legal / generic role words. As the FIRST token they mark a role-reference,
# not a clean personal name ("Defendant Acme", "Estate of X").
ROLE_PREFIXES = {
    "defendant", "plaintiff", "witness", "appellant", "appellee", "petitioner",
    "respondent", "deceased", "victim", "suspect", "claimant", "counsel",
    "estate", "matter", "office", "department", "committee", "bureau",
    "agency", "commission", "administration", "ministry",
}

# Abstract / eventive nouns that begin article titles even without a function
# word ("Remembering Alex Acme", "Wealth of X", "Death of X"). Linguistic,
# not entity-specific. Used as ONE signal, and to infer topic_kind.
TOPIC_LEAD_NOUNS = {
    "death": "event", "murder": "event", "killing": "event", "suicide": "event",
    "assassination": "event", "arrest": "event", "indictment": "legal",
    "conviction": "legal", "sentencing": "legal", "trial": "legal",
    "lawsuit": "legal", "litigation": "legal", "case": "legal", "appeal": "legal",
    "prosecution": "legal", "testimony": "legal", "deposition": "legal",
    "investigation": "legal", "inquiry": "legal", "probe": "legal",
    "life": "aspect", "wealth": "aspect", "career": "aspect", "properties": "aspect",
    "property": "aspect", "finances": "aspect", "legacy": "aspect",
    "biography": "aspect", "personal": "aspect", "downfall": "event",
    "rise": "aspect", "fall": "event", "history": "aspect", "timeline": "list",
    "list": "list", "chronology": "list", "relationship": "relationship",
    "relationships": "relationship", "connection": "relationship",
    "connections": "relationship", "ties": "relationship", "links": "relationship",
    "association": "relationship", "scandal": "theme", "affair": "theme",
    "controversy": "theme", "allegations": "theme", "alleged": "theme",
    "conspiracy": "theme", "theories": "theme", "theory": "theme",
    "meme": "theme", "memes": "theme", "funeral": "event", "memorial": "event",
    "obituary": "event", "birthday": "event", "wedding": "event",
    "marriage": "aspect", "divorce": "event", "surviving": "theme",
    "understanding": "theme", "remembering": "theme", "impeachment": "legal",
}

# Person name suffixes (generational / honorary / disambiguating).
PERSON_SUFFIXES = {
    "jr", "sr", "jnr", "snr", "ii", "iii", "iv", "v", "vi", "esq", "phd",
    "md", "cpa", "dds", "llc", "llp",
}

_CONTRACTION = re.compile(r"\b\w+(?:n't|'re|'ve|'ll|'d)\b", re.I)
_POSSESSIVE = re.compile(r"[’']s$", re.I)
_TOKEN = re.compile(r"[^\s]+")


@dataclass
class NameShape:
    label: str                       # 'person' | 'topic' | 'uncertain'
    confidence: float                # 0.0 - 1.0
    signals: list[str] = field(default_factory=list)
    topic_kind: Optional[str] = None  # event|legal|aspect|list|relationship|theme

    @property
    def is_topic(self) -> bool:
        return self.label == "topic"

    @property
    def is_person(self) -> bool:
        return self.label == "person"


def _strip(tok: str) -> str:
    """Lowercased token with surrounding punctuation removed (keeps interior
    apostrophes/periods for initial/possessive detection upstream)."""
    return tok.strip(".,;:()[]{}\"“”").lower()


def _is_initial(tok: str) -> bool:
    t = tok.strip(".")
    return len(t) == 1 and t.isalpha()


def _looks_proper(tok: str) -> bool:
    """Token shaped like a name part: starts uppercase (or is ALL CAPS OCR), no
    digits, not purely punctuation."""
    core = tok.strip(".,;:()[]{}\"“”'’")
    if not core or any(c.isdigit() for c in core):
        return False
    return core[0].isupper()


def classify_name(name: Optional[str],
                  is_common_word: Optional[Callable[[str], bool]] = None) -> NameShape:
    """Classify a label as person / topic / uncertain.

    is_common_word: optional oracle (e.g. a lexicon lookup) returning True for
    ordinary dictionary words. When absent, the curated TOPIC_LEAD_NOUNS set and
    the structural signals carry the decision. Supplying it only *adds* recall
    for leading-common-noun titles; it is never required for precision.
    """
    if not name or not name.strip():
        return NameShape("uncertain", 0.0, ["empty"])

    raw = name.strip()
    toks = _TOKEN.findall(raw)
    lows = [_strip(t) for t in toks]
    n = len(toks)
    signals: list[str] = []
    topic_kind: Optional[str] = None

    has_title = any(l in TITLES for l in lows)

    # --- function-word connective (of/and/involving/between ...) --------------
    # Interior only: a leading "The"/"A" is a title/name lead, not a connective.
    connective = False
    for i, (tok, l) in enumerate(zip(toks, lows)):
        if i == 0:
            continue
        # A middle/trailing initial ("John A. Smith", "SMITH, JOHN A.")
        # is not the article "a"/"i" — skip single-letter tokens.
        if _is_initial(tok):
            continue
        if l in FUNCTION_WORDS and l not in NAME_PARTICLES:
            connective = True
            break
    if connective:
        signals.append("function_connective")

    # --- leading topic noun ("Death of X", "Surviving X") ---------------------
    first = lows[0] if lows else ""
    lead_topic = first in TOPIC_LEAD_NOUNS
    if lead_topic:
        signals.append("topic_lead_noun")
        topic_kind = TOPIC_LEAD_NOUNS[first]
    # any topic noun anywhere (weaker) — also used to infer kind
    for l in lows:
        if l in TOPIC_LEAD_NOUNS:
            topic_kind = topic_kind or TOPIC_LEAD_NOUNS[l]
            if "topic_noun" not in signals and not lead_topic:
                signals.append("topic_noun")
            break

    # optional lexicon oracle: a name is proper nouns; a phrase carries common
    # dictionary words. A SINGLE common token is fine — many given names are
    # common words (Grant, Hope, Faith, June, Art) — but TWO+ common content
    # tokens means it's a phrase, not a name ("Acme Anniversary Album").

    # --- possessive -----------------------------------------------------------
    trailing_possessive = bool(toks) and bool(_POSSESSIVE.search(toks[-1]))
    interior_possessive = any(_POSSESSIVE.search(t) for t in toks[:-1])
    if interior_possessive:
        signals.append("interior_possessive")   # "Acme's Ledger" -> topic
    elif trailing_possessive:
        signals.append("trailing_possessive")    # "Alex Acme's" -> variant

    # --- role prefix ----------------------------------------------------------
    if first in ROLE_PREFIXES:
        signals.append("role_prefix")

    # --- sentence structure ---------------------------------------------------
    if _CONTRACTION.search(raw) or "?" in raw or "!" in raw or "&" in raw:
        signals.append("sentence_marker")

    # --- token count ----------------------------------------------------------
    if n >= 7:
        signals.append("too_many_tokens")

    # --- proper-token ratio ---------------------------------------------------
    content = [t for t, l in zip(toks, lows)
               if l not in NAME_PARTICLES and l not in FUNCTION_WORDS
               and l not in PERSON_SUFFIXES and not _is_initial(t)]
    proper = [t for t in content if _looks_proper(t)]
    proper_ratio = (len(proper) / len(content)) if content else 1.0

    # optional lexicon: 2+ common content words => a phrase, not a name.
    if is_common_word and len(content) >= 2:
        commons = sum(1 for t in content
                      if is_common_word(_strip(t)) and _strip(t) not in TITLES)
        if commons >= 2:
            signals.append("lexicon_phrase")

    # ---------------- decision (precision-first) ------------------------------
    # Strong topic signals that are unambiguous even when a title is present
    # ("Relationship of Prince A and B" is a topic despite "Prince").
    strong_hard = {"topic_lead_noun", "interior_possessive", "sentence_marker",
                   "too_many_tokens", "lexicon_phrase"}
    hard = strong_hard.intersection(signals)
    if hard:
        conf = min(0.95, 0.78 + 0.06 * len(hard))
        topic_kind = topic_kind or _infer_kind(lows)
        return NameShape("topic", conf, signals, topic_kind)

    # Titled person whose only topic signal is the connective ("… Princess of
    # Norway") — a real person, abstain rather than migrate.
    if has_title and connective:
        return NameShape("uncertain", 0.5, signals + ["title_with_connective"], topic_kind)

    # Trailing-possessive-only variant ("Alex Acme's") — a mergeable
    # variant of a person, not a topic. Leave for coref/dedup.
    if trailing_possessive and not interior_possessive and not connective:
        return NameShape("uncertain", 0.6, signals + ["possessive_variant"], None)

    # Connective without a title -> topic ("Three Days at Acme's").
    if connective:
        topic_kind = topic_kind or _infer_kind(lows)
        return NameShape("topic", 0.8, signals, topic_kind)

    # Role-prefixed ("Defendant Acme") — references a person but isn't a
    # clean name. Abstain (don't migrate, don't trust as canonical person).
    if "role_prefix" in signals:
        return NameShape("uncertain", 0.5, signals, topic_kind)

    # Single token: a proper non-function word is a mononym/surname (person);
    # otherwise abstain (could be a common word — no lexicon to decide).
    if n == 1:
        if _looks_proper(toks[0]) and first not in FUNCTION_WORDS and first not in TOPIC_LEAD_NOUNS:
            return NameShape("person", 0.6, signals + ["mononym"])
        return NameShape("uncertain", 0.4, signals + ["single_token"])

    # Otherwise: mostly-proper tokens, no topic signals -> person.
    if proper_ratio >= 0.5:
        return NameShape("person", min(0.9, 0.6 + 0.3 * proper_ratio), signals + ["proper_tokens"])

    return NameShape("uncertain", 0.4, signals + ["low_proper_ratio"], topic_kind)


def _infer_kind(lows: list[str]) -> str:
    for l in lows:
        if l in TOPIC_LEAD_NOUNS:
            return TOPIC_LEAD_NOUNS[l]
    if "between" in lows:
        return "relationship"
    return "theme"
