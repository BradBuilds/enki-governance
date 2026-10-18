"""
Feed Graph — hierarchical news intelligence graph.

GET /feed/graph — Returns a 3-level hierarchy:
    Level 0: Categories (News, Security, Environment, Finance)
    Level 1: Topic clusters within a category
    Level 2: Individual articles within a cluster

Only signals with real headlines are included. Position reports,
sensor readings, and raw telemetry are filtered out.
"""

from __future__ import annotations

import re
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.feed_graph")
router = APIRouter(prefix="/feed", tags=["feed"])

# Signal types to EXCLUDE (noise, not articles)
_NOISE_TYPES = {
    "signal.geospatial.aircraft_position",
    "signal.geospatial.vessel_position",
    "signal.geospatial.position_report",
    "signal.device.sensor_reading",
    "signal.infrastructure.sensor_reading",
    "signal.financial.balance_update",
    "signal.financial.transaction",
}

# Content-based category detection — keywords that trigger each category
_CATEGORY_KEYWORDS: dict[str, set[str]] = {
    "World News": {
        "war", "election", "president", "nato", "treaty", "sanctions", "diplomacy",
        "united nations", "geopolitics", "conflict", "ceasefire", "peace talks",
        "parliament", "prime minister", "coup", "protest", "refugee", "asylum",
        "embassy", "foreign policy", "summit", "bilateral", "humanitarian",
        "ukraine", "russia", "china", "iran", "israel", "gaza", "palestine",
        "korea", "taiwan", "syria", "afghanistan", "africa", "europe",
        "middle east", "latin america", "terrorism", "insurgency",
    },
    "Cybersecurity": {
        "vulnerability", "breach", "hack", "malware", "ransomware", "exploit",
        "cve", "phishing", "zero-day", "zeroday", "backdoor", "botnet",
        "threat", "cybersecurity", "infosec", "netsec", "cisa", "apt",
        "data leak", "credential", "encryption", "firewall", "intrusion",
        "patch", "security advisory", "incident response",
    },
    "Tech": {
        "software", "startup", "developer", "code", "open source", "github",
        "programming", "api", "framework", "database", "cloud", "saas",
        "machine learning", "deep learning", "neural", "gpu", "chip",
        "semiconductor", "processor", "linux", "rust", "python", "javascript",
        "browser", "app", "platform", "release", "update", "version",
        "silicon valley", "venture", "ycombinator", "tech company",
    },
    "AI & ML": {
        "artificial intelligence", "llm", "gpt", "claude", "chatgpt", "gemini",
        "openai", "anthropic", "language model", "transformer", "diffusion",
        "generative", "copilot", "prompt", "fine-tune", "training",
        "ai safety", "alignment", "superintelligence", "agi",
    },
    "Finance": {
        "stock", "market", "economy", "inflation", "gdp", "earnings",
        "cryptocurrency", "crypto", "bitcoin", "ethereum", "blockchain",
        "fed", "federal reserve", "interest rate", "recession", "banking",
        "investment", "hedge fund", "ipo", "trading", "wall street",
        "tariff", "trade war", "debt", "budget", "fiscal",
    },
    "Military & Defense": {
        "military", "troops", "missile", "defense", "pentagon", "weapons",
        "army", "navy", "air force", "marines", "nuclear", "drone",
        "strike", "deployment", "artillery", "combat", "warfare",
        "intelligence", "espionage", "surveillance", "radar",
    },
    "Science & Space": {
        "research", "study", "nasa", "space", "discovery", "physics",
        "biology", "chemistry", "genome", "quantum", "telescope",
        "asteroid", "mars", "moon", "satellite", "launch", "orbit",
        "climate science", "evolution", "experiment", "peer review",
    },
    "Environment": {
        "wildfire", "earthquake", "hurricane", "flood", "tornado",
        "climate change", "emissions", "carbon", "pollution", "renewable",
        "solar", "wind energy", "drought", "sea level", "deforestation",
        "endangered", "ecosystem", "conservation", "epa",
    },
    "Health": {
        "health", "disease", "fda", "vaccine", "hospital", "pandemic",
        "medical", "cancer", "treatment", "drug", "clinical trial",
        "mental health", "obesity", "virus", "outbreak", "who",
        "pharmaceutical", "biotech", "surgery", "diagnosis",
    },
    "Automotive": {
        "car", "vehicle", "automotive", "self-driving", "autonomous",
        "tesla", "electric vehicle", "charging", "hybrid", "sedan",
        "truck", "suv", "recall", "crash test", "highway",
        "ford", "gm", "toyota", "volkswagen", "bmw", "rivian",
    },
}

