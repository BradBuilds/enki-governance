"""
Address extractor + geocoder.

Two halves:

  1. Regex pass (cheap) — finds US/UK/CA-style addresses in text via
     a couple of battle-tested patterns. Catches "1600 Pennsylvania
     Avenue, Washington, DC" and "10 Downing Street, London".

  2. LLM pass (deep) — run when addresses are likely but the regex
     missed them (heuristic: article mentions a facility entity with
     no address). The LLM returns structured {line1, city, country}
     tuples.

Resolver upserts into the `addresses` table, linking to whichever
entity(ies) the address belongs to (owner signals: possessive
"Apple's headquarters at ..."; appositive "Kremlin, located at ...";
near-mention within 100 chars). Also creates a place node for the
address itself so maps can render it.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from core.ldu.lineage import lineage_columns

log = logging.getLogger("enki.extraction.addresses")

# US-style: "1600 Pennsylvania Avenue, Washington, DC 20500"
# UK-style: "10 Downing Street, London SW1A 2AA"
# Generic:  "<number> <word>+ (St|Rd|Ave|Blvd|Dr|Way|Ln|Hwy|Pl|Ct|Ter|Pkwy|Sq)"
_ADDRESS_PATTERNS = [
    re.compile(
        r"\b(\d+[\s,]+[A-Z][\w\s\.']{2,60}?"
        r"(?:St(?:reet)?|Rd|Road|Ave(?:nue)?|Blvd|Boulevard|Dr(?:ive)?|"
        r"Way|Lane|Ln|H(?:igh)?way|Hwy|Pl(?:ace)?|Court|Ct|Terrace|Ter|"
        r"Pkwy|Parkway|Square|Sq|Circle|Cir|Close|Cl)\.?)"
        r"(?:[\s,]+(?P<city>[A-Z][A-Za-z\s\-]{2,40}?))?"
        r"(?:[\s,]+(?P<region>[A-Z]{2,3}|[A-Z][a-z]+))?"
        r"(?:\s+(?P<postal>[A-Z0-9][A-Z0-9\s\-]{3,9}))?",
        re.UNICODE,
    ),
]


def extract_addresses_cheap(body: str) -> list[dict]:
    if not body or len(body) < 30:
        return []
    found: list[dict] = []
    seen: set[str] = set()
    for pat in _ADDRESS_PATTERNS:
        for m in pat.finditer(body):
            raw = (m.group(1) or "").strip()
            if not raw or len(raw) < 10:
                continue
            if raw.lower() in seen:
                continue
            seen.add(raw.lower())
            found.append({
                "line1": raw,
                "city": (m.groupdict().get("city") or "").strip() or None,
                "admin_region": (m.groupdict().get("region") or "").strip() or None,
                "postal_code": (m.groupdict().get("postal") or "").strip() or None,
                "country": None,
                "first_offset": m.start(),
                "confidence": 0.7,
                "source": "regex",
            })
            if len(found) >= 10:
                break
    return found


def _geocode(addr: dict, timeout: float = 8.0) -> dict:
    """Best-effort Nominatim geocode. Fills lat/lon + country_code.
    Silently returns the address unchanged on failure."""
    if addr.get("latitude") is not None:
        return addr
    line = addr.get("line1") or ""
    city = addr.get("city") or ""
    region = addr.get("admin_region") or ""
    country = addr.get("country") or ""
    q = ", ".join(x for x in (line, city, region, country) if x)
    if not q:
        return addr
    try:
        import httpx
        r = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1,
                    "addressdetails": 1},
            headers={"User-Agent": "Enki/1.0 (extraction)"},
            timeout=timeout,
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                hit = data[0]
                addr["latitude"] = float(hit.get("lat") or 0) or None
                addr["longitude"] = float(hit.get("lon") or 0) or None
                a = hit.get("address") or {}
                addr["country"] = addr.get("country") or a.get("country")
                addr["country_code"] = (a.get("country_code") or "").upper()[:2] or None
                addr["geocoder"] = "nominatim"
                addr["geocoded_at"] = True
    except Exception as e:
        log.debug("geocode for %r failed: %s", q[:60], e)
    return addr


def persist_addresses(
    addresses: list[dict],
    envelope: dict,
    conn,
    *,
    tenant_id: str = "default",
    signal_rid: Optional[int] = None,
    geocode: bool = True,
) -> int:
    """Insert address rows. Ties each address to the first resolved
    organization/facility entity within 200 chars of its offset, if any."""
    if not addresses or conn is None:
        return 0

    entities = [
        e for e in (envelope.get("entities") or [])
        if isinstance(e, dict)
        and isinstance(e.get("node_id"), str)
        and e["node_id"].startswith("entity:")
    ]

    def _owner_entity_for(offset: Optional[int]) -> Optional[int]:
        if offset is None:
            return None
        best = None
        best_dist = 200
        for e in entities:
            eo = e.get("first_offset")
            if eo is None:
                continue
            dist = abs(eo - offset)
            if dist < best_dist and e.get("type") in (
                "organization", "facility", "person", None,
            ):
                best = e
                best_dist = dist
        if best:
            try:
                return int(best["node_id"].split(":", 1)[1])
            except (ValueError, IndexError):
                return None
        return None

    lin = lineage_columns(ldu_t_id=signal_rid)
    inserted = 0

    for addr in addresses:
        if geocode:
            _geocode(addr)
        entity_id = _owner_entity_for(addr.get("first_offset"))
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO addresses (
                        tenant_id, entity_id, line1, city, admin_region,
                        postal_code, country, country_code,
                        latitude, longitude, geocoded_at, geocoder,
                        source_signal_rids, confidence,
                        admitted_under_active_set, gdu_canonical, ldu_t_id
                    ) VALUES (
                        %(tenant)s, %(entity)s, %(line1)s, %(city)s, %(region)s,
                        %(postal)s, %(country)s, %(cc)s,
                        %(lat)s, %(lon)s, %(geocoded)s, %(geocoder)s,
                        %(signals)s::bigint[], %(conf)s,
                        %(active_set)s, %(canonical)s, %(ldu_t)s
                    )
                    """,
                    {
                        "tenant": tenant_id,
                        "entity": entity_id,
                        "line1": (addr.get("line1") or "")[:500],
                        "city": addr.get("city"),
                        "region": addr.get("admin_region"),
                        "postal": addr.get("postal_code"),
                        "country": addr.get("country"),
                        "cc": addr.get("country_code"),
                        "lat": addr.get("latitude"),
                        "lon": addr.get("longitude"),
                        "geocoded": "NOW()" if addr.get("geocoded_at") else None,
                        "geocoder": addr.get("geocoder"),
                        "signals": [signal_rid] if signal_rid else [],
                        "conf": addr.get("confidence", 0.7),
                        **lin,
                    },
                )
            inserted += 1
        except Exception as e:
            log.debug("address insert failed %r: %s", addr.get("line1", "")[:40], e)
            try: conn.rollback()
            except Exception: pass

    try:
        conn.commit()
    except Exception:
        pass
    return inserted
