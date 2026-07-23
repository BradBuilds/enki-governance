-- 029: Entity Investigation System
-- Stores structured results from public data source investigations
-- (SEC EDGAR, FEC, USAspending, ProPublica, CourtListener, etc.)

-- Investigation requests — one per entity being investigated
CREATE TABLE IF NOT EXISTS entity_investigations (
    investigation_id  BIGSERIAL PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL DEFAULT 'default',
    entity_id         BIGINT REFERENCES entities(entity_id),
    seed_name         TEXT NOT NULL,          -- original search term
    seed_type         VARCHAR(64),            -- person, organization, address
    status            VARCHAR(32) NOT NULL DEFAULT 'pending',  -- pending, running, complete, failed
    sources_requested TEXT[] DEFAULT '{}',    -- which sources to query
    sources_completed TEXT[] DEFAULT '{}',    -- which sources finished
    findings_count    INT DEFAULT 0,
    entities_discovered INT DEFAULT 0,
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error             TEXT
);

CREATE INDEX IF NOT EXISTS idx_investigations_tenant ON entity_investigations(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_investigations_entity ON entity_investigations(entity_id);

-- Investigation findings — raw results from each source
CREATE TABLE IF NOT EXISTS investigation_findings (
    finding_id        BIGSERIAL PRIMARY KEY,
    investigation_id  BIGINT NOT NULL REFERENCES entity_investigations(investigation_id),
    tenant_id         VARCHAR(64) NOT NULL DEFAULT 'default',
    source            VARCHAR(64) NOT NULL,   -- sec_edgar, fec, usaspending, propublica, courtlistener
    finding_type      VARCHAR(64) NOT NULL,   -- company, filing, donation, contract, nonprofit, court_case, officer, financial
    title             TEXT NOT NULL,
    data              JSONB NOT NULL DEFAULT '{}',  -- full structured result
    confidence        REAL NOT NULL DEFAULT 0.7,
    entity_id         BIGINT REFERENCES entities(entity_id),  -- linked entity if resolved
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_findings_investigation ON investigation_findings(investigation_id);
CREATE INDEX IF NOT EXISTS idx_findings_source ON investigation_findings(source);
CREATE INDEX IF NOT EXISTS idx_findings_entity ON investigation_findings(entity_id);
CREATE INDEX IF NOT EXISTS idx_findings_type ON investigation_findings(finding_type);

-- Investigation discovered entities — new entities found during investigation
CREATE TABLE IF NOT EXISTS investigation_discovered (
    discovered_id     BIGSERIAL PRIMARY KEY,
    investigation_id  BIGINT NOT NULL REFERENCES entity_investigations(investigation_id),
    tenant_id         VARCHAR(64) NOT NULL DEFAULT 'default',
    name              TEXT NOT NULL,
    entity_type       VARCHAR(64),           -- person, organization, pac, nonprofit
    source            VARCHAR(64),           -- which source found it
    relationship      VARCHAR(128),          -- officer_of, director_of, donor_to, etc.
    relationship_detail JSONB DEFAULT '{}',  -- amount, date, title, etc.
    entity_id         BIGINT REFERENCES entities(entity_id),  -- if resolved to existing
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discovered_investigation ON investigation_discovered(investigation_id);
CREATE INDEX IF NOT EXISTS idx_discovered_entity ON investigation_discovered(entity_id);