_CATEGORY_COLORS: dict[str, str] = {
    "World News": "#f97316",       # orange
    "Cybersecurity": "#ef4444",    # red
    "Tech": "#60a5fa",             # blue
    "AI & ML": "#a78bfa",          # purple
    "Finance": "#fbbf24",          # amber
    "Military & Defense": "#6b7280",  # gray
    "Science & Space": "#22d3ee",  # cyan
    "Environment": "#34d399",      # green
    "Health": "#ec4899",           # pink
    "Automotive": "#f59e0b",       # yellow
    "General": "#94a3b8",          # slate
}


def _categorize_article(headline: str, summary: str) -> str:
    """Categorize an article by scanning headline + summary for keywords."""
    text = f"{headline} {summary}".lower()

    scores: dict[str, int] = {}
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in text:
                # Multi-word keywords score higher
                score += 2 if " " in kw else 1
        if score > 0:
            scores[cat] = score

    if not scores:
        return "General"

    # Return highest-scoring category
    return max(scores, key=scores.get)  # type: ignore

# Stopwords
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
    "says", "per", "around", "without", "across", "against", "along",
    "among", "behind", "beside", "beyond", "down", "inside", "near",
    "onto", "outside", "past", "since", "toward", "upon", "within",
    "story", "article", "report", "update", "source", "news",
}


@router.get("/graph")
async def get_feed_graph(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(300, le=500),
    after: Optional[str] = None,
):
    """Hierarchical feed graph: categories → topic clusters → articles."""
    from storage.postgres.db import get_connection

    log.warning(f"feed_graph called: tenant={auth.tenant_id}, limit={limit}, after={after}")

    now = datetime.now(timezone.utc)
    after_dt = datetime.fromisoformat(after) if after else now - timedelta(days=7)

    # Fetch only article-like signals (filter out noise)
    cards = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                noise_list = list(_NOISE_TYPES)
                placeholders = ",".join(["%s"] * len(noise_list))
                # Articles ingested via a watch-topic connector stamp
                # metadata._topic_id. The Intelligence/Network graph is a
                # DISCOVERY view — articles already belonging to a watched
                # topic live under /watch/{id}, not here.
                cur.execute(f"""
                    SELECT signal_id, signal_type, severity, device_id,
                           timestamp, payload, latitude, longitude
                    FROM signals
                    WHERE tenant_id = %s AND timestamp >= %s
                      AND signal_type NOT IN ({placeholders})
                      AND (metadata->>'_topic_id' IS NULL OR metadata->>'_topic_id' = '')
                    ORDER BY timestamp DESC
                    LIMIT %s
                """, (auth.tenant_id, after_dt, *noise_list, limit))

                rows = cur.fetchall()
                log.warning(f"feed_graph query returned {len(rows)} rows")

                for r in rows:
                    sig_type = r[1]

                    # Skip noise signals
                    if sig_type in _NOISE_TYPES:
                        continue

                    payload = r[5] or {}
                    headline = (
                        payload.get("title")
                        or payload.get("headline")
                        or payload.get("name")
                        or ""
                    )
                    # Skip signals without real headlines
                    if not headline or len(headline) < 15:
                        continue
                    # Skip "Story #12345" placeholder headlines
                    if headline.startswith("Story #"):
                        continue

                    summary = (
                        payload.get("summary")
                        or payload.get("og_description")
                        or payload.get("description")
                        or ""
                    )[:300]

                    domain = sig_type.split(".")[1] if "." in sig_type else "unknown"
                    category = _categorize_article(headline, summary)

                    cards.append({
                        "signal_id": r[0],
                        "signal_type": sig_type,
                        "severity": r[2],
                        "device_id": r[3],
                        "timestamp": r[4].isoformat() if r[4] else None,
                        "headline": headline,
                        "summary": summary,
                        "source": payload.get("source", payload.get("feed_title", "")),
                        "source_url": payload.get("link") or payload.get("url") or "",
                        "thumbnail_url": payload.get("thumbnail_url") or payload.get("socialimage") or "",
                        "domain": domain,
                        "category": category,
                        "keywords": _extract_keywords(headline, summary),
                        "author": payload.get("author", ""),
                        "article_preview": {
                            "full_text": payload.get("full_text", ""),
                            "first_paragraph": payload.get("first_paragraph", ""),
                            "og_description": payload.get("og_description", ""),
                            "word_count": payload.get("word_count", 0),
                            "author": payload.get("author", ""),
                        },
                        "metrics": {
                            "points": payload.get("points"),
                            "comments": payload.get("num_comments"),
                            "score": payload.get("score") or (payload.get("data", {}).get("score") if isinstance(payload.get("data"), dict) else None),
                        },
                        "badges": [],
                    })
    except Exception as e:
        log.error(f"Feed graph query failed: {e}", exc_info=True)
        return {"categories": [], "error": str(e)}

    log.warning(f"feed_graph: {len(cards)} cards passed filter")

    # Build hierarchy: categories → topic clusters → articles

    # Group by category
    by_category: dict[str, list[dict]] = defaultdict(list)
    for card in cards:
        by_category[card["category"]].append(card)

    categories = []
    all_topics = []

    for cat_name, cat_cards in by_category.items():
        # Cluster articles within this category
        clusters = _cluster_articles(cat_cards, min_overlap=2)

        topics = []
        for i, cluster in enumerate(clusters):
            articles = cluster["articles"]
            top_kw = cluster["keywords"][:5]

            label = " / ".join(w.title() for w in top_kw[:3]) if top_kw else articles[0]["headline"][:40]
            sources = list(set(a["source"] for a in articles if a["source"]))

            # Detect conflicts within this cluster
            conflict = _detect_conflicts(articles) if len(articles) >= 2 else None

            # Engagement: sum points/comments/scores from articles
            total_engagement = 0
            for a in articles:
                m = a.get("metrics") or {}
                total_engagement += (m.get("points") or 0) + (m.get("score") or 0) + (m.get("comments") or 0)

            # Sort articles within cluster by engagement
            articles.sort(key=lambda a: (
                (a.get("metrics") or {}).get("points") or 0) + (
                (a.get("metrics") or {}).get("score") or 0), reverse=True)

            topic = {
                "id": f"topic_{cat_name}_{i}",
                "label": label,
                "headline": articles[0]["headline"],
                "article_count": len(articles),
                "source_count": len(sources),
                "category": cat_name,
                "domain": articles[0]["domain"],
                "severity": "high" if any(a["severity"] in ("critical", "high") for a in articles) else "informational",
                "keywords": top_kw,
                "sources": sources[:5],
                "timestamp": articles[0]["timestamp"],
                "thumbnail_url": next((a["thumbnail_url"] for a in articles if a["thumbnail_url"]), ""),
                "articles": articles,
                "conflict": conflict,
                "engagement": total_engagement,
            }
            topics.append(topic)
            all_topics.append(topic)

        # Sort topics: most sources first, then by engagement, then recency
        topics.sort(key=lambda t: (-t["source_count"], -t["engagement"], t.get("timestamp") or ""), reverse=False)

        conflict_count = sum(1 for t in topics if t.get("conflict"))

        categories.append({
            "id": f"cat_{cat_name}",
            "name": cat_name,
            "color": _CATEGORY_COLORS.get(cat_name, "#94a3b8"),
            "article_count": len(cat_cards),
            "topic_count": len(topics),
            "conflict_count": conflict_count,
            "topics": topics,
            "has_critical": any(c["severity"] in ("critical", "high") for c in cat_cards),
        })

    # Sort categories: most articles first
    categories.sort(key=lambda c: -c["article_count"])

    # Build topic-level edges (shared keywords between topics across categories)
    topic_edges = []
    for i, t1 in enumerate(all_topics):
        kw1 = set(t1["keywords"])
        for j, t2 in enumerate(all_topics):
            if j <= i:
                continue
            if t1["category"] == t2["category"]:
                continue  # Only cross-category edges
            shared = kw1 & set(t2["keywords"])
            if shared:
                topic_edges.append({
                    "source": t1["id"],
                    "target": t2["id"],
                    "shared_keywords": list(shared)[:3],
                    "strength": min(len(shared) / 3, 1.0),
                })

    return {
        "categories": categories,
        "topic_edges": topic_edges,
        "total_articles": len(cards),
        "total_topics": len(all_topics),
    }


