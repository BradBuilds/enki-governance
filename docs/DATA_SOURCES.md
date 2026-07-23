<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Data sources & API keys — bring your own

Beyond the documents you feed it and the [live connectors](CONNECTORS.md), Enki can enrich
entities against **public-record and open-data sources**. Most work with **no key at all**;
the rest are **bring-your-own-key** — you paste your own credential and it works. Nothing is
sold or bundled; you use your own free/paid accounts.

## Where keys go

Two equivalent ways — pick either:

1. **Settings → Data sources** in the UI — paste a key, it's stored per-tenant in the `secrets`
   table and picked up within seconds (hot-reloadable).
2. **`.env`** — set an environment variable of the **same name** (e.g. `DATA_GOV_API_KEY=…`).

Resolution order is *secrets table → environment → none*. A missing key never breaks a run — the
source simply falls back to its free tier or is skipped.

## Works with no key

These need nothing — they're queried at polite/free rates out of the box:

- **SEC EDGAR** — filings, DEF 14A proxy statements, officers.
- **CourtListener** — US court records / dockets.
- **OFAC + UN consolidated** — sanctions designations.
- **Wikidata / Wikipedia** — entity identity, types, descriptions.
- **OpenCorporates** — company search (free ~500/day; a token unlocks more, below).

## Bring-your-own-key sources

Each key unlocks deeper access. All are your own accounts (free signups except where a provider
charges). Set the variable name shown, or paste it in Settings → Data sources.

| Key (env / secret name) | Source | Free without a key | What your key unlocks | Sign up |
|---|---|---|---|---|
| `DATA_GOV_API_KEY` | **data.gov** (FEC, Congress.gov, FDA) | FEC search at DEMO_KEY rate; Congress returns nothing; FDA throttled | FEC donor money-trail (top contributions, committee→donor cascade), Congress member/committee lookup, OpenFDA at 240/min | https://api.data.gov/signup/ |
| `SAM_GOV_API_KEY` | **SAM.gov** | No contractor data | Federal contractor records (UEI, CAGE, registration), capability statements, USAspending cross-ref | https://sam.gov/profile/details |
| `GEONAMES_USERNAME` | **GeoNames** | Skipped (places rely on Wikipedia + Nominatim) | Alternate place names, population, admin hierarchy; resolves case-variant place collisions | https://www.geonames.org/login |
| `OPENCORPORATES_API_TOKEN` | **OpenCorporates** | Bulk search only, 500/day | Per-company officers (full board), corporate-empire mapping per person, 100/page, subsidiary chains | https://opencorporates.com/api_accounts |
| `OPENSANCTIONS_API_KEY` | **OpenSanctions** | OFAC + UN only | Real-time per-name lookup, PEP + family-of-PEP detection, corruption/oligarch/military topics beyond OFAC/UN | https://www.opensanctions.org/api/ |
| `COMPANIES_HOUSE_API_KEY` | **UK Companies House** | No UK-specific data | UK directors/officers, company filings + status + addresses, appointment history, DOB/address disambiguation | https://developer-specs.company-information.service.gov.uk/ |
| `PROPUBLICA_NONPROFITS_API_KEY` | **ProPublica Nonprofits** | Fine at normal rates | Bulk 990 pulls, higher-throughput board-of-directors cascades | https://projects.propublica.org/nonprofits/api |
| `FAA_REGISTRY_BULK_ENABLED` | **FAA aircraft registry** | Direct N-number lookup only | Reverse lookup (every aircraft for a person/org), type breakdown, registration-history timeline | https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download |

The same catalog (with live free/keyed status) drives the **Settings → Data sources** wizard —
it's defined in `libraries/researchpack/definitions/researchpack.credentials.json`.

## Satellite imagery (Copernicus / Sentinel-2)

The map can overlay ESA **Copernicus Sentinel-2** imagery and capture overhead of an entity's
coordinates as evidence. It's credential-driven too, but via its own panel:

- Create a free **[Copernicus / Sentinel Hub](https://dataspace.copernicus.eu/)** WMS instance.
- Paste the **WMS instance ID** in **Settings → Satellite** (OAuth client id/secret optional,
  reserved for monitoring). Then the map's satellite layer and per-entity capture work.

## The AI model

The extraction model is **local by default** (Ollama, no key). If you'd rather point extraction
at a hosted model, set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` — the governed collapse layer is
identical either way (see the README's LLM section). Local stays the default; cloud is opt-in.

## How enrichment uses these

When an entity is investigated, Enki queries the applicable sources, and every fact it admits is
**collapsed and receipted** like any other — you can trace each claim back to the source record.
Free tiers give you a working system; your keys deepen coverage and lift rate limits. Deep,
orchestrated multi-source research campaigns are part of the private platform, but the per-source
lookups above ship here and work with your own keys.
