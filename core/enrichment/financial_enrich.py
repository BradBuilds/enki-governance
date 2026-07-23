"""
Financial Signal Enrichment — AI-powered categorization + geocoding.

Runs post-ingestion on signal.financial.transaction signals:
  1. Merchant categorization via Ollama (description → category + MCC code)
  2. Merchant geocoding via Nominatim (merchant name → lat/lon)
  3. Spending pattern tags (high_value, recurring, new_merchant)

Called from the ingestion pipeline after LDU-W storage.
Results are written back to the signal's metadata field.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

logger = logging.getLogger("enki.enrichment.financial")


# ─── MCC Category Taxonomy ───────────────────────────────────────────────────
# Standard Merchant Category Code groups for LLM prompt context.

MCC_CATEGORIES = {
    "groceries": {"label": "Groceries", "mcc": "5411", "keywords": ["grocery", "market", "food", "kroger", "walmart", "aldi", "trader joe", "whole foods", "safeway", "publix"]},
    "fuel": {"label": "Fuel & Gas", "mcc": "5541", "keywords": ["shell", "chevron", "bp", "exxon", "mobil", "gas", "fuel", "sunoco", "marathon", "speedway", "wawa", "circle k"]},
    "restaurants": {"label": "Restaurants & Dining", "mcc": "5812", "keywords": ["restaurant", "cafe", "pizza", "burger", "grill", "diner", "mcdonald", "starbucks", "chipotle", "subway", "taco", "wendy"]},
    "subscriptions": {"label": "Subscriptions & Digital", "mcc": "5968", "keywords": ["netflix", "spotify", "hulu", "disney", "apple.com", "google", "amazon prime", "youtube", "adobe", "microsoft"]},
    "utilities": {"label": "Utilities", "mcc": "4900", "keywords": ["electric", "water", "gas co", "utility", "power", "energy", "comcast", "verizon", "at&t", "t-mobile", "internet"]},
    "insurance": {"label": "Insurance", "mcc": "6300", "keywords": ["insurance", "geico", "state farm", "allstate", "progressive", "usaa", "liberty mutual"]},
    "medical": {"label": "Medical & Health", "mcc": "8099", "keywords": ["pharmacy", "cvs", "walgreens", "hospital", "medical", "dental", "doctor", "clinic", "health", "urgent care"]},
    "home_improvement": {"label": "Home Improvement", "mcc": "5200", "keywords": ["home depot", "lowes", "lowe's", "ace hardware", "menards"]},
    "electronics": {"label": "Electronics & Tech", "mcc": "5732", "keywords": ["best buy", "apple store", "newegg", "micro center", "b&h photo"]},
    "clothing": {"label": "Clothing & Apparel", "mcc": "5651", "keywords": ["nike", "target", "old navy", "gap", "h&m", "zara", "nordstrom", "tjmaxx", "marshalls", "ross"]},
    "automotive": {"label": "Automotive", "mcc": "5511", "keywords": ["autozone", "o'reilly", "advance auto", "jiffy lube", "firestone", "goodyear", "car wash"]},
    "travel": {"label": "Travel & Lodging", "mcc": "7011", "keywords": ["hotel", "airbnb", "marriott", "hilton", "airline", "delta", "united", "southwest", "american air", "uber", "lyft"]},
    "entertainment": {"label": "Entertainment", "mcc": "7832", "keywords": ["cinema", "theater", "amc", "regal", "ticketmaster", "bowling", "golf", "gym", "fitness", "planet fitness"]},
    "education": {"label": "Education", "mcc": "8220", "keywords": ["university", "college", "school", "tuition", "coursera", "udemy"]},
    "government": {"label": "Government & Taxes", "mcc": "9211", "keywords": ["irs", "tax", "dmv", "court", "government", "state of", "city of", "county"]},
    "transfers": {"label": "Transfers & Payments", "mcc": "4829", "keywords": ["transfer", "venmo", "zelle", "paypal", "cash app", "wire", "ach", "direct deposit"]},
    "atm": {"label": "ATM & Cash", "mcc": "6011", "keywords": ["atm", "cash", "withdrawal", "withdraw"]},
    "shopping": {"label": "General Shopping", "mcc": "5999", "keywords": ["amazon", "ebay", "etsy", "costco", "sam's club", "dollar"]},
}


def _categorize_by_keywords(description: str) -> Optional[dict]:
    """Fast keyword-based categorization — no LLM needed."""
    desc_lower = description.lower()
    for cat_id, cat in MCC_CATEGORIES.items():
        for kw in cat["keywords"]:
            if kw in desc_lower:
                return {
                    "category": cat["label"],
                    "category_id": cat_id,
                    "mcc_code": cat["mcc"],
                    "confidence": 0.85,
                    "method": "keyword",
                }
    return None


def _categorize_by_llm(description: str, amount: float = None) -> Optional[dict]:
    """AI-powered categorization via Ollama for merchants that don't match keywords."""
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
    model = os.environ.get("OLLAMA_MODEL", "gemma4:e2b")

    category_list = ", ".join(
        f"{cat['label']} (MCC {cat['mcc']})"
        for cat in MCC_CATEGORIES.values()
    )

    prompt = f"""Classify this bank transaction into exactly one category.

Transaction: {description}
{"Amount: $" + f"{abs(amount):.2f}" if amount else ""}

Categories: {category_list}

Respond with ONLY a JSON object, no other text:
{{"category": "category name", "mcc_code": "4-digit code", "confidence": 0.0-1.0}}"""

    try:
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_predict": 100, "temperature": 0.1},
        }).encode("utf-8")

        req = Request(
            f"{base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        content = data.get("message", {}).get("content", "")

        # Extract JSON from response
        json_match = re.search(r'\{[^}]+\}', content)
        if json_match:
            result = json.loads(json_match.group())
            # Map back to category_id
            category_name = result.get("category", "")
            cat_id = None
            for cid, cat in MCC_CATEGORIES.items():
                if cat["label"].lower() == category_name.lower():
                    cat_id = cid
                    break
            return {
                "category": result.get("category", "Other"),
                "category_id": cat_id or "other",
                "mcc_code": result.get("mcc_code", "5999"),
                "confidence": float(result.get("confidence", 0.7)),
                "method": "llm",
            }
    except Exception as e:
        logger.debug(f"LLM categorization failed for '{description}': {e}")

    return None


def _geocode_merchant(description: str) -> Optional[dict]:
    """Geocode a merchant name via Nominatim (best-effort)."""
    # Clean up common merchant name patterns for better geocoding
    clean = re.sub(r'\d{3,}', '', description)  # Remove long numbers
    clean = re.sub(r'#\d+', '', clean)           # Remove store numbers
    clean = clean.strip().strip('*').strip()

    if len(clean) < 3:
        return None

    try:
        encoded = clean.replace(' ', '+')
        url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1&countrycodes=us"
        req = Request(url, headers={"User-Agent": "EnkiCommandCenter/1.0"})

        with urlopen(req, timeout=5) as resp:
            results = json.loads(resp.read())

        if results and len(results) > 0:
            r = results[0]
            return {
                "latitude": float(r["lat"]),
                "longitude": float(r["lon"]),
                "display_name": r.get("display_name", ""),
                "geocode_confidence": 0.6,  # Nominatim on merchant names is approximate
            }
    except Exception as e:
        logger.debug(f"Geocoding failed for '{description}': {e}")

    return None


def enrich_financial_signal(signal_dict: dict, dsn: Optional[str] = None) -> dict:
    """
    Enrich a financial transaction signal with category + geocoding.

    Called post-ingestion. Returns enrichment dict to merge into signal metadata.

    Args:
        signal_dict: Full signal dict (from Signal.model_dump())
        dsn: Database connection string

    Returns:
        Enrichment dict with category, mcc_code, geocoded location
    """
    payload = signal_dict.get("payload", {})
    description = payload.get("description", "") or payload.get("merchant", "") or payload.get("memo", "")
    amount = payload.get("amount")

    enrichment = {}

    # ─── Categorize merchant ─────────────────────────────────────────────
    if description:
        # Try fast keyword match first
        cat_result = _categorize_by_keywords(description)

        # Fall back to LLM if keywords didn't match
        if not cat_result:
            cat_result = _categorize_by_llm(description, amount)

        if cat_result:
            enrichment["merchant_category"] = cat_result["category"]
            enrichment["merchant_category_id"] = cat_result["category_id"]
            enrichment["merchant_mcc_code"] = cat_result["mcc_code"]
            enrichment["category_confidence"] = cat_result["confidence"]
            enrichment["category_method"] = cat_result["method"]

    # ─── Geocode merchant (best-effort, skip for transfers/ATM) ──────────
    skip_geo = {"transfers", "atm", "subscriptions"}
    cat_id = enrichment.get("merchant_category_id", "")
    if description and cat_id not in skip_geo:
        geo_result = _geocode_merchant(description)
        if geo_result:
            enrichment["merchant_lat"] = geo_result["latitude"]
            enrichment["merchant_lon"] = geo_result["longitude"]
            enrichment["merchant_address"] = geo_result["display_name"]
            enrichment["geocode_confidence"] = geo_result["geocode_confidence"]

    # ─── Spending tags ───────────────────────────────────────────────────
    tags = []
    if amount is not None:
        abs_amount = abs(amount) if isinstance(amount, (int, float)) else 0
        if abs_amount >= 10000:
            tags.append("very_high_value")
        elif abs_amount >= 1000:
            tags.append("high_value")
        elif abs_amount >= 500:
            tags.append("elevated")
    if enrichment.get("merchant_category"):
        tags.append(enrichment["merchant_category_id"])
    enrichment["financial_tags"] = tags

    return enrichment


def enrich_and_update_signal(signal_rid: int, signal_dict: dict, dsn: Optional[str] = None) -> None:
    """Legacy entrypoint — signals are append-only, use enrich_payload_before_insert instead."""
    pass


def enrich_payload_before_insert(payload: dict, signal_type: str) -> tuple[dict, dict]:
    """
    Enrich a financial payload BEFORE signal insertion (signals are append-only).

    Called from the ingestion pipeline before insert_signal().

    Args:
        payload: The parsed payload dict
        signal_type: Signal type string

    Returns:
        (enriched_payload, enrichment_metadata) — payload with category added,
        and enrichment data for metadata field
    """
    if not signal_type or not signal_type.startswith("signal.financial."):
        return payload, {}

    enrichment = enrich_financial_signal({"payload": payload}, None)
    if not enrichment:
        return payload, {}

    # Merge category into payload so it's queryable
    enriched = {**payload}
    if enrichment.get("merchant_category"):
        enriched["category"] = enrichment["merchant_category"]
        enriched["category_id"] = enrichment.get("merchant_category_id", "")

    return enriched, {"financial_enrichment": enrichment}