def _extract_keywords(headline: str, summary: str) -> list[str]:
    """Extract significant keywords from headline + summary."""
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


def _cluster_articles(cards: list[dict], min_overlap: int = 2) -> list[dict]:
    """Cluster articles by shared keywords using greedy merge."""
    if not cards:
        return []

    kw_index: dict[str, list[int]] = defaultdict(list)
    for i, card in enumerate(cards):
        for kw in card["keywords"]:
            kw_index[kw].append(i)

    assigned = set()
    clusters = []
    order = sorted(range(len(cards)), key=lambda i: len(cards[i]["keywords"]), reverse=True)

    for idx in order:
        if idx in assigned:
            continue

        card = cards[idx]
        kw_set = set(card["keywords"])

        candidates = Counter()
        for kw in kw_set:
            for other_idx in kw_index[kw]:
                if other_idx != idx and other_idx not in assigned:
                    candidates[other_idx] += 1

        cluster_indices = [idx]
        cluster_keywords = Counter(card["keywords"])
        assigned.add(idx)

        for other_idx, overlap in candidates.most_common():
            if overlap >= min_overlap and other_idx not in assigned:
                cluster_indices.append(other_idx)
                assigned.add(other_idx)
                for kw in cards[other_idx]["keywords"]:
                    cluster_keywords[kw] += 1

        clusters.append({
            "articles": [cards[i] for i in cluster_indices],
            "keywords": [kw for kw, _ in cluster_keywords.most_common(10)],
        })

    clusters.sort(key=lambda c: (-len(c["articles"]), c["articles"][0].get("timestamp", "")))
    return clusters


