"""
Product knowledge for the copilot — grounds "how do I…/how does X work" answers
in real, curated help content instead of the model's imagination.

Two pieces:
  1. A small corpus of markdown help docs under core/ai/product_docs/ (core/ is
     mounted into the container, so this is reliable even though docs/ is not).
     Retrieval is a dependency-free BM25-lite scorer over heading-sized chunks.
  2. A static SETUP_CATALOG describing how to enable each key-gated feature —
     what it needs, where to register, and which Settings page to open. Used by
     the `setup_help` action (explain + deep-link; the bot never stores keys).

Kept intentionally simple: the corpus is tiny (a handful of docs), so a linear
BM25 scan is instant and needs no vector store / external dependency.
"""
from __future__ import annotations

import math
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

_DOCS_DIR = Path(__file__).parent / "product_docs"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "of", "to", "and", "or", "is", "are", "in", "on", "for",
    "how", "do", "i", "we", "you", "it", "this", "that", "with", "can", "what",
    "does", "our", "my", "your", "be", "as", "at", "by", "from", "so",
}


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP and len(t) > 1]


def _chunk_markdown(text: str, source: str) -> list[dict]:
    """Split a doc into heading-anchored chunks (title + body)."""
    chunks: list[dict] = []
    title = source
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            chunks.append({"source": source, "title": title, "text": body})

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            buf = []
            title = line.lstrip("#").strip() or source
        else:
            buf.append(line)
    flush()
    return chunks


@lru_cache(maxsize=1)
def _index() -> dict:
    """Build the retrieval index once. Returns {chunks, df, N, avgdl}."""
    chunks: list[dict] = []
    if _DOCS_DIR.is_dir():
        for p in sorted(_DOCS_DIR.glob("*.md")):
            try:
                chunks.extend(_chunk_markdown(p.read_text(encoding="utf-8"), p.stem))
            except Exception:
                continue

    # Precompute token stats for BM25.
    df: dict[str, int] = {}
    lengths = []
    for c in chunks:
        toks = _tokens(c["title"] + " " + c["text"])
        c["_toks"] = toks
        c["_tf"] = {}
        for t in toks:
            c["_tf"][t] = c["_tf"].get(t, 0) + 1
        lengths.append(len(toks) or 1)
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0
    return {"chunks": chunks, "df": df, "N": len(chunks), "avgdl": avgdl}


def search_product_docs(query: str, k: int = 4) -> list[dict]:
    """Return the top-k help chunks for a query (BM25-lite). Each: {source,title,text}."""
    idx = _index()
    chunks = idx["chunks"]
    if not chunks:
        return []
    q = _tokens(query)
    if not q:
        return []
    N, avgdl, df = idx["N"], idx["avgdl"], idx["df"]
    k1, b = 1.5, 0.75
    scored = []
    for c in chunks:
        dl = len(c["_toks"]) or 1
        score = 0.0
        for t in q:
            f = c["_tf"].get(t, 0)
            if not f:
                continue
            idf = math.log(1 + (N - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"source": c["source"], "title": c["title"], "text": c["text"]}
            for _, c in scored[:k]]


# ── Setup catalog: how to enable each key-gated feature ──────────────────────
# Static + self-contained (the data_sources table isn't present on every node).
# `settings` is a UI deep-link path. The copilot EXPLAINS + links; it never
# stores keys itself.

SETUP_CATALOG: dict[str, dict] = {
    "satellite": {
        "feature": "Satellite imagery (ESA / Copernicus Sentinel-2)",
        "needs": "A Sentinel Hub WMS instance ID (free).",
        "register": "https://shapps.dataspace.copernicus.eu/dashboard/",
        "steps": [
            "Register free at dataspace.copernicus.eu.",
            "Open the Sentinel Hub dashboard → Configuration Utility → create a config from the Full WMS template.",
            "Copy the config's Instance ID.",
            "Paste it in Settings → Satellite Imagery and click Test.",
        ],
        "settings": "/settings/satellite",
        "keywords": "satellite sentinel copernicus esa imagery ndvi overlay map",
    },
    "data_gov": {
        "feature": "Investigation sources on data.gov (FEC, Congress.gov, FDA)",
        "needs": "A free DATA_GOV_API_KEY (raises rate limits from ~30/hr to 1000/hr).",
        "register": "https://api.data.gov/signup/",
        "steps": [
            "Sign up free at api.data.gov/signup.",
            "Copy the API key from the confirmation email.",
            "Add it as the DATA_GOV_API_KEY environment secret on the node.",
        ],
        "settings": "/settings/data-sources",
        "keywords": "fec campaign finance congress fda investigation data.gov key rate limit",
    },
    "sam_gov": {
        "feature": "SAM.gov (federal contractor registry) investigation source",
        "needs": "A free SAM_GOV_API_KEY (raises limits from ~10/day to 1000/day).",
        "register": "https://sam.gov/content/api",
        "steps": [
            "Request an API key at sam.gov/content/api.",
            "Add it as the SAM_GOV_API_KEY environment secret on the node.",
        ],
        "settings": "/settings/data-sources",
        "keywords": "sam.gov contractor uei cage federal registry investigation key",
    },
    "ai": {
        "feature": "AI provider for chat / enrichment",
        "needs": "Either local Ollama (no key) or a cloud key (OpenAI/Anthropic/etc.).",
        "register": "https://platform.openai.com/api-keys",
        "steps": [
            "Local & private: run Ollama and pick it in Settings → AI (no key).",
            "Or choose a cloud provider in Settings → AI and paste its API key.",
            "Click Save & Test Connection.",
        ],
        "settings": "/settings?tab=ai",
        "keywords": "ai llm openai anthropic gemini ollama model chat api key provider",
    },
    "opencorporates": {
        "feature": "OpenCorporates (global company registry) investigation source",
        "needs": "An optional OPENCORPORATES_API_TOKEN (raises rate limits).",
        "register": "https://opencorporates.com/api_accounts/new",
        "steps": [
            "Create an API account at opencorporates.com/api_accounts/new.",
            "Add the token as OPENCORPORATES_API_TOKEN on the node.",
        ],
        "settings": "/settings/data-sources",
        "keywords": "opencorporates company registry global investigation key",
    },
}


def get_setup_help(feature: str) -> Optional[dict]:
    """Match a free-text feature/source name to a setup catalog entry."""
    q = (feature or "").lower().strip()
    if not q:
        return None
    if q in SETUP_CATALOG:
        return SETUP_CATALOG[q]
    # keyword match
    best, best_score = None, 0
    qtoks = set(_tokens(q))
    for key, entry in SETUP_CATALOG.items():
        hay = set(_tokens(entry["feature"] + " " + entry.get("keywords", "") + " " + key))
        score = len(qtoks & hay)
        if score > best_score:
            best, best_score = entry, score
    return best if best_score > 0 else None


def list_setup_catalog() -> list[dict]:
    return [{"key": k, "feature": v["feature"], "needs": v["needs"]} for k, v in SETUP_CATALOG.items()]
