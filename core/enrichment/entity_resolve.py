"""
Entity Resolution Engine.

Given a seed (address, company name, person name), discover connected
entities by querying public data sources, then build a knowledge graph
of canonical entities and their relationships.

Each discovered entity becomes a knowledge node. Each connection becomes
an edge. Observables are auto-added to the case watchlist.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import httpx

from storage.postgres.db import get_connection

logger = logging.getLogger("enki.entity")
TIMEOUT = 12


# ─── SEC EDGAR ───────────────────────────────────────────────────────────────

async def search_sec_companies(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Search SEC EDGAR for companies by name — uses ticker-to-CIK mapping."""
    try:
        # First try the authoritative ticker/CIK mapping
        resp = await client.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "EnkiCommandCenter admin@example.com"},
        )
        if resp.status_code == 200:
            data = resp.json()
            query_upper = query.upper()
            companies = []
            for _, v in data.items():
                title = v.get("title", "")
                ticker = v.get("ticker", "")
                if query_upper in title.upper() or query_upper == ticker.upper():
                    companies.append({
                        "name": title,
                        "cik": str(v.get("cik_str", "")),
                        "ticker": ticker,
                    })
                    if len(companies) >= 5:
                        break
            if companies:
                return companies
    except Exception as e:
        logger.warning(f"SEC ticker search failed: {e}")

    # Fallback: full-text search
    try:
        resp = await client.get(
            "https://efts.sec.gov/LATEST/search-index",
            params={"q": query, "dateRange": "custom", "startdt": "2020-01-01"},
            headers={"User-Agent": "EnkiCommandCenter admin@example.com"},
        )
        if resp.status_code == 200:
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            companies = []
            seen = set()
            for hit in hits[:10]:
                src = hit.get("_source", {})
                entity = src.get("entity_name", "")
                if not entity or entity in seen:
                    continue
                seen.add(entity)
                companies.append({
                    "name": entity,
                    "cik": src.get("file_num", "").split("-")[0] if src.get("file_num") else "",
                })
            return companies
    except Exception:
        pass
    return []


async def get_sec_company_facts(client: httpx.AsyncClient, cik: str) -> dict:
    """Get XBRL financial facts for a company by CIK."""
    try:
        padded = cik.zfill(10)
        resp = await client.get(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{padded}.json",
            headers={"User-Agent": "EnkiCommandCenter admin@example.com"},
        )
        if resp.status_code == 200:
            data = resp.json()
            facts = data.get("facts", {}).get("us-gaap", {})
            result = {"entity_name": data.get("entityName", ""), "cik": cik}

            for key, label in [
                ("Revenues", "revenue"),
                ("Assets", "total_assets"),
                ("Liabilities", "total_liabilities"),
                ("NetIncomeLoss", "net_income"),
                ("StockholdersEquity", "equity"),
            ]:
                if key in facts:
                    units = facts[key].get("units", {})
                    usd = units.get("USD", [])
                    if usd:
                        latest = sorted(usd, key=lambda x: x.get("end", ""), reverse=True)
                        if latest:
                            result[label] = latest[0].get("val")
                            result[f"{label}_period"] = latest[0].get("end", "")
            return result
    except Exception:
        pass
    return {}


async def get_sec_submissions(client: httpx.AsyncClient, cik: str) -> dict:
    """Get company info and recent filings by CIK."""
    try:
        padded = cik.zfill(10)
        resp = await client.get(
            f"https://data.sec.gov/submissions/CIK{padded}.json",
            headers={"User-Agent": "EnkiCommandCenter admin@example.com"},
        )
        if resp.status_code == 200:
            data = resp.json()
            recent = data.get("filings", {}).get("recent", {})
            filing_count = len(recent.get("accessionNumber", []))
            return {
                "name": data.get("name", ""),
                "cik": cik,
                "sic": data.get("sic", ""),
                "sic_description": data.get("sicDescription", ""),
                "state": data.get("stateOfIncorporation", ""),
                "fiscal_year_end": data.get("fiscalYearEnd", ""),
                "filing_count": filing_count,
                "tickers": data.get("tickers", []),
                "exchanges": data.get("exchanges", []),
                "addresses": data.get("addresses", {}),
            }
    except Exception:
        pass
    return {}


# ─── USAspending ─────────────────────────────────────────────────────────────