# ─── Conflict Detection ──────────────────────────────────────────────────

# Opposing word pairs
_OPPOSING_PAIRS = [
    ("confirms", "denies"), ("confirmed", "denied"), ("confirms", "denied"),
    ("rises", "falls"), ("rising", "falling"), ("rose", "fell"),
    ("increase", "decrease"), ("increases", "decreases"), ("increased", "decreased"),
    ("gains", "loses"), ("gained", "lost"), ("gain", "loss"),
    ("safe", "dangerous"), ("safety", "danger"), ("secure", "vulnerable"),
    ("peace", "war"), ("peaceful", "violent"), ("ceasefire", "attack"),
    ("success", "failure"), ("succeeded", "failed"),
    ("approves", "rejects"), ("approved", "rejected"), ("approval", "rejection"),
    ("supports", "opposes"), ("supported", "opposed"), ("support", "opposition"),
    ("advances", "retreats"), ("advance", "retreat"), ("advancing", "retreating"),
    ("wins", "loses"), ("won", "lost"), ("victory", "defeat"),
    ("grows", "shrinks"), ("growth", "decline"), ("growing", "shrinking"),
    ("agrees", "disagrees"), ("agreement", "disagreement"),
    ("accepts", "rejects"), ("accepted", "rejected"),
    ("launches", "cancels"), ("launched", "cancelled"),
    ("upgrades", "downgrades"), ("upgraded", "downgraded"),
    ("innocent", "guilty"), ("acquitted", "convicted"),
    ("alive", "dead"), ("survived", "killed"),
    ("true", "false"), ("fact", "misinformation"),
    ("stable", "unstable"), ("stabilized", "destabilized"),
]

# Hedging / uncertainty words
_HEDGING_WORDS = {
    "allegedly", "reportedly", "unconfirmed", "unverified", "disputed",
    "claims", "claimed", "rumored", "rumoured", "sources say",
    "according to", "may have", "might have", "could be",
    "possible", "possibly", "uncertain", "unclear", "questions",
}

_CERTAINTY_WORDS = {
    "confirmed", "verified", "official", "officially", "proven",
    "established", "definitive", "conclusive", "certainly",
}


def _detect_conflicts(articles: list[dict]) -> dict | None:
    """Detect contradictions between articles in a cluster.

    Returns conflict info dict or None if no conflict found.
    """
    if len(articles) < 2:
        return None

    texts = []
    for a in articles:
        t = f"{a.get('headline', '')} {a.get('summary', '')}".lower()
        texts.append(t)

    conflicts = []

    # Check each pair of articles
    for i in range(len(articles)):
        for j in range(i + 1, len(articles)):
            t1, t2 = texts[i], texts[j]
            a1, a2 = articles[i], articles[j]

            # Check opposing word pairs
            for w1, w2 in _OPPOSING_PAIRS:
                if (w1 in t1 and w2 in t2) or (w2 in t1 and w1 in t2):
                    conflicts.append({
                        "type": "contradiction",
                        "trigger": f"{w1} vs {w2}",
                        "article_a": {
                            "signal_id": a1.get("signal_id"),
                            "headline": a1.get("headline"),
                            "source": a1.get("source"),
                        },
                        "article_b": {
                            "signal_id": a2.get("signal_id"),
                            "headline": a2.get("headline"),
                            "source": a2.get("source"),
                        },
                    })
                    break  # One conflict per pair is enough

            # Check certainty vs hedging
            t1_hedging = any(h in t1 for h in _HEDGING_WORDS)
            t1_certain = any(c in t1 for c in _CERTAINTY_WORDS)
            t2_hedging = any(h in t2 for h in _HEDGING_WORDS)
            t2_certain = any(c in t2 for c in _CERTAINTY_WORDS)

            if (t1_certain and t2_hedging) or (t2_certain and t1_hedging):
                conflicts.append({
                    "type": "credibility_gap",
                    "trigger": "confirmed vs unverified",
                    "article_a": {
                        "signal_id": a1.get("signal_id"),
                        "headline": a1.get("headline"),
                        "source": a1.get("source"),
                        "stance": "confirmed" if t1_certain else "hedging",
                    },
                    "article_b": {
                        "signal_id": a2.get("signal_id"),
                        "headline": a2.get("headline"),
                        "source": a2.get("source"),
                        "stance": "confirmed" if t2_certain else "hedging",
                    },
                })

    if not conflicts:
        return None

    return {
        "count": len(conflicts),
        "conflicts": conflicts[:5],  # Cap at 5
        "severity": "high" if any(c["type"] == "contradiction" for c in conflicts) else "medium",
    }
