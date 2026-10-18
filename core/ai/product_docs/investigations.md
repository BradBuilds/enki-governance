# Running an investigation

An **investigation** takes a person or organization name and automatically
queries 30+ public-records sources to build a dossier. You can start one by
asking this assistant, e.g. "investigate Dana Vale" or "run an investigation
on JPMorgan Chase". The assistant queues it and the findings stream into the
entity's dossier as each source returns.

# What sources it pulls

- **US public records:** SEC EDGAR (corporate filings, officers), FEC (campaign
  finance), USAspending (contracts/grants), federal courts (CourtListener),
  IRS 990 / ProPublica nonprofits, FDIC, FDA, EPA, Congress.gov.
- **Sanctions / global:** OFAC, UN sanctions, OpenSanctions.
- **Corporate registries:** OpenCorporates, SAM.gov, UK Companies House, and
  US state registries (FL, NY, CA).
- **Reference / OSINT:** Wikipedia, Wikidata (always run first, to detect the
  entity's country), Google Books, Library of Congress, Wayback, GitHub, Reddit.

It is **jurisdiction-aware**: it detects the entity's nationality first and then
skips sources that don't apply (it won't report a US "Dana Vale" filing when
investigating a British person of the same name).

# Checking status of a run

Ask the assistant "is that investigation done?" or "status of the Vale run".
Each investigation reports which sources have completed, how many findings were
produced, and how many new related entities were discovered. Findings and
discovered entities (officers, donors, linked orgs) appear on the entity's
dossier page.

# What it produces

- **Findings** — one per source hit (a filing, donation, court case, officer
  record), each with a confidence score.
- **Discovered entities** — new people/orgs found (e.g. "director_of",
  "donor_to") that you can investigate next.
- Everything is filed to the entity's **dossier**.

# API keys that help

Most sources need no key. A few free keys raise rate limits: **DATA_GOV_API_KEY**
(FEC, Congress, FDA), **SAM_GOV_API_KEY** (SAM.gov), and an optional
**OPENCORPORATES_API_TOKEN**. Ask "how do I get a data.gov key" for steps.