async def search_usaspending(client: httpx.AsyncClient, company_name: str) -> dict:
    """Search USAspending for federal contracts/grants by recipient name."""
    try:
        resp = await client.post(
            "https://api.usaspending.gov/api/v2/search/spending_by_award/",
            json={
                "filters": {
                    "recipient_search_text": [company_name],
                    "time_period": [{"start_date": "2023-01-01", "end_date": "2026-12-31"}],
                },
                "fields": ["Award ID", "Recipient Name", "Award Amount",
                           "Awarding Agency", "Award Type", "Start Date"],
                "limit": 10,
                "page": 1,
                "sort": "Award Amount",
                "order": "desc",
            },
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            total = sum(float(r.get("Award Amount", 0) or 0) for r in results)
            return {
                "awards": [{
                    "id": r.get("Award ID", ""),
                    "recipient": r.get("Recipient Name", ""),
                    "amount": r.get("Award Amount"),
                    "agency": r.get("Awarding Agency", ""),
                    "type": r.get("Award Type", ""),
                    "start_date": r.get("Start Date", ""),
                } for r in results[:5]],
                "total_amount": total,
                "award_count": len(results),
            }
    except Exception:
        pass
    return {}


# ─── GLEIF (Legal Entity Identifier) ─────────────────────────────────────────

async def search_gleif(client: httpx.AsyncClient, company_name: str) -> dict:
    """Search GLEIF for Legal Entity Identifier by company name."""
    try:
        resp = await client.get(
            "https://api.gleif.org/api/v1/lei-records",
            params={"filter[entity.legalName]": company_name, "page[size]": 3},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            records = data.get("data", [])
            if records:
                r = records[0]
                entity = r.get("attributes", {}).get("entity", {})
                legal_addr = entity.get("legalAddress", {})
                return {
                    "lei": r.get("attributes", {}).get("lei", ""),
                    "legal_name": entity.get("legalName", {}).get("name", ""),
                    "jurisdiction": entity.get("jurisdiction", ""),
                    "status": entity.get("status", ""),
                    "address": f"{legal_addr.get('addressLines', [''])[0]}, {legal_addr.get('city', '')}, {legal_addr.get('region', '')} {legal_addr.get('postalCode', '')}".strip(", "),
                    "country": legal_addr.get("country", ""),
                    "category": entity.get("category", ""),
                }
    except Exception:
        pass
    return {}


# ─── FINRA BrokerCheck ───────────────────────────────────────────────────────

async def search_finra(client: httpx.AsyncClient, person_name: str) -> list[dict]:
    """Search FINRA BrokerCheck for a person."""
    try:
        resp = await client.get(
            "https://api.brokercheck.finra.org/search/individual",
            params={"query": person_name, "hl": "true", "nrows": 5, "start": 0, "r": 25},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            results = []
            for hit in hits[:3]:
                src = hit.get("_source", {})
                results.append({
                    "name": f"{src.get('ind_firstname', '')} {src.get('ind_lastname', '')}".strip(),
                    "crd": src.get("ind_source_id", ""),
                    "firm": src.get("ind_current_employer", [{}])[0].get("org_name", "") if src.get("ind_current_employer") else "",
                    "disclosures": src.get("ind_num_of_disclosures", 0),
                    "licenses": src.get("ind_num_of_exams_passed", 0),
                })
            return results
    except Exception:
        pass
    return []


# ─── CourtListener ───────────────────────────────────────────────────────────

async def search_courtlistener(client: httpx.AsyncClient, name: str) -> list[dict]:
    """Search CourtListener for federal court cases involving a name."""
    try:
        resp = await client.get(
            "https://www.courtlistener.com/api/rest/v4/search/",
            params={"q": f'"{name}"', "type": "r", "order_by": "score desc"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            return [{
                "case_name": r.get("caseName", ""),
                "court": r.get("court", ""),
                "date_filed": r.get("dateFiled", ""),
                "docket_number": r.get("docketNumber", ""),
                "snippet": r.get("snippet", "")[:200],
            } for r in results[:5]]
    except Exception:
        pass
    return []


# ─── ProPublica Nonprofits ───────────────────────────────────────────────────

async def search_nonprofits(client: httpx.AsyncClient, name: str) -> dict:
    """Search ProPublica Nonprofit Explorer."""
    try:
        resp = await client.get(
            f"https://projects.propublica.org/nonprofits/api/v2/search.json",
            params={"q": name},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            orgs = data.get("organizations", [])
            if orgs:
                o = orgs[0]
                return {
                    "name": o.get("name", ""),
                    "ein": o.get("ein", ""),
                    "city": o.get("city", ""),
                    "state": o.get("state", ""),
                    "income_amount": o.get("income_amount"),
                    "asset_amount": o.get("asset_amount"),
                    "ntee_code": o.get("ntee_code", ""),
                }
    except Exception:
        pass
    return {}


# ─── Aircraft Registry (local DB) ────────────────────────────────────────────

def lookup_aircraft_for_company(company_name: str, dsn=None) -> list[dict]:
    """Find aircraft registered to a company (from local registry + ADS-B observations)."""
    # Search local aircraft registry
    results = []
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Search by registrant name (if FAA data was imported)
            cur.execute("""
                SELECT n_number, model, registrant_name, registrant_city, registrant_state
                FROM aircraft_registry
                WHERE upper(registrant_name) LIKE upper(%s)
                LIMIT 10
            """, (f"%{company_name}%",))
            for r in cur.fetchall():
                results.append({
                    "n_number": r[0], "model": r[1],
                    "registrant": r[2], "city": r[3], "state": r[4],
                })
    return results


# ─── Master Entity Resolution ────────────────────────────────────────────────

async def resolve_address(
    address: str,
    lat: float,
    lon: float,
    tenant_id: str = "default",
    case_id: Optional[str] = None,
    project_id: Optional[str] = None,
    dsn: Optional[str] = None,
) -> dict:
    """Given an address, discover all connected entities."""
    results = {
        "address": address,
        "latitude": lat,
        "longitude": lon,
        "companies": [],
        "people": [],
        "assets": [],
        "contracts": [],
        "legal": [],
        "nonprofits": [],
    }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # Search SEC for companies at/near this address
        sec_hits = await search_sec_companies(client, address)
        for hit in sec_hits[:3]:
            company = {"name": hit["name"], "source": "sec_edgar", "cik": hit.get("cik")}

            # Enrich each company found
            if hit.get("cik"):
                submissions = await get_sec_submissions(client, hit["cik"])
                if submissions:
                    company.update(submissions)
                facts = await get_sec_company_facts(client, hit["cik"])
                if facts:
                    company["financials"] = facts

            # USAspending
            spending = await search_usaspending(client, hit["name"])
            if spending and spending.get("award_count", 0) > 0:
                company["government_contracts"] = spending
                results["contracts"].extend(spending.get("awards", []))

            # GLEIF
            gleif = await search_gleif(client, hit["name"])
            if gleif:
                company["gleif"] = gleif

            # Nonprofit check
            np_data = await search_nonprofits(client, hit["name"])
            if np_data and np_data.get("ein"):
                company["nonprofit"] = np_data
                results["nonprofits"].append(np_data)

            # Aircraft registered to this company
            aircraft = lookup_aircraft_for_company(hit["name"], dsn)
            if aircraft:
                company["aircraft"] = aircraft
                results["assets"].extend([{**a, "type": "aircraft"} for a in aircraft])

            # Court cases
            legal = await search_courtlistener(client, hit["name"])
            if legal:
                company["legal_cases"] = legal
                results["legal"].extend(legal)

            results["companies"].append(company)

    return results


async def resolve_company(
    company_name: str,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> dict:
    """Given a company name, discover all connected entities."""
    results = {
        "company_name": company_name,
        "sec": {},
        "financials": {},
        "contracts": {},
        "gleif": {},
        "nonprofit": {},
        "aircraft": [],
        "legal": [],
        "finra": [],
    }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # SEC EDGAR
        sec_hits = await search_sec_companies(client, company_name)
        if sec_hits:
            cik = sec_hits[0].get("cik", "")
            results["sec"] = await get_sec_submissions(client, cik) if cik else {}
            results["financials"] = await get_sec_company_facts(client, cik) if cik else {}

        # USAspending
        results["contracts"] = await search_usaspending(client, company_name)

        # GLEIF
        results["gleif"] = await search_gleif(client, company_name)

        # Nonprofit
        results["nonprofit"] = await search_nonprofits(client, company_name)

        # Court cases
        results["legal"] = await search_courtlistener(client, company_name)

        # Aircraft
        results["aircraft"] = lookup_aircraft_for_company(company_name, dsn)

    return results


async def resolve_person(
    person_name: str,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> dict:
    """Given a person name, discover connections."""
    results = {
        "person_name": person_name,
        "finra": [],
        "legal": [],
        "fec": {},
    }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # FINRA BrokerCheck
        results["finra"] = await search_finra(client, person_name)

        # Court cases
        results["legal"] = await search_courtlistener(client, person_name)

        # FEC Campaign Finance
        try:
            resp = await client.get(
                "https://api.open.fec.gov/v1/names/candidates/",
                params={"q": person_name, "api_key": "DEMO_KEY"},
                timeout=TIMEOUT,
            )
            if resp.status_code == 200:
                results["fec"] = resp.json()
        except Exception:
            pass

    return results
