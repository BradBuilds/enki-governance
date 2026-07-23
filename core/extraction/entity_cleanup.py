"""
Entity Cleanup — demonym resolution, abbreviation expansion, source filtering,
and cross-type merging. Called by the reconciliation worker and on entity access.

This module handles the cases that simple bigram similarity misses:
  - "Iranian" → "Iran" (demonym to country)
  - "US" → "United States" (abbreviation)
  - "Reuters" → skip (news source, not intelligence entity)
  - "US" (org) + "United States" (country) → merge cross-type
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.extraction.cleanup")

# ─── Demonym/adjective → base entity ──────────────────────────────────────────
# Maps adjective/demonym forms to their canonical country/entity name
DEMONYM_MAP = {
    "iranian": "iran", "iraqi": "iraq", "israeli": "israel",
    "palestinian": "palestine", "syrian": "syria", "lebanese": "lebanon",
    "turkish": "turkey", "egyptian": "egypt", "saudi": "saudi arabia",
    "yemeni": "yemen", "jordanian": "jordan", "kuwaiti": "kuwait",
    "qatari": "qatar", "bahraini": "bahrain", "emirati": "united arab emirates",
    "russian": "russia", "ukrainian": "ukraine", "belarusian": "belarus",
    "polish": "poland", "german": "germany", "french": "france",
    "british": "united kingdom", "italian": "italy", "spanish": "spain",
    "portuguese": "portugal", "dutch": "netherlands", "belgian": "belgium",
    "swiss": "switzerland", "austrian": "austria", "greek": "greece",
    "romanian": "romania", "hungarian": "hungary", "czech": "czech republic",
    "swedish": "sweden", "norwegian": "norway", "danish": "denmark",
    "finnish": "finland", "chinese": "china", "japanese": "japan",
    "korean": "south korea", "north korean": "north korea",
    "indian": "india", "pakistani": "pakistan", "afghan": "afghanistan",
    "thai": "thailand", "vietnamese": "vietnam", "indonesian": "indonesia",
    "filipino": "philippines", "malaysian": "malaysia",
    "australian": "australia", "canadian": "canada", "mexican": "mexico",
    "brazilian": "brazil", "argentinian": "argentina", "colombian": "colombia",
    "peruvian": "peru", "chilean": "chile", "cuban": "cuba",
    "nigerian": "nigeria", "kenyan": "kenya", "south african": "south africa",
    "ethiopian": "ethiopia", "somali": "somalia", "sudanese": "sudan",
    "american": "united states", "taiwanese": "taiwan",
}

# ─── Abbreviation → full name ─────────────────────────────────────────────────
ABBREVIATION_MAP = {
    "us": "united states", "u.s.": "united states", "u.s": "united states",
    "usa": "united states", "u.s.a.": "united states",
    "uk": "united kingdom", "u.k.": "united kingdom",
    "uae": "united arab emirates", "u.a.e.": "united arab emirates",
    "eu": "european union", "e.u.": "european union",
    "un": "united nations", "u.n.": "united nations",
    "nato": "nato",  # NATO is its own canonical form
    "who": "world health organization",
    "iaea": "international atomic energy agency",
    "imf": "international monetary fund",
    "cia": "central intelligence agency",
    "fbi": "federal bureau of investigation",
    "nsa": "national security agency",
    "doj": "department of justice",
    "dod": "department of defense",
    "gop": "republican party",
    "hamas": "hamas",  # canonical
    "hezbollah": "hezbollah",  # canonical
    "isis": "islamic state", "isil": "islamic state", "daesh": "islamic state",
    "pla": "people's liberation army",
    "irgc": "islamic revolutionary guard corps",
    "irs": "internal revenue service",
    "sec": "securities and exchange commission",
    "fed": "federal reserve",
    "ecb": "european central bank",
    "opec": "organization of the petroleum exporting countries",
}

# ─── News sources to exclude from entity extraction ────────────────────────────
NEWS_SOURCES = {
    "reuters", "ap", "ap news", "associated press",
    "bbc", "bbc news", "cnn", "fox news", "msnbc", "nbc",
    "abc news", "cbs", "cbs news", "pbs",
    "the guardian", "guardian", "the times", "financial times",
    "the washington post", "washington post", "the new york times",
    "new york times", "nyt", "wall street journal", "wsj",
    "al jazeera", "france 24", "dw", "deutsche welle",
    "sky news", "euronews", "rt", "sputnik",
    "times of israel", "jerusalem post", "haaretz",
    "south china morning post", "scmp", "nhk",
    "politico", "axios", "the hill", "vox", "vice",
    "the intercept", "huffpost", "daily mail",
    "ars technica", "techcrunch", "the verge", "wired",
    "jns.org", "euronews.com", "better world campaign",
    "arms control association", "nuclear watchdog",
    "bbc monitoring", "scientific american", "factcheck.org",
    "new york post", "xinhua", "ms", "pbs newshour",
}

# Non-entity words that the LLM sometimes extracts as entities
NON_ENTITIES = {
    "chief", "official", "officials", "nuclear official", "nuclear",
    "june", "july", "august", "september", "october", "november", "december",
    "january", "february", "march", "april", "may",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "warns", "risks", "says", "report", "reports", "update",
    "breaking", "exclusive", "analysis", "opinion", "editorial",
    "uranium enrichment", "nuclear power", "nuclear program",
    "ceasefire", "peace talks", "sanctions",
    "president", "minister", "general", "admiral", "senator",
    "ms", "mr", "mrs", "dr",
}


def resolve_canonical_name(name: str) -> tuple[str, str]:
    """Resolve a name to its canonical form. Returns (canonical_name, resolution_type).

    Resolution types: 'demonym', 'abbreviation', 'source_filter', 'passthrough'
    """
    lower = name.lower().strip()

    # Check if it's a news source
    if lower in NEWS_SOURCES:
        return name, "source_filter"

    # Check if it's a non-entity (common word, date, generic term)
    if lower in NON_ENTITIES:
        return name, "source_filter"

    # Check demonym map
    if lower in DEMONYM_MAP:
        canonical = DEMONYM_MAP[lower]
        return canonical.title(), "demonym"

    # Check abbreviation map
    if lower in ABBREVIATION_MAP:
        canonical = ABBREVIATION_MAP[lower]
        # Keep original casing for acronyms
        if name.isupper() and len(name) <= 5:
            return canonical.title(), "abbreviation"
        return canonical.title(), "abbreviation"

    # Check if entity name is too short or generic
    if len(lower) <= 2 and lower not in ABBREVIATION_MAP:
        return name, "source_filter"

    return name, "passthrough"


# Type corrections — entities that are commonly mistyped by LLM
TYPE_CORRECTIONS = {
    "russia": "country", "china": "country", "india": "country",
    "iran": "country", "iraq": "country", "israel": "country",
    "ukraine": "country", "turkey": "country", "pakistan": "country",
    "syria": "country", "egypt": "country", "japan": "country",
    "germany": "country", "france": "country", "brazil": "country",
    "mexico": "country", "canada": "country", "australia": "country",
    "united states": "country", "united kingdom": "country",
    "south korea": "country", "north korea": "country",
    "saudi arabia": "country", "united arab emirates": "country",
    "white house": "facility", "pentagon": "facility",
    "kremlin": "facility", "capitol": "facility",
}


def correct_entity_type(name: str, current_type: str) -> str:
    """Correct mistyped entities (e.g., Russia typed as organization → country)."""
    lower = name.lower().strip()
    if lower in TYPE_CORRECTIONS:
        return TYPE_CORRECTIONS[lower]
    return current_type


def cleanup_topic_entities(topic_id: str, tenant_id: str = "default",
                            dsn: Optional[str] = None) -> dict:
    """Clean up entities in a topic: merge demonyms, expand abbreviations,
    remove news sources, merge cross-type duplicates."""
    stats = {"merged": 0, "removed": 0, "renamed": 0}

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Load all entities for this topic
                cur.execute("""
                    SELECT entity_id, name, entity_type, mention_count, canonical_entity_id, article_ids
                    FROM watch_topic_entities
                    WHERE topic_id = %s AND tenant_id = %s
                    ORDER BY mention_count DESC
                """, (topic_id, tenant_id))
                entities = [{
                    "entity_id": r[0], "name": r[1], "type": r[2],
                    "mentions": r[3], "canonical_id": r[4], "articles": r[5] or [],
                } for r in cur.fetchall()]

                if not entities:
                    return stats

                # Build lookup by canonical name
                canonical_groups: dict[str, list[dict]] = {}
                sources_to_remove: list[str] = []

                for ent in entities:
                    canonical, resolution = resolve_canonical_name(ent["name"])

                    if resolution == "source_filter":
                        sources_to_remove.append(ent["entity_id"])
                        continue

                    key = canonical.lower()
                    if key not in canonical_groups:
                        canonical_groups[key] = []
                    canonical_groups[key].append({
                        **ent,
                        "canonical_name": canonical,
                        "resolution": resolution,
                    })

                # Remove news sources
                for eid in sources_to_remove:
                    cur.execute("DELETE FROM watch_topic_entities WHERE entity_id = %s AND topic_id = %s",
                                (eid, topic_id))
                    stats["removed"] += 1

                # Merge groups with multiple entries (duplicates)
                for key, group in canonical_groups.items():
                    if len(group) <= 1:
                        ent = group[0]
                        needs_update = False
                        new_name = ent["name"]
                        new_type = ent["type"]

                        # Rename if demonym/abbreviation
                        if ent["resolution"] in ("demonym", "abbreviation") and ent["name"] != ent["canonical_name"]:
                            new_name = ent["canonical_name"]
                            needs_update = True

                        # Correct type if needed
                        corrected_type = correct_entity_type(new_name, new_type)
                        if corrected_type != new_type:
                            new_type = corrected_type
                            needs_update = True

                        if needs_update:
                            cur.execute("""
                                UPDATE watch_topic_entities SET name = %s, entity_type = %s, updated_at = NOW()
                                WHERE entity_id = %s AND topic_id = %s
                            """, (new_name, new_type, ent["entity_id"], topic_id))
                            stats["renamed"] += 1
                        continue

                    # Multiple entities map to the same canonical name — merge
                    # Keep the one with most mentions as the winner
                    group.sort(key=lambda e: e["mentions"], reverse=True)
                    winner = group[0]
                    losers = group[1:]

                    # Merge mention counts and article_ids into winner
                    total_mentions = sum(e["mentions"] for e in group)
                    all_articles = []
                    for e in group:
                        for aid in e["articles"]:
                            if aid not in all_articles:
                                all_articles.append(aid)

                    # Determine best type — use type correction table, then prefer country > org
                    best_type = correct_entity_type(winner["canonical_name"], winner["type"])
                    for e in group:
                        corrected = correct_entity_type(e["canonical_name"], e["type"])
                        if corrected == "country":
                            best_type = "country"
                            break
                        if corrected == "person":
                            best_type = "person"
                            break

                    # Update winner
                    cur.execute("""
                        UPDATE watch_topic_entities SET
                            name = %s, entity_type = %s,
                            mention_count = %s, article_ids = %s,
                            updated_at = NOW()
                        WHERE entity_id = %s AND topic_id = %s
                    """, (
                        winner["canonical_name"], best_type,
                        total_mentions, all_articles[:100],
                        winner["entity_id"], topic_id,
                    ))

                    # Delete losers
                    for loser in losers:
                        cur.execute("DELETE FROM watch_topic_entities WHERE entity_id = %s AND topic_id = %s",
                                    (loser["entity_id"], topic_id))
                        stats["merged"] += 1

                    stats["renamed"] += 1

                # Update topic counts
                cur.execute("""
                    UPDATE watch_topics SET
                        entity_count = (SELECT count(*) FROM watch_topic_entities WHERE topic_id = %s),
                        updated_at = NOW()
                    WHERE topic_id = %s
                """, (topic_id, topic_id))

            conn.commit()
    except Exception as e:
        log.warning("Entity cleanup failed for topic %s: %s", topic_id, e)

    log.info("Cleanup for %s: %d merged, %d removed, %d renamed", topic_id, stats["merged"], stats["removed"], stats["renamed"])
    return stats


def validate_entity_on_access(entity_id: str, topic_id: str,
                                tenant_id: str = "default",
                                dsn: Optional[str] = None) -> dict:
    """Validate and refine an entity when it's accessed (clicked/expanded).
    Checks canonical name, type correctness, and triggers merge if duplicate found."""
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT name, entity_type, mention_count, canonical_entity_id
                    FROM watch_topic_entities
                    WHERE entity_id = %s AND topic_id = %s
                """, (entity_id, topic_id))
                row = cur.fetchone()
                if not row:
                    return {"status": "not_found"}

                name, etype, mentions, canonical_id = row
                canonical, resolution = resolve_canonical_name(name)

                updates = {}
                if resolution == "source_filter":
                    # This is a news source — should be removed
                    cur.execute("DELETE FROM watch_topic_entities WHERE entity_id = %s AND topic_id = %s",
                                (entity_id, topic_id))
                    conn.commit()
                    return {"status": "removed", "reason": "news_source"}

                if resolution in ("demonym", "abbreviation") and canonical != name:
                    # Check if the canonical form already exists
                    cur.execute("""
                        SELECT entity_id, mention_count, article_ids
                        FROM watch_topic_entities
                        WHERE topic_id = %s AND LOWER(name) = LOWER(%s) AND entity_id != %s
                        LIMIT 1
                    """, (topic_id, canonical, entity_id))
                    existing = cur.fetchone()

                    if existing:
                        # Merge into existing
                        cur.execute("""
                            UPDATE watch_topic_entities SET
                                mention_count = mention_count + %s,
                                updated_at = NOW()
                            WHERE entity_id = %s AND topic_id = %s
                        """, (mentions, existing[0], topic_id))
                        cur.execute("DELETE FROM watch_topic_entities WHERE entity_id = %s AND topic_id = %s",
                                    (entity_id, topic_id))
                        conn.commit()
                        return {"status": "merged", "into": existing[0], "canonical": canonical}
                    else:
                        # Just rename
                        cur.execute("""
                            UPDATE watch_topic_entities SET name = %s, updated_at = NOW()
                            WHERE entity_id = %s AND topic_id = %s
                        """, (canonical, entity_id, topic_id))
                        conn.commit()
                        return {"status": "renamed", "from": name, "to": canonical}

                return {"status": "valid"}

    except Exception as e:
        log.debug("Entity validation failed: %s", e)
        return {"status": "error", "message": str(e)}
