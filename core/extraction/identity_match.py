"""Tier-1 deterministic identity matching — the "dictionary of proper nouns".

Matches document text against the identity library (entities + their aliases) by
normalized n-gram lookup, the same way places resolve against the gazetteer.
Known identities are found without any LLM call — robust to OCR grit, works on
any document. Novel names (not in the library) fall through to Tier-2 discovery
(the LLM), which adds them back so the library self-grows.

This is what makes the collapse general: senses come from the word dictionary,
identities from the identity library; the LLM handles only what neither knows.
"""
from __future__ import annotations

import re

from storage.postgres.db import get_connection
from core.extraction.name_shape import classify_name

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9.'\-]*")
# Single common words that would over-match if used as identities alone.
_STOP = {"the", "and", "for", "report", "department", "office", "committee",
         "states", "united", "general", "letter", "memo", "file", "page",
         "confidential", "secret", "agency", "bureau", "director", "commission"}

# Function words a real person/org name never STARTS with. A work-title or
# common-phrase alias does ("In Fact", "On the Waterfront", "Of Mice and Men"),
# and normalized case-insensitive matching makes such an alias fire on every
# sentence-initial "In fact," / "On point," in prose. Reject these as matching
# aliases — linguistic function words, not entity knowledge. Articles (the/a/an)
# are deliberately excluded: legitimate names begin with them ("The Beatles").
_LEADING_STOP = {"in", "on", "at", "of", "for", "to", "by", "with", "from",
                 "as", "and", "or", "nor", "but", "into", "onto", "upon", "off"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def build_index(tenant: str = "default", min_chars: int = 4):
    """{normalized alias -> (entity_id, canonical_type)} + max phrase word-count.

    Tier-1 matches against the CURATED identity library only — entities that
    came from a reference source (source_kind, e.g. wikipedia_ref / gazetteer),
    were validated, or carry a resolved identifier. Raw LLM-extraction strings
    (source_kind NULL, e.g. "JUNE", "A person", "Cost Account") are NOT a
    dictionary of proper nouns and would only add OCR noise, so they're excluded;
    genuinely new names are still discovered by Tier-2 (the LLM) as before.
    """
    idx: dict[str, tuple[int, str]] = {}
    maxwords = 1
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT entity_id, canonical_name, aliases, canonical_type "
                        "FROM entities WHERE canonical_name IS NOT NULL "
                        "AND tenant_id = %s "
                        # never match entities reclassified out to the topic
                        # library — their mentions were unresolved on purpose.
                        "AND NOT (envelope ? 'reclassified_to') "
                        "AND (source_kind IS NOT NULL "
                        "     OR validation_status = 'validated' "
                        "     OR verified_at IS NOT NULL "
                        "     OR identifier_hash IS NOT NULL)", (tenant,))
            for eid, name, aliases, ctype in cur.fetchall():
                ct = ctype or "entity"
                for nm in [name] + list(aliases or []):
                    n = _norm(nm)
                    parts = n.split()
                    nw = len(parts)
                    if len(n) < min_chars or nw > 6:     # skip too short / OCR-garbage aliases
                        continue
                    # never index a topic-shaped label as a matchable identity
                    # ("Life of Alex Acme", "Relationship of A and B") —
                    # those belong to the topic library, not the person index.
                    if classify_name(nm).is_topic:
                        continue
                    # Precision. A single word must be an acronym (CIA, NSA) —
                    # a lone common word ("grant", "chief") is almost always a
                    # false hit. A multiword name must be PROPER-NOUN-SHAPED
                    # (≥2 capitalized tokens: "Sam Roe", "Analysis
                    # Corporation"). Category loaders sweep in concept pages that
                    # capitalize only the first word ("Security clearance",
                    # "Intelligence agency", "Agent handling") whether typed
                    # person OR organization — this drops them.
                    caps = sum(1 for w in nm.split() if w[:1].isupper())
                    if nw == 1:
                        if not (nm.isupper() and 2 <= len(n) <= 6):
                            continue
                    elif caps < 2:
                        continue
                    # skip phrases that are all stopwords / OCR fragments
                    if all(len(w) < 3 or w in _STOP for w in parts):
                        continue
                    # skip common-phrase / work-title aliases that begin with a
                    # function word ("In Fact" -> George Seldes fired on every
                    # sentence-initial "In fact,"). Keeps single-word acronyms and
                    # real names, which never lead with a bare preposition.
                    if nw > 1 and parts[0] in _LEADING_STOP:
                        continue
                    if n not in idx:                     # first (usually canonical) wins
                        idx[n] = (eid, ct)
                        maxwords = max(maxwords, nw)
    return idx, maxwords


def match_text(text: str, idx: dict, maxwords: int) -> list[tuple]:
    """Longest-match, non-overlapping alias hits: [(surface, start, end, entity_id, type)]."""
    spans = [(m.start(), m.end()) for m in _WORD.finditer(text)]
    words = [text[s:e] for s, e in spans]
    out, i = [], 0
    while i < len(words):
        hit = 0
        for n in range(min(maxwords, len(words) - i), 0, -1):
            phrase = _norm(" ".join(words[i:i + n]))
            if phrase in idx:
                surface = " ".join(words[i:i + n])
                # Proper-noun-in-CONTEXT gate: a real named entity is
                # capitalized where it's used ("Lee Harvey Oswald", "CIA").
                # A concept entity that leaked into the library ("intelligence
                # agency", "security clearance") appears in lowercase prose —
                # reject those. Guards against library-typing mistakes at the
                # point of use, independent of how the entity was stored.
                first_alpha = next((c for c in surface if c.isalpha()), "")
                if not first_alpha or not first_alpha.isupper():
                    continue
                s, e = spans[i][0], spans[i + n - 1][1]
                eid, ctype = idx[phrase]
                out.append((surface, s, e, eid, ctype))
                hit = n
                break
        i += hit or 1
    return out


def match_document(document_id: int, tenant: str = "default",
                   idx: dict | None = None, maxwords: int | None = None) -> dict:
    """Insert Tier-1 identity mentions (entity_id already set) for a document's
    non-garbage paragraphs. Idempotent: clears prior identity_match rows first."""
    if idx is None:
        idx, maxwords = build_index(tenant)
    if not idx:
        return {"identity_mentions": 0, "note": "empty identity library"}

    with get_connection() as conn:
        with conn.cursor() as cur:
            # detach any statements that referenced prior identity mentions,
            # then clear them (idempotent re-match on an already-collapsed doc).
            sub = ("SELECT mention_id FROM document_mentions WHERE document_id=%s "
                   "AND extraction_method='identity_match'")
            cur.execute(f"UPDATE statements SET speaker_mention_id=NULL "
                        f"WHERE speaker_mention_id IN ({sub})", (document_id,))
            cur.execute(f"UPDATE statements SET object_mention_id=NULL "
                        f"WHERE object_mention_id IN ({sub})", (document_id,))
            cur.execute("DELETE FROM document_mentions WHERE document_id=%s "
                        "AND extraction_method='identity_match'", (document_id,))
            cur.execute(
                "SELECT paragraph_id, page_id, raw_text FROM document_paragraphs "
                "WHERE document_id=%s AND coalesce(metadata->>'quality_band','pass') <> 'drop'",
                (document_id,))
            paras = cur.fetchall()
        inserted = 0
        for pid, page_id, text in paras:
            hits = match_text(text or "", idx, maxwords)
            if not hits:
                continue
            with conn.cursor() as cur:
                for surface, cs, ce, eid, ctype in hits:
                    cur.execute(
                        """INSERT INTO document_mentions
                             (tenant_id, document_id, page_id, paragraph_id,
                              char_start, char_end, surface_form, surface_form_norm,
                              suggested_type, confidence, resolution_status,
                              extraction_method, entity_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1.0,'resolved',
                                   'identity_match',%s)""",
                        (tenant, document_id, page_id, pid, cs, ce, surface,
                         _norm(surface), ctype, eid))
                    inserted += 1
        conn.commit()
    return {"identity_mentions": inserted}
